---
description: Execute one verified, checkpoint-addressable OneCo task at a time.
---

## User Input

```text
$ARGUMENTS
```

## Procedure

1. Read `BRIEF.md`, `project.json`, `.specify/memory/constitution.md`, `.specify/feature.json`,
   the active `spec.md`, `plan.md`, `tasks.md`, and the OneCo inbox.
2. Confirm every task has a unique ID matching `TNNN`. Do not renumber existing IDs.
3. Execute the next dependency-ready task within the assigned project boundary. Preserve user work.
4. Run the task's verification. Mark only its checkbox complete when current evidence passes.
5. At a meaningful checkpoint, report the stable `spec_id` and exact `task_id`, result, key files,
   verification, deviations, complexity or dependency changes, risk or blocker, and next task.
6. Continue the next compatible task. Stop only for an unsafe, irreversible, conflicting, or
   product-changing decision, or when verification fails and no safe bounded correction remains.
