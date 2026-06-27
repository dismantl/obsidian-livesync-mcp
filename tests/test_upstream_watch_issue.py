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

    def issue_exists(self, marker):
        assert marker == "upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.77"
        return self.existing

    def create_issue(self, title, body):
        self.created.append({"title": title, "body": body})
        return "https://github.com/dismantl/obsidian-livesync-mcp/issues/1"


def test_parse_copilot_decision_accepts_json_code_fence():
    decision = parse_copilot_decision(
        """
```json
{
  "needs_local_review": true,
  "decision_reason": "The upstream files overlap with local chunking assumptions.",
  "summary": "The release changed the chunking implementation.",
  "compatibility_risk": "Local chunk parsing should be reviewed.",
  "review_focus": ["Compare chunk boundaries.", "Run chunking tests."]
}
```
""".strip()
    )

    assert decision == CopilotCompatibilityDecision(
        needs_local_review=True,
        decision_reason="The upstream files overlap with local chunking assumptions.",
        summary="The release changed the chunking implementation.",
        compatibility_risk="Local chunk parsing should be reviewed.",
        review_focus=["Compare chunk boundaries.", "Run chunking tests."],
    )


def test_parse_copilot_decision_accepts_no_review_decision():
    decision = parse_copilot_decision(
        """
{
  "needs_local_review": false,
  "decision_reason": "Only docs changed upstream; local compatibility code is unaffected.",
  "summary": "",
  "compatibility_risk": "",
  "review_focus": []
}
""".strip()
    )

    assert decision == CopilotCompatibilityDecision(
        needs_local_review=False,
        decision_reason="Only docs changed upstream; local compatibility code is unaffected.",
        summary="",
        compatibility_risk="",
        review_focus=[],
    )


def test_parse_copilot_decision_rejects_missing_fields_when_review_needed():
    with pytest.raises(ValueError, match="compatibility_risk"):
        parse_copilot_decision(
            '{"needs_local_review": true, '
            '"decision_reason": "Relevant upstream code changed.", '
            '"summary": "Only a summary.", '
            '"review_focus": ["Check tests."]}'
        )


def test_build_issue_payload_preserves_marker_and_scanner_evidence():
    decision = CopilotCompatibilityDecision(
        needs_local_review=True,
        decision_reason="The upstream files overlap with local chunking assumptions.",
        summary="Chunking code changed upstream.",
        compatibility_risk="The local Rabin-Karp splitter may need updates.",
        review_focus=["Inspect chunk boundaries.", "Run storage fixture tests."],
    )

    title, body = build_issue_payload(SAMPLE_EVIDENCE, decision)

    assert title == "[upstream-watch] LiveSync 0.25.77: review upstream compatibility changes"
    assert body.startswith("<!-- upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.77 -->")
    assert (
        "## Compatibility Decision\n\n"
        "The upstream files overlap with local chunking assumptions." in body
    )
    assert "## Automated Summary\n\nChunking code changed upstream." in body
    assert "## Compatibility Risk\n\nThe local Rabin-Karp splitter may need updates." in body
    assert "- Inspect chunk boundaries." in body
    assert "This is an automated triage issue" in body
    assert "## Scanner Evidence" in body
    assert "src/lib/src/string_and_binary/chunks.ts" in body
    assert "https://github.com/vrtmrz/obsidian-livesync/compare/0.25.76...0.25.77" in body


def test_build_issue_payload_redacts_embedded_markers_from_scanner_evidence():
    decision = CopilotCompatibilityDecision(
        needs_local_review=True,
        decision_reason="The upstream files overlap with local chunking assumptions.",
        summary="Chunking code changed upstream.",
        compatibility_risk="The local Rabin-Karp splitter may need updates.",
        review_focus=["Inspect chunk boundaries."],
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
        summary=("Chunking changed. upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.78"),
        compatibility_risk=(
            "<!-- upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.79 -->"
        ),
        review_focus=[
            "Review upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.80",
        ],
    )

    _, body = build_issue_payload(SAMPLE_EVIDENCE, decision)

    assert body.count("upstream-release-watch:dismantl/obsidian-livesync-mcp:") == 1
    assert body.startswith("<!-- upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.77 -->")
    assert body.count("<!-- redacted upstream release watch marker -->") == 4
    assert "Chunking changed." in body
    assert "- Review <!-- redacted upstream release watch marker -->" in body


def test_publish_issue_skips_missing_evidence(tmp_path):
    decision_path = tmp_path / "decision.json"
    decision_path.write_text(
        '{"needs_local_review": false, '
        '"decision_reason": "No-op.", '
        '"summary": "", '
        '"compatibility_risk": "", '
        '"review_focus": []}'
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
        '"summary": "Chunking changed.", '
        '"compatibility_risk": "Review local chunking.", '
        '"review_focus": ["Run tests."]}'
    )
    client = FakeIssueClient(existing=True)

    result = publish_issue(evidence_path, decision_path, client)

    assert result == "skipped_existing"
    assert client.created == []


def test_publish_issue_skips_when_copilot_finds_no_local_review_needed(tmp_path):
    evidence_path = tmp_path / "evidence.md"
    evidence_path.write_text(SAMPLE_EVIDENCE)
    decision_path = tmp_path / "decision.json"
    decision_path.write_text(
        '{"needs_local_review": false, '
        '"decision_reason": "The upstream change only touched docs.", '
        '"summary": "", '
        '"compatibility_risk": "", '
        '"review_focus": []}'
    )
    client = FakeIssueClient()

    result = publish_issue(evidence_path, decision_path, client)

    assert result == "skipped_not_needed"
    assert client.created == []


def test_publish_issue_creates_validated_issue(tmp_path):
    evidence_path = tmp_path / "evidence.md"
    evidence_path.write_text(SAMPLE_EVIDENCE)
    decision_path = tmp_path / "decision.json"
    decision_path.write_text(
        '{"needs_local_review": true, '
        '"decision_reason": "Relevant upstream code changed.", '
        '"summary": "Chunking changed.", '
        '"compatibility_risk": "Review local chunking.", '
        '"review_focus": ["Run tests."]}'
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
    assert "## Compatibility Decision\n\nRelevant upstream code changed." in created["body"]
    assert "## Automated Summary\n\nChunking changed." in created["body"]
    assert "## Compatibility Risk\n\nReview local chunking." in created["body"]
    assert "- Run tests." in created["body"]
