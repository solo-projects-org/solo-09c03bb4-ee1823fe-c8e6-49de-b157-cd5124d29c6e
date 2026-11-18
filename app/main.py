"""FastAPI 应用入口与路由。

端点：
- GET  /healthz                       健康检查
- GET  /docs                          Swagger UI（入口）
- PUT  /api/maps/{version}            按版上传/整版更正 GeoJSON（流式受理）
- GET  /api/maps                      已受理版本列表（含修订号）
- GET  /api/maps/{version}            版本元数据
- POST /api/weights/jobs              提交旧版→新版权重计算任务
- GET  /api/weights/jobs              任务列表
- GET  /api/weights/jobs/{job_id}     查询并交付结果（含失效检测）
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Iterator

import anyio
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from . import geo
from .db import Database, StagingDuplicateError
from .geo import GeoValidationError
from .ingest import (
    StreamingFeatureCollection,
    StreamingJSONError,
    async_stream_to_sync_iter,
    multipart_file_chunks,
)
from .jobs import COMPLETED, FAILED, PENDING, RUNNING, STALE, JobError, JobManager


# ----------------------------------------------------------------------
# 响应模型
# ----------------------------------------------------------------------
class MapUploadResponse(BaseModel):
    version: str
    revision: int = Field(..., description="该版当前修订号，重传一次加 1")
    feature_count: int
    replaced_previous: bool
    message: str


class MapVersionMeta(BaseModel):
    version: str
    revision: int
    feature_count: int
    created_at: str
    updated_at: str


class JobSubmitRequest(BaseModel):
    old_version: str = Field(..., description="旧版（历史指标所在边界）版号")
    new_version: str = Field(..., description="新版边界版号")


# ----------------------------------------------------------------------
# 生命周期
# ----------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = Database()
    app.state.jobs = JobManager(app.state.db)
    yield
    app.state.jobs.shutdown()
    app.state.db.close()


app = FastAPI(
    title="乡镇边界面积权重 API",
    description=(
        "按版受理 WGS84 乡镇面 GeoJSON，计算旧版乡镇到新版乡镇的"
        "等面积交叠权重。同版重传为整版更正；源版在计算期间变化时，"
        "旧权重拒绝交付并提示重试。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_jobs(request: Request) -> JobManager:
    return request.app.state.jobs


def _request_body_chunks(request: Request) -> Iterator[bytes]:
    """把请求体（JSON 原始体或 multipart 文件）包装成同步字节迭代器。

    实际拉取发生在工作线程内，通过 anyio 回到事件循环执行 receive，
    事件循环不被解析占用，其他版本的上传/查询照常服务。
    """
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        raw = async_stream_to_sync_iter(request.stream)
        return multipart_file_chunks(content_type, raw)
    return async_stream_to_sync_iter(request.stream)


def _ingest_stream(
    db: Database, version: str, chunks: Iterator[bytes]
) -> tuple[int, int, bool]:
    """流式受理的同步核心：逐要素解析 -> 校验 -> 暂存 -> 整版发布。

    返回 (revision, feature_count, replaced_previous)。
    任何语法/校验/暂存/入库失败都抛异常，由端点转成 400；
    失败时暂存被清理，maps/map_features 从未被修改，旧版保持不动。
    """
    staging_id = db.new_staging_id()
    try:
        fc = StreamingFeatureCollection(chunks)
        # 逐要素：解析一个、校验一个；按定长微批暂存，批与批之间引用
        # 即释放。解析期内存只与“当前要素/当前 token + 一个微批”有关，
        # 不随要素总数线性增长。
        fc.start()
        feature_count = 0
        pending: list[tuple[int, str, dict]] = []
        for idx, feature in enumerate(fc.features()):
            township_id, geom = geo.validate_feature(feature, idx)
            pending.append((idx, township_id, geo.to_storage_geometry(geom)))
            feature_count += 1
            if len(pending) >= db.STAGE_BATCH_SIZE:
                db.stage_features(staging_id, pending)
                pending.clear()
        fc.finish()
        if pending:
            db.stage_features(staging_id, pending)
            pending.clear()

        # 头部字段在读完整个文件后再校验（crs 等），拒收同样不改旧版
        geo.validate_crs(fc.header)
        if fc.header.get("type") != "FeatureCollection":
            raise GeoValidationError("仅支持 GeoJSON FeatureCollection")
        if feature_count == 0:
            raise GeoValidationError("features 为空，至少需要一个面要素")

        replaced = db.get_revision(version) is not None
        revision = db.publish_staging(version, staging_id)
        return revision, feature_count, replaced
    except StagingDuplicateError as exc:
        # 暂存唯一约束 -> 与旧实现一致的 400 文案
        db.discard_staging(staging_id)
        raise GeoValidationError(str(exc)) from exc
    except (GeoValidationError, StreamingJSONError):
        # 语义化拒收：不包装、不加前缀，由端点映射为对应 400 文案
        db.discard_staging(staging_id)
        raise
    except Exception:
        # 入库等基础设施失败：清理暂存后上抛，端点回 500，旧版不变
        db.discard_staging(staging_id)
        raise


# ----------------------------------------------------------------------
# 基础端点
# ----------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/healthz", tags=["system"])
def healthz(request: Request):
    return {"status": "ok", "versions": len(get_db(request).list_versions())}


# ----------------------------------------------------------------------
# 地图上传 / 查询
# ----------------------------------------------------------------------
@app.put(
    "/api/maps/{version}",
    response_model=MapUploadResponse,
    tags=["maps"],
    status_code=status.HTTP_200_OK,
    summary="按版上传 GeoJSON（同版重传 = 整版更正，逐要素流式受理）",
)
async def upload_map(version: str, request: Request) -> MapUploadResponse:
    if not version or len(version) > 128:
        raise HTTPException(status_code=400, detail="版号不能为空且长度不超过 128")

    db = get_db(request)
    chunks = _request_body_chunks(request)

    # 重 CPU/IO 的解析放到工作线程：事件循环保持空闲，其他版的上传与
    # 查询在解析期间仍可被处理。空请求体在词法器 EOF 处即报错。
    try:
        revision, feature_count, replaced = await anyio.to_thread.run_sync(
            _ingest_stream, db, version, chunks
        )
    except StreamingJSONError as exc:
        # 尾部截断、JSON/multipart 语法损坏、空请求体
        raise HTTPException(status_code=400, detail=f"GeoJSON 解析失败：{exc}") from exc
    except GeoValidationError as exc:
        # 字段/几何拒收规则与旧实现完全一致（含重复乡镇号文案）
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        # 暂存/入库等基础设施失败：publish_staging 已回滚、暂存已清理，
        # 旧版与修订号保持不变；以 500 明确告知本次受理未生效。
        raise HTTPException(
            status_code=500,
            detail=f"地图受理失败，整版未发布、旧版保持不变：{type(exc).__name__}: {exc}",
        ) from exc

    return MapUploadResponse(
        version=version,
        revision=revision,
        feature_count=feature_count,
        replaced_previous=replaced,
        message=(
            f"版号 {version} 已整版更正（第 {revision} 次受理）"
            if replaced
            else f"版号 {version} 首次受理成功"
        ),
    )


@app.get("/api/maps", response_model=list[MapVersionMeta], tags=["maps"])
def list_maps(request: Request) -> list[MapVersionMeta]:
    rows = get_db(request).list_versions()
    return [
        MapVersionMeta(
            version=r["version"],
            revision=r["revision"],
            feature_count=r["feature_cnt"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


@app.get("/api/maps/{version}", response_model=MapVersionMeta, tags=["maps"])
def get_map(version: str, request: Request) -> MapVersionMeta:
    for r in get_db(request).list_versions():
        if r["version"] == version:
            return MapVersionMeta(
                version=r["version"],
                revision=r["revision"],
                feature_count=r["feature_cnt"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
    raise HTTPException(status_code=404, detail=f"版号不存在：{version}")


# ----------------------------------------------------------------------
# 权重计算任务
# ----------------------------------------------------------------------
@app.post(
    "/api/weights/jobs",
    tags=["weights"],
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交旧版→新版交叠权重计算任务",
)
def submit_job(body: JobSubmitRequest, request: Request) -> dict[str, Any]:
    db = get_db(request)
    jobs = get_jobs(request)

    if db.get_revision(body.old_version) is None:
        raise HTTPException(
            status_code=404, detail=f"旧版地图不存在：{body.old_version}"
        )
    if db.get_revision(body.new_version) is None:
        raise HTTPException(
            status_code=404, detail=f"新版地图不存在：{body.new_version}"
        )

    # 同步几何预检：新版乡镇存在正面积交叠候选 -> 立即拒绝，不建任务。
    # 定量平方米面积在工作线程的等面积投影阶段复核并报告。
    new_stored = db.get_features(body.new_version)
    from shapely.geometry import shape

    new_geoms = [(tid, shape(g)) for tid, g in (new_stored or [])]
    overlap_pairs = geo.find_overlapping_pairs(new_geoms)
    if overlap_pairs:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"新版 {body.new_version} 内乡镇之间存在 "
                    f"{len(overlap_pairs)} 处正面积交叠，拒绝计算（不能重复计权），"
                    "请更正新版后重试"
                ),
                "overlapping_pairs": [
                    {"township_a": a, "township_b": b} for a, b in overlap_pairs
                ],
            },
        )

    try:
        job = jobs.submit(body.old_version, body.new_version)
    except JobError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return job.public()


@app.get(
    "/api/weights/jobs",
    tags=["weights"],
    summary="列出全部计算任务",
)
def list_jobs(request: Request) -> dict[str, Any]:
    return {"jobs": get_jobs(request).list_jobs()}


@app.get(
    "/api/weights/jobs/{job_id}",
    tags=["weights"],
    summary="查询任务并交付结果（源版变化则拒绝交付）",
)
def get_job(job_id: str, request: Request):
    jobs = get_jobs(request)
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{job_id}")

    # 交付前失效检测
    job = jobs.deliver(job_id)
    payload = job.public()

    if job.state in (PENDING, RUNNING):
        return payload  # 200 + state=pending/running，客户端轮询即可
    if job.state == COMPLETED:
        return payload
    if job.state == STALE:
        raise HTTPException(status_code=409, detail=payload)
    if job.state == FAILED:
        raise HTTPException(status_code=422, detail=payload)
    return payload
