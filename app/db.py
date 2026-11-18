"""SQLite 持久化层。

只持久化“已受理”的地图版本；计算任务是临时的，仅保留在内存中
（详见 jobs.py 与 README 的说明）。
所有写入在单个连接上串行化，并通过 WAL 提升并发读能力。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from . import config


class Database:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.path = Path(db_path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS maps (
                    version     TEXT PRIMARY KEY,
                    revision    INTEGER NOT NULL DEFAULT 1,
                    feature_cnt INTEGER NOT NULL,
                    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS map_features (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    version     TEXT NOT NULL REFERENCES maps(version) ON DELETE CASCADE,
                    township_id TEXT NOT NULL,
                    geom_json   TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_map_features_version
                    ON map_features(version);
                CREATE UNIQUE INDEX IF NOT EXISTS uq_map_features_version_township
                    ON map_features(version, township_id);
                """
            )

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def replace_map(
        self, version: str, features: list[tuple[str, dict]]
    ) -> int:
        """整版写入（同版重传 = 整版更正）。

        在单个事务内先删后插；任何一步失败都会回滚，旧版完整保留。
        返回写入后的版本修订号 revision。
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT revision FROM maps WHERE version = ?", (version,)
                ).fetchone()
                next_revision = (row["revision"] + 1) if row else 1

                self._conn.execute(
                    "INSERT INTO maps(version, revision, feature_cnt) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(version) DO UPDATE SET "
                    "revision = excluded.revision, "
                    "feature_cnt = excluded.feature_cnt, "
                    "updated_at = datetime('now')",
                    (version, next_revision, len(features)),
                )
                self._conn.execute(
                    "DELETE FROM map_features WHERE version = ?", (version,)
                )
                self._conn.executemany(
                    "INSERT INTO map_features(version, township_id, geom_json) "
                    "VALUES (?, ?, ?)",
                    [
                        (version, township_id, json.dumps(geom, ensure_ascii=False))
                        for township_id, geom in features
                    ],
                )
                self._conn.execute("COMMIT")
                return next_revision
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def get_revision(self, version: str) -> Optional[int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT revision FROM maps WHERE version = ?", (version,)
            ).fetchone()
            return int(row["revision"]) if row else None

    def get_features(self, version: str) -> Optional[list[tuple[str, dict]]]:
        """返回 [(township_id, geojson_geometry_dict), ...]；版本不存在返回 None。"""
        with self._lock:
            if self.get_revision(version) is None:
                return None
            rows = self._conn.execute(
                "SELECT township_id, geom_json FROM map_features "
                "WHERE version = ? ORDER BY id",
                (version,),
            ).fetchall()
            return [(r["township_id"], json.loads(r["geom_json"])) for r in rows]

    def list_versions(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, revision, feature_cnt, created_at, updated_at "
                "FROM maps ORDER BY version"
            ).fetchall()
            return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
