# Refreshing chunk parity fixtures

These fixtures pin the chunker to LiveSync's `v3-rabin-karp` output. Regenerate
them only when intentionally tracking a new upstream LiveSync version.

1. Clone the LiveSync/commonlib version you target and inspect
   `livesync-commonlib/src/string_and_binary/chunks.ts`.
2. Port any sizing-constant changes into `chunking.py`. Rolling hash changes
   should be rare. Record the commonlib commit and plugin version in the
   module docstring.
3. Re-capture each JSON oracle from the authority, never from `split_chunks`:
   write the fixture's exact bytes via the Obsidian app, sync, and read the
   parent doc's `children` from CouchDB. Fallback: run upstream
   `splitPiecesRabinKarp` via Node on the exact fixture bytes. Generating the
   oracle from `split_chunks` turns these into determinism tests and lets real
   parity drift pass green.
4. Commit the changed JSON alongside `chunking.py`, bumping the `source` and
   `captured` fields.

Do not fetch upstream or run the app during CI. Committed fixtures must be
reproducible offline; the app or upstream-JS step happens only when refreshing
the fixtures.
