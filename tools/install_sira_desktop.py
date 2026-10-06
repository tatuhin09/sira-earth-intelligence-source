"""Install a per-user Ubuntu launcher for SIRA Desktop without root access."""
from __future__ import annotations

import argparse
from pathlib import Path
import os
import stat


LAUNCHER = """#!/usr/bin/env bash
set -euo pipefail
ROOT={root}
cd "$ROOT/src"
exec "$ROOT/.venv/bin/python" -m sira.desktop_app \
  --root "$ROOT" \
  --host 127.0.0.1 \
  --port 8765 \
  --open
"""

DESKTOP = """[Desktop Entry]
Version=1.0
Type=Application
Name=SIRA
Comment=Self-Improving Research Agent
Exec={launcher}
Icon={icon}
Terminal=false
Categories=Development;Utility;
StartupNotify=true
StartupWMClass=SIRA
Keywords=AI;Research;Agent;SIRA;
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not (root / ".venv" / "bin" / "python").is_file():
        raise SystemExit("SIRA virtual environment Python was not found")
    if not (root / "src" / "sira" / "desktop_app.py").is_file():
        raise SystemExit("SIRA Desktop backend is not installed")

    home = Path.home()
    bin_dir = home / ".local" / "bin"
    app_dir = home / ".local" / "share" / "applications"
    bin_dir.mkdir(parents=True, exist_ok=True)
    app_dir.mkdir(parents=True, exist_ok=True)

    launcher = bin_dir / "sira-desktop"
    launcher.write_text(
        LAUNCHER.format(root=repr(str(root))),
        encoding="utf-8",
    )
    launcher.chmod(
        launcher.stat().st_mode
        | stat.S_IXUSR
        | stat.S_IXGRP
        | stat.S_IXOTH
    )

    desktop = app_dir / "sira.desktop"
    icon = root / "desktop" / "assets" / "sira.svg"
    if not icon.is_file() or icon.is_symlink():
        raise SystemExit("SIRA custom icon is missing")
    desktop.write_text(
        DESKTOP.format(launcher=str(launcher), icon=str(icon)),
        encoding="utf-8",
    )
    desktop.chmod(0o644)

    print(f"launcher={launcher}")
    print(f"desktop_entry={desktop}")
    print(f"icon={icon}")
    print("custom_logo_icon_stage=installed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
