from importlib.metadata import requires
from pathlib import Path

from packaging.requirements import Requirement


def _mcp_requirement() -> Requirement:
    for raw in requires("obsidian-livesync-mcp") or []:
        requirement = Requirement(raw)
        if requirement.name == "mcp" and requirement.marker is None:
            return requirement
    raise AssertionError("mcp is not a runtime dependency")


def test_mcp_dependency_excludes_v2() -> None:
    # mcp 2.x removed mcp.server.fastmcp, which server.py imports.
    specifier = _mcp_requirement().specifier

    assert specifier.contains("1.26.0")
    assert not specifier.contains("2.0.0")


def test_docker_image_installs_from_lockfile() -> None:
    dockerfile = Path("Dockerfile").read_text()

    assert "COPY pyproject.toml uv.lock" in dockerfile
    assert "uv sync --locked --no-dev" in dockerfile
    assert "pip install" not in dockerfile
