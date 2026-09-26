from __future__ import annotations

import json
from pathlib import Path

import pytest

from oneco_os.models import ProjectState
from oneco_os.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace.initialize(tmp_path / "company", "Test Company", git_init=False)


@pytest.fixture
def active_project(workspace: Workspace) -> tuple[Workspace, str, Path]:
    project = workspace.register_project(
        "Alpha Product", Path("alpha-product"), initialize_git=False
    )
    project_root = workspace.project_path(project.id)

    # Readiness is intentionally based on explicit durable artifacts. Complete the
    # generated drafts without reaching into runtime state.
    for relative in (
        "BRIEF.md",
        ".specify/memory/constitution.md",
        "spec-seeds/001-first-vertical.md",
    ):
        path = project_root / relative
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace("TBD", "Defined").replace("DRAFT —", "READY —"), encoding="utf-8"
        )
    (project_root / "decisions" / "launch-endorsements.json").write_text(
        json.dumps({"CEO": True, "CTO": True}), encoding="utf-8"
    )
    (project_root / ".git").mkdir()

    workspace.set_state(project.id, ProjectState.ACTIVE, actor="BOARD")
    return workspace, project.id, project_root
