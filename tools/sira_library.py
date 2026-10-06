#!/usr/bin/env python3
"""Local Library v1: owner tool for a large read-only document library.

    python tools/sira_library.py init --path /mnt/BIGDISK/sira_library
    python tools/sira_library.py set-trust python-docs official_documentation
    python tools/sira_library.py sync [--max-files 500] [--seconds 600]   # scan + index (resumable)
    python tools/sira_library.py status
    python tools/sira_library.py search "pytest fixtures" [--collection c] [--limit 8]
    python tools/sira_library.py verify [--sample 25]

Layout: <library>/collections/<collection>/<source>/files...   (.txt .md .rst .html .htm .pdf)
A <source> is one independent origin; two different sources can corroborate a claim.
Library text is data, never instructions. Nothing here is verified knowledge by itself.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira import library  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--path", type=Path, required=True)
    trust = commands.add_parser("set-trust")
    trust.add_argument("collection")
    trust.add_argument("trust", choices=library.TRUST_CLASSES)
    trust.add_argument("--description", default="")
    sync = commands.add_parser("sync")
    sync.add_argument("--max-files", type=int, default=500)
    sync.add_argument("--seconds", type=float, default=600.0)
    commands.add_parser("status")
    find = commands.add_parser("search")
    find.add_argument("query")
    find.add_argument("--collection")
    find.add_argument("--limit", type=int, default=8)
    find.add_argument("--evidence-only", action="store_true")
    verify = commands.add_parser("verify")
    verify.add_argument("--sample", type=int, default=25)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "init":
            output = library.init_library(root, args.path)
            output = {"library_root": str(output["library_root"]),
                      "next": "copy documents into collections/<collection>/<source>/ then run sync"}
        elif args.command == "set-trust":
            output = library.set_trust(root, args.collection, args.trust, args.description)
        elif args.command == "sync":
            output = {"scan": library.scan(root),
                      "index": library.index(root, max_files=args.max_files, seconds=args.seconds)}
        elif args.command == "status":
            output = library.status(root)
        elif args.command == "verify":
            output = library.verify_sample(root, sample=args.sample)
        else:
            output = [{**row, "text": row["text"][:300]} for row in library.search(
                root, args.query, limit=args.limit, collection=args.collection,
                evidence_only=args.evidence_only)]
    except library.LibraryError as exc:
        print(f"Library error: {exc}")
        return 2
    print(json.dumps(output, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
