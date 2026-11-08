# 11 扩展与演进

## 11.1 功能扩展路线

| 阶段 | 扩展内容 | 前置条件 | 对现有设计的影响 |
| --- | --- | --- | --- |
| 二期 | 盘点全流程、批次与保质期管理、库存上下限与临期预警、周转率与呆滞料报表 | 一期数据积累 ≥3 个月 | 表结构已预留（`wms_stocktake_*`、`batch_no`、`expire_date`、`warning_low/high`），无需改表 |
| 二期 | 数据看板（ECharts 大屏） | 报表接口稳定 | 只读查询，新增只读账号 `wms_ro` |
| 三期 | PDA 手持端 + 条码扫码作业 | 完成目标机型兼容性验证（Android 10+ / Chrome 内核） | 新增扫码接口（按条码查商品/库位），入库出库明细支持扫码累加；表结构不变 |
| 三期 | RFID 批量识别 | 硬件选型与标签方案确定 | 新增批量识别接口，一次提交多行明细；过账逻辑与幂等机制复用 |
| 三期 | ERP / 电商平台对接 | 明确对方接口协议（REST/中间库/文件） | 新增对接模块与同步日志表，采用"对账 + 幂等"方式同步单据与库存 |
| 三期 | 多组织、多货主 | 业务模式确认 | 需引入组织/货主维度，库存唯一索引扩展为 `(owner_id, goods_id, loc_id, batch_no)`，属较大改造 |

## 11.2 容量与架构演进触发条件

| 现象 | 阈值 | 应对 |
| --- | --- | --- |
| 数据库 CPU 持续偏高 | 5 分钟平均 >70% 且慢 SQL 增多 | 优化索引与 SQL → 升级服务器规格 → 读写分离（报表走只读库） |
| 单表数据量过大 | 流水/日志表 >1,000 万行 | 按月归档 + 分区表（`PARTITION BY RANGE (TO_DAYS(create_time))`） |
| 并发在线人数超设计上限 | 持续 >设计值 80% | 升级服务器档位；必要时应用多实例 + Nginx 负载均衡（会话已集中存 Redis，可直接横向扩展） |
| 库存表热点冲突 | 乐观锁失败率 >5% | 热点商品按库位拆分库存行、缩短事务、必要时对热点行使用队列串行化 |
| 报表查询拖慢业务 | 报表 P95 >3 s | 引入只读从库或预汇总表（按日/按商品汇总），报表走汇总表 |
| 团队规模扩大 | 开发人员 >8 人、模块间频繁冲突 | 按 2.4 已划定的模块边界拆分服务（先拆报表与对接模块，保留库存与单据在同一事务内） |

## 11.3 不引入的技术（保持简单）

| 技术 | 不引入的原因 | 何时再考虑 |
| --- | --- | --- |
| 微服务 / 服务网格 | 单机 10—50 人规模下，分布式事务与运维复杂度远超收益 | 多实例部署且模块边界稳定后，按 11.2 逐模块拆分 |
| 消息队列（Kafka/RocketMQ） | 库存过账要求强一致，同步事务内完成更可靠；异步消息会引入最终一致补偿成本 | 出现大量异步场景（对接电商、批量导入、报表预计算）时 |
| Elasticsearch | 单表千万级以内 MySQL 索引足够；模糊搜索需求有限 | 需要商品全文检索或日志检索时 |
| 分库分表中间件 | 3 年数据量 5—15 GB，远未到单库瓶颈 | 单库 >500 GB 或多租户隔离需求明确时 |

# 附录

## 附录 A 核心表建表脚本（摘录）

完整建表脚本随文档交付，见 `db/schema.sql`（含 25 张表、全部索引与初始化数据）。以下为并发与业务核心表的建表语句。

