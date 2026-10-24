# Architecture

OneCo v1 keeps authority small and recovery boring.

```text
Signal → CPO evidence + Product Bet → human Board decision
                                      ↓ exact selected digest
                 CEO product shaping ↔ CTO technical shaping
                                      ↓ matching endorsements
                              one Owner / product
                                      ↓ verified increment
                    CEO + CTO release readiness → Board approval
```

## Source-of-truth hierarchy

1. Versioned Markdown and JSON are durable company truth: Signals, sources, immutable Bet revisions, Board decisions, promotions, project Specs, checkpoints, reviews, and release evidence.
2. Each product is an independent direct-child Git repository. The company root is a separate Git repository and ignores product directories.
3. `.oneco/runtime.sqlite` is disposable concurrency truth: current identities and epochs, provider-thread bindings, messages, acknowledgements, queues, leases, read indexes, and supervision timers.
4. A bound Trae thread provides conversational continuity, not corporate memory.
5. Cockpit is a read model and navigation surface, not another project-management database.

Formal records use atomic replacement and narrow local Git commits. OneCo never pushes automatically. `oneco runtime rebuild` recreates derivable runtime state from the versioned artifacts.

## Identity and authority

CPO, CEO, and CTO are persistent logical identities with one current session, an exact Trae thread UUID, and a monotonically increasing epoch. CPO uses `advisory` authority; CEO and CTO use `canonical` authority. Availability of an MCP tool does not grant permission.

Every privileged MCP call resolves its session ID in SQLite and checks the current epoch. A replaced process cannot write or message as the current role. Research delegates and Secretaries inherit conversation context but have `delegate` identity and may report only to their parent. Recovery candidates have no role authority until the Board enters the one-time phrase in that exact candidate thread.

## Runtime processes

- Ghostty hosts one exact-resume window for each persistent CPO, CEO, and CTO role.
- Hidden tmux panes run atomic Owner turns, one canonical writer per product.
- The background supervisor wakes queued Owners and creates CTO review notifications. It never injects keystrokes or starts a second writer.
- Cockpit is a native pywebview window. Closing it leaves supervision running; `oneco stop` stops the supervisor.
- Trae hooks are best-effort observations. They never create authority, approvals, checkpoints, or completion.

## Product and release integrity

A Board selection binds the digest of one submitted Product Bet revision. CEO promotion copies that exact packet, manifest, and Board decision into the new product repository. A newer Bet revision makes the old selection ineligible for promotion until the Board selects again.

The launch digest covers the Brief, project agent contract, constitution, and current Spec or seed. Any content change invalidates both CEO and CTO endorsements. The Board waiver is narrow, recent, provenance-checked, one-use, and valid only when exactly one peer endorsement is missing; mandatory artifacts cannot be waived.

A release candidate binds an exact product commit and hashes of its acceptance-evidence files. CEO and CTO separately endorse that release digest. Only the human Board can approve production, after which exactly one Owner release work item is queued. The Owner records the production result and evidence separately.

## Recovery

- Closed/lost role window: reopen the same logical session and exact Trae thread.
- Dead Owner pane: requeue claimed work, preserve its provider thread, and resume at the next atomic boundary.
- Replaced session returns: epoch fencing rejects it.
- SQLite loss: recreate schema 3 and run `oneco runtime rebuild`; Git-backed Signal, Bet, Board, project, review, and release truth survives.
- Chat-context loss: re-read the company snapshot and referenced artifacts.
- External research skill missing or incompatible: health reports it explicitly; CPO does not silently substitute a method.

No embeddings, vector database, knowledge graph, or hidden long-term agent memory is required. Bounded indexes and summaries are rebuildable views over ordinary files.
