from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
import argparse
import json
from pathlib import Path
import sqlite3
from typing import Any
from urllib.parse import quote


CORE_COUNT_TABLES = ("memories", "occurrences", "transitions")


@dataclass(frozen=True)
class DatabaseHealth:
    path: str
    exists: bool
    size_bytes: int | None
    quick_check: str | None
    schema_version: int | None
    counts: dict[str, int | None]
    tables: tuple[str, ...]
    healthy: bool
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["tables"] = list(self.tables)
        value["errors"] = list(self.errors)
        return value


@dataclass(frozen=True)
class MemoryIntegrityReport:
    primary: DatabaseHealth
    backup: DatabaseHealth | None
    parity: bool | None
    schema_match: bool | None
    count_deltas: dict[str, int | None]
    migration_ready: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary.to_dict(),
            "backup": self.backup.to_dict() if self.backup else None,
            "parity": self.parity,
            "schema_match": self.schema_match,
            "count_deltas": dict(self.count_deltas),
            "migration_ready": self.migration_ready,
        }


def _readonly_connection(path: Path) -> sqlite3.Connection:
    # mode=ro guarantees this preflight cannot create or mutate the database.
    uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _read_schema_version(conn: sqlite3.Connection, tables: set[str]) -> int | None:
    if "metadata" not in tables:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = ? LIMIT 1",
            ("schema_version",),
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    if not row:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def inspect_database(path: str | Path) -> DatabaseHealth:
    db_path = Path(path).expanduser()
    counts: dict[str, int | None] = {name: None for name in CORE_COUNT_TABLES}

    if not db_path.is_file():
        return DatabaseHealth(
            path=str(db_path),
            exists=False,
            size_bytes=None,
            quick_check=None,
            schema_version=None,
            counts=counts,
            tables=(),
            healthy=False,
            errors=("database_missing",),
        )

    errors: list[str] = []
    quick_check: str | None = None
    schema_version: int | None = None
    tables: set[str] = set()

    try:
        with closing(_readonly_connection(db_path)) as conn:
            quick_rows = conn.execute("PRAGMA quick_check").fetchall()
            quick_values = [str(row[0]) for row in quick_rows]
            quick_check = "ok" if quick_values == ["ok"] else "; ".join(quick_values)
            if quick_check != "ok":
                errors.append("quick_check_failed")

            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            schema_version = _read_schema_version(conn, tables)

            for table in CORE_COUNT_TABLES:
                if table in tables:
                    row = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
                    counts[table] = int(row[0]) if row else 0
    except (sqlite3.DatabaseError, OSError) as exc:
        errors.append(f"database_error:{type(exc).__name__}")

    healthy = quick_check == "ok" and not errors

    return DatabaseHealth(
        path=str(db_path),
        exists=True,
        size_bytes=db_path.stat().st_size,
        quick_check=quick_check,
        schema_version=schema_version,
        counts=counts,
        tables=tuple(sorted(tables)),
        healthy=healthy,
        errors=tuple(errors),
    )


def build_integrity_report(
    primary_path: str | Path,
    backup_path: str | Path | None = None,
) -> MemoryIntegrityReport:
    primary = inspect_database(primary_path)
    backup = inspect_database(backup_path) if backup_path is not None else None

    if backup is None:
        return MemoryIntegrityReport(
            primary=primary,
            backup=None,
            parity=None,
            schema_match=None,
            count_deltas={name: None for name in CORE_COUNT_TABLES},
            migration_ready=primary.healthy,
        )

    schema_match = (
        primary.schema_version is not None
        and backup.schema_version is not None
        and primary.schema_version == backup.schema_version
    )

    count_deltas: dict[str, int | None] = {}
    for table in CORE_COUNT_TABLES:
        current = primary.counts.get(table)
        saved = backup.counts.get(table)
        count_deltas[table] = (
            current - saved
            if current is not None and saved is not None
            else None
        )

    comparable_counts = [
        delta for delta in count_deltas.values() if delta is not None
    ]
    counts_match = bool(comparable_counts) and all(
        delta == 0 for delta in comparable_counts
    )
    parity = (
        primary.healthy
        and backup.healthy
        and schema_match
        and counts_match
    )

    return MemoryIntegrityReport(
        primary=primary,
        backup=backup,
        parity=parity,
        schema_match=schema_match,
        count_deltas=count_deltas,
        migration_ready=bool(parity),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only SIRA memory SQLite integrity/migration preflight."
    )
    parser.add_argument("--db", required=True, help="Primary memory SQLite database.")
    parser.add_argument("--backup", help="Optional backup database to compare.")
    parser.add_argument(
        "--require-parity",
        action="store_true",
        help="Fail unless primary and backup schema/counts match.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = build_integrity_report(args.db, args.backup)
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))

    if not report.primary.healthy:
        return 2
    if args.require_parity:
        if report.backup is None or not report.parity:
            return 3
    elif report.backup is not None and not report.backup.healthy:
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
