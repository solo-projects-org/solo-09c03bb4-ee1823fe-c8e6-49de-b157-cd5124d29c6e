"""流式受理专项测试：逐要素解析、尾部截断、暂存隔离与并发修订号。

覆盖任务要求的自测点：
- 流式识别器在任意分块尺寸下与 json 全量解析等价；
- 尾部损坏（JSON / multipart）一律 400，旧版与修订号不变；
- 解析期内存不随要素总数线性增长（解析器内部缓冲有界）；
- 真实 HTTP 并发下同版修订号按发布顺序严格递增，且无半版可读。
"""
from __future__ import annotations

import json
import socket

import pytest

from app.ingest import (
    StreamingFeatureCollection,
    StreamingJSONError,
    multipart_file_chunks,
)
from tests.conftest import feature_collection, rect_geojson


# ----------------------------------------------------------------------
# 流式识别器单元测试
# ----------------------------------------------------------------------
def _stream_parse(data: bytes, chunk_size: int):
    chunks = (data[i : i + chunk_size] for i in range(0, len(data), chunk_size))
    fc = StreamingFeatureCollection(chunks)
    fc.start()
    features = list(fc.features())
    fc.finish()
    return features, fc.header


def test_streaming_parser_matches_json_for_all_chunk_sizes():
    payload = feature_collection(
        rect_geojson("T1", 0, 0, 1, 1),
        rect_geojson("T2", 1, 0, 2, 1),
    )
    payload["crs"] = {
        "type": "name",
        "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"},
    }
    payload["_meta"] = {"n": None, "ok": True, "f": False, "x": 1.25e-3, "s": "中文\"转义"}
    data = json.dumps(payload, ensure_ascii=False).encode()

    for size in (1, 2, 3, 5, 16, 64, 4096):
        features, header = _stream_parse(data, size)
        assert [f["properties"]["township_code"] for f in features] == ["T1", "T2"]
        assert features[0]["geometry"]["coordinates"][0][0] == [0, 0]
        assert header["crs"]["properties"]["name"].endswith("CRS84")
        assert header["_meta"] == payload["_meta"]


def test_streaming_parser_rejects_truncated_inputs():
    good = json.dumps(
        feature_collection(rect_geojson("T1", 0, 0, 1, 1))
    ).encode()
    truncated = [
        good[: len(good) // 2],
        good.rstrip(b"]}"),
        b'{"type":"FeatureCollection","features":[{"type":"Feature"',
        b'{"type":"FeatureCollection","features":[',
        b'{"type":"FeatureCollection","features":[{"type":"Feature",'
        b'"properties":{"township_code":"T1"},"geometry":{"type":"Polygon",'
        b'"coordinates":[[[0,0],[1,0]',
    ]
    for raw in truncated:
        with pytest.raises(StreamingJSONError):
            _stream_parse(raw, 7)


def test_streaming_parser_rejects_empty_garbage_and_trailing_data():
    for raw in (b"", b"   ", b"{not json", b'{"type":"Feature"}'):
        with pytest.raises(StreamingJSONError):
            _stream_parse(raw, 4)
    good = json.dumps(
        feature_collection(rect_geojson("T1", 0, 0, 1, 1))
    ).encode()
    with pytest.raises(StreamingJSONError):
        _stream_parse(good + b"  garbage", 4)
    # 合法尾随空白应当接受
    features, _ = _stream_parse(good + b" \n\t ", 3)
    assert len(features) == 1


def test_multipart_reader_roundtrips_and_detects_truncation():
    body = json.dumps(
        feature_collection(rect_geojson("T1", 0, 0, 1, 1))
    ).encode()
    boundary = "----pytest-boundary"
    content_type = f"multipart/form-data; boundary={boundary}"
    full = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="m.geojson"\r\n'
        "Content-Type: application/geo+json\r\n\r\n"
    ).encode() + body + f"\r\n--{boundary}--\r\n".encode()

    # 各种小块尺寸下字节完全一致
    for size in (1, 7, 64, 4096):
        chunks = (full[i : i + size] for i in range(0, len(full), size))
        assert b"".join(multipart_file_chunks(content_type, chunks)) == body

    # 缺 file 字段
    no_file = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="other"\r\n\r\n'
        f"x\r\n--{boundary}--\r\n"
    ).encode()
    with pytest.raises(StreamingJSONError):
        list(multipart_file_chunks(content_type, iter([no_file])))

    # 尾部截断（少结束符的任意字节）都必须拒收
    for cut in (len(full) - 1, len(full) - 2, len(full) - 4, 50):
        chunks = (full[:cut][i : i + 33] for i in range(0, cut, 33))
        with pytest.raises(StreamingJSONError):
            list(multipart_file_chunks(content_type, chunks))


def test_tokenizer_internal_buffer_is_bounded_by_current_feature():
    """解析器内部缓冲不随要素总数增长：稳态只保留当前 token 的字节。"""
    big = json.dumps(
        feature_collection(
            *(rect_geojson(f"T{i}", 0, 0, 1, 1) for i in range(2000))
        )
    ).encode()
    chunks = (big[i : i + 128] for i in range(0, len(big), 128))
    fc = StreamingFeatureCollection(chunks)
    fc.start()
    max_buf = 0
    count = 0
    for _ in fc.features():
        count += 1
        # 单个要素远小于整版；内部缓冲始终在“单块 + 单 token”量级
        max_buf = max(max_buf, len(fc._tk._buf))
        assert len(fc._tk._buf) < 64 * 1024
    fc.finish()
    assert count == 2000
    assert max_buf < 64 * 1024


