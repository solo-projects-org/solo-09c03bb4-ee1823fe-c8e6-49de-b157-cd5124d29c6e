# 乡镇边界面积权重 API

把**旧版乡镇面**映射到**新版边界**，为历史指标的跨版汇总提供面积权重。
技术栈：FastAPI + SQLite + Shapely 2 + pyproj。

## 功能与业务规则

| 规则 | 实现方式 |
| --- | --- |
| 按版上传 WGS84 GeoJSON（Polygon / MultiPolygon），要素带唯一乡镇号 | `PUT /api/maps/{version}`，支持 `application/json` 与 `multipart/form-data` 文件上传，两种入参均**逐块读取、逐要素流式受理** |
| 大图层解析内存不随要素总数线性增长 | 手写增量 JSON 识别器（`app/ingest.py`），内部缓冲只保留当前 token/当前要素；要素校验后按 200 条微批写入隔离暂存表 `map_staging`，稳态内存约为“单块 + 单要素 + 单微批” |
| 同版重传 = 整版更正 | 读完整个合法文件后才在单事务内从暂存表切换发布，版本修订号 `revision` 每次 +1 |
| 无效面 / 重复乡镇号 / 非面几何 / 缺乡镇号 / 坐标越界 → 拒收并保留旧版 | 字段与拒收规则不变；重复乡镇号由暂存表 `(staging_id, township_id)` 唯一约束拦截，失败返回 400、清理暂存，数据库回滚 |
| 尾部损坏 / 截断（JSON 与 multipart）→ 拒收且不发布 | 流式识别器在 EOF 处校验括号/引号闭合；multipart 额外严格校验结束 boundary。未读完整文件前不会发布，旧版与修订号原样保留 |
| 权重任务读不到半版 | 暂存数据对查询与权重任务不可见；只有发布事务提交后整版才出现在 `maps/map_features`，发布失败自动回滚 |
| 解析期间其他版可上传/查询；同版并发修订号按发布顺序递增 | 解析在线程池工作线程执行，事件循环不被占用；所有写入经单连接 `RLock` 串行化，同版并发上传的修订号严格 1..n 递增 |
| 等面积坐标计算 | 以数据中心构造 Albers 等面积投影（AEA，单位米），两版在同一 CRS 下计算 |
| 逐个旧乡镇返回与各新乡镇的交叠面积、占旧乡镇比例、未覆盖面积 | `POST /api/weights/jobs` 异步任务 + `GET /api/weights/jobs/{id}` 交付 |
| 洞内面积、仅共边部分不计入 | Shapely 面交集天然扣除内环；共边交集面积为 0，被 `POSITIVE_AREA_EPS_M2` 阈值过滤 |
| 新版乡镇彼此正面积交叠 → 拒绝计算，不能重复计权 | 提交时几何预检 409；工作线程在等面积投影后按平方米复核（双保险） |
| 计算期间仍可更正边界 | 计算在线程池执行，上传接口不被阻塞 |
| 任一源版变化 → 拒绝交付旧权重并提示重试 | 提交时钉取 `revision`，交付前比对当前修订号，变化则任务置 `stale` 并返回 409 |
| 重启后保留已受理地图 | 地图持久化在 SQLite（WAL）；任务为可重算的临时态，仅存内存；启动时清理上次异常退出遗留的暂存数据 |

## 目录结构

```
app/
  config.py   环境变量配置（DB_PATH、面积阈值等）
  db.py       SQLite 持久化：maps / map_features / map_staging，暂存隔离与事务化整版发布
  geo.py      GeoJSON 逐要素校验、AEA 等面积投影、互斥检查、交叠权重计算
  ingest.py   增量 JSON 识别器、multipart 事件式转译、异步请求体的工作线程桥接
  jobs.py     后台任务、版本修订号钉取、交付失效检测
  main.py     FastAPI 路由与 Swagger
tests/        pytest 套件（42 个用例，含流式解析、截断拒收、并发修订号）
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

覆盖：流式识别器在任意分块尺寸下与全量解析等价、JSON/multipart 尾部
截断拒收且修订号不变、重复乡镇号（含跨微批）、无效面、WGS84 范围、
洞面积扣除、仅共边不计、未覆盖面积、等面积投影精度、暂存/入库失败回滚、
真实 uvicorn 下的同版并发修订号递增、新版互斥 409（含工作线程平方米复核）、
计算期间更正的 stale 409 与重试、重启持久化等 42 个用例。

## 设计说明与已知边界

- **流式受理与内存边界**：受理不再 `await request.body()` 全量缓存，
  而是由 `ingest.StreamingFeatureCollection` 对请求体字节块做增量分词，
  每产出一个完整 Feature 即调用 `geo.validate_feature` 校验，并以
  200 条为微批写入 `map_staging`；解析期内存峰值实测与要素总数无关
  （5 万要素、约 10MB 请求体时解析+暂存峰值仍在亚 MB 级，主要内存来自
  当前要素的 Shapely 几何与单条 token）。multipart 上传经
  python-multipart 事件解析器逐块转译，同样不缓存整个文件。
- **发布原子性**：整版文件读完且暂存完整前，`maps/map_features` 不会被
  修改；发布在单事务内统计暂存数、递增修订号、删旧要素、拷入新要素，
  任一步失败全部回滚，旧版与修订号保持不变，权重任务因此不可能读到半版。
- **并发模型**：解析与几何校验在 anyio 工作线程执行，通过
  `from_thread` 回到事件循环拉取请求块，事件循环保持空闲；其他版的
  上传与查询在解析期间照常服务。所有数据库写操作经单连接 `RLock`
  串行化，同版并发上传按“成功发布顺序”取得严格递增、无缺号的修订号。
- **投影选择**：Albers 等面积投影保证面积不失真，中央经线/纬度原点取两版
  并集质心，标准纬线相对原点 ±5°，适合县域尺度；面积与 pyproj `Geod`
  大地测量面积的相对误差 < 0.1%。
- **浮点缝隙**：相邻边界经重投影后可能出现极窄缝隙（实测约 1e-5 量级比例），
  表现为小额 `uncovered_area_m2`，可通过阈值或后续边界预处理（坐标对齐）控制。
- **任务态不持久化**：任务结果是可由地图重新派生的临时产物，进程重启后
  需重新提交；已受理地图本身持久化，不受重启影响。未发布的暂存数据在
  进程启动时统一清理。
- 超大图层（数十万要素）单任务的权重计算仍为单机线程模型；受理阶段已
  使用 STRtree 空间索引避免配对计算 O(n²) 化，几何校验耗时与要素数成正比
  （沿用既有校验规则，未降低校验强度）。
