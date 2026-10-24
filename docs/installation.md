# Installation and first start

## Requirements

OneCo v1 targets macOS 13 or newer and currently requires Python 3.11, `uv`, Git, tmux, Ghostty 1.3+, TraeCode CLI, and access to the configured model. Python 3.12+ is intentionally rejected because the native Cockpit does not render reliably there. OneCo adds a local Trae plugin; it does not replace the user's normal profile.

CPO research can use separately installed `agent-reach`, `analyzing-a-product-skill`, `analyzing-a-business-system`, and `proposing-a-product-bet`. They are external contracts, not bundled workflow logic. Missing capabilities are reported explicitly. Daily Product Teardown is optional and external.

## Install

```bash
git clone https://github.com/Dong237/oneco.git
cd oneco
uv tool install --python 3.11 . --force
oneco host install-trae --yes
oneco --version
```

A wheel works too:

```bash
uv tool install --python 3.11 ./oneco_os-1.0.0-py3-none-any.whl --force
```

## Create one company

```bash
oneco create ~/my-company --name "My Company"
```

Pass `--model MODEL_NAME` if the default is unavailable. OneCo verifies the exact model and never silently substitutes one. Creation writes the company/role contracts, Signal and Bet stores, supervision policy, runtime schema, initial local Git commit, and a Finder launcher.

## Start and stop

```bash
oneco start --root ~/my-company
```

This starts the independent supervisor, opens Cockpit, and opens or resumes CPO, CEO, and CTO in Ghostty. Starting again focuses the existing Cockpit. Closing Cockpit does not stop queued Owner work or supervision.

All role processes default to Trae Full Access (`bypass_permissions`), including resumed or forked executive sessions and hidden Owner turns. To use Trae's automatic permission decisions instead, start with `--permission auto`; the supervisor propagates that mode to Owner work.

```bash
oneco stop --root ~/my-company
```

Use `stop` when you want the background supervisor to end. Recreate a moved Finder launcher with `oneco create-launcher --root ~/my-company`.

## Diagnose

```bash
oneco doctor --root ~/my-company
oneco status --root ~/my-company
traecli plugin validate --path adapters/trae-plugin/oneco
```

If a role says `OneCo tools are available only inside a launched OneCo agent`, reinstall the plugin, close only the affected OneCo role window, and reopen it so Trae creates a new MCP subprocess with the session environment:

```bash
oneco host install-trae --yes
oneco talk CPO --root ~/my-company   # or CEO / CTO
```

Do not close unrelated terminal windows. Ghostty may request macOS Automation permission the first time.

## Upgrade and recovery

```bash
git pull --ff-only
uv tool install --python 3.11 . --force
oneco host install-trae --yes
oneco create-launcher --root ~/my-company
```

Opening a v0.2 company creates manifest/runtime backups, preserves existing CPO/CEO/CTO/Owner identities and projects, adds the v1 layout, and migrates SQLite to schema 3. v0.2 projects remain usable. New v1 projects must be promoted by CEO from an exact Board-selected Bet.

To move a company, clone the company control repository and each registered child product repository at the same direct-child paths, install the same OneCo version, then run:

```bash
oneco runtime rebuild --root /path/to/company
oneco host install-trae --yes
oneco create-launcher --root /path/to/company
oneco start --root /path/to/company
```

Do not copy `.oneco/runtime.sqlite` as company memory. Versioned artifacts and Git are durable truth; SQLite is rebuilt coordination state. OneCo never pushes Git commits automatically.
