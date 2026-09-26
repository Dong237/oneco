from __future__ import annotations

import pytest
from pydantic import ValidationError

from oneco_os.models import Checkpoint, Instruction, Portfolio, ProjectRecord


def _checkpoint(**overrides: object) -> Checkpoint:
    values: dict[str, object] = {
        "checkpoint_id": "cp_001",
        "project_id": "PROJ-001",
        "spec_id": "SPEC-001",
        "task_id": "TASK-001",
        "result": "Implemented the vertical slice",
        "key_files": ["src/app.py"],
        "verification": ["pytest passed"],
        "next_task": "TASK-002",
    }
    values.update(overrides)
    return Checkpoint.model_validate(values)


@pytest.mark.parametrize("path", ["../outside", "nested/../../outside", "/absolute/project"])
def test_project_record_requires_safe_relative_path(path: str) -> None:
    with pytest.raises(ValidationError, match="relative"):
        ProjectRecord(id="PROJ-001", name="Alpha", slug="alpha", path=path)


def test_portfolio_rejects_duplicate_project_ids_and_paths() -> None:
    alpha = ProjectRecord(id="PROJ-001", name="Alpha", slug="alpha", path="alpha")
    same_id = ProjectRecord(id="PROJ-001", name="Beta", slug="beta", path="beta")
    same_path = ProjectRecord(id="PROJ-002", name="Other", slug="other", path="alpha")

    with pytest.raises(ValidationError, match="IDs must be unique"):
        Portfolio(projects=[alpha, same_id])
    with pytest.raises(ValidationError, match="paths must be unique"):
        Portfolio(projects=[alpha, same_path])


@pytest.mark.parametrize("field", ["spec_id", "task_id"])
def test_checkpoint_requires_nonempty_spec_and_task_identifiers(field: str) -> None:
    with pytest.raises(ValidationError):
        _checkpoint(**{field: ""})


@pytest.mark.parametrize("key_file", ["../outside.py", "src/../../outside.py", "/absolute/file.py"])
def test_checkpoint_key_files_must_be_safe_relative_paths(key_file: str) -> None:
    with pytest.raises(ValidationError, match="relative|escape"):
        _checkpoint(key_files=[key_file])


def test_checkpoint_requires_verification_and_next_task() -> None:
    with pytest.raises(ValidationError):
        _checkpoint(verification=[])
    with pytest.raises(ValidationError):
        _checkpoint(next_task="")


@pytest.mark.parametrize(
    "authority,action",
    [("CEO", "CORRECT"), ("CTO", "ALIGN"), ("OWNER", "CONTINUE")],
)
def test_instruction_action_is_owned_by_issuing_role(authority: str, action: str) -> None:
    with pytest.raises(ValidationError, match="cannot issue|CEO or CTO"):
        Instruction(
            instruction_id="inst_001",
            sender_authority=authority,
            action=action,
            project_id="PROJ-001",
            spec_id="SPEC-001",
            task_id="TASK-001",
            observed="Observed fact",
            evidence="Evidence",
            required_action="Required change",
            do_not="Do not widen scope",
        )
