"""pytest 公共夹具：每个测试使用独立临时 SQLite 的 FastAPI TestClient。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# 让测试不依赖工作目录即可导入 /workspace/app
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="twtest-")
os.environ["DB_PATH"] = str(Path(_TMP) / "test.db")
os.environ["POSITIVE_AREA_EPS_M2"] = "1e-6"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        # 每个测试开始前清空共享库，保证版号修订号从 1 开始、用例互不污染
        with app.state.db._lock:
            app.state.db._conn.execute("DELETE FROM maps")
        yield c


@pytest.fixture()
def tmp_db_path() -> str:
    return os.environ["DB_PATH"]


# ----------------------------------------------------------------------
# GeoJSON 构造辅助
# ----------------------------------------------------------------------
def rect_geojson(township_code: str, x0: float, y0: float, x1: float, y1: float):
    """构造轴对齐矩形 Polygon GeoJSON Feature（经纬度）。"""
    return {
        "type": "Feature",
        "properties": {"township_code": township_code},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [x0, y0],
                    [x1, y0],
                    [x1, y1],
                    [x0, y1],
                    [x0, y0],
                ]
            ],
        },
    }


def rect_with_hole(township_code: str, x0, y0, x1, y1, hx0, hy0, hx1, hy1):
    """带洞矩形（外环逆时针、内环顺时针按 GeoJSON 规范）。"""
    return {
        "type": "Feature",
        "properties": {"code": township_code},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]],
                [[hx0, hy0], [hx0, hy1], [hx1, hy1], [hx1, hy0], [hx0, hy0]],
            ],
        },
    }


def multipolygon_geojson(township_code: str, boxes):
    """boxes: [(x0,y0,x1,y1), ...] 互不相邻的多个矩形。"""
    polys = []
    for x0, y0, x1, y1 in boxes:
        polys.append(
            [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]
        )
    return {
        "type": "Feature",
        "properties": {"township_code": township_code},
        "geometry": {"type": "MultiPolygon", "coordinates": polys},
    }


def feature_collection(*features) -> dict:
    return {"type": "FeatureCollection", "features": list(features)}
