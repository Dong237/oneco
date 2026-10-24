# OneCo

[中文说明](README.zh-CN.md)

OneCo is a local-first operating system for a one-person, AI-native company. You are the Board. A persistent CPO, CEO, and CTO help turn an opportunity into an approved product, while one Owner per product writes and verifies the implementation.

OneCo's runtime and company records stay on your Mac. Signals, Product Bets, decisions, specs, checkpoints, evidence, and code live in ordinary files and Git repositories. SQLite holds local coordination state; OneCo does not run a hosted control service.

> **Current status:** v1.0 implements the full Signal-to-release workflow on macOS. Read the [requirements](#requirements) before installing.

## How it works

```text
Human Board
  │
  ├─ assigns a traceable Signal
  ▼
CPO investigates and submits a Product Bet
  │
  ├─ Board selects, rejects, or asks for more evidence
  ▼
CEO promotes the exact selected Bet into a product repository
  │
  ├─ CEO and CTO shape the launch packet
  ├─ both endorse the same launch-packet digest
  ▼
Owner builds and verifies; CTO supervises key gates
  │
  ├─ Owner records an exact release candidate
  ├─ CEO and CTO endorse the same release digest
  ▼
Board approves or rejects production
  │
  └─ Owner executes an approved release and records the result
```

| Role | Responsibility |
| --- | --- |
| **Board** | You. Assign Signals, decide Product Bets, approve production, and resolve material conflicts. |
| **CPO** | Investigates only Board-assigned Signals and prepares evidence-backed Product Bets. It is advisory and cannot create projects or direct Owners. |
| **CEO** | Promotes selected Bets and owns product intent, scope, sequence, and acceptance. |
| **CTO** | Shapes the technical path and supervises architecture, correctness, safety, and maintainability. |
| **Owner** | The canonical writer for one product. It executes accepted work, verifies it, and records checkpoints and release evidence. |
| **Cockpit** | The Board's native view of roles, Signals, Bets, products, health, and decisions that need attention. |

**Daily Product Teardown is optional and external.** It is not built into OneCo and is not a CPO step. If you already use it, a reviewed output can be attached to a Signal as a traceable source. OneCo does not schedule or invoke it.

## Requirements

- macOS 13 or newer
- Python 3.11 (Python 3.12+ is not currently supported because the native Cockpit does not render reliably there)
- [`uv`](https://docs.astral.sh/uv/)
- Git
- tmux
- Ghostty 1.3 or newer
- TraeCode CLI, signed in and able to use the configured model (default: `GPT-5.6-Sol`)

The optional Spec Kit commands also need the `specify` CLI. OneCo currently supports macOS only because the Cockpit and role windows use native macOS APIs and Ghostty automation. macOS may ask for permission to control Ghostty on first use.

## Security

OneCo defaults every CPO, CEO, CTO, and Owner launch, resume, fork, and turn to Trae Full Access (`bypass_permissions`) so routine work does not pause for tool approval. Run OneCo only in repositories you trust. To use Trae's automatic permission decisions instead, pass `--permission auto` when starting or opening a role; the supervisor carries that mode into Owner turns.

## Install

```bash
git clone https://github.com/Dong237/oneco.git
cd oneco
uv tool install --python 3.11 .
oneco --version
```

To work on OneCo locally instead:

```bash
uv sync --group dev
uv run oneco --version
```

## Create and start a company

```bash
oneco create ~/my-company --name "My Company"
```

If the default model is unavailable in your TraeCode account, choose one the account provides:

```bash
oneco create ~/my-company --name "My Company" --model "MODEL_NAME"
```

OneCo checks the exact model before writing company state and never substitutes another model silently. The command installs the local OneCo plugin, creates the company control repository, initializes Git, and adds `OneCo — My Company.app` to `~/Applications`.

Check the setup, then start:

```bash
oneco doctor --root ~/my-company
oneco start --root ~/my-company
```

You can also double-click the generated app in Finder. Starting again focuses the existing Cockpit instead of opening a duplicate.

## Cockpit

The Cockpit is the normal Board interface. It shows:

- CPO, CEO, and CTO status, with buttons to focus or resume each conversation;
- assigned Signals, Product Bets, and their current decision state;
- Board actions to select a Bet, reject it, or request named evidence;
- selected products, current work, Owner state, and a read-only **Watch** view;
- system health, blockers, and release candidates awaiting a production decision.

It is a control surface, not a second project tracker. Product shaping happens in the role conversations, and implementation stays in the product repository.

## The v1.0 workflow

### 1. Assign a Signal

Start with an observation, problem, or opportunity that has a traceable source. Click **Assign signal** in the Cockpit, or use:

```bash
oneco signal add \
  --title "Release confidence" \
  --summary "Independent developers need a reliable release gate" \
  --source-uri "https://example.com/source" \
  --root ~/my-company
```

The Board owns this assignment. The CPO does not start unassigned research.

### 2. Let the CPO prepare a Product Bet

The CPO investigates the assigned Signal and records the user and job, alternatives, dated sources, counterevidence, unknowns, product opportunity, business hypothesis, validation result when applicable, and a recommendation with its confidence limits.

When ready, the CPO submits an immutable Product Bet revision. Validation is evidence, not approval.

### 3. Make the Board decision

In the Cockpit, the Board chooses one of three actions for the exact submitted revision and digest:

- **Select** sends the Bet to the CEO for promotion.
- **More evidence** names the missing evidence; the CPO chooses the research method and submits a new revision.
- **Reject** keeps the decision and evidence on disk but creates no project.

Selecting a Bet does not create a project by itself. In v1.0, every project starts with CEO promotion of a Board-selected Bet.

### 4. Promote and shape the product

The CEO promotes the exact Board-selected Bet into an independent product repository. The selected packet, its manifest, and the Board decision travel with the project.

The CEO writes the product framing, scope, acceptance examples, sequence, and non-goals. The CTO adds the constitution overlay, technical constraints, and quality floor. Both then review and endorse the same launch-packet digest. Editing a launch artifact invalidates both endorsements.

Once both endorsements are current, either executive can launch the Owner.

### 5. Execute under supervision

The Owner is the product's only canonical writer. It takes one accepted task at a time, implements the smallest useful result, verifies it, records a checkpoint, reads queued instructions, and continues with compatible work.

The CEO handles product drift. The CTO reviews active work on the supervision interval and at key correctness, safety, architecture, and release gates. Corrections wait until the Owner's current turn ends; OneCo does not type into a running agent. The CTO can stop affected unsafe work, not the whole product.

### 6. Approve production

When an increment meets its acceptance criteria, the Owner records a release candidate tied to an exact Git commit and evidence files. The CEO reviews product acceptance; the CTO reviews the technical evidence. Each endorsement is bound to the same release digest.

Only after both endorsements does the release appear for Board attention. The Board approves or rejects that exact candidate in the Cockpit. Approval queues one production task for the Owner, which executes it and records evidence of the result.

OneCo does not automatically push any repository. You remain responsible for Git remotes and pushes.

## Sessions, stop, and recovery

CPO, CEO, and CTO each have one persistent Trae conversation in a dedicated Ghostty window. Closing a window does not discard its thread; focusing that role again resumes the exact saved UUID. Owner workers also resume their exact threads rather than guessing from the latest session.

Closing the Cockpit does not stop background supervision. Stop it explicitly when needed:

```bash
oneco stop --root ~/my-company
```

Start the company again with `oneco start --root ~/my-company`.

Recovery uses the versioned records on disk:

- If an Owner pane dies, claimed work is requeued and the exact Owner thread resumes.
- If the local runtime is lost, run `oneco runtime rebuild --root ~/my-company`, then start OneCo again.
- If the Finder launcher is missing, run `oneco create-launcher --root ~/my-company`.
- If a CPO, CEO, or CTO provider thread cannot resume, create a recovery candidate with `oneco branch ROLE --purpose "recover ROLE" --recovery-candidate --root ~/my-company`. Succession requires the Board's exact, time-limited confirmation phrase inside that candidate conversation.

## Useful commands

| Command | Purpose |
| --- | --- |
| `oneco start --root ~/my-company` | Start supervision and open or focus the Cockpit and persistent roles |
| `oneco stop --root ~/my-company` | Stop background supervision |
| `oneco status --root ~/my-company` | Show roles, products, unread messages, work, and pending actions |
| `oneco talk CPO --root ~/my-company` | Focus or exact-resume a persistent role; `CEO` and `CTO` work the same way |
| `oneco branch CEO --purpose "pricing" --root ~/my-company` | Fork an advisory conversation without changing canonical authority |
| `oneco doctor --root ~/my-company` | Check the host, plugin, model, role sessions, and runtime environment |
| `oneco runtime rebuild --root ~/my-company` | Rebuild derivable runtime state from versioned records |

Run `oneco --help` or `oneco COMMAND --help` for the full command reference.

## Files and Git

A company is a control repository with independent product repositories as direct children:

```text
my-company/
├── .oneco/                  local manifest and disposable runtime
├── COMPANY.md               charter and operating principles
├── roles/                   CPO, CEO, CTO, and Owner boundaries
├── signals/                 Board assignments and source manifests
├── bets/                    versioned Product Bets and Board decisions
├── portfolio/               selected product registry and approvals
├── operations/              supervision and release records
├── playbook/                demonstrated reusable practices
└── first-product/           independent Git repository
```

Commit the company control files and each product repository. Do not commit `.oneco/runtime.sqlite`, sockets, logs, or backups. On another Mac, clone the same repositories at the same direct-child paths and run `oneco runtime rebuild`. The Markdown, JSON, evidence files, and Git history are the portable record.

## Documentation

- [Version 1.0.0 release notes](docs/release-1.0.0.md)
- [Installation and upgrades](docs/installation.md)
- [Daily Board operations](docs/board-operations.md)
- [Architecture](docs/architecture.md)
- [Operating protocol](docs/protocol.md)
- [Spec Kit integration](integrations/speckit/README.md)

## Development

```bash
uv sync --group dev
uv run ruff check .
uv run pytest
uv build
traecli plugin validate --path adapters/trae-plugin/oneco
```

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a change.

## License

[MIT](LICENSE)
