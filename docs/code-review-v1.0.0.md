# OneCo v1.0.0 code-review closeout

Date: 2026-09-29  
Review result: **38 confirmed findings fixed; all dedicated regressions pass**

Five independent review groups examined messaging/runtime, lifecycle/recovery, process/session orchestration, CLI/MCP/plugin integration, and Cockpit/contracts. A final cross-boundary review then covered packaging, native Python compatibility, exact macOS Space placement, and SQLite-loss behavior. Severity at confirmation was 1 P0, 32 P1, and 5 P2.

| # | Severity | Confirmed defect | Resolution |
| --- | --- | --- | --- |
| 1 | P1 | Successful succession left the promoted session classified as a candidate | Promotion atomically changes identity kind to persistent role and capability checks pass |
| 2 | P1 | One acknowledgement consumed an `ALL` message for every recipient | Broadcast acknowledgements are stored per message and actor |
| 3 | P1 | Role-turn leases survived takeover and fenced the replacement | Superseded leases are removed or reclaimed after current-session validation |
| 4 | P1 | Message insertion did not atomically validate sender session/epoch | Sender identity, lifecycle, active session, and epoch are checked inside the write transaction |
| 5 | P2 | Inbox ordering ignored thread sequence | Inbox uses stable thread/sequence ordering |
| 6 | P1 | Crafted Signal IDs could escape the Signal root | Canonical ID and containment validation now occurs before every read/write |
| 7 | P1 | Release success trusted stale approval/runtime state | Result recording reloads and revalidates candidate, decision, and exact digest |
| 8 | P1 | Acceptance evidence was not bound to the nominated product commit | Candidate records committed blob hashes and verifies working bytes against them |
| 9 | P1 | Interrupted Bet promotion could not be retried | Compatible partial promotion resumes and completes one existing project |
| 10 | P1 | Runtime rebuild trusted uncommitted authority records | Board/release records must be committed and byte-identical to `HEAD` |
| 11 | P1 | Concurrent executive opens could launch duplicate canonical processes | Per-company/per-role thread and filesystem locks serialize launch/focus |
| 12 | P1 | Supervisor timeout could unlink a live daemon socket and start a second daemon | Startup and daemon-lifetime `flock` locks protect socket ownership |
| 13 | P1 | Unrelated project records could be credited as an Owner outcome | Completion requires an outcome attributable to the exact Owner session/epoch |
| 14 | P1 | Same-named companies could collide in the same tmux Owner yard | Yard names include a stable company-root hash |
| 15 | P1 | A transient Trae crash permanently failed Owner work | Exact work is requeued with bounded retries; exhaustion remains terminal |
| 16 | P0 | Public CLI session commands let an agent mint canonical authority | Agent-marked environments are denied the human control plane and must use fenced MCP tools |
| 17 | P1 | MCP advertised arbitrary Bet evidence objects and unconstrained state strings | Evidence and constrained arguments now emit typed closed schemas/enums |
| 18 | P1 | Missing Trae crashed health collection | Missing host capability degrades explicitly and all expected health rows remain available |
| 19 | P1 | Plugin prompts referenced non-existent unqualified skills | Every cross-skill reference uses the `oneco:oneco-*` namespace |
| 20 | P2 | Empty same-name external skill files appeared compatible | Health requires matching identity, description, heading, and substantive body |
| 21 | P1 | Inline Cockpit handlers allowed stored script injection through IDs | Inline handlers were removed, IDs were constrained, and delegated data-action events are escaped |
| 22 | P1 | Cockpit offered Board approval for stale release evidence | Snapshot revalidates current evidence before displaying state or actions |
| 23 | P2 | Resolved technical reviews remained in Board attention | Attention collapses by project/scope and uses only the latest verdict |
| 24 | P2 | Superseded messages remained visible and unread | Current message views and unread counts exclude stale versions |
| 25 | P1 | Cockpit could create provenance-free Signals | Board ideas receive durable `board://` provenance; local artifacts are captured and hashed |
| 26 | P1 | Runtime rebuild insertion order could make an older release appear current | Release order derives from the durable candidate timestamp with deterministic ID tie-breaking |
| 27 | P1 | Historical stale releases remained in Board attention after a later terminal release | Only the latest release per project contributes release attention |
| 28 | P1 | A resolved blocker had no durable resolution record and revived after SQLite loss | Owner writes an immutable resolution tied to a later evidence-backed checkpoint; rebuild overlays it deterministically |
| 29 | P1 | Managed roles repeatedly stopped for routine tool approvals | CPO, CEO, CTO, delegates, and Owner turns default to `bypass_permissions`, translated to Trae's supported Full Access flag |
| 30 | P1 | Noninteractive Trae execution received the unsupported `--no-daemon` argument | Noninteractive launch and exact resume omit that flag |
| 31 | P1 | Owner work could inherit or collide with unrelated sessions on the default tmux server | Every company uses a root-hashed private tmux server and watcher target |
| 32 | P2 | A background Cockpit process could exit after starting the supervisor | Native Cockpit lifetime is retained independently; supervisor start remains duplicate-safe |
| 33 | P1 | Launch recovery used an ambiguous `launch-*` filename match and could select an endorsement or older record | Recovery loads committed HEAD data, requires a valid digest plus timezone-aware timestamp, and selects by timestamp with a stable tie-break |
| 34 | P1 | Package metadata claimed Python 3.12/3.13 support although the native Cockpit rendered blank there | v1 is constrained to `>=3.11,<3.12`; the repository and install instructions pin Python 3.11 |
| 35 | P1 | Cockpit placement could move or validate the wrong native window | Placement targets the exact pywebview `NSWindow.windowNumber()` before activation |
| 36 | P1 | `loaded` and `shown` callbacks could recapture different Spaces after activation changed focus | The invocation Space is captured once and reused by both native callbacks |
| 37 | P1 | `oneco doctor` accepted Python 3.12+ despite the verified v1 runtime range | Doctor now fails closed unless the interpreter major/minor is exactly Python 3.11 |
| 38 | P1 | After SQLite loss, the legacy readiness fallback treated stale digest-bound endorsements as current | Legacy booleans remain compatible, while v1 endorsement objects must match the current launch digest |

No confirmed finding was waived, hidden, or converted into a documentation-only exception. Durable regressions are distributed across `tests/test_release_hardening.py`, `tests/test_release_pressure.py`, `tests/test_v1_release_qa.py`, `tests/test_live_qa_regressions.py`, `tests/test_refresh_regressions.py`, `tests/test_records.py`, `tests/test_bridge_boundaries.py`, `tests/test_cli.py`, and `tests/test_package_metadata.py`.

