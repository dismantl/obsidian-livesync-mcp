import pytest

from obsidian_livesync_mcp.upstream_watch_issue import (
    CopilotIssueDraft,
    build_issue_payload,
    parse_copilot_draft,
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


def test_parse_copilot_draft_accepts_json_code_fence():
    draft = parse_copilot_draft(
        """
```json
{
  "summary": "The release changed the chunking implementation.",
  "compatibility_risk": "Local chunk parsing should be reviewed.",
  "review_focus": ["Compare chunk boundaries.", "Run chunking tests."]
}
```
""".strip()
    )

    assert draft == CopilotIssueDraft(
        summary="The release changed the chunking implementation.",
        compatibility_risk="Local chunk parsing should be reviewed.",
        review_focus=["Compare chunk boundaries.", "Run chunking tests."],
    )


def test_parse_copilot_draft_rejects_missing_fields():
    with pytest.raises(ValueError, match="compatibility_risk"):
        parse_copilot_draft('{"summary": "Only a summary.", "review_focus": ["Check tests."]}')


def test_build_issue_payload_preserves_marker_and_scanner_evidence():
    draft = CopilotIssueDraft(
        summary="Chunking code changed upstream.",
        compatibility_risk="The local Rabin-Karp splitter may need updates.",
        review_focus=["Inspect chunk boundaries.", "Run storage fixture tests."],
    )

    title, body = build_issue_payload(SAMPLE_EVIDENCE, draft)

    assert title == "[upstream-watch] LiveSync 0.25.77: review upstream compatibility changes"
    assert body.startswith("<!-- upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.77 -->")
    assert "## Automated Summary\n\nChunking code changed upstream." in body
    assert "## Compatibility Risk\n\nThe local Rabin-Karp splitter may need updates." in body
    assert "- Inspect chunk boundaries." in body
    assert "This is an automated triage issue" in body
    assert "## Scanner Evidence" in body
    assert "src/lib/src/string_and_binary/chunks.ts" in body
    assert "https://github.com/vrtmrz/obsidian-livesync/compare/0.25.76...0.25.77" in body


def test_build_issue_payload_redacts_embedded_markers_from_scanner_evidence():
    draft = CopilotIssueDraft(
        summary="Chunking code changed upstream.",
        compatibility_risk="The local Rabin-Karp splitter may need updates.",
        review_focus=["Inspect chunk boundaries."],
    )
    evidence = (
        SAMPLE_EVIDENCE
        + "\n## Upstream Release Notes\n\n"
        + "<!-- upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.78 -->\n"
    )

    _, body = build_issue_payload(evidence, draft)

    assert body.count("upstream-release-watch:dismantl/obsidian-livesync-mcp:") == 1
    assert body.startswith("<!-- upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.77 -->")
    assert "<!-- redacted upstream release watch marker -->" in body


def test_build_issue_payload_redacts_markers_from_copilot_draft():
    draft = CopilotIssueDraft(
        summary=("Chunking changed. upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.78"),
        compatibility_risk=(
            "<!-- upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.79 -->"
        ),
        review_focus=[
            "Review upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.80",
        ],
    )

    _, body = build_issue_payload(SAMPLE_EVIDENCE, draft)

    assert body.count("upstream-release-watch:dismantl/obsidian-livesync-mcp:") == 1
    assert body.startswith("<!-- upstream-release-watch:dismantl/obsidian-livesync-mcp:0.25.77 -->")
    assert body.count("<!-- redacted upstream release watch marker -->") == 3
    assert "Chunking changed." in body
    assert "- Review <!-- redacted upstream release watch marker -->" in body


def test_publish_issue_skips_missing_evidence(tmp_path):
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(
        '{"summary": "No-op.", "compatibility_risk": "No-op.", "review_focus": ["No-op."]}'
    )
    client = FakeIssueClient()

    result = publish_issue(tmp_path / "missing.md", draft_path, client)

    assert result == "noop"
    assert client.created == []


def test_publish_issue_deduplicates_existing_marker(tmp_path):
    evidence_path = tmp_path / "evidence.md"
    evidence_path.write_text(SAMPLE_EVIDENCE)
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(
        '{"summary": "Chunking changed.", '
        '"compatibility_risk": "Review local chunking.", '
        '"review_focus": ["Run tests."]}'
    )
    client = FakeIssueClient(existing=True)

    result = publish_issue(evidence_path, draft_path, client)

    assert result == "skipped_existing"
    assert client.created == []


def test_publish_issue_creates_validated_issue(tmp_path):
    evidence_path = tmp_path / "evidence.md"
    evidence_path.write_text(SAMPLE_EVIDENCE)
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(
        '{"summary": "Chunking changed.", '
        '"compatibility_risk": "Review local chunking.", '
        '"review_focus": ["Run tests."]}'
    )
    client = FakeIssueClient()

    result = publish_issue(evidence_path, draft_path, client)

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
    assert "## Automated Summary\n\nChunking changed." in created["body"]
    assert "## Compatibility Risk\n\nReview local chunking." in created["body"]
    assert "- Run tests." in created["body"]
