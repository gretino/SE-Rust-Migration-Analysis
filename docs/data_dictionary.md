# Data dictionary

Snapshot cutoff: items and comments created after **2026-10-23 23:59:59 UTC** are excluded.
Open/closed state, labels and counts are as they were when the collector ran. Labels
added or removed before that point are not tracked.

CSV files are UTF-8 with BOM, so Excel opens them correctly. Every text field is
quoted, so bodies with line breaks stay in one cell. Booleans are written as `True` or
`False`, and a field that does not apply is left empty. Times are ISO 8601 in UTC.
Excel cuts cells off at 32,767 characters, which can truncate a few very long bodies.
Use pandas (`pd.read_csv(path, encoding="utf-8-sig")`) if you need the full text.

## Rust migration markers

| Repo | `rust_start` | Evidence |
| --- | --- | --- |
| Qiskit/qiskit | 2022-02-28 21:49:54 UTC | First `Cargo.toml`, `.rs` files and `setuptools-rust` arrived in PR #7658 ("Implement multithreaded stochastic swap in rust"), commit `ccc371f8f`. The first release with Rust was 0.20.0 (2022-03-31). |
| quantumlib/Cirq | none | Git history has no `.rs` or `Cargo.toml` files, so every Cirq item is `no_rust` (control group). |

Qiskit/qiskit is the former `qiskit-terra` repository, renamed in 2023, so its issue numbers run continuously.

## items.csv

| Column | Meaning |
| --- | --- |
| `item_id` | `qiskit#<number>` or `cirq#<number>`, unique across both repos |
| `repo` | `Qiskit/qiskit` or `quantumlib/Cirq` |
| `number` | Issue or PR number on GitHub |
| `type` | `issue` or `pr` |
| `title`, `body` | Raw Markdown as written on GitHub |
| `url` | Link to the item |
| `state` | `open` or `closed` (merged PRs are `closed`) |
| `state_reason` | Issues only: `completed`, `not_planned`, `reopened`, or empty |
| `merged` | PRs only |
| `created_at`, `closed_at`, `merged_at` | Timestamps |
| `days_to_close` | `closed_at - created_at` in days, empty if open |
| `author_login` | GitHub login, or `ghost` for deleted accounts |
| `author_association` | Author's relation to the repo: `OWNER`, `MEMBER`, `COLLABORATOR`, `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, `FIRST_TIMER`, `NONE` |
| `author_is_bot` | GitHub bot accounts, `*[bot]` logins, and known bots (qiskit-bot, mergify, dependabot, github-actions, codecov, coveralls, CLAassistant, pre-commit-ci, renovate) |
| `labels` | All labels, separated by `;` |
| `is_bug` | True if the item has its repo's bug label: `bug` (Qiskit) or `kind/bug-report` (Cirq) |
| `bug_labels` | The labels that matched |
| `milestone` | Milestone title |
| `comments_count` | Conversation comments (not review comments) |
| `reactions_count` | Total emoji reactions on the item |
| `linked_items` | For issues: PRs that close it. For PRs: issues it closes. `;`-separated `item_id`s |
| `pr_additions`, `pr_deletions`, `pr_changed_files` | PR size |
| `pr_files_rs`, `pr_files_py`, `pr_files_other` | Changed files by type (`.py`, `.pyi` and `.pyx` count as Python) |
| `pr_touches_rust` | True if the PR changed any `.rs`, `Cargo.toml` or `Cargo.lock` file |
| `release_at_creation` | Latest non-prerelease release published before the item was opened |
| `rust_period` | `pre_rust` or `post_rust` (Qiskit, by `created_at` vs `rust_start`); `no_rust` for Cirq |

## comments.csv

| Column | Meaning |
| --- | --- |
| `item_id` | The issue or PR the comment belongs to |
| `repo` | Repository |
| `comment_id` | GitHub's id for the comment |
| `kind` | `issue_comment` (conversation on an issue or PR), `review` (body of a PR review), `review_comment` (inline comment on PR code) |
| `author_login`, `author_association`, `author_is_bot` | As in items.csv |
| `created_at` | Timestamp |
| `review_state` | Reviews only: `approved`, `changes_requested`, `commented`, `dismissed` |
| `path` | Review comments only: the file commented on |
| `body` | Raw Markdown |

Only reviews with a non-empty body are kept. Only the first 50 reviews per PR are collected.

## labels.csv

`repo`, `label`, `description`, `issues_count`, `prs_count`, `is_bug_label`. Check
`is_bug_label` to see exactly which labels feed `is_bug`. Qiskit's `Changelog: Fixed`
PR label marks bug-fix PRs, but it is not counted as a bug label. Label counts cover only
items created before the cutoff.

## bug_items.csv and bug_comments.csv

These files contain only the rows of items.csv where `is_bug` is True, with the same columns.
bug_comments.csv holds the comments on those items, in the same format as comments.csv.

## releases.csv

`repo`, `tag`, `name`, `published_at`, `is_prerelease`.
