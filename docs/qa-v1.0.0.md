# OneCo v1.0.0 release QA

Date: 2026-09-29
Verdict: **PASS as a local v1.0.0 release candidate on Python 3.11**

This release was exercised from source, from a built wheel, and through the installed host command. Destructive, concurrency, and recovery work used disposable companies. the existing company workspace and unrelated Ghostty windows, Spaces, agents, processes, and tmux sessions were not touched.

Python 3.11 is the verified v1 runtime. Native pywebview controls rendered correctly on Python 3.11.15 and remained blank on Python 3.12.11 and 3.13.11, so package metadata, the repository interpreter pin, installation instructions, and `oneco doctor` all fail closed outside Python 3.11.

## Final measured gates

| Gate | Result | Measured evidence |
| --- | --- | --- |
| Full Python suite | PASS | 258 passed in 951.49s on Python 3.11.15 |
| Fresh coverage run | PASS | The same final 258-test run covered 4,066/5,146 statements: 79.01% |
| Critical-module coverage | PASS | runtime 89%, workspace 94%, models 99%, company services 86%, services 86%, Bridge 82%, CLI 67% |
| New final regressions | PASS | 3/3 red-then-green cases: Python 3.12/3.13 doctor rejection and stale endorsement recovery |
| Static analysis | PASS | Ruff reports `All checks passed!` |
| Dependency lock | PASS | `uv lock --check` resolves the Python 3.11 lock without changes |
| Patch integrity | PASS | `git diff --check` |
| Public schemas | PASS | 13 Draft 2020-12 schemas, including immutable blocker resolution |
| MCP surface | PASS | 28 tools, including `oneco_blocker_resolve`; typed inputs and fail-closed role/session checks |
| Trae plugin | PASS | Source and installed v1.0.0 plugins validate with protocol, CPO, CEO, CTO, and Owner skills |
| Distribution | PASS | Wheel and sdist pass `twine check`; the wheel contains 107 files and declares `Requires-Python: <3.12,>=3.11` |
| Clean-wheel smoke | PASS | A clean Python 3.11.15 environment installed the wheel, created a schema-3 company, read status, rebuilt runtime, and loaded packaged Cockpit/schema/template/CPO/plugin resources |
| Installed host | PASS | `oneco --version` reports 1.0.0; `oneco doctor --json` passes all 7 required checks on Python 3.11.15 |
| External skill contracts | PASS | `agent-reach`, product analysis, business-system analysis, and `proposing-a-product-bet` all pass name/body compatibility checks |
| Full Access propagation | PASS | Real CPO, CEO, and CTO turns and exact CPO resume used `--dangerously-bypass-approvals-and-sandbox`; no approval prompts appeared |
| Cockpit visibility | PASS | Native content rendered in exact window 88371; its Space membership included invocation Space 647, it was on-screen, and a second call reused the same PID/window |
| SQLite-loss recovery | PASS | Durable state, Git heads, and tracked bytes matched before and after two rebuilds; delivery and release state remained correct |

## Real initialized-company pressure run

The disposable QA lineage culminated in a disposable initialized QA company, exercised by the installed Python 3.11 wheel rather than test doubles alone.

- Real CPO, CEO, and CTO Trae turns exited successfully under Full Access. CPO exact-resumed its exact saved provider thread.
- Durable role communication completed CPO -> CEO, CEO -> CTO, and CTO -> CEO routes. OneCo meetings are persistent role conversations plus durable messages; there is no second ephemeral meeting database.
- The supervisor ran independently with `bypass_permissions`. Owner execution used a company-scoped tmux server and never attached to unrelated sessions.
- The native Cockpit rendered Signals, Bets, project and release state, unread changes, roles, blockers, and health on the invocation Space without launching extra executives during the final Cockpit-only check.
- The recovered company contains 2 Signals, 2 immutable Bet revisions, 3 Board decisions including production, 1 promotion, 2 checkpoints, 1 release candidate, and 1 successful release result.
- Runtime rebuild was repeated twice and remained idempotent. The effective view stayed at 2 Signals, 1 Bet, 1 active project, 1 released release, no attention items, and unread `{"CPO": 1}`.
- Before and after SQLite loss, the company tree remained `e35fba57dbc379f2b4558667bdc47df5c75412a7` and the product tree remained `22e7390b841cdee623c9c1536a6a5cce80d980`.

