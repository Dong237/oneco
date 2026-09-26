# JSON Schemas

All schemas use JSON Schema draft 2020-12 and reject unknown properties.

| Schema | Durable artifact |
| --- | --- |
| `manifest.schema.json` | `.oneco/manifest.json` |
| `project.schema.json` | `<project>/project.json` |
| `checkpoint.schema.json` | `<project>/checkpoints/*.json` |
| `instruction.schema.json` | `<project>/instructions/*.json` |
| `action-request.schema.json` | `<project>/decisions/act_*.json` |

The action-request artifact includes its current `status` and may include durable approval records. Stable project IDs match `^PROJ-[0-9]{3,}$`.

Validate a file with any draft 2020-12 implementation, for example:

```bash
uv run jsonschema \
  -i templates/project/project.json \
  schemas/project.schema.json
```
