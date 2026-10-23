"""Collect issues, pull requests and comments from Qiskit and Cirq.

Usage (from the project root):
    set GITHUB_TOKEN=<token>        (Windows cmd)   or   gh auth login
    python scripts/collect.py              # fetch everything, then build CSVs
    python scripts/collect.py --build-only # rebuild CSVs from the cached raw JSON

Standard library only. Raw API responses are cached under data/raw/ so the run
can be resumed after an interruption and the tables rebuilt without refetching.
"""

import argparse
import csv
import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "processed"

# Snapshot cutoff: items and comments created after this are dropped.
CUTOFF = "2026-10-23T23:59:59Z"

REPOS = {
    "Qiskit/qiskit": {
        "key": "qiskit",
        # First Rust code (Cargo.toml, setuptools-rust, src/*.rs) merged in
        # PR #7658, commit ccc371f8f, first released in 0.20.0 (2022-03-31).
        "rust_start": "2022-02-28T21:49:54Z",
    },
    "quantumlib/Cirq": {
        "key": "cirq",
        # No Rust code in the repository history: control group.
        "rust_start": None,
    },
}

# Labels that mark an item as a bug report. Picked from labels.csv: a plain
# "contains bug" match also caught Cirq's area/debugging, which is not a bug label.
BUG_LABELS = {
    "Qiskit/qiskit": {"bug"},
    "quantumlib/Cirq": {"kind/bug-report"},
}
KNOWN_BOTS = {
    "qiskit-bot", "mergify", "dependabot", "github-actions", "codecov",
    "coveralls", "claassistant", "pre-commit-ci", "renovate",
}

GRAPHQL_URL = "https://api.github.com/graphql"
REST_URL = "https://api.github.com"


# --------------------------------------------------------------------------- API


def get_token():
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token.strip()
    try:
        return subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        sys.exit("No GitHub token. Set GITHUB_TOKEN or run `gh auth login` first.")


class GitHub:
    def __init__(self, token):
        self.headers = {
            "Authorization": f"bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "se-rust-migration-analysis",
        }

    def _request(self, url, data=None, tries=8):
        for attempt in range(tries):
            req = urllib.request.Request(url, data=data, headers=self.headers)
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    self._respect_limit(resp.headers)
                    return json.loads(resp.read().decode("utf-8")), resp.headers
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                if e.code in (403, 429) and ("rate limit" in body.lower() or e.headers.get("Retry-After")):
                    self._sleep_until_reset(e.headers)
                    continue
                if e.code in (500, 502, 503, 504):
                    wait = 2 ** attempt
                    print(f"  HTTP {e.code}, retrying in {wait}s", flush=True)
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"HTTP {e.code} for {url}: {body[:500]}")
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError,
                    ConnectionError, json.JSONDecodeError) as e:
                wait = 2 ** attempt
                print(f"  network error ({e}), retrying in {wait}s", flush=True)
                time.sleep(wait)
        raise RuntimeError(f"Giving up on {url}")

    def _respect_limit(self, headers):
        remaining = headers.get("X-RateLimit-Remaining")
        if remaining is not None and int(remaining) < 50:
            self._sleep_until_reset(headers)

    @staticmethod
    def _sleep_until_reset(headers):
        retry_after = headers.get("Retry-After")
        if retry_after:
            wait = int(retry_after) + 1
        else:
            reset = int(headers.get("X-RateLimit-Reset", time.time() + 60))
            wait = max(reset - int(time.time()), 0) + 5
        print(f"  rate limit reached, sleeping {wait}s", flush=True)
        time.sleep(wait)

    def graphql(self, query, variables):
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        # Few retries: a repeated 502 usually means the page is too heavy, and the
        # caller reacts by asking for fewer items per page.
        data, _ = self._request(GRAPHQL_URL, payload, tries=3)
        if data.get("errors"):
            raise RuntimeError(json.dumps(data["errors"])[:1000])
        return data["data"]

    def rest_pages(self, url):
        """Yield (page data, URL of the next page or None), following Link headers."""
        while url:
            data, headers = self._request(url)
            url = None
            for part in (headers.get("Link") or "").split(","):
                if 'rel="next"' in part:
                    url = part[part.index("<") + 1 : part.index(">")]
            yield data, url