```sql
-- 库存表：并发控制核心
CREATE TABLE `wms_inventory` (
  `id`            bigint unsigned NOT NULL AUTO_INCREMENT COMMENT '库存ID',
  `goods_id`      bigint unsigned NOT NULL COMMENT '商品ID',
  `loc_id`        bigint unsigned NOT NULL COMMENT '库位ID',
  `batch_no`      varchar(64) NOT NULL DEFAULT '' COMMENT '批次号（无批次管理存空串）',
  `quantity`      decimal(18,3) NOT NULL DEFAULT 0 COMMENT '实物数量',
  `locked_qty`    decimal(18,3) NOT NULL DEFAULT 0 COMMENT '预占数量',
  `available_qty` decimal(18,3) NOT NULL DEFAULT 0 COMMENT '可用量=quantity-locked_qty',
  `version`       int unsigned NOT NULL DEFAULT 0 COMMENT '乐观锁版本号',
  `expire_date`   date DEFAULT NULL COMMENT '到期日（二期）',
  `in_time`       datetime DEFAULT NULL COMMENT '最近入库时间（FIFO依据）',
  `update_time`   datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_goods_loc_batch` (`goods_id`,`loc_id`,`batch_no`),
  KEY `idx_loc_goods` (`loc_id`,`goods_id`),
  KEY `idx_goods_available` (`goods_id`,`available_qty`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='实时库存';

-- 库存流水（台账）：一手单据一手流水，唯一索引兜底幂等
CREATE TABLE `wms_inventory_txn` (
  `id`          bigint unsigned NOT NULL AUTO_INCREMENT,
  `txn_no`      varchar(64) NOT NULL COMMENT '流水号',
  `biz_type`    tinyint NOT NULL COMMENT '1采购入 2退货入 3其他入 4销售出 5领料出 6调拨出 7调拨入 8盘盈 9盘亏 10调整 11期初',
  `biz_id`      bigint unsigned NOT NULL COMMENT '来源单据ID',
  `biz_no`      varchar(64) DEFAULT NULL COMMENT '来源单据号',
  `detail_id`   bigint unsigned NOT NULL DEFAULT 0 COMMENT '来源明细ID',
  `goods_id`    bigint unsigned NOT NULL,
  `loc_id`      bigint unsigned NOT NULL,
  `batch_no`    varchar(64) NOT NULL DEFAULT '',
  `change_qty`  decimal(18,3) NOT NULL COMMENT '变动数量（正入负出）',
  `before_qty`  decimal(18,3) NOT NULL COMMENT '变动前',
  `after_qty`   decimal(18,3) NOT NULL COMMENT '变动后',
  `create_by`   bigint unsigned NOT NULL,
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_txn_no` (`txn_no`),
  UNIQUE KEY `uk_biz_detail` (`biz_type`,`biz_id`,`detail_id`),
  KEY `idx_goods_time` (`goods_id`,`create_time`),
  KEY `idx_loc_time` (`loc_id`,`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='库存流水（台账）';

-- 出库单主表：request_id 唯一索引实现过账幂等
CREATE TABLE `wms_out_order` (
  `id`           bigint unsigned NOT NULL AUTO_INCREMENT,
  `order_no`     varchar(64) NOT NULL COMMENT '出库单号',
  `order_type`   tinyint NOT NULL COMMENT '1销售出库 2领料出库 3调拨出库',
  `customer_id`  bigint unsigned DEFAULT NULL,
  `wh_id`        bigint unsigned NOT NULL,
  `status`       tinyint NOT NULL DEFAULT 0 COMMENT '0草稿 1已审核 2拣货中 3已出库 4已作废',
  `total_plan_num`   decimal(18,3) NOT NULL DEFAULT 0,
  `total_actual_num` decimal(18,3) NOT NULL DEFAULT 0,
  `request_id`   varchar(64) DEFAULT NULL COMMENT '过账幂等键',
  `audit_by`     bigint unsigned DEFAULT NULL,
  `audit_time`   datetime DEFAULT NULL,
  `remark`       varchar(255) DEFAULT NULL,
  `create_by`    bigint unsigned NOT NULL,
  `create_time`  datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_by`    bigint unsigned DEFAULT NULL,
  `update_time`  datetime DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `is_deleted`   tinyint(1) NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_order_no` (`order_no`),
  UNIQUE KEY `uk_request_id` (`request_id`),
  KEY `idx_status_time` (`status`,`create_time`),
  KEY `idx_wh_time` (`wh_id`,`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='出库单主表';
```

## 附录 B 单据状态流转

| 单据 | 状态流转 | 允许的操作 |
| --- | --- | --- |
| 入库单 | 草稿(0) → 已审核(1) → 部分入库(2) → 已入库(3)；草稿/已审核 → 已作废(4) | 草稿：修改、删除、提交审核；已审核：收货、确认入库、作废；部分入库：继续收货；已入库：只读、打印 |
| 出库单 | 草稿(0) → 已审核(1，预占可用量) → 拣货中(2) → 已出库(3)；草稿/已审核/拣货中 → 已作废(4，释放预占) | 草稿：修改、删除；已审核：打印拣货单、确认出库、作废；已出库：只读、打印 |
| 调拨单 | 草稿(0) → 已审核(1) → 已调拨(2)；草稿/已审核 → 已作废(3) | 确认调拨时同事务完成调出与调入 |
| 盘点单（二期） | 草稿(0) → 盘点中(1) → 待审核(2) → 已完成(3)；→ 已作废(4) | 完成后生成盘盈/盘亏流水，不可再修改 |

状态机约束：任何状态变更均校验前置状态（如"已入库"不可再确认入库，重复提交由幂等键拦截）；状态变更写入操作日志；作废需填写原因。

## 附录 C 系统参数与字典清单

| 参数键 | 默认值 | 说明 |
| --- | --- | --- |
| `order.no.rule` | `PREFIX+yyyyMMdd+SEQ4` | 单据编号规则 |
| `inventory.fifo.enabled` | `true` | 出库是否默认按 FIFO 推荐批次 |
| `inventory.negative.allowed` | `false` | 是否允许负库存（默认不允许，硬约束） |
| `inventory.lock.expire.hours` | `24` | 预占超时提醒阈值（超期单据列入待办） |
| `idempotent.cache.minutes` | `10` | 幂等结果缓存时长 |
| `session.timeout.minutes` | `30` | 前端无操作自动登出 |
| `login.fail.lock.count` | `5` | 连续登录失败锁定的次数 |
| `password.expire.days` | `90` | 密码有效期 |
| `export.sync.max.rows` | `20000` | 同步导出上限，超限转异步 |
| `upload.max.rows` | `5000` | Excel 导入单次行数上限 |
| `report.retain.months` | `36` | 报表数据在线保留月数 |
| 字典 `unit` | 个、件、箱、包、千克、米、卷 | 计量单位（含小数单位标记） |
| 字典 `biz_type` | 见 `wms_inventory_txn.biz_type` | 库存变动类型 |
| 字典 `adjust_reason` | 盘盈、盘亏、损耗、录错、漏记、其他 | 库存调整原因（必填） |