## Required workflow pressure matrix

| # | Scenario | Durable/runtime/Cockpit and recovery result | Result |
| --- | --- | --- | --- |
| 1 | Board idea and teardown artifact enter as Signals | Both retain traceable source metadata. Daily Product Teardown remains an external source and is never invoked or scheduled by CPO. | PASS |
| 2 | CPO creates or revises a Bet | CPO can write evidence and recommendations. Selection, rejection, promotion, endorsement, launch, release approval, project-code edits, and Owner instruction fail closed. | PASS |
| 3 | Board requests more evidence | The decision records the evidence gap and selected revision digest. CPO chooses the research method; identical retries are idempotent. | PASS |
| 4 | Board rejects a Bet | Bet evidence and decision history remain committed; no project or Owner queue is created. | PASS |
| 5 | Board selects a specific digest | CEO promotion requires that exact selected revision and digest. The promoted project links back to immutable Bet and Board-decision provenance. | PASS |
| 6 | Selected Bet changes | A later Bet revision makes stale selection or promotion attempts fail before project mutation. Renewed Board selection is required. | PASS |
| 7 | CEO and CTO disagree | The affected work becomes blocked and Board attention is visible. Owner can continue only compatible work; neither role silently overrides the other. | PASS |
| 8 | Launch digest changes | Any mandatory artifact change invalidates both CEO and CTO endorsements, including after runtime rebuild. A stale digest cannot launch. | PASS |
| 9 | Owner asks product or technical questions | Product corrections route to CEO and technical corrections to CTO. CPO cannot intercept, answer with delivery authority, or command Owner. | PASS |
| 10 | CPO supplies post-selection evidence | Advice and evidence can reach CEO/CTO without altering the project, work queue, launch state, or Owner authority. | PASS |
| 11 | CPO/CEO/CTO windows close and resume | Persistent identities retain exact provider-thread bindings while runtime exists. Concurrent focus is serialized to one canonical role window. | PASS |
| 12 | A stale or replaced session writes | Session ID and epoch fencing reject its writes and messages. Recovery marks lost sessions without transferring authority implicitly. | PASS |
| 13 | A CPO research delegate acts | The delegate can report only within its bounded advisory route. It cannot become CPO, use privileged lifecycle tools, or message Owner. | PASS |
| 14 | Messages duplicate, retry, race, or acknowledge out of order | Idempotency keys deduplicate exact retries and reject changed payloads. Thread sequence is monotonic; broadcast acknowledgement is per recipient. | PASS |
| 15 | SQLite is deleted | Git-committed Signals, Bet revisions, decisions, promotion, project records, reviews, releases, blockers, and resolutions rebuild. Uncommitted authority is ignored. Provider-thread bindings remain runtime state and must be re-established after database loss. | PASS |
| 16 | Chat context is lost | Role snapshots and bounded durable artifacts reconstruct what happened, evidence, decisions, open work, and permitted next actions. | PASS |
| 17 | Multiple Bets and products coexist | IDs, durable paths, queues, writers, and Cockpit cards remain separate. No agent performs portfolio allocation. | PASS |
| 18 | Validation is passed/mixed/failed/invalid/TBD/not-run | The exact state remains evidence. None auto-selects, rejects, promotes, or launches. | PASS |
| 19 | External skill is missing or incompatible | Health names the exact contract and location. Work requiring it stops without fallback or copied-in methodology. | PASS |
| 20 | A product increment meets acceptance criteria | Verification creates evidence and a release candidate only. Production still requires current CEO/CTO digest endorsements and a separate Board release decision. | PASS |

## Messaging, meetings, Cockpit, and process behavior

