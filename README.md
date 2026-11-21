# 集团购电碳排放盘查发布

面向集团碳盘查员的网页工具：按省份和月份发布购电排放明细。FastAPI + SQLite，Docker Compose 一键启动。

## 启动

```bash
docker compose up --build
# 打开 http://localhost:8000 （自动跳转「预览与发布」）
```

SQLite 数据持久化在命名卷 `carbon-data`（容器内 `/data/app.db`，可用环境变量 `APP_DB` 改路径）。

本地开发（不用 Docker）：

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload
.venv/bin/python -m pytest tests/   # 跑测试
```

## 功能与规则

- **用电补报/更正**（`/upload`）：上传 CSV，表头 `省份,月份,表号,千瓦时`（月份 YYYY-MM，UTF-8）。同一 省份+月份+表号 重复上传即更正，新表号即补报；有错行则整批拒绝。
- **排放因子维护**（`/factors`）：省级因子按 省份+年度、全国因子按 年度维护（单位 kg CO₂/kWh），可增改删。
- **预览与发布**（`/preview`）：选省份和月份，预览逐表排放与所用因子。
  - 因子优先级：**同年省级因子 → 同年全国因子 → 两者都缺则提示补录、暂缓发布**（确认按钮禁用，API 返回 422）。
  - **确认一致性**：预览时服务端保存快照（当期逐表用电明细 + 所用因子来源/数值）。确认时重算比对，只要用电明细或所用因子的来源、数值与预览不符即拒绝（HTTP 409），要求重新预览——**即使排放总量相同**（例如补录了与全国因子等值的省级因子，来源由全国变省级，也必须重新预览）。其他省份/月份/年份的改动不影响本快照，无需重新预览。
- **已发布查询**（`/published`）：查看每期最新发布的逐表排放与所用因子。发布为单事务写入新快照，旧发布记录不被修改，**新结果发布成功前原已发内容照常可查**。
- **最新发布索引**：为每个「省份+月」维护一行最新发布索引（`published_latest`）。发布时索引在同一事务内推进，索引只允许更大发布号覆盖。历史数据按批次补建（页面「最新发布索引补建」或接口）；补建期间列表仍即时按发布号取每期最新，结果与建齐后一致；中途重启从已提交批次继续。建齐后列表只查索引，不再扫描历次发布。

## 索引补建

- 默认启动时（`AUTO_BACKFILL=1`）后台分批补建，每批期数由 `BACKFILL_BATCH_SIZE`（默认 50）控制；也可在 `/published` 页面或 `POST /api/published/index/backfill` 手动补建。
- 每批一个事务：索引行与进度游标一起提交；批次中断整批回滚，重启后从上一已提交批次继续。
- 首批开始时快照历史期数用于进度展示；补建期间新发布的期不计入分母，但会立即写入索引。
- `GET /api/published/index/status` 返回 `completed`、`list_source`（`live`/`index`）、游标与进度。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/usage/upload` | 上传用电 CSV（multipart，字段名 `file`） |
| GET | `/api/usage?province=&month=` | 查询当前用电明细 |
| POST | `/api/factors` | 新增/更新因子 `{scope, province, year, factor}` |
| GET | `/api/factors?year=` | 因子列表 |
| DELETE | `/api/factors?scope=&province=&year=` | 删除因子 |
| POST | `/api/preview` | 生成预览 `{province, month}`，返回 `preview_id` |
| POST | `/api/publish` | 确认发布 `{preview_id}`；409=须重新预览，422=缺因子暂缓 |
| GET | `/api/published?province=&month=` | 该期最新发布明细 |
| GET | `/api/published/list` | 全部已发布期次（建齐后只查索引） |
| GET | `/api/published/index/status` | 索引补建进度 |
| POST | `/api/published/index/backfill` | 补建一批 `{batch_size}` |

## 目录结构

```
app/
  main.py        # 路由：页面 + JSON API，CSV 解析，后台补建线程
  services.py    # 业务：补报/更正、因子优先级、预览快照、确认发布、最新发布索引
  db.py          # SQLite 连接与建表（含 published_latest / published_index_state）
  templates/     # 四个页面
tests/test_flows.py  # 端到端流程测试
tests/test_index.py  # 索引补建、补建期间改发/查询、重启续建、失败回退
Dockerfile / docker-compose.yml
```
