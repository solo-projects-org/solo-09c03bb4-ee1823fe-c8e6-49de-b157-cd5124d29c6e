"""SQLite 持久化层。

只持久化“已受理”的地图版本；计算任务是临时的，仅保留在内存中
（详见 jobs.py 与 README 的说明）。
所有写入在单个连接上串行化，并通过 WAL 提升并发读能力。

大图层流式受理分两张表：
- ``map_staging``：解析期间逐要素暂存，对其他版本的查询与发布完全
  不可见；(staging_id, township_id) 唯一约束在入库瞬间即拦截重复乡镇号；
- ``maps`` / ``map_features``：仅在整版文件读完且暂存完整后，于单事务
  内从暂存表切换发布，失败回滚，旧版与修订号原样保留。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Optional

from . import config


class StagingDuplicateError(Exception):
    """暂存时命中 (staging_id, township_id) 唯一约束：整版内乡镇号重复。"""

    def __init__(self, township_id: str) -> None:
        super().__init__(f"乡镇号重复：{township_id}")
        self.township_id = township_id


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

                CREATE TABLE IF NOT EXISTS map_staging (
                    staging_id  TEXT NOT NULL,
                    feature_idx INTEGER NOT NULL,
                    township_id TEXT NOT NULL,
                    geom_json   TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_map_staging_id
                    ON map_staging(staging_id);
                CREATE UNIQUE INDEX IF NOT EXISTS uq_map_staging_township
                    ON map_staging(staging_id, township_id);
                """
            )
            # 上次进程异常退出可能留下未发布的暂存数据；发布是原子的，
            # 这些残留从未对外可见，启动时直接清理。
            self._conn.execute("DELETE FROM map_staging")

    # ------------------------------------------------------------------
    # 流式暂存（解析期；与已发布地图完全隔离）
    # ------------------------------------------------------------------
    # 微批大小：暂存按批提交，既保持“内存只与单批要素相关”的有界性，
    # 又避免大图层逐要素一次事务的 fsync 放大；重复约束在批次提交时触发。
    STAGE_BATCH_SIZE = 200

    def new_staging_id(self) -> str:
        return uuid.uuid4().hex

    def stage_features(
        self, staging_id: str, features: list[tuple[int, str, dict]]
    ) -> None:
        """暂存一批已校验要素 ``[(index, township_id, geom_dict), ...]``。

        批次包在 SAVEPOINT 中：成功则随连接自动提交，失败仅回滚本批，
        此前已暂存的要素保留，既能精确定位重复乡镇号，又保证
        maps/map_features（已发布数据）始终不被触碰。
        """
        if not features:
            return
        rows = [
            (staging_id, index, township_id, json.dumps(geom, ensure_ascii=False))
            for index, township_id, geom in features
        ]
        with self._lock:
            try:
                self._conn.execute("SAVEPOINT stage_batch")
                self._conn.executemany(
                    "INSERT INTO map_staging(staging_id, feature_idx, "
                    "township_id, geom_json) VALUES (?, ?, ?, ?)",
                    rows,
                )
                self._conn.execute("RELEASE SAVEPOINT stage_batch")
            except sqlite3.IntegrityError as exc:
                # 仅回滚本批；此前批次已落库，可据此找出真正的重复号
                self._conn.execute("ROLLBACK TO SAVEPOINT stage_batch")
                self._conn.execute("RELEASE SAVEPOINT stage_batch")
                for _, township_id, _ in features:
                    hit = self._conn.execute(
                        "SELECT 1 FROM map_staging WHERE staging_id = ? "
                        "AND township_id = ? LIMIT 1",
                        (staging_id, township_id),
                    ).fetchone()
                    if hit is not None:
                        raise StagingDuplicateError(township_id) from exc
                # 批内自重复（此前批次没有同名）：逐个比对找出重复号
                seen: set[str] = set()
                for _, township_id, _ in features:
                    if township_id in seen:
                        raise StagingDuplicateError(township_id) from exc
                    seen.add(township_id)
                raise StagingDuplicateError(features[0][1]) from exc

    def discard_staging(self, staging_id: str) -> None:
        """拒收/异常时清理本次暂存，尽量不留下痕迹（失败仅记录，不影响拒收语义）。"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM map_staging WHERE staging_id = ?", (staging_id,)
            )

    def publish_staging(self, version: str, staging_id: str) -> int:
        """把暂存区整版原子发布为 ``version`` 的最新修订。

        单事务内：统计暂存要素 -> 递增/新建 revision -> 删除旧要素
        -> 拷入暂存要素。任何一步失败全部回滚，旧版与修订号保持不变。
        返回发布后的修订号。
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                count_row = self._conn.execute(
                    "SELECT COUNT(*) AS cnt FROM map_staging WHERE staging_id = ?",
                    (staging_id,),
                ).fetchone()
                feature_cnt = int(count_row["cnt"])
                if feature_cnt == 0:
                    # 防御：空整版不允许发布（正常在解析阶段已被拒）
                    raise RuntimeError("暂存区为空，拒绝发布空整版")

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
                    (version, next_revision, feature_cnt),
                )
                self._conn.execute(
                    "DELETE FROM map_features WHERE version = ?", (version,)
                )
                self._conn.execute(
                    "INSERT INTO map_features(version, township_id, geom_json) "
                    "SELECT ?, township_id, geom_json FROM map_staging "
                    "WHERE staging_id = ? ORDER BY feature_idx",
                    (version, staging_id),
                )
                self._conn.execute(
                    "DELETE FROM map_staging WHERE staging_id = ?", (staging_id,)
                )
                self._conn.execute("COMMIT")
                return next_revision
            except Exception:
                # 回滚本身若再失败（如连接级异常），不掩盖原始错误
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
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
