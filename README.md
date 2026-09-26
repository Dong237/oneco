# OneCo

[中文说明](README.zh-CN.md)

OneCo is a local operating layer for running a one-person, AI-native product company from a Mac. You act as the Board. A persistent CEO and CTO help set product direction and technical boundaries. Each product has one Owner, who takes agreed work through implementation and verification.

OneCo is not a hosted service. Company decisions, product briefs, specifications, and code remain in ordinary files and Git repositories on your Mac, not in a proprietary database. SQLite is used only for local coordination.

> **Current status:** v0.2 is an early macOS release built around TraeCode, Ghostty, and tmux. Read the [requirements](#requirements) before installing.

## How it works

```text
You (the Board)
  ├─ talk with CEO about users, scope, and priorities
  └─ talk with CTO about architecture, quality, and risk
          │
          ├─ both shape and endorse a product launch packet
          ▼
     one Owner works in that product repository
          │
          └─ tests, checkpoints, decisions, and evidence stay on disk
```

- **Board:** you. You set the company direction and resolve important trade-offs.
- **CEO:** keeps product work aligned with user intent, scope, sequence, and momentum.
- **CTO:** keeps the architecture sound and the implementation correct, safe, and maintainable.
- **Owner:** the only role with canonical write authority for one product. It executes the accepted spec, verifies the result, records a checkpoint, and moves on to the next compatible task.
- **Cockpit:** a small native view of company state, not a separate project-management system.

The CEO and CTO each run in a dedicated Ghostty window. Owners work in a hidden tmux session, so closing a window does not stop their work. The bundled Trae plugin provides the role instructions and authorized OneCo tools. Runtime identity determines what each role may do.

## Requirements

- macOS 13 or newer
- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Git
- tmux
- Ghostty 1.3 or newer
- TraeCode CLI, signed in and able to use the configured model (default: `GPT-5.6-Sol`)

OneCo currently supports macOS only because the Cockpit and executive-window integration rely on native macOS APIs and Ghostty automation.

## Security

OneCo launches local agents with the permissions already granted to your TraeCode setup. Review those permissions before use, and run OneCo only in repositories you trust.

## Install

```bash
git clone https://github.com/Dong237/oneco.git
cd oneco
uv tool install .
oneco --version
```

To work on OneCo locally instead of installing it as a tool:

```bash
uv sync --group dev
uv run oneco --version
```

## Create your company

```bash
oneco create ~/my-company --name "My Company"
```

If your TraeCode account does not offer the default model, choose one it does provide:

```bash
oneco create ~/my-company --name "My Company" --model "MODEL_NAME"
```

OneCo will report an unavailable model instead of silently choosing another one.

This checks the configured Trae model, installs the bundled OneCo plugin locally, creates the company repository, initializes Git, and adds `OneCo — My Company.app` to `~/Applications`.

Check the setup:

```bash
oneco doctor --root ~/my-company
```

Then start OneCo from Finder or the terminal:

```bash
oneco start --root ~/my-company
```

## Typical workflow

1. Tell the CEO what you want to build, for whom, and what useful result should come first.
2. Ask the CTO for the simplest technically sound approach and a clear quality bar.
3. Have the CEO and CTO finish the product brief, first spec, constitution, acceptance evidence, and explicit non-goals.
4. Once both endorse the same launch packet, ask them to launch the product.
5. The Owner then executes the work. Follow progress in the Cockpit; send product corrections through the CEO and technical corrections through the CTO.
6. Check company state at any time:

```bash
oneco status --root ~/my-company
```

In normal use, you do not need to manage tmux, sessions, queues, or internal messages yourself. Those commands are available for inspection, automation, and recovery.

## Useful commands

| Command | Purpose |
| --- | --- |
| `oneco start --root ~/my-company` | Open or focus the Cockpit, CEO, and CTO |
| `oneco status --root ~/my-company` | Show projects, identities, unread messages, and pending actions |
| `oneco talk CEO --root ~/my-company` | Focus or reopen the exact CEO conversation |
| `oneco talk CTO --root ~/my-company` | Focus or reopen the exact CTO conversation |
| `oneco branch CEO --purpose "pricing" --root ~/my-company` | Fork an advisory executive conversation without changing who holds canonical authority |
| `oneco doctor --root ~/my-company` | Check the host, plugin, model, and session environment |
| `oneco runtime rebuild --root ~/my-company` | Rebuild disposable runtime state from versioned artifacts |

Run `oneco --help` or `oneco COMMAND --help` for the complete command reference.

## Moving a company between Macs

A OneCo company consists of a control repository with independent product repositories directly inside it:

```text
my-company/
├── .oneco/                  local manifest and disposable runtime
├── COMPANY.md               charter and working principles
├── roles/                   CEO, CTO, and Owner boundaries
├── portfolio/               project registry, decisions, approvals
├── playbook/                demonstrated reusable practices
├── first-product/           independent Git repository
└── second-product/          independent Git repository
```

Commit the company control files and each product repository. Do not commit `.oneco/runtime.sqlite`, sockets, logs, or backups. To move to another Mac, clone the same repositories and run `oneco runtime rebuild`. The Markdown, JSON, and Git history are the durable record.

## Documentation

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