# ----------------------------------------------------------------------- queries

COMMON_FIELDS = """
  number title body url state createdAt closedAt updatedAt
  author { __typename login }
  authorAssociation
  labels(first: 50) { nodes { name } }
  milestone { title }
  comments { totalCount }
  reactions { totalCount }
"""

ISSUES_QUERY = """
query($owner: String!, $name: String!, $after: String, $n: Int!) {
  repository(owner: $owner, name: $name) {
    issues(first: $n, after: $after, orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        %s
        stateReason
        closedByPullRequestsReferences(first: 20, includeClosedPrs: true) { nodes { number } }
      }
    }
  }
  rateLimit { remaining resetAt }
}
""" % COMMON_FIELDS

PRS_QUERY = """
query($owner: String!, $name: String!, $after: String, $n: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: $n, after: $after, orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        %s
        merged mergedAt additions deletions changedFiles
        closingIssuesReferences(first: 20) { nodes { number } }
        files(first: 100) { pageInfo { hasNextPage endCursor } nodes { path } }
        reviews(first: 50) {
          nodes { databaseId state body createdAt authorAssociation author { __typename login } }
        }
      }
    }
  }
  rateLimit { remaining resetAt }
}
""" % COMMON_FIELDS

PR_FILES_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      files(first: 100, after: $after) { pageInfo { hasNextPage endCursor } nodes { path } }
    }
  }
}
"""

RELEASES_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    releases(first: 100, after: $after, orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes { tagName name publishedAt createdAt isPrerelease }
    }
  }
}
"""

