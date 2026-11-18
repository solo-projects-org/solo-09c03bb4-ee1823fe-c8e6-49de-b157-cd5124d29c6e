"""交叠权重计算端到端测试。"""
from __future__ import annotations

import time

from tests.conftest import (
    feature_collection,
    multipolygon_geojson,
    rect_geojson,
    rect_with_hole,
)


def _wait_job(client, job_id, timeout=10.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = client.get(f"/api/weights/jobs/{job_id}")
        last = r
        if r.status_code in (409, 422):
            return r
        state = r.json()["state"]
        if state in ("completed", "failed", "stale"):
            return r
        time.sleep(0.02)
    raise AssertionError(f"任务未在 {timeout}s 内结束：{last.text if last else None}")


def _submit(client, old="v1", new="v2"):
    return client.post("/api/weights/jobs", json={"old_version": old, "new_version": new})


def test_basic_split_weights(client):
    # 旧版：一个 2x1 的乡镇
    client.put("/api/maps/v1", json=feature_collection(rect_geojson("OLD1", 0, 0, 2, 1)))
    # 新版：左右各 1x1，恰好完全覆盖旧乡镇
    client.put(
        "/api/maps/v2",
        json=feature_collection(
            rect_geojson("NEW1", 0, 0, 1, 1),
            rect_geojson("NEW2", 1, 0, 2, 1),
        ),
    )
    r = _submit(client)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]

    done = _wait_job(client, job_id)
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["state"] == "completed"

    w = body["result"]["weights"][0]
    assert w["old_township_id"] == "OLD1"
    overlaps = {o["new_township_id"]: o for o in w["overlaps"]}
    assert set(overlaps) == {"NEW1", "NEW2"}

    # 两个新乡镇各占一半（等面积投影下的米制面积，比例与面积口径一致）
    assert abs(overlaps["NEW1"]["ratio_of_old"] - 0.5) < 1e-4
    assert abs(overlaps["NEW2"]["ratio_of_old"] - 0.5) < 1e-4
    assert abs(overlaps["NEW1"]["intersection_area_m2"]
               - overlaps["NEW2"]["intersection_area_m2"]) < 1e-3

    # 全覆盖：未覆盖面积≈0（仅允许相邻边重投影的浮点缝隙），权重和≈1
    assert w["uncovered_area_m2"] / w["old_area_m2"] < 1e-4
    assert abs(w["coverage_ratio"] - 1.0) < 1e-4
    assert abs(sum(o["ratio_of_old"] for o in w["overlaps"]) - 1.0) < 1e-4


def test_uncovered_area_when_new_does_not_cover_old(client):
    client.put("/api/maps/v1", json=feature_collection(rect_geojson("OLD1", 0, 0, 2, 2)))
    # 新版只覆盖旧乡镇的左下四分之一
    client.put("/api/maps/v2", json=feature_collection(rect_geojson("NEW1", 0, 0, 1, 1)))

    job_id = _submit(client).json()["job_id"]
    body = _wait_job(client, job_id).json()
    w = body["result"]["weights"][0]
    assert len(w["overlaps"]) == 1
    assert abs(w["overlaps"][0]["ratio_of_old"] - 0.25) < 1e-4
    # 未覆盖面积约为旧面积的 3/4
    assert abs(w["uncovered_area_m2"] / w["old_area_m2"] - 0.75) < 1e-3
    assert abs(w["coverage_ratio"] - 0.25) < 1e-4


def test_shared_edge_only_not_counted(client):
    # 旧乡镇 [0,1]x[0,1]，新乡镇仅与其共边（贴在右边，无正面积交叠）
    client.put("/api/maps/v1", json=feature_collection(rect_geojson("OLD1", 0, 0, 1, 1)))
    client.put("/api/maps/v2", json=feature_collection(rect_geojson("NEW1", 1, 0, 2, 1)))

    body = _wait_job(client, _submit(client).json()["job_id"]).json()
    w = body["result"]["weights"][0]
    assert w["overlaps"] == []
    assert abs(w["coverage_ratio"]) < 1e-9
    assert abs(w["uncovered_area_m2"] - w["old_area_m2"]) < 1e-3


def test_hole_area_excluded(client):
    # 旧乡镇是带 1x1 洞的 4x4 矩形，新乡镇是完整 4x4 矩形
    client.put(
        "/api/maps/v1",
        json=feature_collection(rect_with_hole("OLD1", 0, 0, 4, 4, 1, 1, 2, 2)),
    )
    client.put("/api/maps/v2", json=feature_collection(rect_geojson("NEW1", 0, 0, 4, 4)))

    body = _wait_job(client, _submit(client).json()["job_id"]).json()
    w = body["result"]["weights"][0]
    # 洞面积（1/16）不计入交叠，也不计入旧乡镇面积：覆盖比例应为 1（全部实体被覆盖）
    assert abs(w["coverage_ratio"] - 1.0) < 1e-4
    assert w["uncovered_area_m2"] / w["old_area_m2"] < 1e-4
    # 旧面积 = 16 - 1 = 15 个“度平方”的投影等价值，明显小于完整矩形
    assert w["old_area_m2"] > 0
    assert abs(w["overlaps"][0]["intersection_area_m2"] - w["old_area_m2"]) < 1e-3


