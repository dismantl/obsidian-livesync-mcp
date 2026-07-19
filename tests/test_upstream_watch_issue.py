import json

import pytest

from obsidian_livesync_mcp.upstream_watch_issue import (
    CopilotCompatibilityDecision,
    build_issue_payload,
    parse_copilot_decision,
    publish_issue,
)

SAMPLE_EVIDENCE = """<!-- upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.77 -->
# Upstream LiveSync 0.25.77 Compatibility Review

## Upstream Release

- Repository: `vrtmrz/obsidian-livesync`
- Release: [0.25.77](https://github.com/vrtmrz/obsidian-livesync/releases/tag/0.25.77)
- Compared range: `0.25.76...0.25.77`
- Compare URL: https://github.com/vrtmrz/obsidian-livesync/compare/0.25.76...0.25.77

## Matched Watch Areas

### chunking-and-hashing

- Risk: `high`
- Upstream files:
  - `src/lib/src/string_and_binary/chunks.ts`
- Local files to inspect:
  - `src/obsidian_livesync_mcp/chunking.py`
"""

SAMPLE_ISSUE_BODY = """## Upstream Release

Chunking code changed upstream.

## Upstream Release Notes

The release notes mention chunking changes.

## Watched Areas That Changed

### High Risk: chunking-and-hashing

Inspect chunk boundaries.

## Compatibility Assessment

The local Rabin-Karp splitter may need updates.

## Next Steps

1. Run storage fixture tests."""


class FakeIssueClient:
    def __init__(self, *, existing=False):
        self.existing = existing
        self.created = []
        self.tracker_entries = []

    def issue_exists(self, marker):
        assert marker == "upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.77"
        return self.existing

    def create_issue(self, title, body):
        self.created.append({"title": title, "body": body})
        return "https://github.com/dismantl/obsidian-livesync-mcp/issues/1"

    def record_no_review_decision(self, body):
        self.tracker_entries.append(body)
        return "https://github.com/dismantl/obsidian-livesync-mcp/issues/1"


def test_parse_copilot_decision_accepts_json_code_fence():
    decision = parse_copilot_decision(
        "```json\n"
        + json.dumps(
            {
                "needs_local_review": True,
                "decision_reason": ("The upstream files overlap with local chunking assumptions."),
                "issue_body_markdown": SAMPLE_ISSUE_BODY,
            }
        )
        + "\n```"
    )

    assert decision == CopilotCompatibilityDecision(
        needs_local_review=True,
        decision_reason="The upstream files overlap with local chunking assumptions.",
        issue_body_markdown=SAMPLE_ISSUE_BODY,
    )


def test_parse_copilot_decision_accepts_no_review_decision():
    decision = parse_copilot_decision(
        """
{
  "needs_local_review": false,
  "decision_reason": "Only docs changed upstream; local compatibility code is unaffected.",
  "issue_body_markdown": ""
}
""".strip()
    )

    assert decision == CopilotCompatibilityDecision(
        needs_local_review=False,
        decision_reason="Only docs changed upstream; local compatibility code is unaffected.",
        issue_body_markdown="",
    )


def test_parse_copilot_decision_rejects_missing_issue_body_when_review_needed():
    with pytest.raises(ValueError, match="issue_body_markdown"):
        parse_copilot_decision(
            '{"needs_local_review": true, '
            '"decision_reason": "Relevant upstream code changed.", '
            '"issue_body_markdown": ""}'
        )


def test_parse_copilot_decision_defers_missing_issue_body_sections_to_publisher():
    decision = parse_copilot_decision(
        '{"needs_local_review": true, '
        '"decision_reason": "Relevant upstream code changed.", '
        '"issue_body_markdown": "## Upstream Release\\n\\n0.25.78\\n\\n'
        "## Watched Areas That Changed\\n\\nChunking.\\n\\n"
        "## Compatibility Assessment\\n\\nReview local chunking.\\n\\n"
        '## Next Steps\\n\\n- Run tests."}'
    )

    assert decision.needs_local_review is True
    assert "## Upstream Release Notes" not in decision.issue_body_markdown


