"""独立子进程执行的真实 HTTP 并发自检（由 test_streaming_ingest 调用）。

单独成文件并以 ``python -I`` 启动，避免与测试进程内的全局 app/数据库
相互污染，也规避工作目录中的同名模块劫持。
用法: python _live_concurrency.py <db_path> <port>
"""
from __future__ import annotations

import json
import sys
import threading
import time

import httpx
import uvicorn


def rect(code: str) -> dict:
    return {
        "type": "Feature",
        "properties": {"township_code": code},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
        },
    }


def collection(n: int) -> bytes:
    return json.dumps(
        {"type": "FeatureCollection", "features": [rect(f"T{i}") for i in range(n)]}
    ).encode()


def main(db_path: str, port: int) -> None:
    import os

    os.environ["DB_PATH"] = db_path
    sys.path.insert(0, "/workspace")
    from app.main import app

    base = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            httpx.get(base + "/healthz", timeout=1)
            break
        except Exception:
            time.sleep(0.1)

    n = 6
    payloads = {i: collection(i + 1) for i in range(n)}
    outcomes: list = []

    def worker(i: int) -> None:
        with httpx.Client(timeout=30) as cli:
            r = cli.put(
                base + "/api/maps/vc",
                content=payloads[i],
                headers={"content-type": "application/json"},
            )
            outcomes.append((i, r))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(outcomes) == n
    revisions = []
    counts = []
    for i, r in outcomes:
        assert r.status_code == 200, (i, r.text)
        body = r.json()
        assert body["version"] == "vc"
        revisions.append(body["revision"])
        counts.append(body["feature_count"])

    # 修订号按成功发布顺序严格递增、无缺号无重号
    assert sorted(revisions) == list(range(1, n + 1)), revisions
    # 每次发布的要素数都对应某个完整请求（1..n 各一次），
    # 证明发布的是完整整版，没有半版/串版
    assert sorted(counts) == list(range(1, n + 1)), counts

    meta = httpx.get(base + "/api/maps/vc", timeout=10).json()
    assert meta["revision"] == n
    # 最终要素数必为某次完整请求的要素数（并发下最后发布者不确定，
    # 但一定是完整整版，不可能是半版）
    assert meta["feature_count"] in counts
    # 暂存表无残留
    from app.main import app as running_app

    residue = running_app.state.db._conn.execute(
        "SELECT COUNT(*) AS c FROM map_staging"
    ).fetchone()["c"]
    assert residue == 0

    server.should_exit = True
    thread.join(timeout=10)
    print("LIVE CONCURRENCY OK")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
