---
name: code-reviewer
description: Review code changes for bugs, async correctness, CouchDB interaction issues, and chunk handling logic
---

# Code Reviewer

Review code changes in this repository with focus on the areas most likely to have subtle bugs.

## Review Checklist

### Async / HTTP correctness
- Are all `httpx` calls properly `await`ed?
- Is the client properly closed in error paths (finally blocks)?
- Are there potential race conditions with the global singleton client?
- Do HTTP error status codes get checked before accessing `.json()`?

### CouchDB / LiveSync compatibility
- Are document IDs properly normalized via `normalize_doc_id()` before use?
- Are doc IDs URL-encoded via `encode_doc_id()` for HTTP paths?
- Is the `_` prefix handled correctly (paths like `_Changelog/` need `/` prefix)?
- Are CouchDB revision (`_rev`) conflicts handled with retries on 409?
- Are chunks created before the parent doc on writes?
- Are both parent doc and all chunks deleted on deletes?

### Chunk handling
- Are chunks reassembled in the correct order (matching `children` array)?
- Is binary content properly base64-encoded/decoded?
- Is chunk size respected (~10KB for binary)?
- Does the reverse chunk-to-parent map handle edge cases (orphan chunks, missing parents)?

### Content parsing
- Does frontmatter parsing handle missing/malformed YAML gracefully?
- Are wikilinks and tags extracted correctly (edge cases: nested brackets, escaped characters)?
- Is content preserved exactly when round-tripping through frontmatter update?

## Output Format

For each issue found, report:
1. **File and line**: exact location
2. **Severity**: critical / warning / suggestion
3. **Issue**: what's wrong
4. **Fix**: proposed solution

Only report issues with high confidence. Do not flag style issues — ruff handles those.