def test_build_issue_payload_uses_copilot_issue_body_and_collapses_scanner_evidence():
    decision = CopilotCompatibilityDecision(
        needs_local_review=True,
        decision_reason="The upstream files overlap with local chunking assumptions.",
        issue_body_markdown=SAMPLE_ISSUE_BODY,
    )

    title, body = build_issue_payload(SAMPLE_EVIDENCE, decision)

    assert title == "[upstream-watch] LiveSync 0.25.77: review upstream compatibility changes"
    assert body.startswith("<!-- upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.77 -->")
    assert "## Upstream Release\n\nChunking code changed upstream." in body
    assert "## Watched Areas That Changed" in body
    assert "## Compatibility Assessment\n\nThe local Rabin-Karp splitter may need updates." in body
    assert "## Next Steps\n\n1. Run storage fixture tests." in body
    assert "This is an automated triage issue" in body
    assert "<details>" in body
    assert "<summary>Scanner evidence</summary>" in body
    assert "src/lib/src/string_and_binary/chunks.ts" in body
    assert "https://github.com/vrtmrz/obsidian-livesync/compare/0.25.76...0.25.77" in body


def test_build_issue_payload_fills_missing_release_notes_from_scanner_evidence():
    evidence = (
        SAMPLE_EVIDENCE
        + """

## Upstream Release Notes

## 0.25.82

The release fixes chunk delivery after replication completes.

## Review Checklist

- [ ] Review compatibility.
"""
    )
    decision = CopilotCompatibilityDecision(
        needs_local_review=True,
        decision_reason="Relevant upstream code changed.",
        issue_body_markdown=SAMPLE_ISSUE_BODY.replace(
            "## Upstream Release Notes\n\nThe release notes mention chunking changes.\n\n", ""
        ),
    )

    _, body = build_issue_payload(evidence, decision)

    release_notes = (
        "## Upstream Release Notes\n\n"
        "## 0.25.82\n\nThe release fixes chunk delivery after replication completes."
    )
    assert release_notes in body
    assert body.index("## Upstream Release") < body.index(release_notes)
    assert body.index(release_notes) < body.index("## Watched Areas That Changed")
    assert body.count("## Upstream Release Notes") == 2


def test_build_issue_payload_fills_all_missing_sections_with_safe_fallbacks():
    decision = CopilotCompatibilityDecision(
        needs_local_review=True,
        decision_reason="Relevant upstream code changed.",
        issue_body_markdown="Copilot returned useful prose without the required headings.",
    )

    _, body = build_issue_payload(SAMPLE_EVIDENCE, decision)
    visible_body = body.split("<details>", 1)[0]

    for heading in (
        "Upstream Release",
        "Upstream Release Notes",
        "Watched Areas That Changed",
        "Compatibility Assessment",
        "Next Steps",
    ):
        assert visible_body.count(f"## {heading}\n") == 1
    assert "No upstream release notes were provided." in visible_body
    assert "Relevant upstream code changed." in visible_body
    assert "Run the relevant local tests" in visible_body


def test_build_issue_payload_redacts_embedded_markers_from_scanner_evidence():
    decision = CopilotCompatibilityDecision(
        needs_local_review=True,
        decision_reason="The upstream files overlap with local chunking assumptions.",
        issue_body_markdown=SAMPLE_ISSUE_BODY,
    )
    evidence = (
        SAMPLE_EVIDENCE
        + "\n## Upstream Release Notes\n\n"
        + "<!-- upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.78 -->\n"
    )

    _, body = build_issue_payload(evidence, decision)

    assert body.count("upstream-release-watch:dismantl/obsidian-livesync-mcp:") == 1
    assert body.startswith("<!-- upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.77 -->")
    assert "<!-- redacted upstream release watch marker -->" in body


