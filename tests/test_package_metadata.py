from __future__ import annotations

import tomllib
from pathlib import Path


def test_package_requires_live_verified_python_range() -> None:
    repository = Path(__file__).parents[1]
    metadata = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["requires-python"] == ">=3.11,<3.12"
    assert (repository / ".python-version").read_text(encoding="utf-8").strip() == "3.11"
