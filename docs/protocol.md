# Operating protocol

## Roles

- **Board:** company direction, product idea approval, material product or architecture changes, sensitive external commitments, production launch, archival, and CEO/CTO conflicts.
- **CEO:** co-designs products and protects product intent, scope, sequence, momentum, and simplicity. It does not allocate Tokens, rank unlike products, or stop development for compute scarcity.
- **CTO:** co-designs the minimum sound technical path and protects correctness, safety, maintainability, and complexity. It can block an affected unsafe merge/deploy, not the product.
- **Owner:** canonical writer for one product, continuously executing its accepted Spec.

CEO and CTO are peer lenses, not a reporting hierarchy. Tool visibility does not change these boundaries.

## Product lifecycle

Projects move through `registered`, `shaping`, `ready`, `active`, `blocked_external`, and `archived`. CEO or CTO creates a project conversationally. Before Owner launch they jointly create:

- `BRIEF.md`: user, problem, desired outcome, smallest useful vertical, scope/non-goals, acceptance evidence, and unknowns;
- `.specify/memory/constitution.md`: stable engineering principles and project overlay;
- a first Spec or `spec-seeds/001-first-vertical.md`: behavior plus technical constraints and quality floor;
- CEO and CTO endorsements bound to the digest of those exact artifacts.

Once launched, the Owner completes Spec Kit's `specify → clarify → plan → tasks → implement → converge` flow as appropriate. Small bounded fixes need not manufacture a full new lifecycle. Requirements cannot change only in chat: durable instructions identify whether a Spec revision is required.

## Owner work queue

An Owner wrapper claims one SQLite work item, resumes the exact Trae thread for one noninteractive atomic turn, streams its log to the hidden tmux pane, records a checkpoint or blocker, acknowledges the source message, then claims the next item.

CEO/CTO instructions are structured records containing authority, target project/Spec/task, observation, evidence, required action, forbidden expansion, acceptance delta, effective boundary, and whether a Spec revision is required. Product actions are `ALIGN`, `NARROW`, `RESEQUENCE`, `CONTINUE`, and `ESCALATE`; technical actions are `CORRECT`, `SIMPLIFY`, `HARDEN`, `CONTINUE`, and `ESCALATE`.

New instructions never inject input into a running Agent. They remain queued until the current atomic turn ends. Safe compatible work continues by default; the Owner waits only for an unsafe, irreversible, conflicting, or product-changing decision.

## Messages, hooks, and authority

Messages and acknowledgment live in SQLite. Notifications and Trae hooks are best-effort hints; neither creates authority or completion. Every MCP operation resolves `ONECO_SESSION_ID` against the live runtime, including its logical ID, authority kind, and epoch. Model-supplied role strings are not trusted.

Canonical executives can create/endorse/launch products, instruct Owners within their verb set, and decide their approval domains. Owners can checkpoint and request action only for their project. Secretaries/candidates cannot mutate the organization and can message only their parent.

## External action approvals

| Domain | Required approval |
| --- | --- |
| `product` | CEO |
| `technical` | CTO |
| `cross_domain` | CEO and CTO |

Strategic, production, sensitive-data, high-permission, or irreversible choices remain Board decisions even when an executive can record the operational approval.

## Recovery

- Terminal lost, provider thread healthy: reopen and exact-resume the same UUID.
- Owner worker lost: requeue claimed/running work, retain the writer epoch, and exact-resume the Owner thread.
- Provider thread broken: exact-fork a recovery candidate and require an exact, time-limited Board phrase inside the candidate chat before succession.
- Runtime lost: rebuild derivable indexes/messages from versioned artifacts, then reopen sessions.
- Old process returns: epoch fencing rejects its writes.

Never rely on `--last`, cwd guessing, a tmux pane ID, or the model's claimed identity for recovery.
