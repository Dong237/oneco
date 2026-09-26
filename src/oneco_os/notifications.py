"""Best-effort host notifications; durable inbox state remains authoritative."""

from __future__ import annotations

import platform
import re
import subprocess


def notify(title: str, body: str) -> bool:
    if platform.system() != "Darwin":
        return False
    compact_title = re.sub(r"\s+", " ", title).strip()[:80]
    compact_body = re.sub(r"\s+", " ", body).strip()[:240]
    script = "on run argv\n display notification (item 2 of argv) with title (item 1 of argv)\nend run"
    result = subprocess.run(
        ["osascript", "-", compact_title, compact_body],
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0
