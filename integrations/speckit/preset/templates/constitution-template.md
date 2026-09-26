<!--
Sync Impact Report
- Version change: none -> 1.0.0
- Added principles: Vertical Delivery; Simplicity; Evidence Before Completion; Safe Boundaries; Honest Escalation
- Added sections: Quality Gates; Governance
- Synchronized templates: spec-template.md; plan-template.md; tasks-template.md
- Deferred items: none
-->
# OneCo Project Constitution

## Core Principles

### I. Vertical Delivery
The current approved Spec MUST reach its smallest runnable user outcome before generalized
infrastructure or optional polish is added. Work outside that vertical requires explicit scope
approval.

### II. Simplicity
The implementation MUST begin with the fewest processes, datastores, services, dependencies,
and abstractions that satisfy the Spec. Added complexity MUST include concrete evidence that a
simpler option cannot meet the requirement.

### III. Evidence Before Completion
A task is complete only when its acceptance behavior has current verification evidence. Every
checkpoint MUST name the exact `TNNN` task, result, key files, verification, deviations, risks,
and next task.

### IV. Safe Boundaries
The Product Owner MUST write only within the assigned project, preserve existing user work, and
obtain the required approval before irreversible or external actions. Recovery, privacy,
security, and failure behavior are part of done.

### V. Honest Escalation
Uncertainty, failed checks, spec drift, and blockers MUST be reported plainly. The Owner MUST
continue the next compatible task and escalate only unsafe, irreversible, conflicting, or
product-changing decisions.

## Quality Gates

- The brief, active Spec, and task queue MUST agree on the smallest vertical result.
- Requirements and acceptance scenarios MUST be testable.
- Tasks MUST use unique sequential IDs matching `TNNN`; IDs MUST NOT be renumbered after a
  checkpoint refers to them.
- Required tests and observable runtime checks MUST pass before the corresponding task is
  marked complete.
- Deviations and new complexity or dependencies MUST be recorded in the next checkpoint.

## Governance

This constitution governs the project-level Spec Kit workflow. Amendments require a documented
rationale, semantic version bump, date update, and synchronization of dependent templates. Each
plan and implementation review MUST check compliance; unresolved violations block the affected
work. Product scope remains governed by the Board-approved brief and Spec.

**Version**: 1.0.0 | **Ratified**: 2026-09-22 | **Last Amended**: 2026-09-22
