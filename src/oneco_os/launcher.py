"""Finder launcher generation for a specific OneCo company."""

from __future__ import annotations

import plistlib
import re
import shlex
import stat
import sys
from pathlib import Path

from .errors import WorkspaceError
from .util import slugify
from .workspace import Workspace


def create_launcher(workspace: Workspace, destination: Path | None = None) -> Path:
    applications = destination or (Path.home() / "Applications")
    applications.mkdir(parents=True, exist_ok=True)
    company_name = workspace.manifest().company_name
    safe_name = re.sub(r"[/\\:]", "-", company_name).strip() or "Company"
    app = applications / f"OneCo — {safe_name}.app"
    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    bundle_id = "local.oneco." + slugify(company_name).replace("-", ".")
    plist = {
        "CFBundleDisplayName": f"OneCo — {company_name}",
        "CFBundleExecutable": "oneco-launch",
        "CFBundleIdentifier": bundle_id,
        "CFBundleName": "OneCo Bridge",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.2.0",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
    }
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(plist, handle)
    executable = macos / "oneco-launch"
    source = (
        "#!/bin/sh\n"
        + "exec "
        + shlex.quote(sys.executable)
        + " -m oneco_os.cli start --root "
        + shlex.quote(str(workspace.root))
        + "\n"
    )
    executable.write_text(source, encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if not executable.exists():
        raise WorkspaceError("failed to create Finder launcher")
    return app
