import subprocess
from pathlib import Path

from obsidian_livesync_mcp.upstream_watch import (
    GitHubClient,
    GitHubRelease,
    find_review_candidate,
    issue_marker,
    load_config,
    match_changed_files,
    render_evidence,
    write_noop,
)


class FakeGitHub:
    def __init__(self, *, releases, comparisons, existing_markers=None):
        self._releases = releases
        self._comparisons = comparisons
        self._existing_markers = set(existing_markers or [])

    def list_releases(self, owner, repo, limit):
        assert (owner, repo) == ("vrtmrz", "obsidian-livesync")
        return self._releases[:limit]

    def compare_files(self, owner, repo, base, head):
        assert (owner, repo) == ("vrtmrz", "obsidian-livesync")
        return self._comparisons[(base, head)]

    def issue_exists(self, marker):
        return marker in self._existing_markers


def test_load_config_reads_watch_areas(tmp_path):
    config_path = tmp_path / "watch.toml"
    config_path.write_text(
        """
[upstream]
owner = "vrtmrz"
repo = "obsidian-livesync"
release_limit = 12

[issue]
marker_prefix = "upstream-release-watch"

[[areas]]
name = "storage-format"
risk = "high"
upstream_paths = ["src/**/models/**"]
local_paths = ["src/obsidian_livesync_mcp/client.py"]
review_notes = ["Check parent document shape."]
""".strip()
    )

    config = load_config(config_path)

    assert config.upstream.owner == "vrtmrz"
    assert config.upstream.release_limit == 12
    assert config.issue.marker_prefix == "upstream-release-watch"
    assert config.areas[0].name == "storage-format"
    assert config.areas[0].local_paths == ["src/obsidian_livesync_mcp/client.py"]


def test_match_changed_files_groups_by_watch_area():
    config_path = Path(".github/upstream-release-watch.toml")
    config = load_config(config_path)

    matches = match_changed_files(
        [
            "_types/src/lib/src/common/models/shared.const.behabiour.d.ts",
            "src/lib/src/string_and_binary/chunks.ts",
            "docs/unrelated.md",
        ],
        config.areas,
    )

    assert {"storage-format", "chunking-and-hashing"} <= {match.area.name for match in matches}
    chunking = next(match for match in matches if match.area.name == "chunking-and-hashing")
    assert "src/lib/src/string_and_binary/chunks.ts" in chunking.changed_files
    assert "src/obsidian_livesync_mcp/chunking.py" in chunking.area.local_paths


def test_find_review_candidate_returns_first_untracked_relevant_release():
    config = load_config(Path(".github/upstream-release-watch.toml"))
    releases = [
        GitHubRelease(tag="0.25.77", name="0.25.77", url="https://example.test/0.25.77", body=""),
        GitHubRelease(tag="0.25.76", name="0.25.76", url="https://example.test/0.25.76", body=""),
        GitHubRelease(tag="0.25.75", name="0.25.75", url="https://example.test/0.25.75", body=""),
    ]
    client = FakeGitHub(
        releases=releases,
        comparisons={
            ("0.25.76", "0.25.77"): ["docs/unrelated.md"],
            ("0.25.75", "0.25.76"): ["_types/src/lib/src/common/models/db.const.d.ts"],
        },
    )

    candidate = find_review_candidate(config, client, "dismantl/obsidian-livesync-mcp")

    assert candidate is not None
    assert candidate.release.tag == "0.25.76"
    assert candidate.base_tag == "0.25.75"
    assert candidate.matches[0].area.name == "storage-format"


def test_find_review_candidate_skips_existing_issue_marker():
    config = load_config(Path(".github/upstream-release-watch.toml"))
    releases = [
        GitHubRelease(tag="0.25.77", name="0.25.77", url="https://example.test/0.25.77", body=""),
        GitHubRelease(tag="0.25.76", name="0.25.76", url="https://example.test/0.25.76", body=""),
    ]
    marker = issue_marker(config, "dismantl/obsidian-livesync-mcp", "0.25.77")
    client = FakeGitHub(
        releases=releases,
        comparisons={("0.25.76", "0.25.77"): ["src/lib/src/string_and_binary/chunks.ts"]},
        existing_markers={marker},
    )

    candidate = find_review_candidate(config, client, "dismantl/obsidian-livesync-mcp")

    assert candidate is None


def test_github_client_compare_files_uses_git_diff(monkeypatch):
    commands = []

    def fake_run(args, cwd, check, capture_output, text, timeout):
        assert cwd
        assert check is True
        assert capture_output is True
        assert text is True
        assert timeout == 120
        commands.append(args)
        if args[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(args, 0, stdout="b.py\na.py\n")
        if args[:2] == ["git", "rev-parse"] and args[2].endswith("base^{commit}"):
            return subprocess.CompletedProcess(args, 0, stdout="base-sha\n")
        if args[:2] == ["git", "rev-parse"] and args[2].endswith("head^{commit}"):
            return subprocess.CompletedProcess(args, 0, stdout="head-sha\n")
        return subprocess.CompletedProcess(args, 0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = GitHubClient(token=None, target_repo="dismantl/obsidian-livesync-mcp")

    changed_files = client.compare_files("vrtmrz", "obsidian-livesync", "base", "head")

    assert changed_files == ["a.py", "b.py"]
    assert [
        "git",
        "fetch",
        "--no-tags",
        "--depth=1",
        "origin",
        "refs/tags/base:refs/tags/base",
        "refs/tags/head:refs/tags/head",
    ] in commands
    assert ["git", "diff", "--name-only", "base-sha", "head-sha"] in commands


def test_render_evidence_includes_marker_and_compare_url():
    config = load_config(Path(".github/upstream-release-watch.toml"))
    release = GitHubRelease(
        tag="0.25.77",
        name="0.25.77",
        url="https://github.com/vrtmrz/obsidian-livesync/releases/tag/0.25.77",
        body="Release notes.",
    )
    candidate = find_review_candidate(
        config,
        FakeGitHub(
            releases=[release, GitHubRelease("0.25.76", "0.25.76", "https://example.test", "")],
            comparisons={("0.25.76", "0.25.77"): ["src/lib/src/string_and_binary/chunks.ts"]},
        ),
        "dismantl/obsidian-livesync-mcp",
    )
    assert candidate is not None

    evidence = render_evidence(config, candidate, "dismantl/obsidian-livesync-mcp")

    assert "<!-- upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.77 -->" in evidence
    assert "https://github.com/vrtmrz/obsidian-livesync/compare/0.25.76...0.25.77" in evidence
    assert "chunking-and-hashing" in evidence
    assert "src/obsidian_livesync_mcp/chunking.py" in evidence


def test_write_noop_appends_safe_output_json(tmp_path):
    output_path = tmp_path / "safe-output.jsonl"

    write_noop(output_path, "No watched upstream files changed.")

    assert output_path.read_text() == (
        '{"type": "noop", "message": "No watched upstream files changed."}\n'
    )
