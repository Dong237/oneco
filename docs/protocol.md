# Operating protocol

## Roles and capabilities

| Actor | Owns | May do | Must not do |
| --- | --- | --- | --- |
| Human Board | Company direction and irreversible authority | Assign Signals; select, reject, or request evidence; approve production and major changes; resolve CEO/CTO deadlock | Delegate portfolio allocation or final Product Bet choice to an agent |
| CPO | Pre-project evidence | Investigate assigned Signals; create/revise/submit Bets; advise Board, CEO, CTO; use bounded research delegates | Select/reject, promote, endorse, launch, approve, instruct Owner, edit product code |
| CEO | Product intent and sequence | Promote the selected Bet; shape user outcome, scope, non-goals, acceptance; endorse launch/release; issue product corrections | Override technical safety, edit product code, rank unrelated products |
| CTO | Technical safety and quality floor | Shape architecture; supervise every Owner; endorse launch/release; issue technical corrections; stop affected unsafe work | Create/promote a project, redefine product intent, stop compatible work |
| Owner | One product's canonical implementation | Execute accepted Spec work; verify; checkpoint; request review/action; execute Board-approved release | Write another product, change intent silently, accept CPO commands, self-approve production |

## Signal and Product Bet lifecycle

1. The Board assigns a traceable Signal. A reviewed Daily Product Teardown file may be a source exactly like a Board note; teardown is not a built-in CPO capability or scheduled step.
2. CPO chooses the evidence method. Product analysis, business-system analysis, validation, and research acquisition remain external skills and can be revisited in any order.
3. CPO maintains `bets/BET-NNNN/revisions/RNNN/PRODUCT_BET_PACKET.md`. Submission freezes a manifest containing file and evidence digests plus `not_run`, `tbd`, `passed`, `mixed`, `failed`, or `invalid` validation evidence.
4. The Board binds one exact digest to `select`, `reject`, or `more_evidence`. `more_evidence` names the gap, never the method. Validation state does not determine the Board decision.
5. Rejected Bets and their history remain in Git and create no project. A new revision requires `more_evidence` or explicit `reopen`; submitted revisions are immutable.
6. CEO alone promotes the current selected digest. The product repository receives an exact snapshot and enters shaping.

Variants use `variant_of`; known duplicates use `duplicate_of`. They remain separate evidence records. No agent allocates attention or funding across them.

## Shaping and Owner launch

CEO owns `BRIEF.md` product intent, user outcome, scope, sequence, non-goals, and acceptance. CTO owns the project constitution overlay, technical constraints, correctness, safety, maintainability, and quality floor. They shape as peers.

Each endorses the same current launch digest. Artifact edits invalidate both endorsements. An Owner starts only after the gate passes. The existing Board waiver remains valid only for one missing peer endorsement and never for a missing mandatory artifact.

## Delivery and supervision

Each project has one writer lease and FIFO-priority work queue. An Owner runs one atomic Trae turn, verifies the smallest complete result, writes a checkpoint or real-gate request, acknowledges its source message, and then claims compatible work. New instructions wait; OneCo never types into a busy pane.

An Owner blocker is append-only. After the corrective work is verified, the same current Owner records a separate resolution tied to a later checkpoint. OneCo keeps the original blocker, removes it from active Board attention, and rebuilds the resolved state from both records after SQLite loss.

CTO receives scheduled review work every 240 minutes by default and immediate real-gate requests. Reviews cite evidence, verdict, affected scope, required action, and next review time. `needs_correction` or `blocked` affects only the named unsafe scope. CEO receives the review for product/sequence discussion; the Owner continues independent compatible work.

Product questions route to CEO. Technical questions route to CTO. CPO may add post-selection evidence to CEO or CTO but cannot alter the product, queue, or Owner mandate.

## Messaging contract

| Sender | Allowed recipients |
| --- | --- |
| Board | CPO, CEO, CTO, and the exact Owner for an approved production action |
| CPO | Board, CEO, CTO |
| CEO / CTO | Board, CPO, each other, and an Owner only with the matching project scope |
| Owner | CEO and CTO, only for its own project |
| Delegate / Secretary | Its parent persistent role only |

Messages carry a thread key, object ID, optional artifact digest, sender session and epoch, and per-thread sequence. Idempotency keys make retries safe. New artifact digests mark older notices stale. Acknowledgement is explicit and repeat-safe. Durable shared records carry decisions; direct messages carry attention and handoff, not secret alternative truth.

## External actions and release

| External-action domain | Required approval |
| --- | --- |
| `product` | CEO |
| `technical` | CTO |
| `cross_domain` | CEO and CTO |

Strategic, sensitive, irreversible, and production choices remain Board decisions.

A verified increment becomes a release candidate only when it names an exact product Git commit and durable acceptance-evidence files. CEO and CTO endorse the same release digest. The Board then approves or rejects that digest. Approval queues one Owner production action; it does not claim that deployment succeeded. The Owner records the result separately.
