# 地铁每日首程客流 · 差分隐私查询平台

地铁数据分析员向合作研究方提供每日首程客流统计的网页服务。基于 **Flask + SQLite**,
对每次新查询的结果添加 **Laplace(0, 2)** 噪声(安全随机源 `secrets.SystemRandom`),
并按 **每次 0.5、每日总额 2** 的隐私预算计费。查询页只显示加噪结果。

## 快速启动

### Docker Compose(推荐)

```bash
docker compose up --build
```

访问 http://localhost:8000 。SQLite 数据库保存在名为 `appdata` 的卷中,
容器重启后已发布结果、预算消耗与锁定状态均不丢失。

### 本地运行

```bash
pip install -r requirements.txt
python app.py            # 开发模式, 监听 8000 端口
# 或: gunicorn --bind 0.0.0.0:8000 --workers 2 --threads 8 app:app
```

数据库路径由环境变量 `APP_DB` 指定(默认 `./app.db`, 容器内为 `/data/app.db`)。

## 功能与规则

| 规则 | 实现 |
| --- | --- |
| 按日期上传 CSV(乘客ID, 进站, 出站), 每位乘客仅一行 | `POST /upload`, 乘客ID 重复即拒绝; 支持中/英文表头或无表头 |
| 锁定前可整日替换, 首个成功查询后拒绝替换 | `day_state.locked`, 替换与锁定检查在同一事务内 |
| 选择进/出站集合查询匹配人数 | `POST /query`, 集合与顺序、重复选择无关(排序去重后规范化) |
| 新查询加 Laplace(0, 2) 噪声, 扣 0.5 预算, 每日总额 2 | 逆变换采样 + `day_state.budget_spent` |
| 相同查询复用原结果, 不再扣费 | `queries` 表主键 `(date, in_key, out_key)`, 命中即返回 |
| 并发不超支、相同查询只生成一份结果 | `BEGIN IMMEDIATE` 写事务串行化 + 事务内复查 + 主键约束 |
| 预算不足不返回新结果 | 事务内检查, 不足即回滚并返回 403 |
| 扣费/保存结果/锁定源数据同生共死 | 三者在**同一 SQLite 事务**中提交或回滚 |
| 响应丢失、重启后重查仍返回原值 | 先提交事务再响应; 结果持久化于 SQLite |
| 撤回授权: 移除乘客在**所有日期**的源明细并登记 | `POST /withdraw`, 登记 + 跨日期删除 + 影响审计在**单个 `BEGIN IMMEDIATE` 事务**内完成 |
| 已锁定日期同样撤回 | 撤回不检查/不改变 `locked`、`budget_spent` 与历史 `queries` |
| 历史带噪结果仍按原值回查, 预算与锁定状态不变 | 缓存命中返回首次发布值; 未发布过的条件按**剩余明细**重新计数并照常扣费 |
| 撤回后含该 ID 的整日 CSV 整批拒绝, 原数据不变 | 上传事务内先查 `withdrawals`, 命中即回滚返回 `422`, 检查先于任何写操作 |
| 重复撤回幂等 | `withdrawals` 主键为乘客 ID, 已登记则直接返回历史影响、不改数据 |
| 撤回/上传/查询交错无中间态 | 所有写操作经 `BEGIN IMMEDIATE` 串行化; 失败/重启由事务回滚, 不会出现部分日期已移除 |
| 页面展示受影响日期、删除行数并可回查 | 撤回表单 + 撤回登记表; `GET /api/withdrawals` |

## 授权撤回语义

1. 在网页(区块④)或 `POST /withdraw` 输入乘客 ID, 服务端在**一个事务**内:
   登记撤回(`withdrawals`)→ 删除该乘客在每个日期的源明细(`passengers`)→
   按日期记录删除行数(`withdrawal_log`)。任一环节失败整体回滚,
   不会留下“部分日期已移除、其他日期仍参与统计”的状态。
2. 已被首个查询锁定的日期同样执行删除; 但当天的锁定标记、已用预算和
   已发布的加噪结果全部保持不变——历史条件仍返回首次发布的同一数值(不扣费)。
3. 撤回之后**未发布过**的查询条件按剩余明细计算真实人数再加噪, 并照常扣 0.5 预算。
4. 此后任何日期的整日上传 CSV 只要包含已撤回的乘客 ID, 整批拒绝(`422`),
   现有数据一行不动; 不含撤回 ID 的批次不受影响。
5. 对同一乘客重复撤回不再改动任何数据, 返回首次撤回时登记的受影响日期与行数。

## API

### `POST /upload`
表单字段: `date`(YYYY-MM-DD), `file`(CSV 文件) 或 `csv`(CSV 文本)。
- `200` `{ok, rows}` 替换成功
- `400` CSV 非法(空字段、乘客ID 重复、列数不足等)
- `409` 当天源数据已被首个查询锁定

### `POST /query`
JSON: `{"date": "2026-11-01", "in_stations": ["西直门"], "out_stations": ["国贸", "望京"]}`
- `200` `{ok, noisy_count, cached, budget_remaining?}` — 只返回加噪人数
- `403` 当日预算不足, 不返回新结果
- 相同日期 + 相同站点集合(任意顺序/重复)返回首次发布的同一数值

### 其他
- `GET /api/stations?date=` 当天出现过的全部站点(供页面多选)
- `GET /api/status?date=` 行数、锁定状态、预算消耗
- `GET /api/queries?date=` 当天已发布的全部加噪结果

### `POST /withdraw`
JSON 或表单: `{"passenger_id": "p3"}`
- `200` `{ok, passenger_id, already_withdrawn, affected_dates: [{date, deleted_rows}], total_deleted_rows, withdrawn_at}`
- `already_withdrawn=true` 表示重复撤回, 本次幂等、未改动数据
- 登记、全日期删除与影响审计在同一事务内, 失败整体回滚
- `400` 乘客 ID 为空

### `GET /api/withdrawals`
- 可选 `?passenger_id=` 过滤; 返回全部撤回登记及每个乘客的受影响日期/删除行数

> 上传接口新增 `422` 响应: CSV 含已撤回授权的乘客 ID 时整批拒绝,
> 响应体含 `withdrawn_ids`, 原数据保持不变。

## 测试

```bash
pip install pytest
python -m pytest tests/ -q
```

覆盖: 上传替换与锁定、规范化复用(乱序/重复)、预算上限(4 次新查询)、
并发不超支(多线程/多进程)、相同查询仅一份结果、重启后结果保持、
Laplace(0, 2) 噪声分布(均值 0、方差 8)、授权撤回(跨日期删除与登记、
锁定日期撤回且历史/预算/锁定不变、撤回后按剩余明细计数扣费、
含撤回 ID 整批拒绝 422、重复撤回幂等、提交失败整体回滚、撤回与查询交错一致性)。
