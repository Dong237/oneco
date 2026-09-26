from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "stage-project"


def snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def run_stage(project: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(SCRIPT),
            "--project-dir",
            str(project),
            "--feature-number",
            "001",
            "--feature-slug",
            "first-vertical",
            "--brief",
            str(project / "BRIEF.md"),
            *extra,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_stages_nonempty_project_and_is_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("existing user work\n", encoding="utf-8")
    (project / "BRIEF.md").write_text("# Brief\n\nA concrete outcome.\n", encoding="utf-8")

    first = run_stage(project)
    assert first.returncode == 0, first.stdout + first.stderr
    payload = json.loads(first.stdout)
    assert payload["ok"] is True
    assert payload["spec_id"] == "SPEC-001"
    assert payload["task_id_pattern"] == "^T[0-9]{3}$"
    assert (project / "README.md").read_text(encoding="utf-8") == "existing user work\n"
    assert (project / "specs/001-first-vertical/spec.md").is_file()

    before = snapshot(project)
    second = run_stage(project)
    assert second.returncode == 0, second.stdout + second.stderr
    assert snapshot(project) == before


def test_accepts_existing_oneco_scaffold_without_overwrite(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".specify/memory").mkdir(parents=True)
    (project / "spec-seeds").mkdir()
    (project / "BRIEF.md").write_text("# Existing brief\n", encoding="utf-8")
    (project / ".specify/memory/constitution.md").write_text(
        "# Existing constitution\n", encoding="utf-8"
    )
    (project / "spec-seeds/001-first-vertical.md").write_text("# Existing seed\n", encoding="utf-8")
    before = {key: value for key, value in snapshot(project).items()}

    result = run_stage(project, "--spec-id", "SPEC-001")
    assert result.returncode == 0, result.stdout + result.stderr
    after = snapshot(project)
    for path, digest in before.items():
        assert after[path] == digest


def test_prefix_conflict_makes_no_writes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "specs/001-other").mkdir(parents=True)
    (project / "BRIEF.md").write_text("# Brief\n", encoding="utf-8")
    (project / "specs/001-other/spec.md").write_text("owned\n", encoding="utf-8")
    before = snapshot(project)

    result = run_stage(project)
    assert result.returncode == 2
    assert json.loads(result.stdout)["ok"] is False
    assert snapshot(project) == before


def test_active_feature_conflict_makes_no_writes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".specify").mkdir(parents=True)
    (project / "BRIEF.md").write_text("# Brief\n", encoding="utf-8")
    (project / ".specify/feature.json").write_text(
        '{"feature_directory":"specs/002-other"}\n', encoding="utf-8"
    )
    before = snapshot(project)

    result = run_stage(project)
    assert result.returncode == 2
    assert json.loads(result.stdout)["ok"] is False
    assert snapshot(project) == before


def test_spec_identity_conflict_makes_no_writes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".specify").mkdir(parents=True)
    (project / "BRIEF.md").write_text("# Brief\n", encoding="utf-8")
    (project / ".specify/feature.json").write_text(
        json.dumps(
            {
                "feature_directory": "specs/001-first-vertical",
                "spec_id": "SPEC-DIFFERENT",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    before = snapshot(project)

    result = run_stage(project)
    assert result.returncode == 2
    assert json.loads(result.stdout)["ok"] is False
    assert snapshot(project) == before


def test_rejects_invalid_number_before_writes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "BRIEF.md").write_text("# Brief\n", encoding="utf-8")
    before = snapshot(project)
    command = [
        str(SCRIPT),
        "--project-dir",
        str(project),
        "--feature-number",
        "1",
        "--feature-slug",
        "first-vertical",
        "--brief",
        str(project / "BRIEF.md"),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    assert result.returncode == 2
    assert snapshot(project) == before


def test_parent_path_conflict_makes_no_writes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "BRIEF.md").write_text("# Brief\n", encoding="utf-8")
    (project / ".specify").write_text("blocking file\n", encoding="utf-8")
    before = snapshot(project)

    result = run_stage(project)
    assert result.returncode == 2
    assert json.loads(result.stdout)["ok"] is False
    assert snapshot(project) == before


def test_relative_brief_is_resolved_from_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "BRIEF.md").write_text("# Brief\n", encoding="utf-8")
    result = subprocess.run(
        [
            str(SCRIPT),
            "--project-dir",
            str(project),
            "--feature-number",
            "001",
            "--feature-slug",
            "first-vertical",
            "--brief",
            "BRIEF.md",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["brief_file"] == "BRIEF.md"


def test_accepts_path_safe_custom_spec_id(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "BRIEF.md").write_text("# Brief\n", encoding="utf-8")
    result = run_stage(project, "--spec-id", "001-first-vertical")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["spec_id"] == "001-first-vertical"
