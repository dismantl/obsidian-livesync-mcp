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
        "{\n"
        '  "needs_local_review": true,\n'
        '  "decision_reason": "The upstream files overlap with local chunking assumptions.",\n'
        '  "issue_body_markdown": "## Upstream Release\\n\\n'
        "Chunking changed upstream.\\n\\n"
        "## Next Steps\\n\\n"
        "- Compare chunk boundaries.\\n"
        '- Run chunking tests."\n'
        "}\n"
        "```"
    )

    assert decision == CopilotCompatibilityDecision(
        needs_local_review=True,
        decision_reason="The upstream files overlap with local chunking assumptions.",
        issue_body_markdown=(
            "## Upstream Release\n\n"
            "Chunking changed upstream.\n\n"
            "## Next Steps\n\n"
            "- Compare chunk boundaries.\n"
            "- Run chunking tests."
        ),
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


def test_build_issue_payload_uses_copilot_issue_body_and_collapses_scanner_evidence():
    decision = CopilotCompatibilityDecision(
        needs_local_review=True,
        decision_reason="The upstream files overlap with local chunking assumptions.",
        issue_body_markdown=(
            "## Upstream Release\n\n"
            "Chunking code changed upstream.\n\n"
            "## Watched Areas That Changed\n\n"
            "### High Risk: chunking-and-hashing\n\n"
            "Inspect chunk boundaries.\n\n"
            "## Compatibility Assessment\n\n"
            "The local Rabin-Karp splitter may need updates.\n\n"
            "## Next Steps\n\n"
            "1. Run storage fixture tests."
        ),
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


def test_build_issue_payload_redacts_embedded_markers_from_scanner_evidence():
    decision = CopilotCompatibilityDecision(
        needs_local_review=True,
        decision_reason="The upstream files overlap with local chunking assumptions.",
        issue_body_markdown="## Upstream Release\n\nChunking code changed upstream.",
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
        '{"needs_local_review": true, '
        '"decision_reason": "Relevant upstream code changed.", '
        '"issue_body_markdown": "## Upstream Release\\n\\nChunking changed.\\n\\n'
        '## Next Steps\\n\\n- Run tests."}'
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
        '{"needs_local_review": true, '
        '"decision_reason": "Relevant upstream code changed.", '
        '"issue_body_markdown": "## Upstream Release\\n\\nChunking changed.\\n\\n'
        "## Compatibility Assessment\\n\\nReview local chunking.\\n\\n"
        '## Next Steps\\n\\n- Run tests."}'
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
    assert "## Upstream Release\n\nChunking changed." in created["body"]
    assert "## Compatibility Assessment\n\nReview local chunking." in created["body"]
    assert "- Run tests." in created["body"]
