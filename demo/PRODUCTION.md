# 生产化说明（WMS V8）

> 目标：高峰期不超卖、不重发、不发空单、不丢单；能横向扩容。
> 本文说明**已经落到代码里的机制**、**部署形态**、**验收与故障演练清单**，以及**仍然存在的外部依赖**。

## 一、五条硬保证（代码级）

| 保证 | 机制 | 失败时的行为 |
|---|---|---|
| 不超卖 | 分配/扣减都是单条条件 UPDATE：`... WHERE id=? AND version=? AND available_qty>=?`；影响行数为 0 就换下一行/重试 | 返回 409 `50202`，绝不出现负可用 |
| 不重复扣减 | 所有写接口支持 `X-Idempotency-Key`，`sys_idempotency` 落库去重；重放返回首次响应并带 `replayed=true` | 重复请求 200 + 原结果，不会二次扣库存 |
| 不重复发货 | 发货按**履约单**推进：`ec_fulfillment` 1→2 只能迁一次；发货单落 `item_qty` | 已发履约单再发 → 409 |
| 不发空单 | 发货前校验：履约单有分配、`item_qty>0`、运单号非空、状态机允许 | 任一不满足 → 409 并说明原因 |
| 不丢单（买了没发货） | `已付款订单必须有分配`、`拆单数量与履约单一致`、`已发货必有发货单` 等 12 项一致性校验 | `/ops/checks` 报警；运维页可一键查看 |

## 二、拆单（跨仓履约）

```
支付 → 逐 SKU FIFO/FEFO 预占（locked_qty）
     → 按分配落到的仓库分组，生成 1..N 张 ec_fulfillment
     → 每张履约单独立：拣货 / 复核 / 打包 / 发货
     → 全部履约单发完，订单才置「已发货」；否则「部分发货(8)」
```

- 多张待发履约单时，`POST /ec/orders/{id}/ship` **必须**带 `fulfillment_id`，系统不替人猜（防止“随便发一仓”）。
- 每张发货单都带 `wh_id + item_qty`，可直接按仓对账、按仓算运费。

## 三、并发与削峰

| 层次 | 做法 |
|---|---|
| 连接 | SQLite：WAL + `synchronous=NORMAL` + `busy_timeout=5000`；MySQL：连接池 + 读写分离 |
| 请求 | 令牌桶限流（IP + 用户×模块），超限 429 + `Retry-After`；阈值 `WMS_RATE_PER_MINUTE` / `WMS_RATE_BURST` |
| 写路径 | 支付/发货保持**同步强一致**（该做的事不异步），但只做“必须原子”的部分 |
| 非强一致 | 自动并波、幂等键清理、告警扫描走 `sys_task_queue` + worker，可多进程消费；失败指数退避重试（默认 5 次） |
| 读路径 | 报表用 `db_readonly()`（MySQL 换从库）；热数据靠索引：`idx_txn_time`、`idx_ec_order_time/pay`、`idx_ec_item_goods` |
| 可观测 | 每请求一行 JSON 日志（trace/method/path/status/ms/ip）；`/ops/runtime` 输出 QPS、P50/P95/P99、错误率、队列积压 |

## 四、表关系与分片

- 订单域：`ec_order 1—N ec_order_item`、`1—N ec_order_alloc`、`1—N ec_fulfillment`、`1—N ec_shipment`、`1—N ec_after_sale`。
- 库存域：`wms_inventory`（库位×SKU×批次，唯一键）—`wms_inventory_txn`（每次变动一条，唯一键 `(biz_type,biz_id,detail_id)` 防重复记账）。
- 分片键：**订单按 order_id**、**库存按 wh_id**（同仓事务不跨库），日志按月分区。跨分片只读汇总表。

## 五、迁移

```bash
# 老库升级（幂等，启动自动执行；也可手动）
python -c "import app; app.init_db()"
curl -H "Authorization: Bearer $T" http://127.0.0.1:8000/api/v1/ops/migrations
```
`migrations` 表记录版本与耗时；每条迁移独立事务，失败即中断，不会留半截版本。

## 六、验收清单（本仓库已自动化）

```powershell
tests/api_smoke.ps1     # 主链路：下单→支付→波次→复核→打包→发货→售后→12 项校验
tests/rbac_smoke.ps1    # 权限拒绝 + 仓库隔离 + 改权限即时生效
tests/v5_smoke.ps1      # 成本/盘点/调拨/批处理/报表
tests/prod_smoke.ps1    # 并发抢库存不超卖、幂等、拆单、防空单、队列、运行时指标
```

## 七、故障演练（上线前必须做一次）

| 演练 | 期望 |
|---|---|
| 并发 200 单抢同一 SKU | 成功数 × 数量 ≤ 初始可用；库存不为负；`/ops/checks` 全绿 |
| 同一 `Idempotency-Key` 重复支付 5 次 | 只产生一次扣减；响应 `replayed=true` |
| 跨仓订单只发一个仓 | 订单为「部分发货」；再发第二仓才变「已发货」 |
| 发货时删掉运单号 | 409 拒绝，库存与预占不变 |
| 杀掉 worker 进程 | 任务仍留在队列（status=0/1），重启后继续消费，不丢任务 |
| 断网重试支付 | 幂等键命中，返回原结果，不重复发货 |

