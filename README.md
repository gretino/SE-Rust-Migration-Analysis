# SE Rust Migration Analysis

Data collection for a study on whether bug reports change when a Python project
moves to Rust. We collect every issue, pull request and comment from
[Qiskit/qiskit](https://github.com/Qiskit/qiskit), which adopted Rust in 2022, and
[quantumlib/Cirq](https://github.com/quantumlib/Cirq), which has no Rust and serves as a control group.
The snapshot cutoff is 2026-10-23.

## Running the collector

Requirements: Python 3.9+ (standard library only) and a GitHub token. The GraphQL API
does not accept anonymous requests.

1. Create a token at https://github.com/settings/tokens. A fine-grained token with
   "Public repositories (read-only)" access and no other permissions is enough.
2. Run the collector from this folder:

   ```
   set GITHUB_TOKEN=<your token>        # Windows cmd
   $env:GITHUB_TOKEN="<your token>"     # PowerShell
   python scripts/collect.py
   ```

   If the GitHub CLI is installed and logged in (`gh auth login`), you can skip the token.

The run prints its progress and should take a few hours. Most of that time is the
comment download and waiting on the API rate limit (5,000 requests per hour).
If the run stops, run the same command again and it resumes where it left off.
To rebuild the CSVs from the cached data without downloading again, run
`python scripts/collect.py --build-only`.

## Output

| File | Contents |
| --- | --- |
| `data/processed/items.csv` | One row per issue or PR, from both repos |
| `data/processed/comments.csv` | One row per comment, review or review comment |
| `data/processed/bug_items.csv` | Only the items with a bug label (`bug` in Qiskit, `kind/bug-report` in Cirq) |
| `data/processed/bug_comments.csv` | Comments on those bug items |
| `data/processed/labels.csv` | Every label in each repo, with usage counts and the bug flag |
| `data/processed/releases.csv` | Release tags and dates for both repos |
| `data/raw/` | Cached API responses (git-ignored) |

The columns are described in [docs/data_dictionary.md](docs/data_dictionary.md).
