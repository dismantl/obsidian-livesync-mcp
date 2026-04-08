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
        try:
            with open(args.file) as f:
                content = f.read()
        except (FileNotFoundError, PermissionError, IsADirectoryError) as e:
            print(f"Error reading file: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.content:
        content = args.content
    else:
        content = sys.stdin.read()
    await client.write_note(args.path, content)
    print(f"Written: {args.path} ({len(content.encode('utf-8'))} bytes)")


async def _cmd_search(client: ObsidianVaultClient, args):
    results = await client.search_notes(query=args.query, folder=args.d, limit=args.n)
    if not results:
        print(f"No results for: {args.query}")
        return
    for r in results:
        print(f"\n{r.path} ({r.matches} matches)")
        for s in r.snippets:
            print(f"  > {s}")


async def _cmd_append(client: ObsidianVaultClient, args):
    if args.file:
        try:
            with open(args.file) as f:
                content = f.read()
        except (FileNotFoundError, PermissionError, IsADirectoryError) as e:
            print(f"Error reading file: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.content:
        content = args.content
    else:
        content = sys.stdin.read()
    await client.append_note(args.path, content)
    print(f"Appended to: {args.path}")


async def _cmd_delete(client: ObsidianVaultClient, args):
    if not args.y:
        kind = "HARD delete" if args.hard else "delete"
        confirm = input(f"{kind} '{args.path}'? [y/N] ")
        if confirm.lower() != "y":
            print("Cancelled.")
            return
    await client.delete_note(args.path, hard=args.hard)
    print(f"Deleted: {args.path}" + (" (hard)" if args.hard else ""))


async def _cmd_props(client: ObsidianVaultClient, args):
    if args.set:
        properties = {}
        for pair in args.set:
            if "=" not in pair:
                print(f"Invalid format (use key=value): {pair}", file=sys.stderr)
                sys.exit(1)
            k, v = pair.split("=", 1)
            # Try to parse as JSON for lists/bools/numbers, fall back to string
            import json

            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                pass
            properties[k.strip()] = v
        await client.update_frontmatter(args.path, properties)
        print(f"Updated frontmatter for: {args.path}")
    else:
        fm = await client.read_frontmatter(args.path)
        if fm is None:
            print(f"No frontmatter in: {args.path}")
            return
        for k, v in fm.items():
            print(f"  {k}: {v}")


async def _cmd_tags(client: ObsidianVaultClient, args):
    if args.find:
        notes = await client.search_by_tag(tag=args.find, folder=args.folder, limit=args.n)
        if not notes:
            print(f"No notes with tag: #{args.find}")
            return
        for n in notes:
            print(f"  {n.path}")
        print(f"\n{len(notes)} notes")
    else:
        tags = await client.list_tags(folder=args.folder)
        if not tags:
            print("No tags found.")
            return
        for tag, count in tags.items():
            print(f"  #{tag}  ({count})")
        print(f"\n{len(tags)} tags")


async def _cmd_backlinks(client: ObsidianVaultClient, args):
    backlinks = await client.get_backlinks(args.path)
    if not backlinks:
        print(f"No backlinks for: {args.path}")
        return
    for bl in backlinks:
        ctx = f"  > {bl.context}" if bl.context else ""
        print(f"  {bl.source_path}")
        if ctx:
            print(ctx)
    print(f"\n{len(backlinks)} backlinks")


async def _cmd_links(client: ObsidianVaultClient, args):
    links = await client.get_outbound_links(args.path)
    if not links:
        print(f"No outbound links in: {args.path}")
        return
    for link in links:
        print(f"  [[{link}]]")
    print(f"\n{len(links)} links")


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
    p_delete.add_argument(
        "--hard",
        action="store_true",
        help="Hard-delete (CouchDB tombstone + chunk cleanup). Default is a "
        "livesync-compatible soft-delete. Use --hard only for broken-manifest "
        "cleanup; it does NOT propagate to filesystem copies on livesync devices.",
    )

    # props
    p_props = sub.add_parser("props", help="Read/set frontmatter properties")
    p_props.add_argument("path", help="Vault path to the note")
    p_props.add_argument("--set", nargs="+", metavar="KEY=VALUE", help="Set properties")

    # tags
    p_tags = sub.add_parser("tags", help="List tags or find notes by tag")
    p_tags.add_argument("folder", nargs="?", help="Folder to filter")
    p_tags.add_argument("--find", metavar="TAG", help="Find notes with this tag")
    p_tags.add_argument("-n", type=int, default=20, help="Limit (default 20)")

    # backlinks
    p_backlinks = sub.add_parser("backlinks", help="Find notes linking to this note")
    p_backlinks.add_argument("path", help="Vault path to the target note")

    # links
    p_links = sub.add_parser("links", help="Show outbound wikilinks from a note")
    p_links.add_argument("path", help="Vault path to the note")

    # folders / tree
    sub.add_parser("folders", aliases=["tree"], help="List folders")

    args = parser.parse_args()

    cmd_map = {
        "list": _cmd_list,
        "ls": _cmd_list,
        "read": _cmd_read,
        "cat": _cmd_read,
        "write": _cmd_write,
        "search": _cmd_search,
        "grep": _cmd_search,
        "append": _cmd_append,
        "delete": _cmd_delete,
        "rm": _cmd_delete,
        "props": _cmd_props,
        "tags": _cmd_tags,
        "backlinks": _cmd_backlinks,
        "links": _cmd_links,
        "folders": _cmd_folders,
        "tree": _cmd_folders,
    }

    handler = cmd_map[args.command]
    client = ObsidianVaultClient(Config())

    async def run():
        try:
            await handler(client, args)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Error: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            await client.close()

    _run(run())


if __name__ == "__main__":
    main()
