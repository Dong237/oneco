---
description: Create checkpoint-addressable tasks for the active OneCo Spec.
---

## User Input

```text
$ARGUMENTS
```

## Procedure

1. Read `BRIEF.md`, `.specify/memory/constitution.md`, `.specify/feature.json`, and the active
   `spec.md` and `plan.md`.
2. Write `<feature_directory>/tasks.md` using the resolved `tasks-template`.
3. Give every task one unique sequential ID matching `TNNN`, starting at `T001`. These IDs are
   durable checkpoint addresses: never renumber or reuse them after work begins.
4. Each checklist item MUST name the exact path or bounded output, dependency if any, and
   verification evidence. Keep the smallest runnable vertical first.
5. Include required correctness, recovery, privacy, security, and observable runtime checks.
6. Verify the queue covers every requirement without adding work outside the Spec.
