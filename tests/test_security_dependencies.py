from pathlib import Path

import tomllib


def test_starlette_dependency_has_badhost_fix_floor() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())

    assert "starlette>=1.0.1" in pyproject["project"]["dependencies"]