def test_build_issue_payload_redacts_markers_from_copilot_decision():
    decision = CopilotCompatibilityDecision(
        needs_local_review=True,
        decision_reason="Check upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.81",
        issue_body_markdown=(
            "## Upstream Release\n\n"
            "Chunking changed. upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.78\n\n"
            "## Upstream Release Notes\n\n"
            "Release notes.\n\n"
            "## Watched Areas That Changed\n\n"
            "Chunking changed.\n\n"
            "## Compatibility Assessment\n\n"
            "<!-- upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.79 -->\n\n"
            "## Next Steps\n\n"
            "- Review upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.80"
        ),
    )

    _, body = build_issue_payload(SAMPLE_EVIDENCE, decision)

    assert body.count("upstream-release-watch:dismantl/obsidian-livesync-mcp:") == 1
    assert body.startswith("<!-- upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.77 -->")
    assert body.count("<!-- redacted upstream release watch marker -->") == 3
    assert "Chunking changed." in body
    assert "- Review <!-- redacted upstream release watch marker -->" in body


def test_publish_issue_skips_missing_evidence(tmp_path):
    decision_path = tmp_path / "decision.json"
    decision_path.write_text(
        '{"needs_local_review": false, "decision_reason": "No-op.", "issue_body_markdown": ""}'
    )
    client = FakeIssueClient()

    result = publish_issue(tmp_path / "missing.md", decision_path, client)

    assert result == "noop"
    assert client.created == []


def test_publish_issue_deduplicates_existing_marker(tmp_path):
    evidence_path = tmp_path / "evidence.md"
    evidence_path.write_text(SAMPLE_EVIDENCE)
    decision_path = tmp_path / "decision.json"
    decision_path.write_text(
        json.dumps(
            {
                "needs_local_review": True,
                "decision_reason": "Relevant upstream code changed.",
                "issue_body_markdown": SAMPLE_ISSUE_BODY,
            }
        )
    )
    client = FakeIssueClient(existing=True)

    result = publish_issue(evidence_path, decision_path, client)

    assert result == "skipped_existing"
    assert client.created == []


def test_publish_issue_records_tracker_marker_when_copilot_finds_no_local_review_needed(
    tmp_path,
):
    evidence_path = tmp_path / "evidence.md"
    evidence_path.write_text(SAMPLE_EVIDENCE)
    decision_path = tmp_path / "decision.json"
    decision_path.write_text(
        '{"needs_local_review": false, '
        '"decision_reason": "The upstream change only touched docs.", '
        '"issue_body_markdown": ""}'
    )
    client = FakeIssueClient()

    result = publish_issue(evidence_path, decision_path, client)

    assert result == "recorded_no_review_marker"
    assert client.created == []
    assert len(client.tracker_entries) == 1
    entry = client.tracker_entries[0]
    assert entry.startswith(
        "<!-- upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.77 -->"
    )
    assert "## LiveSync 0.25.77: no local compatibility review needed" in entry
    assert "### Compatibility Decision\n\nThe upstream change only touched docs." in entry
    assert "does not require local compatibility review" in entry
    assert "### Scanner Evidence" not in entry
    assert "## Matched Watch Areas" not in entry
    assert "src/lib/src/string_and_binary/chunks.ts" not in entry
    assert "https://github.com/vrtmrz/obsidian-livesync/compare/0.25.76...0.25.77" not in entry


def test_publish_issue_creates_validated_issue(tmp_path):
    evidence_path = tmp_path / "evidence.md"
    evidence_path.write_text(SAMPLE_EVIDENCE)
    decision_path = tmp_path / "decision.json"
    decision_path.write_text(
        json.dumps(
            {
                "needs_local_review": True,
                "decision_reason": "Relevant upstream code changed.",
                "issue_body_markdown": SAMPLE_ISSUE_BODY,
            }
        )
    )
    client = FakeIssueClient()

    result = publish_issue(evidence_path, decision_path, client)

    assert result == "created"
    assert len(client.created) == 1
    created = client.created[0]
    assert (
        created["title"]
        == "[upstream-watch] LiveSync 0.25.77: review upstream compatibility changes"
    )
    assert created["body"].startswith(
        "<!-- upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.77 -->"
    )
    assert "## Upstream Release\n\nChunking code changed upstream." in created["body"]
    assert (
        "## Compatibility Assessment\n\nThe local Rabin-Karp splitter may need updates."
        in created["body"]
    )
    assert "1. Run storage fixture tests." in created["body"]
