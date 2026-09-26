# Installation and first start

## Requirements

OneCo Bridge v0.2 targets macOS 13 or newer and requires:

- Python 3.11 or newer and `uv`
- Git
- tmux for hidden Owner workers
- Ghostty 1.3 or newer for native CEO, CTO, Secretary, and read-only Owner windows
- TraeCode CLI with the chosen model available

All normal Trae Skills, plugins, MCP servers, model access, and permission settings remain available. OneCo adds a plugin; it does not replace the normal Trae profile.

## Install from source or a wheel

```bash
git clone https://github.com/Dong237/oneco.git
cd oneco
uv tool install .
oneco --version
```

A built wheel is equally portable:

```bash
uv tool install ./oneco_os-0.2.0-py3-none-any.whl
```

## One-time company creation

The shortest complete path is:

```bash
oneco create ~/my-company --name "My Company"
```

With no arguments, the wizard asks only for the name and folder:

```bash
oneco create
```

The command first verifies Trae and the exact configured model, then installs the local OneCo plugin, creates the company, initializes Git, and creates `~/Applications/OneCo — My Company.app`. If host verification fails, it does not leave a half-created company. Use `--no-install-trae`, `--no-finder-launcher`, or `--no-git-init` only for controlled automation.

## Start and daily use

Double-click the generated Finder app, or run:

```bash
oneco start --root ~/my-company
```

Starting again focuses the existing Cockpit instead of creating another one. The Cockpit opens or focuses canonical CEO and CTO Ghostty windows. Closing a terminal does not destroy its Trae conversation; reopening resumes its exact UUID. Owners stay in a hidden tmux yard and are selected through the Cockpit's Watch button.

If the launcher was deleted or OneCo's interpreter moved after reinstalling, recreate it:

```bash
oneco create-launcher --root ~/my-company
```

## Diagnostics

```bash
oneco doctor --root ~/my-company
oneco status --root ~/my-company
traecli plugin validate oneco@local
```

If an executive says `OneCo tools are available only inside a launched OneCo agent`, run
`oneco doctor --root ~/my-company`. The `oneco-mcp-session-env` check must be `OK`.
If it fails after an upgrade, reinstall the local plugin with
`oneco host install-trae --yes`, then close and reopen the affected OneCo executive
window so Trae starts a new MCP subprocess with the repaired session environment.

Ghostty may ask for macOS Automation permission the first time OneCo opens or focuses a window. Grant the launching application permission to control Ghostty under System Settings → Privacy & Security → Automation.

## Upgrade and portability

```bash
git pull --ff-only
uv tool install . --force
oneco host install-trae --yes
oneco create-launcher --root ~/my-company
```

To move a company, install the same OneCo version on the new Mac, clone the company control repository and every registered child project at the same direct-child paths, then run:

```bash
oneco runtime rebuild --root /path/to/company
oneco host install-trae --yes
oneco create-launcher --root /path/to/company
oneco start --root /path/to/company
```

Do not treat `.oneco/runtime.sqlite` as portable company memory. It is local runtime coordination and is excluded from Git. Versioned Markdown/JSON, product repositories, and decision records are the portable truth. Opening a v0.1 company automatically backs up and migrates its manifest and runtime schema before use.
