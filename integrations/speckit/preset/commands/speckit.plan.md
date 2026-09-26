---
description: Create the minimum sound implementation plan for the active OneCo Spec.
---

## User Input

```text
$ARGUMENTS
```

## Procedure

1. Read `BRIEF.md`, `.specify/memory/constitution.md`, `.specify/feature.json`, and the active
   `spec.md`. Inspect the actual code paths that the plan depends on.
2. Write `<feature_directory>/plan.md` using the resolved `plan-template`.
3. Choose the fewest processes, datastores, services, dependencies, and abstractions that meet
   the Spec. Record evidence for every complexity exception.
4. Name exact files, dependencies, verification commands, safe boundaries, and failure behavior.
5. Complete the Constitution Check before implementation; an unresolved failure blocks only the
   affected work and must be reported plainly.
