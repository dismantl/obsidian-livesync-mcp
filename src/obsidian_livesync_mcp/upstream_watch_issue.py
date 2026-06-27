"""Publish validated upstream release watch issues."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from obsidian_livesync_mcp.upstream_watch import DEFAULT_API_URL

ISSUE_TITLE_PREFIX = "[upstream-watch] "
TRACKER_ISSUE_TITLE = f"{ISSUE_TITLE_PREFIX}processed upstream release state"
TRACKER_ISSUE_HEADER = "\n".join(
    [
        "# Upstream Release Watch State",
        "",
        (
            "This closed issue records upstream releases that Copilot evaluated "
            "and decided do not require local compatibility review."
        ),
        "",
        (
            "The hidden markers in this body are used by the scheduled watcher "
            "to avoid reprocessing the same release."
        ),
    ]
)
MARKER_RE = re.compile(r"<!--\s*(upstream-release-watch:[^>]+?)\s*-->")
MARKER_TOKEN_RE = re.compile(
    r"upstream-release-watch:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+:[^\s<>)\]}\"']+"
)
EMBEDDED_MARKER_REDACTION = "<!-- redacted upstream release watch marker -->"


@dataclass(frozen=True)
class CopilotCompatibilityDecision:
    needs_local_review: bool
    decision_reason: str
    summary: str
    compatibility_risk: str
    review_focus: list[str]


class IssueClient(Protocol):
    def issue_exists(self, marker: str) -> bool: ...

    def create_issue(self, title: str, body: str) -> str: ...

    def record_no_review_decision(self, body: str) -> str: ...


class GitHubIssueClient:
    def __init__(self, *, token: str, target_repo: str, api_url: str = DEFAULT_API_URL):
        self._token = token
        self._target_repo = target_repo
        self._api_url = api_url.rstrip("/")

    def issue_exists(self, marker: str) -> bool:
        query = f'repo:{self._target_repo} is:issue "{marker}" in:body'
        result = self._request("GET", "/search/issues", {"q": query, "per_page": "1"})
        return int(result.get("total_count", 0)) > 0

    def create_issue(self, title: str, body: str) -> str:
        result = self._create_issue(title, body)
        return str(result.get("html_url") or "")

    def record_no_review_decision(self, body: str) -> str:
        tracker = self._find_issue_by_title(TRACKER_ISSUE_TITLE)
        if tracker is None:
            result = self._create_issue(TRACKER_ISSUE_TITLE, f"{TRACKER_ISSUE_HEADER}\n\n{body}")
            number = result.get("number")
            if not isinstance(number, int):
                raise RuntimeError("GitHub issue creation did not return an issue number")
            self._close_issue(number)
            return str(result.get("html_url") or "")

        current_body = str(tracker.get("body") or "")
        if body.strip() in current_body:
            return str(tracker.get("html_url") or "")

        number = tracker.get("number")
        if not isinstance(number, int):
            raise RuntimeError("GitHub tracker issue lookup did not return an issue number")
        updated_body = f"{current_body.rstrip()}\n\n{body}"
        result = self._request(
            "PATCH",
            f"/repos/{self._target_repo}/issues/{number}",
            {},
            {"body": updated_body},
        )
        return str(result.get("html_url") or "")

    def _create_issue(self, title: str, body: str) -> dict[str, object]:
        result = self._request(
            "POST",
            f"/repos/{self._target_repo}/issues",
            {},
            {"title": title, "body": body},
        )
        return result

    def _close_issue(self, number: int) -> None:
        self._request(
            "PATCH",
            f"/repos/{self._target_repo}/issues/{number}",
            {},
            {"state": "closed", "state_reason": "not_planned"},
        )

    def _find_issue_by_title(self, title: str) -> dict[str, object] | None:
        query = f'repo:{self._target_repo} is:issue in:title "{title}"'
        result = self._request("GET", "/search/issues", {"q": query, "per_page": "10"})
        items = result.get("items")
        if not isinstance(items, list):
            return None
        for item in items:
            if not isinstance(item, dict) or item.get("title") != title:
                continue
            number = item.get("number")
            if not isinstance(number, int):
                continue
            return self._request("GET", f"/repos/{self._target_repo}/issues/{number}", {})
        return None

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str],
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        query = urllib.parse.urlencode(params)
        url = f"{self._api_url}{path}"
        if query:
            url = f"{url}?{query}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "obsidian-livesync-mcp-upstream-watch",
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": f"Bearer {self._token}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            message = f"GitHub API request failed for {path}: {exc.code} {detail}"
            raise RuntimeError(message) from exc


def parse_copilot_decision(raw: str) -> CopilotCompatibilityDecision:
    payload = _parse_json_object(raw)
    needs_local_review = _required_bool(payload, "needs_local_review")
    decision_reason = _required_string(payload, "decision_reason")

    summary = _optional_string(payload, "summary")
    compatibility_risk = _optional_string(payload, "compatibility_risk")
    raw_review_focus = payload.get("review_focus")
    if raw_review_focus is None:
        raw_review_focus = []
    if not isinstance(raw_review_focus, list):
        raise ValueError("Copilot decision field 'review_focus' must be a list")
    focus_items = []
    for item in raw_review_focus:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("Copilot decision field 'review_focus' must contain non-empty strings")
        focus_items.append(item.strip())

    if needs_local_review:
        if not summary:
            raise ValueError(
                "Copilot decision field 'summary' is required when local review is needed"
            )
        if not compatibility_risk:
            raise ValueError(
                "Copilot decision field 'compatibility_risk' is required "
                "when local review is needed"
            )
        if not focus_items:
            raise ValueError(
                "Copilot decision field 'review_focus' must be non-empty "
                "when local review is needed"
            )

    return CopilotCompatibilityDecision(
        needs_local_review=needs_local_review,
        decision_reason=decision_reason,
        summary=summary,
        compatibility_risk=compatibility_risk,
        review_focus=focus_items,
    )


def build_issue_payload(evidence: str, decision: CopilotCompatibilityDecision) -> tuple[str, str]:
    marker, tag, safe_evidence = _issue_context(evidence)
    title = f"{ISSUE_TITLE_PREFIX}LiveSync {tag}: review upstream compatibility changes"
    safe_decision_reason = _redact_embedded_markers(decision.decision_reason)
    safe_summary = _redact_embedded_markers(decision.summary)
    safe_compatibility_risk = _redact_embedded_markers(decision.compatibility_risk)
    safe_review_focus = [_redact_embedded_markers(item) for item in decision.review_focus]
    focus = "\n".join(f"- {item}" for item in safe_review_focus)
    body = "\n".join(
        [
            f"<!-- {marker} -->",
            "",
            "## Compatibility Decision",
            "",
            safe_decision_reason,
            "",
            "## Automated Summary",
            "",
            safe_summary,
            "",
            "## Compatibility Risk",
            "",
            safe_compatibility_risk,
            "",
            "## Review Focus",
            "",
            focus,
            "",
            (
                "This is an automated triage issue. "
                "A human should decide whether code changes are needed."
            ),
            "",
            "## Scanner Evidence",
            "",
            safe_evidence.rstrip(),
            "",
        ]
    )
    return title, body


def build_no_review_tracker_entry(evidence: str, decision: CopilotCompatibilityDecision) -> str:
    marker = extract_marker(evidence)
    tag = marker.rsplit(":", 1)[-1]
    safe_decision_reason = _redact_embedded_markers(decision.decision_reason)
    return "\n".join(
        [
            f"<!-- {marker} -->",
            "",
            f"## LiveSync {tag}: no local compatibility review needed",
            "",
            "### Compatibility Decision",
            "",
            safe_decision_reason,
            "",
            (
                "Copilot determined that this upstream release does not require "
                "local compatibility review."
            ),
            "",
        ]
    )


def _issue_context(evidence: str) -> tuple[str, str, str]:
    marker = extract_marker(evidence)
    tag = marker.rsplit(":", 1)[-1]
    evidence_without_marker = MARKER_RE.sub("", evidence, count=1).lstrip()
    safe_evidence = _redact_embedded_markers(evidence_without_marker)
    return marker, tag, safe_evidence


def _redact_embedded_markers(value: str) -> str:
    without_comment_markers = MARKER_RE.sub(EMBEDDED_MARKER_REDACTION, value)
    return MARKER_TOKEN_RE.sub(EMBEDDED_MARKER_REDACTION, without_comment_markers)


def publish_issue(evidence_path: Path, decision_path: Path, client: IssueClient) -> str:
    if not evidence_path.exists() or not evidence_path.read_text().strip():
        return "noop"

    evidence = evidence_path.read_text()
    marker = extract_marker(evidence)
    if client.issue_exists(marker):
        return "skipped_existing"

    decision = parse_copilot_decision(decision_path.read_text())
    if not decision.needs_local_review:
        body = build_no_review_tracker_entry(evidence, decision)
        client.record_no_review_decision(body)
        return "recorded_no_review_marker"

    title, body = build_issue_payload(evidence, decision)
    client.create_issue(title, body)
    return "created"


def extract_marker(evidence: str) -> str:
    match = MARKER_RE.search(evidence)
    if not match:
        raise ValueError("Evidence does not contain an upstream release watch marker")
    return match.group(1)


def _parse_json_object(raw: str) -> dict[str, object]:
    stripped = raw.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    else:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end != -1 and start < end:
            stripped = stripped[start : end + 1]
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Copilot decision is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Copilot decision must be a JSON object")
    return payload


def _required_string(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Copilot decision field '{field}' must be a non-empty string")
    return value.strip()


def _optional_string(payload: dict[str, object], field: str) -> str:
    value = payload.get(field, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"Copilot decision field '{field}' must be a string")
    return value.strip()


def _required_bool(payload: dict[str, object], field: str) -> bool:
    value = payload.get(field)
    if not isinstance(value, bool):
        raise ValueError(f"Copilot decision field '{field}' must be a boolean")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--decision", type=Path, required=True)
    parser.add_argument("--target-repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", DEFAULT_API_URL))
    args = parser.parse_args(argv)

    if not args.target_repo:
        raise SystemExit("--target-repo is required outside GitHub Actions")

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GH_TOKEN or GITHUB_TOKEN is required to create GitHub issues")

    client = GitHubIssueClient(token=token, target_repo=args.target_repo, api_url=args.api_url)
    result = publish_issue(args.evidence, args.decision, client)
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
