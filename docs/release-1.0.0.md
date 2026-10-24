# OneCo 1.0.0

OneCo 1.0.0 delivers the complete local Signal-to-release workflow: Board assignment, CPO evidence and immutable Product Bets, exact Board selection, peer CEO/CTO shaping, one Owner per product, verified release candidates, and a separate Board production decision.

## Release closeout

- The distributed Cockpit matches the source UI, including persistent themes and accessible in-app forms.
- Cockpit uses a regular macOS window at normal level. Switching to another app places that app in front; Cockpit no longer uses a floating panel or joins every application's Space.
- Native Trae plugin registration and MCP session-environment passthrough are verified on TraeX 0.211.1.
- Agent-marked environments cannot enter the human CLI control plane to mint or launch canonical roles. Internal session-fenced host commands remain available.
- Concurrent approvals preserve committed evidence, including recovery when a SQLite write fails after the Git commit.
- Owner launches are serialized; startup failures and abandoned work recover without replacing the exact provider thread.
- Checkpoints resolve their own instructions. Legacy unattributed checkpoints require matching project, specification, task, and chronology.
- Release endorsement retries preserve Board-approved and terminal release states.
- Delayed form responses cannot close or overwrite a newer form. Manual health refresh survives concurrent polling, and rejected releases have an accurate delivery state.

## Supported environment

macOS 13 or newer, Python 3.11, uv, Git, tmux, Ghostty 1.3+, and TraeCode CLI with access to the configured model. Python 3.12+ remains intentionally unsupported. Defaults remain GPT-5.6-Sol and Full Access.

## Validation

Run the required local gates:

```bash
uv sync --locked --group dev
uv run ruff check .
uv run pytest
uv build
traecli plugin validate --path adapters/trae-plugin/oneco
```

The additional browser regression check uses an isolated headless Chrome profile and a synthetic bridge. It does not read or modify a company:

```bash
uv run --with playwright python tests/cockpit_release_check.py
```

Release verification also covers a clean wheel installation, the native Cockpit window properties, fenced MCP snapshot/inbox calls, production-result reconstruction after SQLite rebuild, source/package parity, and preservation of existing company files and product Git heads.
