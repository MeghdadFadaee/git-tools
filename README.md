# Git Tools

A collection of small command-line utilities for comparing and synchronizing
GitHub and Gitea repositories, downloading GitHub release assets, and updating
HTTPS Git remote URLs.

## Tools

| Tool                                                       | Purpose                                                                                           |
|------------------------------------------------------------|---------------------------------------------------------------------------------------------------|
| [`repository_comparator.py`](repository_comparator.py)     | Inventory personal GitHub and Gitea repositories and compare all branches.                        |
| [`repository_synchronizer.py`](repository_synchronizer.py) | Safely copy missing repositories and branches or fast-forward branches using a comparison report. |
| [`download_release_assets.sh`](download_release_assets.sh) | Download every asset from a GitHub release.                                                       |
| [`git-set-username.ps1`](git-set-username.ps1)             | Insert or replace a username in HTTPS Git remote URLs.                                            |


## License

Released under the [MIT License](LICENSE).
