# 乡镇边界面积权重 API

把**旧版乡镇面**映射到**新版边界**，为历史指标的跨版汇总提供面积权重。
技术栈：FastAPI + SQLite + Shapely 2 + pyproj。

## 功能与业务规则

| 规则 | 实现方式 |
| --- | --- |
| 按版上传 WGS84 GeoJSON（Polygon / MultiPolygon），要素带唯一乡镇号 | `PUT /api/maps/{version}`，支持 `application/json` 与 `multipart/form-data` 文件上传 |
| 同版重传 = 整版更正 | 单事务先删后插，版本修订号 `revision` 每次 +1 |
| 无效面 / 重复乡镇号 / 非面几何 / 缺乡镇号 / 坐标越界 → 拒收并保留旧版 | 整版先校验后入库，失败返回 400，数据库回滚 |
| 等面积坐标计算 | 以数据中心构造 Albers 等面积投影（AEA，单位米），两版在同一 CRS 下计算 |
| 逐个旧乡镇返回与各新乡镇的交叠面积、占旧乡镇比例、未覆盖面积 | `POST /api/weights/jobs` 异步任务 + `GET /api/weights/jobs/{id}` 交付 |
| 洞内面积、仅共边部分不计入 | Shapely 面交集天然扣除内环；共边交集面积为 0，被 `POSITIVE_AREA_EPS_M2` 阈值过滤 |
| 新版乡镇彼此正面积交叠 → 拒绝计算，不能重复计权 | 提交时几何预检 409；工作线程在等面积投影后按平方米复核（双保险） |
| 计算期间仍可更正边界 | 计算在线程池执行，上传接口不被阻塞 |
| 任一源版变化 → 拒绝交付旧权重并提示重试 | 提交时钉取 `revision`，交付前比对当前修订号，变化则任务置 `stale` 并返回 409 |
| 重启后保留已受理地图 | 地图持久化在 SQLite（WAL）；任务为可重算的临时态，仅存内存 |

## 目录结构

```
app/
  config.py   环境变量配置（DB_PATH、面积阈值等）
  db.py       SQLite 持久化：maps / map_features，事务化整版替换
  geo.py      GeoJSON 校验、AEA 等面积投影、互斥检查、交叠权重计算
  jobs.py     后台任务、版本修订号钉取、交付失效检测
  main.py     FastAPI 路由与 Swagger
tests/        pytest 套件（30 个用例）
Dockerfile
docker-compose.yml
```

## 快速启动（Docker Compose）

```bash
docker compose up --build
# API:        http://localhost:8000
# Swagger UI: http://localhost:8000/docs   （根路径 / 自动跳转）
# 健康检查:    http://localhost:8000/healthz
```

SQLite 数据存放在命名卷 `township_data`（容器内 `/data/weighting.db`），
容器重建后已受理地图不丢失。

## 本地运行（无 Docker 时）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 可选：DB_PATH=/your/path.db uvicorn app.main:app ...
```

## API 用法示例

### 1. 上传 / 整版更正

```bash
curl -X PUT http://localhost:8000/api/maps/2010版 \
  -H "Content-Type: application/json" \
  --data-binary @old.geojson
# 200 -> {"version":"2010版","revision":1,"feature_count":12,
#         "replaced_previous":false,"message":"...首次受理成功"}
```

GeoJSON 要素属性中的乡镇号字段取 `township_code`，其次 `code`，
值会统一转为字符串。再次 `PUT` 同版号即整版更正（`revision` 递增）；
若 payload 不合法，返回 400 且**旧版原样保留**。

### 2. 提交权重计算任务

```bash
curl -X POST http://localhost:8000/api/weights/jobs \
  -H "Content-Type: application/json" \
  -d '{"old_version":"2010版","new_version":"2020版"}'
# 202 -> {"job_id":"...","state":"pending","pinned_revisions":{...}}
```

### 3. 轮询并交付

```bash
curl http://localhost:8000/api/weights/jobs/{job_id}
```

`state` 为 `pending/running` 时继续轮询；`completed` 时返回：

```json
{
  "state": "completed",
  "result": {
    "area_unit": "m2",
    "crs": "PROJCRS[...Albers 等面积...]",
    "weights": [
      {
        "old_township_id": "OLD_A",
        "old_area_m2": 4277873.39,
        "covered_area_m2": 4277839.67,
        "uncovered_area_m2": 33.71,
        "coverage_ratio": 0.999992,
        "overlaps": [
          {"new_township_id": "NEW_1",
           "intersection_area_m2": 2851820.49,
           "ratio_of_old": 0.666644},
          {"new_township_id": "NEW_2",
           "intersection_area_m2": 1426019.18,
           "ratio_of_old": 0.333348}
        ]
      }
    ]
  }
}
```

- `ratio_of_old = 交叠面积 / 旧乡镇面积`，历史指标汇总时直接作为权重；
- 若源版在交付前被更正：返回 **409 + `state=stale`**，并在
  `error_detail.changed_versions` 中给出钉取/当前修订号，用同版号重新提交即可；
- 新版内部交叠：提交即 **409**，给出冲突乡镇号配对。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `/data/weighting.db`（容器内） | SQLite 文件路径 |
| `POSITIVE_AREA_EPS_M2` | `1e-6` | 平方米阈值，低于此交叠面积视为仅共边/数值噪声 |
| `MAX_WORKERS` | `4` | 后台计算线程数 |

## 测试

```bash
pip install pytest httpx
pytest -q
```

覆盖：上传校验与拒收保留、整版更正修订号、重复乡镇号、无效面、
WGS84 范围、洞面积扣除、仅共边不计、未覆盖面积、等面积投影精度、
新版互斥 409（含工作线程平方米复核）、计算期间更正的 stale 409 与重试、
重启持久化等 32 个用例。

## 设计说明与已知边界

- **投影选择**：Albers 等面积投影保证面积不失真，中央经线/纬度原点取两版
  并集质心，标准纬线相对原点 ±5°，适合县域尺度；面积与 pyproj `Geod`
  大地测量面积的相对误差 < 0.1%。
- **浮点缝隙**：相邻边界经重投影后可能出现极窄缝隙（实测约 1e-5 量级比例），
  表现为小额 `uncovered_area_m2`，可通过阈值或后续边界预处理（坐标对齐）控制。
- **任务态不持久化**：任务结果是可由地图重新派生的临时产物，进程重启后
  需重新提交；已受理地图本身持久化，不受重启影响。
- 超大图层（数十万要素）建议前置分片/抽稀；当前实现已使用 STRtree 空间索引
  避免配对计算 O(n²) 化，但单任务仍为单机线程模型。
