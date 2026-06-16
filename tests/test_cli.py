"""Tests for the obsidian_livesync_mcp CLI parser."""

import pytest


def test_prune_orphans_parser_defaults_to_dry_run():
    from obsidian_livesync_mcp.cli import build_parser

    args = build_parser().parse_args(["prune-orphans"])

    assert args.delete is False


def test_repair_requires_from_file():
    from obsidian_livesync_mcp.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["repair", "Sample/x.md"])
