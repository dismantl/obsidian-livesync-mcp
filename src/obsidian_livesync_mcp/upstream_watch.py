"""Detect upstream LiveSync releases that need compatibility review."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 in CI
    import tomli as tomllib

DEFAULT_API_URL = "https://api.github.com"


@dataclass(frozen=True)
class Upstream:
    owner: str
    repo: str
    release_limit: int


@dataclass(frozen=True)
class IssueConfig:
    marker_prefix: str


@dataclass(frozen=True)
class WatchArea:
    name: str
    risk: str
    upstream_paths: list[str]
    local_paths: list[str]
    review_notes: list[str]


@dataclass(frozen=True)
class WatchConfig:
    upstream: Upstream
    issue: IssueConfig
    areas: list[WatchArea]


@dataclass(frozen=True)
class GitHubRelease:
    tag: str
    name: str
    url: str
    body: str


@dataclass(frozen=True)
class AreaMatch:
    area: WatchArea
    changed_files: list[str]


@dataclass(frozen=True)
class ReviewCandidate:
    release: GitHubRelease
    base_tag: str
    compare_url: str
    matches: list[AreaMatch]


class UpstreamClient(Protocol):
    def list_releases(self, owner: str, repo: str, limit: int) -> list[GitHubRelease]: ...

    def compare_files(self, owner: str, repo: str, base: str, head: str) -> list[str]: ...

    def issue_exists(self, marker: str) -> bool: ...


class GitHubClient:
    def __init__(self, *, token: str | None, target_repo: str, api_url: str = DEFAULT_API_URL):
        self._token = token
        self._target_repo = target_repo
        self._api_url = api_url.rstrip("/")

    def list_releases(self, owner: str, repo: str, limit: int) -> list[GitHubRelease]:
        releases = self._request(f"/repos/{owner}/{repo}/releases", {"per_page": str(limit)})
        parsed = [
            GitHubRelease(
                tag=release["tag_name"],
                name=release.get("name") or release["tag_name"],
                url=release.get("html_url") or "",
                body=release.get("body") or "",
            )
            for release in releases
            if not release.get("draft")
        ]
        if parsed:
            return parsed

        tags = self._request(f"/repos/{owner}/{repo}/tags", {"per_page": str(limit)})
        return [
            GitHubRelease(
                tag=tag["name"],
                name=tag["name"],
                url=f"https://github.com/{owner}/{repo}/releases/tag/{tag['name']}",
                body="",
            )
            for tag in tags
        ]

    def compare_files(self, owner: str, repo: str, base: str, head: str) -> list[str]:
        return self._compare_files_with_git(owner, repo, base, head)

    def issue_exists(self, marker: str) -> bool:
        query = f'repo:{self._target_repo} is:issue "{marker}" in:body'
        result = self._request("/search/issues", {"q": query, "per_page": "1"})
        return int(result.get("total_count", 0)) > 0

    def _request(self, path: str, params: dict[str, str]) -> object:
        query = urllib.parse.urlencode(params)
        url = f"{self._api_url}{path}"
        if query:
            url = f"{url}?{query}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "obsidian-livesync-mcp-upstream-watch",
                "X-GitHub-Api-Version": "2022-11-28",
                **({"Authorization": f"Bearer {self._token}"} if self._token else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            message = f"GitHub API request failed for {path}: {exc.code} {detail}"
            raise RuntimeError(message) from exc

    def _compare_files_with_git(self, owner: str, repo: str, base: str, head: str) -> list[str]:
        repo_url = f"https://github.com/{owner}/{repo}.git"
        with tempfile.TemporaryDirectory(prefix="upstream-watch-") as tempdir:
            self._run_git(["git", "init", "-q"], tempdir)
            self._run_git(["git", "remote", "add", "origin", repo_url], tempdir)
            self._run_git(
                [
                    "git",
                    "fetch",
                    "--no-tags",
                    "--depth=1",
                    "origin",
                    f"refs/tags/{base}:refs/tags/{base}",
                    f"refs/tags/{head}:refs/tags/{head}",
                ],
                tempdir,
            )
            base_commit = self._run_git(
                ["git", "rev-parse", f"refs/tags/{base}^{{commit}}"], tempdir
            ).strip()
            head_commit = self._run_git(
                ["git", "rev-parse", f"refs/tags/{head}^{{commit}}"], tempdir
            ).strip()
            changed = self._run_git(
                ["git", "diff", "--name-only", base_commit, head_commit], tempdir
            )
            return sorted(line for line in changed.splitlines() if line)

    def _run_git(self, args: list[str], cwd: str) -> str:
        try:
            result = subprocess.run(
                args,
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Git command failed while comparing upstream tags: {args}") from exc
        return result.stdout


def load_config(path: Path) -> WatchConfig:
    raw = tomllib.loads(path.read_text())
    upstream = raw["upstream"]
    issue = raw["issue"]
    areas = raw["areas"]
    return WatchConfig(
        upstream=Upstream(
            owner=str(upstream["owner"]),
            repo=str(upstream["repo"]),
            release_limit=int(upstream.get("release_limit", 20)),
        ),
        issue=IssueConfig(marker_prefix=str(issue["marker_prefix"])),
        areas=[
            WatchArea(
                name=str(area["name"]),
                risk=str(area["risk"]),
                upstream_paths=[str(pattern) for pattern in area["upstream_paths"]],
                local_paths=[str(local_path) for local_path in area["local_paths"]],
                review_notes=[str(note) for note in area.get("review_notes", [])],
            )
            for area in areas
        ],
    )


def match_changed_files(changed_files: list[str], areas: list[WatchArea]) -> list[AreaMatch]:
    matches: list[AreaMatch] = []
    for area in areas:
        area_files = sorted(
            {
                changed_file
                for changed_file in changed_files
                if any(path_matches(changed_file, pattern) for pattern in area.upstream_paths)
            }
        )
        if area_files:
            matches.append(AreaMatch(area=area, changed_files=area_files))
    return matches


def path_matches(path: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(path, pattern)


def issue_marker(config: WatchConfig, target_repo: str, tag: str) -> str:
    return f"{config.issue.marker_prefix}:{target_repo}:{tag}"


def find_review_candidate(
    config: WatchConfig, client: UpstreamClient, target_repo: str
) -> ReviewCandidate | None:
    releases = client.list_releases(
        config.upstream.owner, config.upstream.repo, config.upstream.release_limit
    )
    for index in range(0, len(releases) - 1):
        release = releases[index]
        marker = issue_marker(config, target_repo, release.tag)
        if client.issue_exists(marker):
            continue

        base_tag = releases[index + 1].tag
        changed_files = client.compare_files(
            config.upstream.owner, config.upstream.repo, base_tag, release.tag
        )
        matches = match_changed_files(changed_files, config.areas)
        if matches:
            compare_url = (
                f"https://github.com/{config.upstream.owner}/{config.upstream.repo}"
                f"/compare/{base_tag}...{release.tag}"
            )
            return ReviewCandidate(
                release=release,
                base_tag=base_tag,
                compare_url=compare_url,
                matches=matches,
            )
    return None


def render_evidence(config: WatchConfig, candidate: ReviewCandidate, target_repo: str) -> str:
    marker = issue_marker(config, target_repo, candidate.release.tag)
    lines = [
        f"<!-- {marker} -->",
        f"# Upstream LiveSync {candidate.release.tag} Compatibility Review",
        "",
        "A new upstream release changed watched compatibility files.",
        "",
        "## Upstream Release",
        "",
        f"- Repository: `{config.upstream.owner}/{config.upstream.repo}`",
        f"- Release: [{candidate.release.name}]({candidate.release.url})",
        f"- Compared range: `{candidate.base_tag}...{candidate.release.tag}`",
        f"- Compare URL: {candidate.compare_url}",
        "",
        "## Matched Watch Areas",
        "",
    ]
    for match in candidate.matches:
        lines.extend(
            [
                f"### {match.area.name}",
                "",
                f"- Risk: `{match.area.risk}`",
                "- Upstream files:",
                *[f"  - `{changed_file}`" for changed_file in match.changed_files],
                "- Local files to inspect:",
                *[f"  - `{local_path}`" for local_path in match.area.local_paths],
            ]
        )
        if match.area.review_notes:
            lines.extend(["- Review notes:", *[f"  - {note}" for note in match.area.review_notes]])
        lines.append("")

    release_notes = candidate.release.body.strip()
    if release_notes:
        lines.extend(["## Upstream Release Notes", "", _truncate(release_notes, 6000), ""])

    lines.extend(
        [
            "## Review Checklist",
            "",
            "- [ ] Read the upstream compare for the matched files.",
            "- [ ] Decide whether local compatibility logic, tests, or docs need updates.",
            "- [ ] Run the local test suite before closing this issue.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def write_noop(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps({"type": "noop", "message": message}) + "\n")


def _truncate(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars].rstrip()}\n\n...[truncated]"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(".github/upstream-release-watch.toml"))
    parser.add_argument(
        "--output", type=Path, default=Path(".github/upstream-release-watch-evidence.md")
    )
    parser.add_argument("--target-repo", default=os.environ.get("GITHUB_REPOSITORY"))
    args = parser.parse_args(argv)

    if not args.target_repo:
        raise SystemExit("--target-repo is required outside GitHub Actions")

    config = load_config(args.config)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    client = GitHubClient(token=token, target_repo=args.target_repo)
    candidate = find_review_candidate(config, client, args.target_repo)

    if candidate is None:
        noop_path = os.environ.get("GH_AW_SAFE_OUTPUTS")
        message = "No untracked upstream release with watched compatibility changes was found."
        if noop_path:
            write_noop(Path(noop_path), message)
        print(message)
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_evidence(config, candidate, args.target_repo))
    print(f"Wrote upstream release evidence for {candidate.release.tag} to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
