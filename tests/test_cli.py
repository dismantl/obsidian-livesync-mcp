"""Tests for the obsidian_livesync_mcp CLI parser."""


def test_prune_orphans_parser_defaults_to_dry_run():
    from obsidian_livesync_mcp.cli import build_parser

    args = build_parser().parse_args(["prune-orphans"])

    assert args.delete is False
