#!/usr/bin/env python3
"""Safely synchronize GitHub and Gitea from a repository comparison report."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from repository_comparator import (
    ComparisonError,
    ProgressReporter,
    _repo_record,
    atomic_write,
    git_auth_env,
    run_git,
    write_askpass,
)


SYNC_SCHEMA_VERSION = 1


class SyncError(RuntimeError):
    """A fatal synchronization or validation error."""


class ApiError(SyncError):
    def __init__(self, provider: str, status: int, detail: str = "") -> None:
        suffix = f": {detail}" if detail else ""
        super().__init__(f"{provider} API request failed with HTTP {status}{suffix}")
        self.status = status


def api_request(
    method: str,
    url: str,
    token: str,
    provider: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    headers = {"Accept": "application/json", "User-Agent": "git-repository-sync/1"}
    if provider == "github":
        headers.update(
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
    else:
        headers["Authorization"] = f"token {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(url, data=data, headers=headers, method=method), timeout=30) as response:
            body = response.read()
            return json.loads(body) if body else None
    except HTTPError as exc:
        try:
            parsed = json.loads(exc.read().decode("utf-8", "replace"))
            detail = parsed.get("message") or parsed.get("error") or ""
        except (json.JSONDecodeError, AttributeError):
            detail = ""
        raise ApiError(provider, exc.code, detail) from exc
    except (URLError, TimeoutError) as exc:
        raise SyncError(f"{provider} API request failed: {getattr(exc, 'reason', exc)}") from exc


def provider_api_base(report: dict[str, Any], provider: str) -> str:
    if provider == "github":
        return report["servers"]["github"]["api_url"].rstrip("/")
    return f"{report['servers']['gitea']['url'].rstrip('/')}/api/v1"


def repository_api_url(report: dict[str, Any], provider: str, owner: str, name: str) -> str:
    base = provider_api_base(report, provider)
    return f"{base}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"


def get_repository(
    report: dict[str, Any], provider: str, owner: str, name: str, token: str
) -> dict[str, Any] | None:
    try:
        raw = api_request("GET", repository_api_url(report, provider, owner, name), token, provider)
    except ApiError as exc:
        if exc.status == 404:
            return None
        raise
    return _repo_record(raw)


def create_repository(
    report: dict[str, Any], provider: str, source: dict[str, Any], token: str
) -> dict[str, Any]:
    base = provider_api_base(report, provider)
    payload = {
        "name": source["name"],
        "description": source.get("description", ""),
        "private": bool(source.get("private")),
        "auto_init": False,
    }
    raw = api_request("POST", f"{base}/user/repos", token, provider, payload)
    return _repo_record(raw)


def update_created_repository(
    report: dict[str, Any],
    provider: str,
    repository: dict[str, Any],
    source: dict[str, Any],
    token: str,
) -> None:
    payload: dict[str, Any] = {
        "description": source.get("description", ""),
        "private": bool(source.get("private")),
    }
    if source.get("default_branch"):
        payload["default_branch"] = source["default_branch"]
    if source.get("archived"):
        payload["archived"] = True
    api_request(
        "PATCH",
        repository_api_url(report, provider, repository["owner"], repository["name"]),
        token,
        provider,
        payload,
    )


def expected_clone_host(report: dict[str, Any], provider: str) -> str:
    if provider == "github":
        api = urlparse(report["servers"]["github"]["api_url"])
        return "github.com" if api.hostname == "api.github.com" else (api.hostname or "")
    return urlparse(report["servers"]["gitea"]["url"]).hostname or ""


def validate_clone_url(report: dict[str, Any], provider: str, clone_url: str) -> None:
    parsed = urlparse(clone_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SyncError(f"unsafe {provider} clone URL: HTTPS is required")
    if parsed.username or parsed.password:
        raise SyncError(f"unsafe {provider} clone URL: embedded credentials are not allowed")
    if parsed.hostname.casefold() != expected_clone_host(report, provider).casefold():
        raise SyncError(f"unsafe {provider} clone URL host: {parsed.hostname}")


def validate_report(report: dict[str, Any], allow_incomplete: bool) -> None:
    if not isinstance(report, dict) or report.get("schema_version") not in (1, 2):
        raise SyncError("unsupported or invalid comparison report schema")
    required = ("servers", "github_only", "gitea_only", "matched", "ambiguous", "errors")
    if any(key not in report for key in required):
        raise SyncError("comparison report is missing required fields")
    if not allow_incomplete and (not report.get("complete") or report["ambiguous"] or report["errors"]):
        raise SyncError("comparison report is incomplete; regenerate it or use --allow-incomplete")
    for provider, repos in (
        ("github", report["github_only"]),
        ("gitea", report["gitea_only"]),
    ):
        for repo in repos:
            validate_clone_url(report, provider, repo.get("clone_url", ""))
    for pair in report["matched"]:
        if pair.get("error"):
            continue
        validate_clone_url(report, "github", pair["github"].get("clone_url", ""))
        validate_clone_url(report, "gitea", pair["gitea"].get("clone_url", ""))


def branch_map(git_dir: Path, provider: str) -> dict[str, str]:
    output = run_git(
        git_dir,
        ["for-each-ref", "--format=%(refname:strip=3) %(objectname)", f"refs/sync/{provider}"],
    ).stdout
    result: dict[str, str] = {}
    for line in output.splitlines():
        if line:
            name, oid = line.rsplit(" ", 1)
            result[name] = oid
    return result


def add_and_fetch_remote(
    git_dir: Path,
    provider: str,
    repository: dict[str, Any],
    username: str,
    token: str,
    askpass: Path,
) -> None:
    run_git(git_dir, ["remote", "add", provider, repository["clone_url"]])
    run_git(
        git_dir,
        ["fetch", "--quiet", "--no-tags", provider, f"+refs/heads/*:refs/sync/{provider}/*"],
        env=git_auth_env(provider, username, token, askpass),
    )


def branch_action(
    git_dir: Path,
    branch: str,
    github_oid: str | None,
    gitea_oid: str | None,
) -> dict[str, Any]:
    base = {
        "type": "sync_branch",
        "branch": branch,
        "github_oid": github_oid,
        "gitea_oid": gitea_oid,
        "status": "planned",
    }
    if github_oid is None:
        return {**base, "source": "gitea", "target": "github", "reason": "missing_on_github"}
    if gitea_oid is None:
        return {**base, "source": "github", "target": "gitea", "reason": "missing_on_gitea"}
    if github_oid == gitea_oid:
        return {**base, "status": "unchanged", "reason": "synchronized"}
    github_ref = f"refs/sync/github/{branch}"
    gitea_ref = f"refs/sync/gitea/{branch}"
    if run_git(git_dir, ["merge-base", "--is-ancestor", gitea_ref, github_ref], check=False).returncode == 0:
        return {**base, "source": "github", "target": "gitea", "reason": "fast_forward_gitea"}
    if run_git(git_dir, ["merge-base", "--is-ancestor", github_ref, gitea_ref], check=False).returncode == 0:
        return {**base, "source": "gitea", "target": "github", "reason": "fast_forward_github"}
    common = run_git(git_dir, ["merge-base", github_ref, gitea_ref], check=False).returncode == 0
    return {
        **base,
        "status": "blocked",
        "reason": "diverged" if common else "unrelated_history",
    }


def transfer_lfs_and_push(
    git_dir: Path,
    action: dict[str, Any],
    users: dict[str, str],
    tokens: dict[str, str],
    askpass: Path,
    on_progress: Callable[[str], None],
) -> None:
    source, target, branch = action["source"], action["target"], action["branch"]
    local_ref = f"refs/sync/{source}/{branch}"
    on_progress(f"transferring LFS for {branch}")
    run_git(
        git_dir,
        ["lfs", "fetch", source, local_ref],
        env=git_auth_env(source, users[source], tokens[source], askpass),
    )
    run_git(
        git_dir,
        ["lfs", "push", target, local_ref],
        env=git_auth_env(target, users[target], tokens[target], askpass),
    )
    on_progress(f"pushing {branch} to {target}")
    run_git(
        git_dir,
        ["push", "--porcelain", target, f"{local_ref}:refs/heads/{branch}"],
        env=git_auth_env(target, users[target], tokens[target], askpass),
    )


def process_repository(
    report: dict[str, Any],
    name: str,
    tokens: dict[str, str],
    apply: bool,
    on_progress: Callable[[str], None],
) -> dict[str, Any]:
    users = {provider: report["servers"][provider]["username"] for provider in ("github", "gitea")}
    current: dict[str, dict[str, Any] | None] = {}
    on_progress("revalidating repository")
    for provider in ("github", "gitea"):
        current[provider] = get_repository(report, provider, users[provider], name, tokens[provider])
        if current[provider]:
            validate_clone_url(report, provider, current[provider]["clone_url"])
    result: dict[str, Any] = {"name": name, "actions": [], "warnings": [], "status": "planned"}
    if not current["github"] and not current["gitea"]:
        raise SyncError("repository no longer exists on either server")

    missing = "github" if not current["github"] else "gitea" if not current["gitea"] else None
    source = "gitea" if missing == "github" else "github"
    source_repo = current[source] if missing else None
    if missing and source_repo:
        create_action = {
            "type": "create_repository",
            "source": source,
            "target": missing,
            "status": "planned",
            "private": bool(source_repo.get("private")),
        }
        if source_repo.get("fork"):
            result["warnings"].append("fork relationship cannot be preserved across servers")
        result["actions"].append(create_action)
        settings_action = {"type": "update_settings", "target": missing, "status": "planned"}
        result["actions"].append(settings_action)
        if apply:
            on_progress(f"creating repository on {missing}")
            current[missing] = create_repository(report, missing, source_repo, tokens[missing])
            validate_clone_url(report, missing, current[missing]["clone_url"])
            create_action["status"] = "applied"

    with tempfile.TemporaryDirectory(prefix="repo-sync-") as directory:
        root = Path(directory)
        git_dir = root / "sync.git"
        subprocess.run(["git", "init", "--bare", "--quiet", str(git_dir)], check=True)
        askpass = root / "askpass.sh"
        write_askpass(askpass)
        for provider in ("github", "gitea"):
            repository = current[provider]
            if repository:
                on_progress(f"fetching {provider} branches")
                add_and_fetch_remote(git_dir, provider, repository, users[provider], tokens[provider], askpass)

        branches = {provider: branch_map(git_dir, provider) if current[provider] else {} for provider in ("github", "gitea")}
        for branch in sorted(set(branches["github"]) | set(branches["gitea"])):
            action = branch_action(git_dir, branch, branches["github"].get(branch), branches["gitea"].get(branch))
            result["actions"].append(action)
            if apply and action["status"] == "planned":
                try:
                    transfer_lfs_and_push(git_dir, action, users, tokens, askpass, on_progress)
                    action["status"] = "applied"
                except (ComparisonError, OSError, subprocess.SubprocessError) as exc:
                    action["status"] = "failed"
                    action["error"] = str(exc)

    if apply and missing and current[missing] and source_repo:
        branch_failures = any(
            action["type"] == "sync_branch" and action["status"] == "failed"
            for action in result["actions"]
        )
        if not branch_failures:
            try:
                on_progress(f"updating {missing} settings")
                update_created_repository(report, missing, current[missing], source_repo, tokens[missing])
                settings_action["status"] = "applied"
            except (SyncError, OSError) as exc:
                settings_action["status"] = "failed"
                settings_action["error"] = str(exc)
        else:
            settings_action["status"] = "skipped"
            settings_action["reason"] = "branch transfer failed"

    statuses = {action["status"] for action in result["actions"]}
    if "failed" in statuses:
        result["status"] = "failed"
    elif "blocked" in statuses:
        result["status"] = "blocked"
    elif statuses <= {"unchanged"}:
        result["status"] = "unchanged"
    elif apply:
        result["status"] = "applied"
    return result


def report_candidates(report: dict[str, Any], allow_incomplete: bool) -> list[tuple[str, dict[str, Any]]]:
    candidates: dict[str, dict[str, Any]] = {}
    for repo in report["github_only"]:
        candidates[repo["name"].casefold()] = {"name": repo["name"], "github": repo, "gitea": None}
    for repo in report["gitea_only"]:
        candidates[repo["name"].casefold()] = {"name": repo["name"], "github": None, "gitea": repo}
    for pair in report["matched"]:
        if pair.get("error"):
            if allow_incomplete:
                continue
            raise SyncError(f"report contains a failed repository: {pair.get('name', 'unknown')}")
        candidates[pair["name"].casefold()] = pair
    return [(item["name"], item) for _, item in sorted(candidates.items())]


def synchronize(
    report: dict[str, Any],
    tokens: dict[str, str],
    apply: bool,
    allow_incomplete: bool,
    progress: ProgressReporter,
) -> dict[str, Any]:
    candidates = report_candidates(report, allow_incomplete)
    results: list[dict[str, Any]] = []
    total = len(candidates)
    progress.phase(f"{'Applying' if apply else 'Planning'} synchronization for {total} repositories...")
    for index, (name, item) in enumerate(candidates):
        progress.start_repository(index, total, name)
        try:
            result = process_repository(
                report,
                name,
                tokens,
                apply,
                lambda stage, i=index, n=name: progress.repository(i, total, n, stage),
            )
        except (SyncError, ComparisonError, OSError, subprocess.SubprocessError) as exc:
            result = {"name": name, "status": "failed", "actions": [], "warnings": [], "error": str(exc)}
        results.append(result)
        progress.finish_repository(index + 1, total, name, result["status"])
    progress.finish(total)
    blocked = any(result["status"] in ("failed", "blocked") for result in results)
    return {
        "schema_version": SYNC_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if apply else "dry-run",
        "source_report": report.get("generated_at"),
        "source_report_warnings": list(report.get("errors", []))
        + ([f"{len(report.get('ambiguous', []))} ambiguous repository match(es) skipped"] if report.get("ambiguous") else []),
        "complete": not blocked,
        "repositories": results,
    }


def render_markdown(audit: dict[str, Any]) -> str:
    lines = [
        "# Repository Synchronization " + ("Result" if audit["mode"] == "apply" else "Plan"),
        "",
        f"Generated: {audit['generated_at']}",
        f"Mode: **{audit['mode']}**",
        f"Complete: **{'yes' if audit['complete'] else 'no'}**",
        "",
        "| Repository | Status | Planned | Applied | Blocked | Failed | Skipped |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for repo in audit["repositories"]:
        counts = {status: 0 for status in ("planned", "applied", "blocked", "failed", "skipped")}
        for action in repo["actions"]:
            if action["status"] in counts:
                counts[action["status"]] += 1
        lines.append(
            f"| {repo['name']} | {repo['status']} | {counts['planned']} | {counts['applied']} | "
            f"{counts['blocked']} | {counts['failed']} | {counts['skipped']} |"
        )
    lines.append("")
    if audit.get("source_report_warnings"):
        lines.extend(["## Source Report Warnings", ""])
        lines.extend(f"- {warning}" for warning in audit["source_report_warnings"])
        lines.append("")
    for repo in audit["repositories"]:
        lines.extend([f"## {repo['name']}", ""])
        if repo.get("error"):
            lines.extend([f"Error: `{repo['error']}`", ""])
        for warning in repo.get("warnings", []):
            lines.append(f"- Warning: {warning}")
        for action in repo["actions"]:
            if action["type"] == "sync_branch":
                direction = f"{action.get('source', '—')} → {action.get('target', '—')}"
                lines.append(
                    f"- `{action['branch']}`: **{action['status']}** ({action['reason']}; {direction})"
                    + (f" — {action['error']}" if action.get("error") else "")
                )
            else:
                lines.append(
                    f"- {action['type']} on {action.get('target', '—')}: **{action['status']}**"
                    + (f" — {action['error']}" if action.get("error") else "")
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def check_prerequisites() -> None:
    for command, label in ((["git", "--version"], "Git"), (["git", "lfs", "version"], "Git LFS")):
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        except FileNotFoundError as exc:
            raise SyncError(f"{label} is required") from exc
        if result.returncode:
            raise SyncError(f"{label} is required")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path("reports/repository-comparison.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--apply", action="store_true", help="apply planned remote changes")
    parser.add_argument("--allow-incomplete", action="store_true", help="process valid entries from an incomplete report")
    parser.add_argument("--no-progress", action="store_true", help="disable progress output")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    tokens = {"github": os.environ.get("GITHUB_TOKEN", ""), "gitea": os.environ.get("GITEA_TOKEN", "")}
    missing = [f"{provider.upper()}_TOKEN" for provider, value in tokens.items() if not value]
    if missing:
        print(f"error: missing required environment variable(s): {', '.join(missing)}", file=sys.stderr)
        return 2
    try:
        check_prerequisites()
        report = json.loads(args.report.read_text(encoding="utf-8"))
        validate_report(report, args.allow_incomplete)
        progress = ProgressReporter(enabled=not args.no_progress)
        audit = synchronize(report, tokens, args.apply, args.allow_incomplete, progress)
        output_dir = args.output_dir.resolve()
        stem = "repository-sync-result" if args.apply else "repository-sync-plan"
        atomic_write(output_dir / f"{stem}.json", json.dumps(audit, indent=2, ensure_ascii=False) + "\n")
        atomic_write(output_dir / f"{stem}.md", render_markdown(audit))
    except (SyncError, ComparisonError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Synchronization {'result' if args.apply else 'plan'} written to {output_dir}")
    return 0 if audit["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
