---
description: Turn the staged OneCo brief and seed into the active specification.
---

## User Input

```text
$ARGUMENTS
```

## Procedure

1. Read `BRIEF.md`, `.specify/memory/constitution.md`, `.specify/feature.json`, and the
   `seed_file` named in feature metadata. If older compatible feature metadata omits `seed_file`,
   derive it as `spec-seeds/<feature-directory-name>.md`. Do not create a second feature directory.
2. Resolve `feature_directory` from `.specify/feature.json`; it MUST match
   `specs/[0-9][0-9][0-9]-<kebab-slug>`. Treat its `spec_id` as stable; if older compatible
   metadata omits it, derive `SPEC-<three-digit-feature-number>`.
3. Write `<feature_directory>/spec.md` with the user and situation, smallest vertical result,
   independently testable acceptance scenarios, `FR-NNN` requirements, explicit non-goals,
   constraints, risks, and escalation triggers.
4. Preserve Board-approved intent. Make informed defaults only within that intent and name
   unresolved product-changing decisions.
5. Check every requirement is testable and the result can be delivered as one runnable vertical.