## 八、已知外部依赖（代码无法替代）

1. **MySQL 实跑**：脚本已含 V8 增量，但本机无 MySQL 实例，需在测试库执行并回归。
2. **Redis**：多实例限流与分布式锁建议用 Redis；当前限流是进程内（单实例精确，多实例按实例数放大）。
3. **消息队列**：`sys_task_queue` 适合中小流量；百万级/天建议换 Kafka/RabbitMQ，接口不变。
4. **监控**：`/ops/runtime` 输出指标，接 Prometheus 时用 exporter 抓取即可。
5. **平台对接**：电子面单/支付/平台订单回传仍需各平台凭证。

## 九、实测结果（本机真实环境，2026-11-08）

### 9.1 MySQL 8.0.31（本机实例，root/root，库名 wms_prod）

```
mysql -u root -proot -e "DROP DATABASE IF EXISTS wms_prod; CREATE DATABASE wms_prod DEFAULT CHARSET utf8mb4;"
mysql -u root -proot wms_prod -e "source db/schema_mysql.sql"
→ 46 张表、11 个外键、48 个唯一索引、1 个 JSON 列，exit=0
```
覆盖到：`ec_shipment`（原脚本遗漏，已补）、`ec_fulfillment`、`sys_idempotency`、`sys_task_queue`、`wms_daily_stat`、`sys_rate_limit`。

### 9.2 压测（scripts/loadtest.py，单进程 Python + SQLite）

| 场景 | 请求 | 成功 | QPS | P50 | P95 | P99 |
|---|---|---|---|---|---|---|
| 读（商城列表，20 线程 8s） | 1112 | 1112 | 137.6 | 120ms | 292ms | 434ms |
| 写（下单，10 线程 6s） | 1152 | 1152 | 187.1 | 30ms | 105ms | 569ms |

压测过程中一致性校验始终 allPassed=True。

### 9.3 压测暴露并修掉的 3 个真问题

| 问题 | 现象 | 修复 |
|---|---|---|
| 监听 backlog 默认 5 | 突发连接被内核拒绝，压测出现 ~95% 连接异常 | `request_queue_size=512` + `allow_reuse_address` |
| HTTP/1.0 短连接 | 6000 次/8s 打满 TIME_WAIT，P99 飙到 1s | `protocol_version="HTTP/1.1"` + 每响应都有 Content-Length |
| 限流分支先写 header 再写状态行 | 响应协议损坏、连接被重置（压测 95% 失败的真因） | 统一由 send_json 在状态行之后写 header；并发闸门超限返回 503 |

### 9.4 没有 Redis 怎么办

限流默认走进程内令牌桶（单实例精确）。多实例用 `wms_infra.DbRateLimiter`：把计数写进 `sys_rate_limit` 表（按分钟窗口），任意实例都能看到，语义与 Redis 版一致，只是多一次 UPSERT。等有 Redis 时，替换这个类即可，调用点不动。

### 9.5 第二轮压测（长压测暴露的真 bug + 修复效果）

第一轮（60 s / 10 线程）实测：下单 68 QPS、P99 2526 ms，出现 9 个 `50000` 内部错误。
定位到两个根因并修复：

| 根因 | 说明 | 修复 |
|---|---|---|
| 单号生成用 `COUNT(*) LIKE '前缀+日期%'` | 每单扫一次表；并发下两个请求会算出同一个号 → `UNIQUE constraint failed: order_no` → `50000` | 改用 `sys_seq` 原子序列（`UPDATE ... RETURNING`），O(1) 且不撞号 |
| `busy_timeout=5000` | 写锁排队超过 5 秒即报错，正好对应 5.6 s 的尾刺 | 放宽到 15000 ms + `wal_autocheckpoint=4000` + 批处理里做被动检查点 |
| 每请求一行 JSON 日志 | 180 QPS 下日志 I/O 本身抬高 P99 | 采样：错误全记，正常请求只记 ≥300 ms 的 |

**同参数复测（60 s / 10 线程，下单）**：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 请求数 | 4096 | **10958** |
| 系统错误 | 9（50000） | **0** |
| QPS | 68.0 | **182.2** |
| P50 | 48.8 ms | 31.2 ms |
| P95 | 454.5 ms | 114.0 ms |
| P99 | 2526.6 ms | **572.7 ms** |

支付阶段（真扣库存）60 s / 10 线程：768 单成功，3560 次 `50201 库存不足`——
这是**业务拒绝**（库存真的卖完了），不是故障；`loadtest.py` 现在把它和系统错误分开统计，
并输出「有效成功率」，避免把正常超卖保护误读成不稳定。
