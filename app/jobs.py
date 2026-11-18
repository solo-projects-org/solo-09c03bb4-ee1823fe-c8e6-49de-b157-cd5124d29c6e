"""交叠权重计算任务的生命周期管理。

关键一致性规则：
1. 提交任务时钉取两个源版本的 revision；
2. 计算在后台线程执行，期间 API 仍可受理地图更正；
3. 交付结果前再次比对当前 revision，任一源版变化则把任务标记为
   STALE 并拒绝交付，提示重新提交；
4. 任务仅存内存（计算结果是可从持久化地图重新派生的临时产物），
   已受理地图本身在 SQLite 中，重启不丢失。
"""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from . import config, geo
from .db import Database

# 任务状态
PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
STALE = "stale"


class JobError(Exception):
    """任务计算失败（如新版乡镇正面积交叠、源版缺失）。"""


@dataclass
class Job:
    id: str
    old_version: str
    new_version: str
    pinned_revisions: dict[str, int]
    state: str = PENDING
    submitted_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    error_detail: Any = None
    result: Optional[dict] = None

    def public(self) -> dict:
        """对外序列化（不含内部大对象以外的字段都可暴露）。"""
        data = {
            "job_id": self.id,
            "state": self.state,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "pinned_revisions": self.pinned_revisions,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        if self.error is not None:
            data["error"] = self.error
        if self.error_detail is not None:
            data["error_detail"] = self.error_detail
        if self.result is not None:
            data["result"] = self.result
        return data


class JobManager:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(
            max_workers=config.MAX_WORKERS,
            thread_name_prefix="weight-job",
        )

    # ------------------------------------------------------------------
    def submit(self, old_version: str, new_version: str) -> Job:
        """创建并提交计算任务；源版不存在立即报错。"""
        old_rev = self.db.get_revision(old_version)
        if old_rev is None:
            raise JobError(f"旧版地图不存在：{old_version}")
        new_rev = self.db.get_revision(new_version)
        if new_rev is None:
            raise JobError(f"新版地图不存在：{new_version}")

        job = Job(
            id=uuid.uuid4().hex,
            old_version=old_version,
            new_version=new_version,
            pinned_revisions={old_version: old_rev, new_version: new_rev},
        )
        with self._lock:
            self._jobs[job.id] = job
        self._pool.submit(self._run, job.id)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict]:
        with self._lock:
            return [j.public() for j in self._jobs.values()]

    # ------------------------------------------------------------------
    def _run(self, job_id: str) -> None:
        job = self.get(job_id)
        if job is None:
            return
        job.state = RUNNING
        job.started_at = datetime.now(timezone.utc).isoformat()
        try:
            self._compute(job)
            job.state = COMPLETED
        except JobError as exc:
            job.state = FAILED
            job.error = str(exc)
        except Exception as exc:  # 防御：任何意外都落为 failed，不拖垮线程池
            job.state = FAILED
            job.error = f"计算异常：{type(exc).__name__}: {exc}"
        finally:
            job.finished_at = datetime.now(timezone.utc).isoformat()

    def _compute(self, job: Job) -> None:
        # 1. 从持久层读取当前两个版本（计算期间允许被更正）
        old_stored = self.db.get_features(job.old_version)
        new_stored = self.db.get_features(job.new_version)
        if old_stored is None:
            raise JobError(f"旧版地图已不存在：{job.old_version}")
        if new_stored is None:
            raise JobError(f"新版地图已不存在：{job.new_version}")

        from shapely.geometry import shape

        old_geoms_wgs = [(tid, shape(g)) for tid, g in old_stored]
        new_geoms_wgs = [(tid, shape(g)) for tid, g in new_stored]

        # 2. 统一在同一等面积投影下计算，保证面积口径一致且单位为平方米
        all_geoms = [g for _, g in old_geoms_wgs] + [
            g for _, g in new_geoms_wgs
        ]
        equal_area_crs = geo.build_equal_area_crs(all_geoms)
        old_proj = geo.project_geoms(
            [g for _, g in old_geoms_wgs], equal_area_crs
        )
        new_proj = geo.project_geoms(
            [g for _, g in new_geoms_wgs], equal_area_crs
        )
        old_features = [
            (tid, pg) for (tid, _), pg in zip(old_geoms_wgs, old_proj)
        ]
        new_features = [
            (tid, pg) for (tid, _), pg in zip(new_geoms_wgs, new_proj)
        ]

        # 3. 新版内部互斥检查（等面积、平方米）：正面积交叠拒绝计算，
        #    避免同一地块在多个新乡镇中重复计权
        overlaps = geo.find_positive_overlaps(new_features)
        if overlaps:
            job.error_detail = {"new_version_overlaps": overlaps}
            raise JobError(
                f"新版 {job.new_version} 内乡镇之间存在 "
                f"{len(overlaps)} 处正面积交叠，拒绝计算（不能重复计权），"
                "请更正新版后重试"
            )

        weights = geo.compute_weights(old_features, new_features)

        import warnings

        with warnings.catch_warnings():
            # proj4 字符串仅作为 GIS 工具兼容字段，权威 CRS 以 WKT2 为准
            warnings.simplefilter("ignore", UserWarning)
            proj4 = equal_area_crs.to_proj4()

        job.result = {
            "old_version": job.old_version,
            "new_version": job.new_version,
            "crs": equal_area_crs.to_wkt(version="WKT2_2019"),
            "crs_proj4": proj4,
            "area_unit": "m2",
            "pinned_revisions": dict(job.pinned_revisions),
            "weights": weights,
        }

    # ------------------------------------------------------------------
    def deliver(self, job_id: str) -> Job:
        """交付前做失效检查；源版变化则置 STALE 并拒绝交付。"""
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)

        if job.state in (PENDING, RUNNING):
            return job

        if job.state in (FAILED, STALE):
            return job

        # COMPLETED：核对源版本 revision
        changed: dict[str, dict[str, int]] = {}
        for version, pinned in job.pinned_revisions.items():
            current = self.db.get_revision(version)
            if current is None or current != pinned:
                changed[version] = {
                    "pinned_revision": pinned,
                    "current_revision": current if current is not None else -1,
                }
        if changed:
            job.state = STALE
            job.error = (
                "源版本在计算期间发生更正，旧权重已拒绝交付，"
                "请使用相同版号重新提交计算任务"
            )
            job.error_detail = {"changed_versions": changed}
        return job

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
