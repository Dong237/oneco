# Architecture

OneCo separates four concerns so the system remains visible, portable, and small.

```text
Human Board
  │ direct conversation
  ├── CEO in native Ghostty ─┐
  └── CTO in native Ghostty ─┤ OneCo MCP tools
                             ▼
                     local SQLite runtime
                  identity · inbox · queue · epoch
                             │
          Cockpit reads state┼queues wake hidden Owner workers
                             ▼
                 tmux Owner yard (one pane/project)
                             │ exact Trae exec resume
                             ▼
                 independent product repositories
```

## 1. Portable truth

The company root stores the charter, role boundaries, stable project registry, decisions, and playbook. Every product is an independent direct-child Git repository containing its Brief, Spec Kit artifacts, code, evidence, checkpoints, instructions, and handoffs. These ordinary files remain useful without Trae, Ghostty, tmux, or OneCo.

## 2. Local runtime

`.oneco/runtime.sqlite` uses WAL transactions for identities, epochs, provider thread UUIDs, terminal handles, messages, work queues, writer leases, launch-packet endorsements, human prompt provenance, succession, and capability health. It is the concurrency truth but not the long-term company memory. `oneco runtime rebuild` restores records derivable from versioned artifacts.

The Bridge is an ephemeral pywebview/Cocoa process, not a daemon or cloud service. A permission-restricted Unix socket only focuses the existing Cockpit. The UI reads state and opens terminals/folders; project shaping remains conversational.

## 3. Host adapters

- **Ghostty:** separate native CEO/CTO windows, titled advisory forks, and read-only Owner watch windows. AppleScript receives cwd and command as arguments rather than interpolated source.
- **Trae:** exact `resume UUID`, exact `fork UUID`, and noninteractive `exec resume UUID`; no ambiguous `--last`. New OneCo sessions inherit the user's normal Trae configuration plus OneCo's plugin.
- **tmux:** an unobtrusive transport and log surface for many Owner workers. It is not company truth and never carries injected instructions.
- **Spec Kit:** project development contracts (`spec.md`, `plan.md`, and `tasks.md`), not an orchestration runtime.

The Trae plugin packages four role/protocol Skills, best-effort lifecycle hooks, and a stdio MCP server. Tools are available to all launched OneCo sessions, but runtime-derived identity decides which operations are authorized. Tool availability never grants organizational authority.

## 4. Identity and recovery

Logical identity, OneCo session ID, Trae thread UUID, and terminal handle are distinct. A logical identity has one current session and a monotonically increasing epoch. Superseded sessions lose authority and Owner leases even if their process later returns.

Closing a CEO or CTO window marks the transport lost but preserves the exact provider thread. Reopening resumes it. A normal Secretary uses Trae fork and has advisory authority only. When the provider thread itself is broken, a recovery candidate requests a time-limited phrase such as `PROMOTE CEO 7K4N`; only the exact phrase typed by the human in that candidate thread transfers the canonical identity and fences the old session.

## 5. Owner serialization

Each project has one durable work queue and one canonical writer. A live Owner executes one atomic `traecli exec resume` turn. New CEO/CTO instructions enter SQLite and wait until that turn ends; no process types into the running terminal. The same wrapper then claims the next item. If its tmux pane dies, OneCo requeues claimed work and resumes the exact provider thread before retrying.

## 6. Launch integrity

Readiness hashes the Brief, Agent contract, constitution, and current Spec or initial seed. CEO and CTO endorsements bind to that digest. Editing any launch artifact invalidates both endorsements. A Board override is accepted only from a recent, recorded human prompt in the canonical initiating Trae thread and bypasses only a missing peer endorsement—never the mandatory artifacts.
