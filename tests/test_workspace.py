from __future__ import annotations

import json
from pathlib import Path

import pytest

from oneco_os.errors import AuthorizationError, ConflictError, ProjectError, WorkspaceError
from oneco_os.models import ProjectState
from oneco_os.util import company_root, safe_child
from oneco_os.workspace import Workspace


def test_initialize_builds_openable_company_without_overwriting_existing_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "company"
    root.mkdir()
    marker = root / "keep-me.txt"
    marker.write_text("user data", encoding="utf-8")

    workspace = Workspace.initialize(
        root,
        "Acme Labs",
        language="zh",
        provider="trae",
        model="test-model",
        terminal="tmux",
        git_init=False,
    )

    assert marker.read_text(encoding="utf-8") == "user data"
    assert workspace.manifest().company_name == "Acme Labs"
    assert workspace.manifest().language == "zh"
    assert workspace.portfolio().projects == []
    assert workspace.runtime.database.is_file()
    assert (root / "COMPANY.md").is_file()
    assert {path.name for path in (root / "roles").iterdir()} == {
        "CEO.md",
        "CTO.md",
        "OWNER.md",
    }
    assert Workspace.open(root).root == root.resolve()


def test_initialize_rejects_existing_company_without_changing_manifest(tmp_path: Path) -> None:
    root = tmp_path / "company"
    Workspace.initialize(root, "Original", git_init=False)
    before = (root / ".oneco" / "manifest.json").read_bytes()

    with pytest.raises(ConflictError, match="already a OneCo company"):
        Workspace.initialize(root, "Replacement", git_init=False)

    assert (root / ".oneco" / "manifest.json").read_bytes() == before


def test_open_and_company_root_reject_non_company_and_find_nested_company(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="not a OneCo company"):
        Workspace.open(tmp_path)
    with pytest.raises(WorkspaceError, match="run `oneco company init`"):
        company_root(tmp_path)

    root = tmp_path / "company"
    Workspace.initialize(root, "Acme", git_init=False)
    nested = root / "products" / "docs"
    nested.mkdir(parents=True)

    assert company_root(nested) == root.resolve()


def test_register_project_scaffolds_direct_child_and_preserves_existing_content(
    workspace: Workspace,
) -> None:
    target = workspace.root / "existing-product"
    target.mkdir()
    marker = target / "README.md"
    marker.write_text("do not replace", encoding="utf-8")

    project = workspace.register_project("Existing Product", target, initialize_git=False)

    assert project.id == "PROJ-001"
    assert project.slug == "existing-product"
    assert project.path == "existing-product"
    assert marker.read_text(encoding="utf-8") == "do not replace"
    assert (target / "project.json").is_file()
    assert (target / "AGENTS.md").is_file()
    assert (target / ".specify" / "memory" / "constitution.md").is_file()
    assert "/existing-product/" in (workspace.root / ".gitignore").read_text(encoding="utf-8")
    assert workspace.find_project(project.id) == project


def test_register_project_rejects_duplicate_without_consuming_project_id(
    workspace: Workspace,
) -> None:
    workspace.register_project("Alpha", Path("alpha"), initialize_git=False)

    with pytest.raises(ConflictError, match="already registered"):
        workspace.register_project("Alpha Again", Path("alpha"), initialize_git=False)

    second = workspace.register_project("Beta", Path("beta"), initialize_git=False)
    assert second.id == "PROJ-002"
    assert [item.path for item in workspace.portfolio().projects] == ["alpha", "beta"]


@pytest.mark.parametrize("relative", [Path("nested/product"), Path("../outside")])
def test_register_project_rejects_non_direct_child_without_creating_it(
    workspace: Workspace, relative: Path
) -> None:
    target = (workspace.root / relative).resolve()
    assert not target.exists()

    with pytest.raises(ProjectError, match="direct children"):
        workspace.register_project("Unsafe", relative, initialize_git=False)

    assert not target.exists()
    assert workspace.portfolio().projects == []


def test_register_project_rejects_direct_child_symlink_that_escapes_company(
    workspace: Workspace, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace.root / "linked-product"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectError, match="direct children"):
        workspace.register_project("Linked", link, initialize_git=False)

    assert workspace.portfolio().projects == []
    assert list(outside.iterdir()) == []


def test_readiness_and_activation_require_complete_artifacts_and_board_authority(
    workspace: Workspace,
) -> None:
    project = workspace.register_project("Alpha", Path("alpha"), initialize_git=False)

    ready, reasons = workspace.readiness(project.id)
    assert ready is False
    assert "missing CEO/CTO launch endorsements" in reasons
    assert "project is not an independent Git repository" in reasons
    assert any(reason.startswith("incomplete BRIEF.md") for reason in reasons)

    with pytest.raises(AuthorizationError, match="only BOARD"):
        workspace.set_state(project.id, ProjectState.ACTIVE, actor="CEO")
    with pytest.raises(ProjectError, match="project is not ready"):
        workspace.set_state(project.id, ProjectState.ACTIVE, actor="BOARD")


def test_board_activation_updates_portfolio_and_project_record(
    active_project: tuple[Workspace, str, Path],
) -> None:
    workspace, project_id, project_root = active_project

    assert workspace.find_project(project_id).state is ProjectState.ACTIVE
    assert (
        json.loads((project_root / "project.json").read_text(encoding="utf-8"))["state"] == "active"
    )

    with pytest.raises(AuthorizationError, match="only BOARD"):
        workspace.set_state(project_id, ProjectState.ARCHIVED, actor="CTO")


def test_role_scaffold_separates_authority_and_five_races_without_leaderboard_metrics(
    workspace: Workspace,
) -> None:
    project = workspace.register_project("Alpha", Path("alpha"), initialize_git=False)
    project_contract = (workspace.project_path(project.id) / "AGENTS.md").read_text(
        encoding="utf-8"
    )
    ceo = (workspace.root / "roles" / "CEO.md").read_text(encoding="utf-8")
    cto = (workspace.root / "roles" / "CTO.md").read_text(encoding="utf-8")
    owner = (workspace.root / "roles" / "OWNER.md").read_text(encoding="utf-8")

    for race in ("Closure", "Commitment", "Simplicity", "Truth", "Reuse"):
        assert f"- {race}:" in project_contract
    for forbidden_metric in (
        "code volume",
        "token use",
        "online time",
        "feature count",
        "report length",
    ):
        assert forbidden_metric in project_contract
    assert "modify product code" in ceo
    assert "never change product intent" in cto.lower()
    assert "inside this project" in owner
    assert "rank unrelated products" in ceo


@pytest.mark.parametrize("relative", ["../escape", "a/../../escape", "/tmp/escape"])
def test_safe_child_rejects_path_escape(workspace: Workspace, relative: str) -> None:
    with pytest.raises(WorkspaceError, match="path escapes company root"):
        safe_child(workspace.root, relative)


def test_safe_child_rejects_symlink_escape(workspace: Workspace, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace.root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceError, match="path escapes company root"):
        safe_child(workspace.root, "link/secret.txt")
