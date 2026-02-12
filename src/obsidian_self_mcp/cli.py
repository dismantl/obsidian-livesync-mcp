"""CLI for Obsidian vault operations via CouchDB."""

import argparse
import asyncio
import sys

from .client import ObsidianVaultClient
from .config import Config


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


async def _cmd_list(client: ObsidianVaultClient, args):
    notes = await client.list_notes(folder=args.folder, limit=args.n)
    if not notes:
        print("No notes found.")
        return
    for n in notes:
        print(f"  {n.path}  ({n.size}B, {n.chunk_count} chunks)")
    print(f"\n{len(notes)} notes")


async def _cmd_read(client: ObsidianVaultClient, args):
    note = await client.read_note(args.path)
    if not note:
        print(f"Not found: {args.path}", file=sys.stderr)
        sys.exit(1)
    if note.is_binary:
        print(f"[Binary file, {note.size} bytes]", file=sys.stderr)
    else:
        print(note.content)


async def _cmd_write(client: ObsidianVaultClient, args):
    if args.file:
        with open(args.file) as f:
            content = f.read()
    elif args.content:
        content = args.content
    else:
        content = sys.stdin.read()
    await client.write_note(args.path, content)
    print(f"Written: {args.path} ({len(content.encode('utf-8'))} bytes)")


async def _cmd_search(client: ObsidianVaultClient, args):
    results = await client.search_notes(
        query=args.query, folder=args.d, limit=args.n
    )
    if not results:
        print(f"No results for: {args.query}")
        return
    for r in results:
        print(f"\n{r.path} ({r.matches} matches)")
        for s in r.snippets:
            print(f"  > {s}")


async def _cmd_append(client: ObsidianVaultClient, args):
    if args.file:
        with open(args.file) as f:
            content = f.read()
    elif args.content:
        content = args.content
    else:
        content = sys.stdin.read()
    await client.append_note(args.path, content)
    print(f"Appended to: {args.path}")


async def _cmd_delete(client: ObsidianVaultClient, args):
    if not args.y:
        confirm = input(f"Delete '{args.path}'? [y/N] ")
        if confirm.lower() != "y":
            print("Cancelled.")
            return
    await client.delete_note(args.path)
    print(f"Deleted: {args.path}")


async def _cmd_folders(client: ObsidianVaultClient, args):
    folders = await client.list_folders()
    if not folders:
        print("No folders found.")
        return
    for f in folders:
        print(f"  {f.path}/  ({f.note_count} notes)")
    print(f"\n{len(folders)} folders")


def main():
    parser = argparse.ArgumentParser(
        prog="obsidian",
        description="Obsidian vault CLI via CouchDB LiveSync",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # list / ls
    p_list = sub.add_parser("list", aliases=["ls"], help="List notes")
    p_list.add_argument("folder", nargs="?", help="Folder to filter")
    p_list.add_argument("-n", type=int, default=50, help="Limit (default 50)")

    # read / cat
    p_read = sub.add_parser("read", aliases=["cat"], help="Read a note")
    p_read.add_argument("path", help="Vault path to the note")

    # write
    p_write = sub.add_parser("write", help="Create/update a note")
    p_write.add_argument("path", help="Vault path")
    p_write.add_argument("content", nargs="?", help="Content (or use -f/stdin)")
    p_write.add_argument("-f", "--file", help="Read content from file")

    # search / grep
    p_search = sub.add_parser("search", aliases=["grep"], help="Search notes")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("-d", help="Folder to search within")
    p_search.add_argument("-n", type=int, default=20, help="Limit (default 20)")

    # append
    p_append = sub.add_parser("append", help="Append to a note")
    p_append.add_argument("path", help="Vault path")
    p_append.add_argument("content", nargs="?", help="Content (or use -f/stdin)")
    p_append.add_argument("-f", "--file", help="Read content from file")

    # delete / rm
    p_delete = sub.add_parser("delete", aliases=["rm"], help="Delete a note")
    p_delete.add_argument("path", help="Vault path")
    p_delete.add_argument("-y", action="store_true", help="Skip confirmation")

    # folders / tree
    sub.add_parser("folders", aliases=["tree"], help="List folders")

    args = parser.parse_args()

    cmd_map = {
        "list": _cmd_list, "ls": _cmd_list,
        "read": _cmd_read, "cat": _cmd_read,
        "write": _cmd_write,
        "search": _cmd_search, "grep": _cmd_search,
        "append": _cmd_append,
        "delete": _cmd_delete, "rm": _cmd_delete,
        "folders": _cmd_folders, "tree": _cmd_folders,
    }

    handler = cmd_map[args.command]
    client = ObsidianVaultClient(Config())

    async def run():
        try:
            await handler(client, args)
        finally:
            await client.close()

    _run(run())


if __name__ == "__main__":
    main()
