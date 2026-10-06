"""Run directly from this checkout without installing any Python packages."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from sira.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
