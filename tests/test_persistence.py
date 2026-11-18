"""持久化测试：重启后已受理地图可恢复；任务态为内存临时态。"""
from __future__ import annotations

import os

from tests.conftest import feature_collection, rect_geojson


def test_maps_survive_database_reopen(client, tmp_db_path):
    client.put(
        "/api/maps/v1",
        json=feature_collection(
            rect_geojson("T1", 0, 0, 1, 1),
            rect_geojson("T2", 1, 0, 2, 1),
        ),
    )
    client.put("/api/maps/v1", json=feature_collection(rect_geojson("T1", 0, 0, 2, 2)))

    # 模拟进程重启：新建 Database 指向同一 SQLite 文件
    from app.db import Database

    db2 = Database(tmp_db_path)
    try:
        assert db2.get_revision("v1") == 2
        feats = db2.get_features("v1")
        assert feats is not None
        assert [tid for tid, _ in feats] == ["T1"]
        assert db2.get_revision("never-uploaded") is None
    finally:
        db2.close()


def test_rejected_upload_leaves_no_trace(client, tmp_db_path):
    client.put("/api/maps/v1", json=feature_collection(rect_geojson("T1", 0, 0, 1, 1)))
    # 非法重传
    client.put(
        "/api/maps/v1",
        json=feature_collection(
            rect_geojson("T1", 0, 0, 1, 1),
            rect_geojson("T1", 1, 0, 2, 1),
        ),
    )
    from app.db import Database

    db2 = Database(tmp_db_path)
    try:
        assert db2.get_revision("v1") == 1
        assert len(db2.get_features("v1")) == 1
    finally:
        db2.close()


def test_db_file_created_under_configured_path(tmp_db_path):
    assert os.path.exists(tmp_db_path)
