# Contributing

OneCo is intentionally small. A useful change should make the core workflow more reliable, easier to understand, or easier to move between machines without turning OneCo into a general-purpose management platform.

## Before changing code

1. Open an issue or describe the concrete user problem in the pull request.
2. Keep company truth in ordinary files; use SQLite only for runtime coordination.
3. Preserve the single-writer rule for product Owners and the separate authority of Board, CEO, CTO, and Owner.
4. Keep host-specific behavior behind an adapter when practical.

## Local checks

```bash
uv sync --group dev
uv run ruff check .
uv run pytest
uv build
traecli plugin validate --path adapters/trae-plugin/oneco
```

If your change affects installation or first use, verify it from a clean clone. Do not commit company runtime databases, terminal handles, local paths, credentials, or personal project data.

## Pull requests

Keep the description short and concrete: the problem, the chosen behavior, the evidence that it works, and any remaining limitation. Documentation and tests should change with the behavior they describe.
