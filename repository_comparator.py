#!/usr/bin/env python3
"""Compare repositories and branches owned on GitHub and a Gitea server."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SCHEMA_VERSION = 2
DEFAULT_GITHUB_API = "https://api.github.com"
DEFAULT_GITEA_URL = "https://mahgit.ir"


class ComparisonError(RuntimeError):
    """A user-facing comparison failure."""


class ProgressReporter:
    """Render comparison progress without adding third-party dependencies."""

    def __init__(self, enabled: bool = True, stream: TextIO | None = None) -> None:
        self.enabled = enabled
        self.stream = stream or sys.stderr
        self.is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self._line_length = 0

    def _dynamic_line(self, message: str) -> None:
        padding = " " * max(0, self._line_length - len(message))
        print(f"\r{message}{padding}", end="", file=self.stream, flush=True)
        self._line_length = len(message)

    def phase(self, message: str) -> None:
        if not self.enabled:
            return
        if self.is_tty and self._line_length:
            self._dynamic_line("")
            print(file=self.stream, flush=True)
            self._line_length = 0
        print(message, file=self.stream, flush=True)

    def repository(self, completed: int, total: int, name: str, stage: str) -> None:
        if not self.enabled or not self.is_tty:
            return
        width = 30
        filled = width if total == 0 else int(width * completed / total)
        bar = "#" * filled + "-" * (width - filled)
        percent = 100 if total == 0 else int(100 * completed / total)
        self._dynamic_line(f"[{bar}] {completed}/{total} ({percent:3d}%) {name} — {stage}")

    def start_repository(self, completed: int, total: int, name: str) -> None:
        if not self.enabled:
            return
        if self.is_tty:
            self.repository(completed, total, name, "starting")
        else:
            print(f"[{completed + 1}/{total}] Comparing {name}...", file=self.stream, flush=True)

    def finish_repository(self, completed: int, total: int, name: str, status: str) -> None:
        if not self.enabled:
            return
        if self.is_tty:
            self.repository(completed, total, name, status)
        else:
            print(f"[{completed}/{total}] {name}: {status}", file=self.stream, flush=True)

    def finish(self, total: int) -> None:
        if not self.enabled:
            return
        if self.is_tty and self._line_length:
            self.repository(total, total, "", "complete")
            print(file=self.stream, flush=True)
            self._line_length = 0


def api_get(url: str, token: str, provider: str) -> tuple[Any, dict[str, str]]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "git-repository-comparison/1",
    }
    if provider == "github":
        headers["Accept"] = "application/vnd.github+json"
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    else:
        headers["Authorization"] = f"token {token}"

    try:
        with urlopen(Request(url, headers=headers), timeout=30) as response:
            body = json.load(response)
            return body, {key.lower(): value for key, value in response.headers.items()}
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("message", "")
        except (json.JSONDecodeError, AttributeError):
            detail = ""
        suffix = f": {detail}" if detail else ""
        raise ComparisonError(f"{provider} API request failed with HTTP {exc.code}{suffix}") from exc
    except (URLError, TimeoutError) as exc:
        detail = getattr(exc, "reason", exc)
        raise ComparisonError(f"{provider} API request failed: {detail}") from exc


def _repo_record(raw: dict[str, Any]) -> dict[str, Any]:
    owner = raw.get("owner") or {}
    return {
        "name": raw["name"],
        "full_name": raw.get("full_name") or f"{owner.get('login', owner.get('username', ''))}/{raw['name']}",
        "owner": owner.get("login") or owner.get("username") or "",
        "url": raw.get("html_url") or raw.get("website") or "",
        "clone_url": raw.get("clone_url") or "",
        "default_branch": raw.get("default_branch") or None,
        "description": raw.get("description") or "",
        "private": bool(raw.get("private")),
        "archived": bool(raw.get("archived")),
        "fork": bool(raw.get("fork")),
        "empty": bool(raw.get("empty")),
    }


def inventory_github(api_url: str, token: str) -> tuple[str, list[dict[str, Any]]]:
    api_url = api_url.rstrip("/")
    user, _ = api_get(f"{api_url}/user", token, "github")
    username = user["login"]
    repos: list[dict[str, Any]] = []
    page = 1
    while True:
        query = urlencode({"affiliation": "owner", "visibility": "all", "per_page": 100, "page": page})
        batch, _ = api_get(f"{api_url}/user/repos?{query}", token, "github")
        if not isinstance(batch, list):
            raise ComparisonError("github API returned an invalid repository list")
        repos.extend(_repo_record(repo) for repo in batch if (repo.get("owner") or {}).get("login", "").casefold() == username.casefold())
        if len(batch) < 100:
            break
        page += 1
    return username, repos


def inventory_gitea(base_url: str, token: str) -> tuple[str, list[dict[str, Any]]]:
    api_url = f"{base_url.rstrip('/')}/api/v1"
    user, _ = api_get(f"{api_url}/user", token, "gitea")
    username = user.get("login") or user.get("username")
    if not username:
        raise ComparisonError("gitea API did not return the authenticated username")
    repos: list[dict[str, Any]] = []
    page = 1
    while True:
        query = urlencode({"limit": 50, "page": page})
        batch, _ = api_get(f"{api_url}/user/repos?{query}", token, "gitea")
        if not isinstance(batch, list):
            raise ComparisonError("gitea API returned an invalid repository list")
        repos.extend(_repo_record(repo) for repo in batch if ((repo.get("owner") or {}).get("login") or (repo.get("owner") or {}).get("username", "")).casefold() == username.casefold())
        if len(batch) < 50:
            break
        page += 1
    return username, repos


def pair_repositories(
    github_repos: list[dict[str, Any]], gitea_repos: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    def grouped(repos: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for repo in repos:
            result.setdefault(repo["name"].casefold(), []).append(repo)
        return result

    gh, gt = grouped(github_repos), grouped(gitea_repos)
    matched: list[dict[str, Any]] = []
    github_only: list[dict[str, Any]] = []
    gitea_only: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    for key in sorted(set(gh) | set(gt)):
        left, right = gh.get(key, []), gt.get(key, [])
        if len(left) > 1 or len(right) > 1:
            ambiguous.append({"normalized_name": key, "github": left, "gitea": right})
        elif left and right:
            matched.append({"name": left[0]["name"], "github": left[0], "gitea": right[0]})
        elif left:
            github_only.append(left[0])
        else:
            gitea_only.append(right[0])
    return matched, github_only, gitea_only, ambiguous


def run_git(git_dir: Path, args: list[str], env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "--git-dir", str(git_dir), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    if check and result.returncode:
        message = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown Git error"
        message = re.sub(r"https?://[^/@\s]+@", "https://", message)
        raise ComparisonError(message)
    return result


def git_auth_env(provider: str, username: str, token: str, askpass: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_ASKPASS": str(askpass),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTH_USERNAME": "x-access-token" if provider == "github" else username,
            "GIT_AUTH_TOKEN": token,
        }
    )
    return env


def write_askpass(path: Path) -> None:
    path.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  *Username*) printf '%s\\n' \"$GIT_AUTH_USERNAME\" ;;\n"
        "  *) printf '%s\\n' \"$GIT_AUTH_TOKEN\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    path.chmod(0o700)


def list_branches(git_dir: Path, namespace: str) -> list[str]:
    result = run_git(git_dir, ["for-each-ref", "--format=%(refname:strip=3)", f"refs/compare/{namespace}"])
    return sorted(line for line in result.stdout.splitlines() if line)


def commit_details(git_dir: Path, include_ref: str, exclude_ref: str) -> list[dict[str, str]]:
    result = run_git(
        git_dir,
        ["log", "--topo-order", "-z", "--format=%H%x00%aI%x00%an%x00%s", include_ref, "--not", exclude_ref],
    )
    fields = result.stdout.split("\0")
    while fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 4:
        raise ComparisonError("could not parse Git commit metadata")
    return [
        {"hash": fields[i], "date": fields[i + 1], "author": fields[i + 2], "subject": fields[i + 3]}
        for i in range(0, len(fields), 4)
    ]


def compare_branch(git_dir: Path, branch: str) -> dict[str, Any]:
    github_ref = f"refs/compare/github/{branch}"
    gitea_ref = f"refs/compare/gitea/{branch}"
    counts = run_git(git_dir, ["rev-list", "--left-right", "--count", f"{github_ref}...{gitea_ref}"]).stdout.split()
    github_ahead, gitea_ahead = int(counts[0]), int(counts[1])
    common_history = run_git(git_dir, ["merge-base", github_ref, gitea_ref], check=False).returncode == 0
    if not common_history and (github_ahead or gitea_ahead):
        status = "unrelated"
    elif github_ahead and gitea_ahead:
        status = "diverged"
    elif github_ahead:
        status = "github_ahead"
    elif gitea_ahead:
        status = "gitea_ahead"
    else:
        status = "synchronized"
    return {
        "name": branch,
        "status": status,
        "github_ahead": github_ahead,
        "gitea_ahead": gitea_ahead,
        "github_commits": commit_details(git_dir, github_ref, gitea_ref) if github_ahead else [],
        "gitea_commits": commit_details(git_dir, gitea_ref, github_ref) if gitea_ahead else [],
    }


def compare_git_repositories(
    github_repo: dict[str, Any],
    gitea_repo: dict[str, Any],
    github_user: str,
    gitea_user: str,
    github_token: str,
    gitea_token: str,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if not github_repo.get("clone_url") or not gitea_repo.get("clone_url"):
        raise ComparisonError("repository API response is missing an HTTPS clone URL")
    with tempfile.TemporaryDirectory(prefix="repo-compare-") as directory:
        root = Path(directory)
        git_dir = root / "comparison.git"
        subprocess.run(["git", "init", "--bare", "--quiet", str(git_dir)], check=True)
        askpass = root / "askpass.sh"
        write_askpass(askpass)
        fetches = (
            ("github", github_repo["clone_url"], github_user, github_token),
            ("gitea", gitea_repo["clone_url"], gitea_user, gitea_token),
        )
        for provider, url, username, token in fetches:
            if on_progress:
                on_progress(f"fetching {provider} branches")
            run_git(
                git_dir,
                ["fetch", "--quiet", "--no-tags", "--prune", url, f"+refs/heads/*:refs/compare/{provider}/*"],
                env=git_auth_env(provider, username, token, askpass),
            )
        if on_progress:
            on_progress("analyzing commit history")
        github_branches = set(list_branches(git_dir, "github"))
        gitea_branches = set(list_branches(git_dir, "gitea"))
        shared = sorted(github_branches & gitea_branches)
        return {
            "status": "compared",
            "github_only_branches": sorted(github_branches - gitea_branches),
            "gitea_only_branches": sorted(gitea_branches - github_branches),
            "branches": [compare_branch(git_dir, branch) for branch in shared],
        }


def markdown_escape(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def repository_summary_status(repo: dict[str, Any]) -> str:
    if repo.get("error"):
        return "error"
    comparison = repo["comparison"]
    statuses = {branch["status"] for branch in comparison["branches"]}
    if comparison["github_only_branches"] or comparison["gitea_only_branches"]:
        statuses.add("missing branches")
    if not statuses:
        return "empty"
    if statuses == {"synchronized"}:
        return "synchronized"
    return ", ".join(sorted(statuses))


def render_markdown(report: dict[str, Any], preview_commits: int) -> str:
    lines = [
        "# Repository Comparison Report",
        "",
        f"Generated: {report['generated_at']}",
        f"GitHub account: `{markdown_escape(report['servers']['github']['username'])}`",
        f"Gitea account: `{markdown_escape(report['servers']['gitea']['username'])}`",
        f"Complete: **{'yes' if report['complete'] else 'no'}**",
        "",
        "## Summary",
        "",
        "| Category | Count |",
        "|---|---:|",
        f"| GitHub repositories | {len(report['inventories']['github'])} |",
        f"| Gitea repositories | {len(report['inventories']['gitea'])} |",
        f"| Matched repositories | {len(report['matched'])} |",
        f"| GitHub only | {len(report['github_only'])} |",
        f"| Gitea only | {len(report['gitea_only'])} |",
        f"| Ambiguous names | {len(report['ambiguous'])} |",
        "",
    ]
    for heading, key in (("GitHub Only", "github_only"), ("Gitea Only", "gitea_only")):
        lines.extend([f"## {heading}", ""])
        repos = report[key]
        if repos:
            lines.extend(["| Repository | Visibility | Default branch | Flags |", "|---|---|---|---|"])
            for repo in repos:
                flags = ", ".join(name for name in ("fork", "archived", "empty") if repo.get(name)) or "—"
                visibility = "private" if repo["private"] else "public"
                lines.append(f"| [{markdown_escape(repo['name'])}]({repo['url']}) | {visibility} | {markdown_escape(repo['default_branch']) or '—'} | {flags} |")
        else:
            lines.append("None.")
        lines.append("")

    lines.extend(["## Matched Repositories", ""])
    if report["matched"]:
        lines.extend(["| Repository | Overall status |", "|---|---|"])
        for repo in report["matched"]:
            lines.append(f"| {markdown_escape(repo['name'])} | {markdown_escape(repository_summary_status(repo))} |")
        lines.append("")
    else:
        lines.extend(["None.", ""])

    for repo in report["matched"]:
        lines.extend([f"### {markdown_escape(repo['name'])}", ""])
        if repo.get("error"):
            lines.extend([f"Comparison failed: `{markdown_escape(repo['error'])}`", ""])
            continue
        comparison = repo["comparison"]
        if comparison["github_only_branches"]:
            lines.append("GitHub-only branches: " + ", ".join(f"`{markdown_escape(x)}`" for x in comparison["github_only_branches"]))
        if comparison["gitea_only_branches"]:
            lines.append("Gitea-only branches: " + ", ".join(f"`{markdown_escape(x)}`" for x in comparison["gitea_only_branches"]))
        if comparison["github_only_branches"] or comparison["gitea_only_branches"]:
            lines.append("")
        if comparison["branches"]:
            lines.extend(["| Branch | Status | GitHub ahead | Gitea ahead |", "|---|---|---:|---:|"])
            for branch in comparison["branches"]:
                lines.append(f"| {markdown_escape(branch['name'])} | {branch['status']} | {branch['github_ahead']} | {branch['gitea_ahead']} |")
            lines.append("")
        elif not comparison["github_only_branches"] and not comparison["gitea_only_branches"]:
            lines.extend(["Both repositories are empty.", ""])
        for branch in comparison["branches"]:
            commits = [("GitHub", branch["github_commits"]), ("Gitea", branch["gitea_commits"])]
            for server, items in commits:
                if not items:
                    continue
                lines.extend([f"#### `{markdown_escape(branch['name'])}` — commits only on {server}", ""])
                for commit in items[:preview_commits]:
                    lines.append(f"- `{commit['hash'][:12]}` {markdown_escape(commit['subject'])} — {markdown_escape(commit['author'])}, {commit['date']}")
                if len(items) > preview_commits:
                    lines.append(f"- … {len(items) - preview_commits} more; see the JSON report.")
                lines.append("")

    if report["ambiguous"]:
        lines.extend(["## Ambiguous Repository Names", ""])
        for item in report["ambiguous"]:
            gh_names = ", ".join(repo["name"] for repo in item["github"]) or "none"
            gt_names = ", ".join(repo["name"] for repo in item["gitea"]) or "none"
            lines.append(f"- `{markdown_escape(item['normalized_name'])}` — GitHub: {markdown_escape(gh_names)}; Gitea: {markdown_escape(gt_names)}")
        lines.append("")
    if report["errors"]:
        lines.extend(["## Errors", ""])
        lines.extend(f"- {markdown_escape(error)}" for error in report["errors"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def generate_report(
    args: argparse.Namespace,
    github_token: str,
    gitea_token: str,
    progress: ProgressReporter | None = None,
) -> dict[str, Any]:
    progress = progress or ProgressReporter(enabled=False)
    progress.phase("Discovering GitHub repositories...")
    github_user, github_repos = inventory_github(args.github_api_url, github_token)
    progress.phase(f"Found {len(github_repos)} GitHub repositories. Discovering Gitea repositories...")
    gitea_user, gitea_repos = inventory_gitea(args.gitea_url, gitea_token)
    matched, github_only, gitea_only, ambiguous = pair_repositories(github_repos, gitea_repos)
    progress.phase(
        f"Found {len(gitea_repos)} Gitea repositories. Comparing {len(matched)} matched repositories..."
    )
    errors: list[str] = []
    compared: list[dict[str, Any]] = []
    total = len(matched)
    for index, pair in enumerate(matched):
        progress.start_repository(index, total, pair["name"])
        item = dict(pair)
        try:
            item["comparison"] = compare_git_repositories(
                pair["github"],
                pair["gitea"],
                github_user,
                gitea_user,
                github_token,
                gitea_token,
                on_progress=lambda stage, i=index, name=pair["name"]: progress.repository(i, total, name, stage),
            )
            status = "done"
        except (ComparisonError, subprocess.SubprocessError, OSError) as exc:
            item["error"] = str(exc)
            errors.append(f"{pair['name']}: {exc}")
            status = "failed"
        compared.append(item)
        progress.finish_repository(index + 1, total, pair["name"], status)
    progress.finish(total)
    if ambiguous:
        errors.append(f"{len(ambiguous)} repository name match(es) were ambiguous")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "complete": not errors,
        "servers": {
            "github": {"api_url": args.github_api_url.rstrip("/"), "username": github_user},
            "gitea": {"url": args.gitea_url.rstrip("/"), "username": gitea_user},
        },
        "inventories": {"github": github_repos, "gitea": gitea_repos},
        "github_only": github_only,
        "gitea_only": gitea_only,
        "ambiguous": ambiguous,
        "matched": compared,
        "errors": errors,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github-api-url", default=DEFAULT_GITHUB_API)
    parser.add_argument("--gitea-url", default=DEFAULT_GITEA_URL)
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--preview-commits", type=int, default=10, metavar="N")
    parser.add_argument("--no-progress", action="store_true", help="disable progress output")
    args = parser.parse_args(argv)
    if args.preview_commits < 0:
        parser.error("--preview-commits must be zero or greater")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    github_token = os.environ.get("GITHUB_TOKEN", "")
    gitea_token = os.environ.get("GITEA_TOKEN", "")
    missing = [name for name, value in (("GITHUB_TOKEN", github_token), ("GITEA_TOKEN", gitea_token)) if not value]
    if missing:
        print(f"error: missing required environment variable(s): {', '.join(missing)}", file=sys.stderr)
        return 2
    try:
        progress = ProgressReporter(enabled=not args.no_progress)
        report = generate_report(args, github_token, gitea_token, progress)
        output_dir = args.output_dir.resolve()
        progress.phase("Writing reports...")
        atomic_write(output_dir / "repository-comparison.json", json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        atomic_write(output_dir / "repository-comparison.md", render_markdown(report, args.preview_commits))
    except (ComparisonError, OSError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Reports written to {output_dir}")
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
