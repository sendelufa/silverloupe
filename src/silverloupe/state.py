"""SQLite checkpoints and persisted session metadata."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from silverloupe.contracts import Asset, RunConfig, SessionConfig


def _now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            mode="w",
            encoding="utf-8",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.is_file():
            temporary.unlink()


def invalidate_report_generation(output: Path, reason: str) -> None:
    """Mark an existing report stale before any upstream artifact changes."""

    marker_path = output.expanduser().resolve() / "report-generation.json"
    if not marker_path.is_file():
        return
    marker: dict[str, Any] = {}
    try:
        loaded = json.loads(marker_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            marker = loaded
    except (OSError, ValueError):
        pass
    marker.update(
        {
            "schema_version": 1,
            "status": "invalidated",
            "reason": reason,
        }
    )
    atomic_write_text(
        marker_path,
        json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


class CheckpointStore:
    def __init__(self, output: Path) -> None:
        output.mkdir(parents=True, exist_ok=True)
        self.output = output
        self.connection = sqlite3.connect(output / "state.sqlite3")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS asset_checkpoints (
                asset_id TEXT PRIMARY KEY,
                content_sha256 TEXT NOT NULL,
                cache_key TEXT,
                asset_json TEXT NOT NULL,
                preview_path TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(asset_checkpoints)")
        }
        if "cache_key" not in columns:
            self.connection.execute("ALTER TABLE asset_checkpoints ADD COLUMN cache_key TEXT")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS run_status (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                status TEXT NOT NULL,
                total INTEGER NOT NULL,
                completed INTEGER NOT NULL,
                error TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def set_status(self, status: str, total: int, completed: int, error: str | None = None) -> None:
        self.connection.execute(
            """
            INSERT INTO run_status(singleton, status, total, completed, error, updated_at)
            VALUES(1, ?, ?, ?, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                status=excluded.status,
                total=excluded.total,
                completed=excluded.completed,
                error=excluded.error,
                updated_at=excluded.updated_at
            """,
            (status, total, completed, error, _now()),
        )
        self.connection.commit()

    def cached(self, asset: Asset, cache_key: str) -> Asset | None:
        row = self.connection.execute(
            """
            SELECT content_sha256, cache_key, asset_json, preview_path
            FROM asset_checkpoints WHERE asset_id = ?
            """,
            (asset.asset_id,),
        ).fetchone()
        if row is None or row[0] != asset.content_sha256 or row[1] != cache_key:
            return None
        preview_path = row[3]
        if preview_path:
            relative = Path(preview_path)
            if relative.is_absolute() or ".." in relative.parts:
                return None
            candidate = self.output / relative
            if candidate.is_symlink() or not candidate.resolve().is_relative_to(self.output.resolve()):
                return None
            try:
                with Image.open(candidate) as image:
                    image.verify()
            except (Image.DecompressionBombError, OSError, UnidentifiedImageError):
                return None
        return Asset.model_validate_json(row[2])

    def record(self, asset: Asset, cache_key: str) -> None:
        provenance = asset.preview_provenance or {}
        self.connection.execute(
            """
            INSERT INTO asset_checkpoints(
                asset_id, content_sha256, cache_key, asset_json, preview_path, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                content_sha256=excluded.content_sha256,
                cache_key=excluded.cache_key,
                asset_json=excluded.asset_json,
                preview_path=excluded.preview_path,
                updated_at=excluded.updated_at
            """,
            (
                asset.asset_id,
                asset.content_sha256,
                cache_key,
                asset.model_dump_json(),
                provenance.get("path"),
                _now(),
            ),
        )
        self.connection.commit()


def read_status(output: Path) -> dict[str, Any]:
    database = output.expanduser().resolve() / "state.sqlite3"
    if not database.is_file():
        raise FileNotFoundError(f"checkpoint database does not exist: {database}")
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT status, total, completed, error, updated_at FROM run_status WHERE singleton = 1"
        ).fetchone()
    if row is None:
        return {"status": "failed", "total": 0, "completed": 0, "error": "run status is missing"}
    return {
        "status": row[0],
        "total": row[1],
        "completed": row[2],
        "error": row[3],
        "updated_at": row[4],
    }


def write_session(output: Path, config: RunConfig, session_id: str) -> SessionConfig:
    session_path = output / "session.json"
    created_at = _now()
    revision = 1
    if session_path.is_file():
        try:
            previous = SessionConfig.model_validate_json(session_path.read_text(encoding="utf-8"))
            created_at = previous.timestamps.get("created_at", created_at)
            revision = previous.revision + 1
        except (OSError, ValueError):
            pass
    session = SessionConfig(
        session_id=session_id,
        source_roots=config.source_roots,
        brief=config.brief,
        tool_versions={"silverloupe": "0.1.2"},
        rendering_policy=config.rendering_policy,
        runtime_mode=config.runtime_mode,
        budget=config.budget,
        timestamps={"created_at": created_at, "updated_at": _now()},
        revision=revision,
        editor_config_snapshot=config.editor_config_snapshot,
    )
    atomic_write_text(session_path, session.model_dump_json(indent=2) + "\n")
    return session


def load_run_config(output: Path) -> RunConfig:
    output = output.expanduser().resolve()
    session = SessionConfig.model_validate_json((output / "session.json").read_text(encoding="utf-8"))
    return RunConfig(
        source_roots=session.source_roots,
        output_root=str(output),
        brief=session.brief,
        runtime_mode=session.runtime_mode,
        rendering_policy=session.rendering_policy,
        budget=session.budget,
        editor_config_snapshot=session.editor_config_snapshot,
    )


def write_session_config(output: Path, session: SessionConfig) -> None:
    """Atomically replace the persisted session contract."""

    atomic_write_text(output / "session.json", session.model_dump_json(indent=2) + "\n")
