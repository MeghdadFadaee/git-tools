# Git Tools

## Compare GitHub and Gitea repositories

`repository_comparator.py` inventories repositories owned by your authenticated personal accounts on GitHub and Gitea. It reports repositories that exist on only one server and compares every branch of repositories found on both.

Requirements:

- Python 3.10 or newer
- Git
- A GitHub personal access token with read access to every repository being compared
- A Gitea access token with read access to every repository being compared

Set tokens in the environment and run the tool:

```bash
export GITHUB_TOKEN='github-token'
export GITEA_TOKEN='gitea-token'
python3 repository_comparator.py
```

By default, Gitea is read from `https://mahgit.ir`. The generated files are:

- `reports/repository-comparison.md` — readable summary and commit previews
- `reports/repository-comparison.json` — structured results containing every differing commit

Useful options:

```text
--gitea-url URL         Gitea server URL (default: https://mahgit.ir)
--github-api-url URL    GitHub API URL (default: https://api.github.com)
--output-dir PATH       Report directory (default: reports)
--preview-commits N     Commits shown per branch/server in Markdown (default: 10)
--no-progress           Disable progress output
```

During comparison, an interactive terminal shows a progress bar with the current repository and operation. Redirected output uses one progress line per repository instead.

Repositories are paired by name without regard to case. Branch names are matched exactly. Tags and organization-owned repositories are not compared. A run exits with status `1` when reports were generated but one or more repositories could not be compared, and status `2` for a fatal configuration or API error.

Tokens are passed to Git through a temporary askpass helper and are not written to report files, Git configuration, or clone URLs.