# ----------------------------------------------------------------------
# API 端到端：截断拒收 + 旧版/修订号不变
# ----------------------------------------------------------------------
def _good_collection(n: int) -> bytes:
    return json.dumps(
        feature_collection(
            *(rect_geojson(f"T{i}", 0, 0, 1, 1) for i in range(n))
        )
    ).encode()


def test_truncated_json_body_rejected_and_revision_kept(client):
    assert client.put(
        "/api/maps/v1",
        content=_good_collection(2),
        headers={"content-type": "application/json"},
    ).status_code == 200
    before = client.get("/api/maps/v1").json()

    full = _good_collection(5)
    for cut in (8, len(full) // 2, len(full) - 1):
        r = client.put(
            "/api/maps/v1",
            content=full[:cut],
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400
    assert client.get("/api/maps/v1").json() == before

    # 合法整版仍可正常发布为下一修订
    ok = client.put(
        "/api/maps/v1",
        content=full,
        headers={"content-type": "application/json"},
    )
    assert ok.status_code == 200
    assert ok.json()["revision"] == before["revision"] + 1


def test_truncated_multipart_rejected_and_revision_kept(client):
    assert client.put(
        "/api/maps/v1",
        content=_good_collection(2),
        headers={"content-type": "application/json"},
    ).status_code == 200
    before = client.get("/api/maps/v1").json()

    body = _good_collection(5)
    boundary = "----pytest-boundary"
    content_type = f"multipart/form-data; boundary={boundary}"
    full = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="m.geojson"\r\n\r\n'
    ).encode() + body + f"\r\n--{boundary}--\r\n".encode()

    for cut in (40, len(full) - 1, len(full) - 3):
        r = client.put(
            "/api/maps/v1",
            content=full[:cut],
            headers={"content-type": content_type},
        )
        assert r.status_code == 400, (cut, r.text)
    assert client.get("/api/maps/v1").json() == before

    # 完整 multipart 正常受理
    ok = client.put(
        "/api/maps/v1",
        files={"file": ("m.geojson", body, "application/geo+json")},
    )
    assert ok.status_code == 200
    assert ok.json()["revision"] == before["revision"] + 1


def test_failed_upload_leaves_no_staging_residue(client):
    client.put(
        "/api/maps/v1",
        content=_good_collection(1),
        headers={"content-type": "application/json"},
    )
    # 尾部损坏
    full = _good_collection(3)
    client.put(
        "/api/maps/v1",
        content=full[: len(full) - 2],
        headers={"content-type": "application/json"},
    )
    # 重复乡镇号
    dup = json.dumps(
        feature_collection(
            rect_geojson("T1", 0, 0, 1, 1),
            rect_geojson("T1", 1, 0, 2, 1),
        )
    ).encode()
    client.put(
        "/api/maps/v1",
        content=dup,
        headers={"content-type": "application/json"},
    )
    from app.main import app

    residue = app.state.db._conn.execute(
        "SELECT COUNT(*) AS c FROM map_staging"
    ).fetchone()["c"]
    assert residue == 0


def test_publish_storage_failure_keeps_old_revision(client):
    """暂存成功但发布事务失败：500、回滚、旧版修订号与要素数不变。"""
    from app.main import app

    assert client.put(
        "/api/maps/v1",
        content=_good_collection(1),
        headers={"content-type": "application/json"},
    ).status_code == 200
    before = client.get("/api/maps/v1").json()

    db = app.state.db
    with db._lock:
        db._conn.execute(
            "CREATE TRIGGER fail_publish BEFORE INSERT ON maps "
            "BEGIN SELECT RAISE(ABORT, 'injected publish failure'); END"
        )
    try:
        r = client.put(
            "/api/maps/v1",
            content=_good_collection(3),
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 500
    finally:
        with db._lock:
            db._conn.execute("DROP TRIGGER fail_publish")

    # 旧版与修订号原样保留
    assert client.get("/api/maps/v1").json() == before
    assert db._conn.execute(
        "SELECT COUNT(*) AS c FROM map_staging"
    ).fetchone()["c"] == 0
    # 连接健康，后续发布可正常递增
    ok = client.put(
        "/api/maps/v1",
        content=_good_collection(3),
        headers={"content-type": "application/json"},
    )
    assert ok.status_code == 200
    assert ok.json()["revision"] == before["revision"] + 1


# ----------------------------------------------------------------------
# 真实 HTTP 并发：同版并发上传修订号按发布顺序严格递增
# ----------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_concurrent_same_version_uploads_get_monotonic_revisions(tmp_path):
    """在独立子进程里启动真实 uvicorn，发起同版并发上传。

    子进程隔离保证全局 app/数据库与测试夹具互不影响；
    要求修订号按发布顺序 1..n 递增，且每次发布要素数与修订号一一对应，
    证明并发下没有半版可见或修订号错号。
    """
    import subprocess
    import sys

    db_path = tmp_path / "conc.db"
    port = _free_port()
    script = (
        __import__("pathlib").Path(__file__).with_name("_live_concurrency.py")
    )
    proc = subprocess.run(
        [sys.executable, "-I", str(script), str(db_path), str(port)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"live concurrency subprocess failed\nstdout={proc.stdout}\n"
        f"stderr={proc.stderr}"
    )
    assert "LIVE CONCURRENCY OK" in proc.stdout
