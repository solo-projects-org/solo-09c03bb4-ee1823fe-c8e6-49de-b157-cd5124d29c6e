"""地图上传与整版更正的校验规则测试。"""
from __future__ import annotations

from tests.conftest import feature_collection, multipolygon_geojson, rect_geojson


def test_upload_first_version_revision_1(client):
    fc = feature_collection(rect_geojson("T1", 0, 0, 1, 1))
    r = client.put("/api/maps/v1", json=fc)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == "v1"
    assert body["revision"] == 1
    assert body["feature_count"] == 1
    assert body["replaced_previous"] is False


def test_same_version_reupload_is_full_correction(client):
    client.put("/api/maps/v1", json=feature_collection(rect_geojson("T1", 0, 0, 1, 1)))
    r2 = client.put(
        "/api/maps/v1",
        json=feature_collection(
            rect_geojson("T1", 0, 0, 2, 2),
            rect_geojson("T2", 2, 0, 3, 1),
        ),
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["revision"] == 2
    assert body["feature_count"] == 2
    assert body["replaced_previous"] is True

    meta = client.get("/api/maps/v1").json()
    assert meta["revision"] == 2
    assert meta["feature_count"] == 2


def test_duplicate_township_code_rejected_and_old_kept(client):
    # 先受理一个合法旧版
    good = client.put(
        "/api/maps/v1",
        json=feature_collection(
            rect_geojson("T1", 0, 0, 1, 1),
            rect_geojson("T2", 1, 0, 2, 1),
        ),
    )
    assert good.status_code == 200

    # 重传含重复乡镇号 -> 400，旧版保留
    bad = client.put(
        "/api/maps/v1",
        json=feature_collection(
            rect_geojson("T1", 0, 0, 1, 1),
            rect_geojson("T1", 1, 0, 2, 1),
        ),
    )
    assert bad.status_code == 400
    assert "重复" in bad.json()["detail"]

    meta = client.get("/api/maps/v1").json()
    assert meta["revision"] == 1
    assert meta["feature_count"] == 2


def test_invalid_polygon_rejected_and_old_kept(client):
    client.put("/api/maps/v1", json=feature_collection(rect_geojson("T1", 0, 0, 1, 1)))

    # 自交蝴蝶结多边形（bowtie）-> 无效面
    bowtie = {
        "type": "Feature",
        "properties": {"township_code": "BAD"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]
            ],
        },
    }
    r = client.put("/api/maps/v1", json=feature_collection(bowtie))
    assert r.status_code == 400
    assert "无效" in r.json()["detail"]

    meta = client.get("/api/maps/v1").json()
    assert meta["revision"] == 1
    assert meta["feature_count"] == 1


def test_non_polygon_geometry_rejected(client):
    point = {
        "type": "Feature",
        "properties": {"township_code": "P1"},
        "geometry": {"type": "Point", "coordinates": [0, 0]},
    }
    r = client.put("/api/maps/v1", json=feature_collection(point))
    assert r.status_code == 400
    assert "Polygon" in r.json()["detail"]


def test_missing_code_rejected(client):
    feat = rect_geojson("X", 0, 0, 1, 1)
    feat["properties"] = {"name": "无名"}
    r = client.put("/api/maps/v1", json=feature_collection(feat))
    assert r.status_code == 400
    assert "乡镇号" in r.json()["detail"]


def test_bad_json_and_empty_body_rejected(client):
    r = client.put("/api/maps/v1", content=b"{not json",
                   headers={"content-type": "application/json"})
    assert r.status_code == 400
    r = client.put("/api/maps/v1", content=b"",
                   headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_multipolygon_accepted(client):
    fc = feature_collection(
        multipolygon_geojson("M1", [(0, 0, 1, 1), (2, 2, 3, 3)])
    )
    r = client.put("/api/maps/v1", json=fc)
    assert r.status_code == 200
    assert r.json()["feature_count"] == 1


def test_wgs84_range_checked(client):
    bad = rect_geojson("T1", 0, 0, 1, 95)  # 纬度越界
    r = client.put("/api/maps/v1", json=feature_collection(bad))
    assert r.status_code == 400
    assert "WGS84" in r.json()["detail"]


def test_malformed_coordinates_rejected(client):
    feat = {
        "type": "Feature",
        "properties": {"township_code": "T1"},
        "geometry": {"type": "Polygon", "coordinates": [[[100.0], [101.0, 30.0]]]},
    }
    r = client.put("/api/maps/v1", json=feature_collection(feat))
    assert r.status_code == 400
    assert "坐标" in r.json()["detail"]


def test_payload_must_be_feature_collection(client):
    r = client.put("/api/maps/v1", json={"type": "Feature", "features": []})
    assert r.status_code == 400


def test_alternate_code_field_and_multipart_upload(client):
    from tests.conftest import rect_with_hole

    fc = feature_collection(rect_with_hole("H1", 0, 0, 4, 4, 1, 1, 2, 2))
    r = client.put(
        "/api/maps/v1",
        files={"file": ("map.geojson", __import__("json").dumps(fc), "application/geo+json")},
    )
    assert r.status_code == 200
    assert r.json()["revision"] == 1


def test_list_versions(client):
    client.put("/api/maps/v1", json=feature_collection(rect_geojson("T1", 0, 0, 1, 1)))
    client.put("/api/maps/v2", json=feature_collection(rect_geojson("T9", 0, 0, 1, 1)))
    versions = {v["version"] for v in client.get("/api/maps").json()}
    assert versions == {"v1", "v2"}