def test_new_version_internal_overlap_rejected(client):
    client.put("/api/maps/v1", json=feature_collection(rect_geojson("OLD1", 0, 0, 2, 2)))
    # 新版两个乡镇有 1x1 的正面积交叠
    client.put(
        "/api/maps/v2",
        json=feature_collection(
            rect_geojson("NEW1", 0, 0, 2, 2),
            rect_geojson("NEW2", 1, 1, 3, 3),
        ),
    )
    # 提交时同步预检即 409
    r = _submit(client)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "交叠" in detail["message"]
    pair = detail["overlapping_pairs"][0]
    assert {pair["township_a"], pair["township_b"]} == {"NEW1", "NEW2"}

    # 修正为互斥后可正常计算
    client.put(
        "/api/maps/v2",
        json=feature_collection(
            rect_geojson("NEW1", 0, 0, 1, 2),
            rect_geojson("NEW2", 1, 0, 2, 2),
        ),
    )
    body = _wait_job(client, _submit(client).json()["job_id"]).json()
    assert body["state"] == "completed"


def test_stale_result_rejected_when_source_changes(client):
    client.put("/api/maps/v1", json=feature_collection(rect_geojson("OLD1", 0, 0, 2, 1)))
    client.put(
        "/api/maps/v2",
        json=feature_collection(
            rect_geojson("NEW1", 0, 0, 1, 1),
            rect_geojson("NEW2", 1, 0, 2, 1),
        ),
    )
    job_id = _submit(client).json()["job_id"]
    body = _wait_job(client, job_id).json()
    assert body["state"] == "completed"

    # 计算期间（或完成后、交付前）整版更正旧版
    client.put(
        "/api/maps/v1",
        json=feature_collection(
            rect_geojson("OLD1", 0, 0, 2, 1),
            rect_geojson("OLD2", 2, 0, 3, 1),
        ),
    )

    r = client.get(f"/api/weights/jobs/{job_id}")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["state"] == "stale"
    assert "重新提交" in detail["error"]
    assert "v1" in detail["error_detail"]["changed_versions"]

    # 重新提交 -> 基于新版 revision 的任务可正常交付
    job2 = _submit(client).json()["job_id"]
    body2 = _wait_job(client, job2).json()
    assert body2["state"] == "completed"
    assert len(body2["result"]["weights"]) == 2


def test_worker_level_overlap_check_reports_square_metres(client, monkeypatch):
    """防御路径：即使同步预检被绕过，工作线程在等面积投影后仍以 m2 拒绝。"""
    client.put("/api/maps/v1", json=feature_collection(rect_geojson("OLD1", 0, 0, 2, 2)))
    client.put(
        "/api/maps/v2",
        json=feature_collection(
            rect_geojson("NEW1", 0, 0, 2, 2),
            rect_geojson("NEW2", 1, 1, 3, 3),
        ),
    )
    # 让提交端几何预检失效，模拟直接进入工作线程
    monkeypatch.setattr("app.main.geo.find_overlapping_pairs", lambda feats: [])

    job_id = _submit(client).json()["job_id"]
    r = _wait_job(client, job_id)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["state"] == "failed"
    ov = detail["error_detail"]["new_version_overlaps"][0]
    assert ov["overlap_area_m2"] > 1.0  # 平方米量级的真实交叠


def test_submit_missing_version_returns_404(client):
    client.put("/api/maps/v1", json=feature_collection(rect_geojson("OLD1", 0, 0, 1, 1)))
    r = _submit(client, old="v1", new="nope")
    assert r.status_code == 404


def test_multipolygon_township_weights(client):
    # 旧乡镇由两个不相邻矩形组成；新版完整矩形分别覆盖二者
    client.put(
        "/api/maps/v1",
        json=feature_collection(multipolygon_geojson("OLD1", [(0, 0, 1, 1), (5, 5, 6, 6)])),
    )
    client.put(
        "/api/maps/v2",
        json=feature_collection(
            rect_geojson("NEW1", 0, 0, 1, 1),
            rect_geojson("NEW2", 5, 5, 6, 6),
        ),
    )
    body = _wait_job(client, _submit(client).json()["job_id"]).json()
    w = body["result"]["weights"][0]
    assert len(w["overlaps"]) == 2
    assert abs(w["coverage_ratio"] - 1.0) < 1e-4


def test_job_listing_and_unknown_job(client):
    client.put("/api/maps/v1", json=feature_collection(rect_geojson("OLD1", 0, 0, 1, 1)))
    client.put("/api/maps/v2", json=feature_collection(rect_geojson("NEW1", 0, 0, 1, 1)))
    _submit(client)
    listed = client.get("/api/weights/jobs").json()["jobs"]
    assert len(listed) == 1
    assert client.get("/api/weights/jobs/does-not-exist").status_code == 404