| Area | Pressure applied | Observed result |
| --- | --- | --- |
| Role messaging | Authorized and unauthorized routes, duplicate keys, changed retries, broadcasts, acknowledgements, stale senders, and session replacement | Authorized messages remain durable and ordered; invalid routes and stale identities fail closed; a safe retry can survive canonical session replacement |
| Role meetings | CPO/CEO/CTO real turns exchanged durable research and shaping messages, then CPO resumed its exact chat | Conversation continuity and company facts survived process exit; no separate meeting state was required |
| Cockpit | Native current-Space launch, exact-window placement, callback races, data refresh, role focus, Signal/Bet/project/release/blocker/health views, and stale-attention recovery | Cockpit remains a rebuildable read model, exposes only current Board attention, appears on the invocation Space, and reuses one native process/window |
| Executives | New launch, exact resume, branch/recovery rules, concurrent focus, stale epoch, lost process, and permission propagation | One persistent identity per role; exact provider thread survives process/window loss; all managed turns default to Full Access |
| Owner | Atomic tmux execution, single-writer lease, transient retry, checkpoint attribution, blocker handling, and stale Owner replacement | Company-scoped tmux prevents cross-company contamination; only current Owner evidence changes delivery state |
| Supervisor | Readiness, duplicate start, Cockpit-independent lifetime, role wakeups, retry, and explicit shutdown in disposable companies | One supervisor per company; start is serialized; failures are bounded and recoverable |

## Confirmed defects found and fixed during pressure testing

| Severity | Defect | Final behavior |
| --- | --- | --- |
| P1 | Runtime rebuild insertion order could make an older release appear current | Release order uses durable candidate time with a deterministic tie-break |
| P1 | Historical stale releases remained in Board attention | Only the latest release per project contributes attention |
| P1 | Resolved blockers revived after SQLite loss | Immutable Owner resolution records reference later evidence-backed checkpoints and rebuild deterministically |
| P1 | Managed agents repeatedly requested routine tool approval | Every role path defaults to Full Access and passes Trae's supported bypass flag |
| P1 | Noninteractive Trae runs received unsupported `--no-daemon` | Noninteractive execution and exact resume omit the flag |
| P1 | Owner workers could use an unrelated default tmux server | Each company has a root-hashed private tmux server and watcher target |
| P2 | Cockpit lifetime could end after supervisor startup | Native Cockpit and supervisor lifetimes are independent and duplicate-safe |
| P1 | Recovery could mistake `launch-endorsements.json` or filename order for the current launch | Only valid committed launch records are considered; timestamp plus path provides deterministic recency |
| P1 | Python 3.12/3.13 were advertised although native Cockpit content stayed blank | v1 pins Python 3.11 in metadata, lockfile, repository, docs, and doctor |
| P1 | Cockpit placement could target the wrong native window or old Space | The exact `windowNumber()` moves to one captured invocation Space before activation |
| P1 | Native `loaded`/`shown` callbacks could race after focus changed | Both callbacks reuse the immutable invocation-Space ID |
| P1 | SQLite rebuild could make stale digest-bound launch endorsements appear ready | v1 endorsement objects must match the current digest after rebuild; dedicated regression passes |
| P1 | Doctor accepted unverified Python runtimes | Python 3.12 and 3.13 now fail the required doctor check explicitly |

All 38 confirmed code-review findings are recorded in `docs/code-review-v1.0.0.md`. None was waived or hidden as documentation-only debt.

## Recovery guarantees and rollback points

- Version-controlled human-readable records and product repositories are durable company truth. SQLite stores coordination, indexes, leases, identity/session bindings, messages, and read models.
- Runtime rebuild accepts authority only from committed records loaded from the inspected Git HEAD. Dirty, missing, or partially written authority records do not become effective state.
- Blocker resolution, release chronology, current Board attention, promotion provenance, and delivery state reconstruct from durable records.
- Schema migration preserves v0.2 CEO/CTO/Owner identities and projects while introducing CPO, Signals, Bets, Board decisions, promotions, releases, and schema version 3.
- A failed promotion resumes only when the existing project matches the same selected Bet and Board decision.
- Installation rollback uses the previous wheel and plugin install. Company truth remains in versioned files and does not depend on keeping v1.0.0 installed.

## Remaining limitations

- OneCo v1.0.0 supports macOS with Python 3.11 only. Python 3.12+ remains blocked until the native Cockpit stack is independently verified there.
- Exact Trae provider-thread bindings are SQLite coordination state. Window/process loss exact-resumes; total SQLite loss preserves durable company truth but requires role sessions to be re-established.
- Daily Product Teardown remains an optional external Signal source, never a bundled CPO capability or automatic step.

## Release state

- Version: `1.0.0`
- Runtime/company schema: `3`
- Default model: `GPT-5.6-Sol`
- Default permission: `bypass_permissions` (Full Access)
- Supported interpreter: `>=3.11,<3.12`
- Repository state: intentionally uncommitted for human review; QA made no commit or push