LABELS_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    labels(first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { name description }
    }
  }
}
"""


# ---------------------------------------------------------------------- fetching


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def fetch_connection(gh, repo, kind, query, conn, page_size):
    """Page through a repository connection, one cached file per page."""
    owner, name = repo.split("/")
    folder = RAW / REPOS[repo]["key"] / kind
    state_file = folder / "_state.json"
    state = load_json(state_file, {"page": 0, "after": None, "done": False})
    if state["done"]:
        print(f"[{repo}] {kind}: already complete ({state['page']} pages)")
        return
    n = page_size
    while True:
        try:
            data = gh.graphql(query, {"owner": owner, "name": name, "after": state["after"], "n": n})
        except RuntimeError as e:
            # Large bodies can make GitHub time out; shrink the page and retry.
            msg = str(e).lower()
            if n > 1 and ("timeout" in msg or "502" in msg or "504" in msg or "giving up" in msg
                          or "couldn't respond" in msg):
                n = max(1, n // 2)
                print(f"  shrinking page size to {n}", flush=True)
                continue
            raise
        page = data["repository"][conn]
        state["page"] += 1
        save_json(folder / f"page_{state['page']:05d}.json", page["nodes"])
        state["after"] = page["pageInfo"]["endCursor"]
        state["done"] = not page["pageInfo"]["hasNextPage"]
        save_json(state_file, state)
        last = page["nodes"][-1]["createdAt"] if page["nodes"] else "-"
        print(f"[{repo}] {kind}: page {state['page']} ({len(page['nodes'])} items, up to {last}), "
              f"{data['rateLimit']['remaining']} points left", flush=True)
        if state["done"]:
            return
        n = page_size


def fetch_extra_pr_files(gh, repo):
    """Complete the file list for PRs that changed more than 100 files."""
    owner, name = repo.split("/")
    out_file = RAW / REPOS[repo]["key"] / "pr_files_extra.json"
    extra = load_json(out_file, {})
    for pr in iter_cached(repo, "pull_requests"):
        info = pr["files"]
        if not info or not info["pageInfo"]["hasNextPage"] or str(pr["number"]) in extra:
            continue
        paths, after = [], info["pageInfo"]["endCursor"]
        while after:
            data = gh.graphql(PR_FILES_QUERY, {"owner": owner, "name": name, "number": pr["number"], "after": after})
            files = data["repository"]["pullRequest"]["files"]
            paths += [f["path"] for f in files["nodes"]]
            after = files["pageInfo"]["endCursor"] if files["pageInfo"]["hasNextPage"] else None
        extra[str(pr["number"])] = paths
        save_json(out_file, extra)
        print(f"[{repo}] PR #{pr['number']}: +{len(paths)} extra files", flush=True)


def fetch_simple(gh, repo, kind, query, conn):
    owner, name = repo.split("/")
    out_file = RAW / REPOS[repo]["key"] / f"{kind}.json"
    if out_file.exists():
        return
    print(f"[{repo}] fetching {kind}...", flush=True)
    nodes, after = [], None
    while True:
        page = gh.graphql(query, {"owner": owner, "name": name, "after": after})["repository"][conn]
        nodes += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    save_json(out_file, nodes)
    print(f"[{repo}] {kind}: {len(nodes)}", flush=True)


def fetch_rest_comments(gh, repo, kind, path):
    """Repo-wide comment listings (issue + PR conversation, or PR review comments)."""
    folder = RAW / REPOS[repo]["key"] / kind
    state_file = folder / "_state.json"
    state = load_json(state_file, {"page": 0, "next_url": None, "done": False})
    if state["done"] or (state["page"] and not state["next_url"]):
        print(f"[{repo}] {kind}: already complete ({state['page']} pages)")
        return
    start = state["next_url"] or (
        f"{REST_URL}{path}?" + urllib.parse.urlencode({"per_page": 100, "sort": "created", "direction": "asc"}))
    for data, next_url in gh.rest_pages(start):
        state["page"] += 1
        save_json(folder / f"page_{state['page']:05d}.json", data)
        state["next_url"] = next_url
        save_json(state_file, state)
        last = data[-1]["created_at"] if data else "-"
        print(f"[{repo}] {kind}: page {state['page']} (up to {last})", flush=True)
    state["done"] = True
    state["next_url"] = None
    save_json(state_file, state)


def iter_cached(repo, kind):
    folder = RAW / REPOS[repo]["key"] / kind
    for f in sorted(folder.glob("page_*.json")):
        yield from load_json(f)


def fetch_all(gh):
    for repo in REPOS:
        owner, name = repo.split("/")
        fetch_simple(gh, repo, "labels", LABELS_QUERY, "labels")
        fetch_simple(gh, repo, "releases", RELEASES_QUERY, "releases")
        fetch_connection(gh, repo, "issues", ISSUES_QUERY, "issues", 25)
        fetch_connection(gh, repo, "pull_requests", PRS_QUERY, "pullRequests", 10)
        fetch_extra_pr_files(gh, repo)
        fetch_rest_comments(gh, repo, "issue_comments", f"/repos/{owner}/{name}/issues/comments")
        fetch_rest_comments(gh, repo, "review_comments", f"/repos/{owner}/{name}/pulls/comments")


# ---------------------------------------------------------------------- building


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def is_bot(author_type, login):
    if not login:
        return False
    login_l = login.lower()
    return author_type == "Bot" or login_l.endswith("[bot]") or login_l.replace("[bot]", "") in KNOWN_BOTS


def release_lookup(repo):
    releases = load_json(RAW / REPOS[repo]["key"] / "releases.json", [])
    rel = sorted(
        (parse_time(r["publishedAt"] or r["createdAt"]), r["tagName"])
        for r in releases if not r["isPrerelease"] and (r["publishedAt"] or r["createdAt"])
    )

    def lookup(ts):
        latest = ""
        for when, tag in rel:
            if when <= ts:
                latest = tag
            else:
                break
        return latest
    return lookup


def classify_files(paths):
    rs = sum(1 for p in paths if p.endswith(".rs"))
    py = sum(1 for p in paths if p.endswith((".py", ".pyi", ".pyx")))
    touches = rs > 0 or any(p.endswith(("Cargo.toml", "Cargo.lock")) for p in paths)
    return rs, py, len(paths) - rs - py, touches


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so Excel opens the files with the right encoding.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_NONNUMERIC)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {path.relative_to(ROOT)} ({len(rows)} rows)")


ITEM_FIELDS = [
    "item_id", "repo", "number", "type", "title", "body", "url", "state", "state_reason",
    "merged", "created_at", "closed_at", "merged_at", "days_to_close", "author_login",
    "author_association", "author_is_bot", "labels", "is_bug", "bug_labels", "milestone",
    "comments_count", "reactions_count", "linked_items", "pr_additions", "pr_deletions",
    "pr_changed_files", "pr_files_rs", "pr_files_py", "pr_files_other", "pr_touches_rust",
    "release_at_creation", "rust_period",
]

COMMENT_FIELDS = [
    "item_id", "repo", "comment_id", "kind", "author_login", "author_association",
    "author_is_bot", "created_at", "review_state", "path", "body",
]


def build():
    cutoff = parse_time(CUTOFF)
    items, comments, labels_rows, release_rows = [], [], [], []

    for repo, cfg in REPOS.items():
        key = cfg["key"]
        rust_start = parse_time(cfg["rust_start"]) if cfg["rust_start"] else None
        release_at = release_lookup(repo)
        extra_files = load_json(RAW / key / "pr_files_extra.json", {})

        repo_labels = load_json(RAW / key / "labels.json", [])
        for r in load_json(RAW / key / "releases.json", []):
            release_rows.append({
                "repo": repo, "tag": r["tagName"], "name": r["name"] or "",
                "published_at": r["publishedAt"] or r["createdAt"], "is_prerelease": r["isPrerelease"],
            })

        for kind, typ in (("issues", "issue"), ("pull_requests", "pr")):
            for n in iter_cached(repo, kind):
                created = parse_time(n["createdAt"])
                if created > cutoff:
                    continue
                closed = parse_time(n["closedAt"])
                author = n["author"] or {}
                label_names = [l["name"] for l in n["labels"]["nodes"]]
                bug_labels = [l for l in label_names if l in BUG_LABELS[repo]]
                row = {
                    "item_id": f"{key}#{n['number']}", "repo": repo, "number": n["number"], "type": typ,
                    "title": n["title"], "body": n["body"] or "", "url": n["url"],
                    "state": n["state"].lower(), "state_reason": "", "merged": "",
                    "created_at": n["createdAt"], "closed_at": n["closedAt"] or "", "merged_at": "",
                    "days_to_close": round((closed - created).total_seconds() / 86400, 3) if closed else "",
                    "author_login": author.get("login", "ghost"),
                    "author_association": n["authorAssociation"],
                    "author_is_bot": is_bot(author.get("__typename"), author.get("login")),
                    "labels": ";".join(label_names), "is_bug": bool(bug_labels),
                    "bug_labels": ";".join(bug_labels),
                    "milestone": (n["milestone"] or {}).get("title", ""),
                    "comments_count": n["comments"]["totalCount"],
                    "reactions_count": n["reactions"]["totalCount"],
                    "release_at_creation": release_at(created),
                    "rust_period": ("no_rust" if rust_start is None
                                    else "pre_rust" if created < rust_start else "post_rust"),
                }
                if typ == "issue":
                    row["state_reason"] = (n["stateReason"] or "").lower()
                    linked = [x["number"] for x in n["closedByPullRequestsReferences"]["nodes"]]
                else:
                    row["state"] = "closed" if n["state"] == "MERGED" else n["state"].lower()
                    row["merged"] = n["merged"]
                    row["merged_at"] = n["mergedAt"] or ""
                    linked = [x["number"] for x in n["closingIssuesReferences"]["nodes"]]
                    paths = [f["path"] for f in (n["files"] or {"nodes": []})["nodes"]]
                    paths += extra_files.get(str(n["number"]), [])
                    rs, py, other, touches = classify_files(paths)
                    row.update({
                        "pr_additions": n["additions"], "pr_deletions": n["deletions"],
                        "pr_changed_files": n["changedFiles"], "pr_files_rs": rs, "pr_files_py": py,
                        "pr_files_other": other, "pr_touches_rust": touches,
                    })
                    for rv in n["reviews"]["nodes"]:
                        if not rv["body"] or parse_time(rv["createdAt"]) > cutoff:
                            continue
                        ra = rv["author"] or {}
                        comments.append({
                            "item_id": row["item_id"], "repo": repo, "comment_id": rv["databaseId"],
                            "kind": "review", "author_login": ra.get("login", "ghost"),
                            "author_association": rv["authorAssociation"],
                            "author_is_bot": is_bot(ra.get("__typename"), ra.get("login")),
                            "created_at": rv["createdAt"], "review_state": rv["state"].lower(),
                            "path": "", "body": rv["body"],
                        })
                row["linked_items"] = ";".join(f"{key}#{x}" for x in linked)
                items.append(row)

        for kind, label in (("issue_comments", "issue_comment"), ("review_comments", "review_comment")):
            for c in iter_cached(repo, kind):
                if parse_time(c["created_at"]) > cutoff:
                    continue
                url = c.get("issue_url") or c.get("pull_request_url") or ""
                user = c.get("user") or {}
                comments.append({
                    "item_id": f"{key}#{url.rstrip('/').rsplit('/', 1)[-1]}", "repo": repo,
                    "comment_id": c["id"], "kind": label, "author_login": user.get("login", "ghost"),
                    "author_association": c.get("author_association", ""),
                    "author_is_bot": is_bot(user.get("type"), user.get("login")),
                    "created_at": c["created_at"], "review_state": "", "path": c.get("path", ""),
                    "body": c.get("body") or "",
                })

        # Label usage counted over the collected items (within the cutoff).
        for lab in repo_labels:
            used = [r for r in items if r["repo"] == repo and lab["name"] in r["labels"].split(";")]
            labels_rows.append({
                "repo": repo, "label": lab["name"], "description": lab["description"] or "",
                "issues_count": sum(r["type"] == "issue" for r in used),
                "prs_count": sum(r["type"] == "pr" for r in used),
                "is_bug_label": lab["name"] in BUG_LABELS[repo],
            })

    items.sort(key=lambda r: (r["repo"], r["number"]))
    comments.sort(key=lambda r: (r["repo"], r["created_at"], str(r["comment_id"])))
    write_csv(OUT / "items.csv", ITEM_FIELDS, items)
    write_csv(OUT / "comments.csv", COMMENT_FIELDS, comments)
    # Bug-only subset: items carrying a bug label, and the comments on them.
    bug_ids = {r["item_id"] for r in items if r["is_bug"]}
    write_csv(OUT / "bug_items.csv", ITEM_FIELDS, [r for r in items if r["is_bug"]])
    write_csv(OUT / "bug_comments.csv", COMMENT_FIELDS, [c for c in comments if c["item_id"] in bug_ids])
    write_csv(OUT / "labels.csv", ["repo", "label", "description", "issues_count", "prs_count", "is_bug_label"], labels_rows)
    write_csv(OUT / "releases.csv", ["repo", "tag", "name", "published_at", "is_prerelease"], release_rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--build-only", action="store_true", help="skip fetching, rebuild CSVs from data/raw")
    args = parser.parse_args()
    if not args.build_only:
        fetch_all(GitHub(get_token()))
    build()


if __name__ == "__main__":
    main()
