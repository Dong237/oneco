# OneCo Spec Kit integration

This package provides a local `oneco-lean` preset and a safe project stager for
Spec Kit 1.0.3. Install the preset from the target project root:

```bash
specify preset add --dev /absolute/path/to/oneco-os/integrations/speckit/preset
```

Then stage a numeric feature without replacing existing project work:

```bash
/absolute/path/to/oneco-os/integrations/speckit/bin/stage-project \
  --project-dir /absolute/path/to/project \
  --feature-number 001 \
  --feature-slug first-vertical \
  --brief /absolute/path/to/project/BRIEF.md \
  --spec-id SPEC-001
```

The stager prints one JSON object. It validates every destination before writing,
accepts existing non-empty OneCo scaffold artifacts, preserves authored files,
and refuses numeric-prefix or active-feature conflicts with no writes.

There is intentionally no `bundle.yml`. Spec Kit 1.0.3 packages arbitrary files
in bundle archives but its bundle installer cannot install a preset embedded in
that archive. `compatibility.json` is the machine-readable installation contract.
