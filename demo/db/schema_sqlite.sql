-- =============================================================================
-- WMS 本地演示库（SQLite 版）
-- 与《WMS 项目方案与技术文档 V2.0》的 MySQL 版表结构保持一致（去掉 ENGINE/COMMENT/UNSIGNED 等 MySQL 专属语法）
-- 该文件仅作参考，实际由 app.py 的 init_db() 自动建库并灌入演示数据
-- =============================================================================

-- 用户
CREATE TABLE IF NOT EXISTS sys_user (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT    NOT NULL UNIQUE,
  password_hash TEXT    NOT NULL,           -- sha256(salt + password)，演示用；生产用 BCrypt
  salt          TEXT    NOT NULL,
  real_name     TEXT    NOT NULL,
  phone         TEXT,
  status        INTEGER NOT NULL DEFAULT 1, -- 1启用 0停用
  create_time   TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 操作日志（仅追加）
CREATE TABLE IF NOT EXISTS sys_operation_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER NOT NULL,
  user_name   TEXT    NOT NULL,
  op_time     TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  ip          TEXT,
  module      TEXT,
  op_type     TEXT    NOT NULL,
  biz_id      INTEGER,
  biz_no      TEXT,
  params      TEXT,
  result      TEXT    NOT NULL DEFAULT 'SUCCESS',
  error_msg   TEXT,
  cost_ms     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_log_time ON sys_operation_log(op_time);

-- 商品分类
CREATE TABLE IF NOT EXISTS base_goods_category (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  category_code TEXT NOT NULL UNIQUE,
  category_name TEXT NOT NULL,
  parent_id     INTEGER NOT NULL DEFAULT 0,
  status        INTEGER NOT NULL DEFAULT 1
);

-- 商品
CREATE TABLE IF NOT EXISTS base_goods (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  goods_code   TEXT NOT NULL UNIQUE,
  goods_name   TEXT NOT NULL,
  category_id  INTEGER NOT NULL REFERENCES base_goods_category(id),
  spec         TEXT,
  unit         TEXT NOT NULL,
  barcode      TEXT,
  cost_price   REAL NOT NULL DEFAULT 0,
  status       INTEGER NOT NULL DEFAULT 1,
  remark       TEXT,
  create_time  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_goods_name ON base_goods(goods_name);

-- 仓库-库区-货架-库位 四级树
CREATE TABLE IF NOT EXISTS base_location (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  loc_code   TEXT NOT NULL UNIQUE,
  loc_name   TEXT NOT NULL,
  parent_id  INTEGER NOT NULL DEFAULT 0,
  loc_type   INTEGER NOT NULL,            -- 1仓库 2库区 3货架 4库位
  wh_id      INTEGER NOT NULL DEFAULT 0,  -- 所属仓库（冗余，数据权限/过滤用）
  status     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_loc_parent ON base_location(parent_id);

-- 往来单位
CREATE TABLE IF NOT EXISTS base_partner (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  partner_type TEXT NOT NULL,              -- supplier / customer
  partner_code TEXT NOT NULL,
  partner_name TEXT NOT NULL,
  contact      TEXT,
  phone        TEXT,
  status       INTEGER NOT NULL DEFAULT 1,
  UNIQUE(partner_type, partner_code)
);

-- 库存：商品 × 库位 × 批次（并发核心）
CREATE TABLE IF NOT EXISTS wms_inventory (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  goods_id      INTEGER NOT NULL REFERENCES base_goods(id),
  loc_id        INTEGER NOT NULL REFERENCES base_location(id),
  batch_no      TEXT    NOT NULL DEFAULT '',
  quantity      REAL    NOT NULL DEFAULT 0,   -- 实物数量
  locked_qty    REAL    NOT NULL DEFAULT 0,   -- 预占数量
  available_qty REAL    NOT NULL DEFAULT 0,   -- 可用量 = quantity - locked_qty
  version       INTEGER NOT NULL DEFAULT 0,   -- 乐观锁版本号
  in_time       TEXT,
  update_time   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  CHECK(quantity >= 0),
  CHECK(locked_qty >= 0),
  CHECK(available_qty >= 0),
  CHECK(available_qty <= quantity),
  UNIQUE(goods_id, loc_id, batch_no)
);
CREATE INDEX IF NOT EXISTS idx_inv_loc ON wms_inventory(loc_id, goods_id);
CREATE INDEX IF NOT EXISTS idx_inv_goods ON wms_inventory(goods_id, available_qty);

-- 库存流水（台账）
CREATE TABLE IF NOT EXISTS wms_inventory_txn (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  txn_no      TEXT    NOT NULL UNIQUE,
  biz_type    INTEGER NOT NULL,   -- 1采购入 2退货入 3其他入 4销售出 5领料出 10调整 11期初
  biz_id      INTEGER NOT NULL,
  biz_no      TEXT,
  detail_id   INTEGER NOT NULL DEFAULT 0,
  goods_id    INTEGER NOT NULL,
  loc_id      INTEGER NOT NULL,
  batch_no    TEXT    NOT NULL DEFAULT '',
  change_qty  REAL    NOT NULL,
  before_qty  REAL    NOT NULL,
  after_qty   REAL    NOT NULL,
  create_by   INTEGER NOT NULL,
  create_time TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE(biz_type, biz_id, detail_id)        -- 过账幂等的数据库层保险
);
CREATE INDEX IF NOT EXISTS idx_txn_goods ON wms_inventory_txn(goods_id, create_time);
CREATE INDEX IF NOT EXISTS idx_txn_loc ON wms_inventory_txn(loc_id, create_time);

-- 入库单
CREATE TABLE IF NOT EXISTS wms_in_order (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  order_no         TEXT NOT NULL UNIQUE,
  order_type       INTEGER NOT NULL DEFAULT 1,
  partner_id       INTEGER,
  wh_id            INTEGER NOT NULL,
  status           INTEGER NOT NULL DEFAULT 0,  -- 0草稿 1已审核 2部分入库 3已入库 4已作废
  total_plan_num   REAL NOT NULL DEFAULT 0,
  total_actual_num REAL NOT NULL DEFAULT 0,
  request_id       TEXT UNIQUE,                 -- 过账幂等键
  audit_by         INTEGER,
  audit_time       TEXT,
  remark           TEXT,
  create_by        INTEGER NOT NULL,
  create_time      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS wms_in_order_detail (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id   INTEGER NOT NULL REFERENCES wms_in_order(id),
  goods_id   INTEGER NOT NULL,
  loc_id     INTEGER NOT NULL,
  plan_num   REAL NOT NULL DEFAULT 0,
  actual_num REAL NOT NULL DEFAULT 0,
  batch_no   TEXT NOT NULL DEFAULT '',
  unit       TEXT,
  CHECK(plan_num > 0),
  CHECK(actual_num > 0)
);
CREATE INDEX IF NOT EXISTS idx_ind_order ON wms_in_order_detail(order_id);

-- 出库单
CREATE TABLE IF NOT EXISTS wms_out_order (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  order_no         TEXT NOT NULL UNIQUE,
  order_type       INTEGER NOT NULL DEFAULT 1,
  partner_id       INTEGER,
  wh_id            INTEGER NOT NULL,
  status           INTEGER NOT NULL DEFAULT 0,  -- 0草稿 1已审核(已预占) 2拣货中 3已出库 4已作废
  total_plan_num   REAL NOT NULL DEFAULT 0,
  total_actual_num REAL NOT NULL DEFAULT 0,
  request_id       TEXT UNIQUE,
  audit_by         INTEGER,
  audit_time       TEXT,
  remark           TEXT,
  create_by        INTEGER NOT NULL,
  create_time      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS wms_out_order_detail (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id    INTEGER NOT NULL REFERENCES wms_out_order(id),
  goods_id    INTEGER NOT NULL,
  loc_id      INTEGER NOT NULL,
  plan_num    REAL NOT NULL DEFAULT 0,
  actual_num  REAL NOT NULL DEFAULT 0,
  batch_no    TEXT NOT NULL DEFAULT '',
  inv_version INTEGER,
  unit        TEXT,
  CHECK(plan_num > 0),
  CHECK(actual_num > 0)
);
CREATE INDEX IF NOT EXISTS idx_outd_order ON wms_out_order_detail(order_id);

-- =============================================================================
-- 电商通用扩展（V3）—— 「用户购买 → 订单 → 库存分配 → 拣货发货 → 售后」这条链路的落地表
--
-- 设计要点（为什么必须新增这些表，而不是给 base_goods 加几个字段）：
--   1. ec_order 是「用户购买」的承载物。商城下单不改库存，只有「支付（分配预占）」和
--      「发货（过账扣减）」两个动作会经由库存域改动物理库存，保证库存只有一个写入口。
--   2. ec_order_alloc 是「订单行 → 库位×批次」的桥。一个订单行可能拆到多个库位/批次
--      （FIFO），反过来同一库位也可能同时服务多张订单——没有这张表就无法表达「分配」。
--   3. 电商发货/退货入库用独立 biz_type（21/22），不复用出库单(4)/入库单(1/2)：
--      wms_inventory_txn 的唯一索引是 (biz_type,biz_id,detail_id)，而 biz_id 只是个整数，
--      不同来源表的自增 ID 会重合，共用 biz_type 会把两条真实流水误判成重复而丢记账。
-- =============================================================================

-- 渠道 / 店铺（自营商城 + 预留平台店铺）
CREATE TABLE IF NOT EXISTS base_shop (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_code    TEXT NOT NULL UNIQUE,
  shop_name    TEXT NOT NULL,
  channel_type TEXT NOT NULL DEFAULT 'self',   -- self 自营商城 / taobao 淘宝 / douyin 抖音 / pdd 拼多多
  status       INTEGER NOT NULL DEFAULT 1,
  remark       TEXT
);

-- 商品上下架（店铺 × 商品），库存可售量按渠道分配
CREATE TABLE IF NOT EXISTS ec_listing (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id     INTEGER NOT NULL REFERENCES base_shop(id),
  goods_id    INTEGER NOT NULL REFERENCES base_goods(id),
  sale_price  REAL    NOT NULL DEFAULT 0,
  sale_status INTEGER NOT NULL DEFAULT 1,      -- 1上架 0下架（下架即刻停止接单）
  reserve_qty REAL    NOT NULL DEFAULT 0,      -- 该渠道保留量（不参与本渠道售卖，给线下/其他渠道留货）
  sort_no     INTEGER NOT NULL DEFAULT 0,
  update_time TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE(shop_id, goods_id)
);
CREATE INDEX IF NOT EXISTS idx_listing_shop ON ec_listing(shop_id, sale_status);

-- 商城订单（用户购买行为的唯一承载物）
CREATE TABLE IF NOT EXISTS ec_order (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  order_no         TEXT NOT NULL UNIQUE,       -- 内部单号 DS+yyyyMMdd+4位
  channel_order_no TEXT,                       -- 平台单号（预留多渠道回传/对账）
  shop_id          INTEGER NOT NULL REFERENCES base_shop(id),
  buyer_name       TEXT NOT NULL,
  buyer_phone      TEXT,
  receiver_name    TEXT,
  receiver_phone   TEXT,
  receiver_address TEXT,
  status           INTEGER NOT NULL DEFAULT 0, -- 0待付款 1待发货(已分配预占) 2拣货中 3已发货 4已完成 5已取消 6售后中
  goods_amount     REAL NOT NULL DEFAULT 0,
  freight          REAL NOT NULL DEFAULT 0,
  pay_amount       REAL NOT NULL DEFAULT 0,
  request_id       TEXT UNIQUE,                -- 支付幂等键
  ship_qty         REAL NOT NULL DEFAULT 0,    -- 已发货件数（对账口径）
  split_count      INTEGER NOT NULL DEFAULT 0, -- 拆出的履约单数量（按仓）
  idem_key         TEXT,                       -- 客户端幂等键（审计用）
  cancel_reason    TEXT,
  create_time      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  pay_time         TEXT,
  ship_time        TEXT,
  finish_time      TEXT,
  create_by        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ec_order_status ON ec_order(status, create_time);
CREATE INDEX IF NOT EXISTS idx_ec_order_shop ON ec_order(shop_id, create_time);

CREATE TABLE IF NOT EXISTS ec_order_item (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES ec_order(id),
  goods_id INTEGER NOT NULL,
  goods_code TEXT,                             -- 下单时快照，商品改名不影响历史订单
  goods_name TEXT,
  price    REAL NOT NULL DEFAULT 0,            -- 成交单价（下单时快照）
  qty      REAL NOT NULL DEFAULT 0,
  amount   REAL NOT NULL DEFAULT 0,
  CHECK(qty > 0)
);
CREATE INDEX IF NOT EXISTS idx_ec_item_order ON ec_order_item(order_id);

-- 库存分配：订单行 → 库位 × 批次（卖出时点了哪个货位的货）
CREATE TABLE IF NOT EXISTS ec_order_alloc (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id   INTEGER NOT NULL REFERENCES ec_order(id),
  item_id    INTEGER NOT NULL REFERENCES ec_order_item(id),
  goods_id   INTEGER NOT NULL,
  loc_id     INTEGER NOT NULL,
  batch_no   TEXT    NOT NULL DEFAULT '',
  alloc_qty  REAL    NOT NULL DEFAULT 0,       -- 分配数量（已锁定在 locked_qty 里）
  status     INTEGER NOT NULL DEFAULT 1,       -- 1已分配 2已拣货 3已发货 4已释放
  fulfillment_id INTEGER,                      -- 归属履约单（按仓拆单）
  wh_id      INTEGER,                          -- 冗余仓库，便于按仓聚合/分片
  create_time TEXT   NOT NULL DEFAULT (datetime('now','localtime')),
  CHECK(alloc_qty > 0)
);
CREATE INDEX IF NOT EXISTS idx_alloc_order ON ec_order_alloc(order_id);
CREATE INDEX IF NOT EXISTS idx_alloc_status ON ec_order_alloc(status, goods_id);

-- 履约单：一个订单可能跨多个仓，按仓拆成 1..N 张履约单，各自拣货/打包/发货
CREATE TABLE IF NOT EXISTS ec_fulfillment (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id    INTEGER NOT NULL REFERENCES ec_order(id),
  wh_id       INTEGER NOT NULL,
  seq         INTEGER NOT NULL DEFAULT 1,
  status      INTEGER NOT NULL DEFAULT 1,     -- 1待发货 2已发货
  total_qty   REAL NOT NULL DEFAULT 0,
  shipped_qty REAL NOT NULL DEFAULT 0,
  create_time TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  ship_time   TEXT
);
CREATE INDEX IF NOT EXISTS idx_ff_order ON ec_fulfillment(order_id, status);
CREATE INDEX IF NOT EXISTS idx_ff_wh ON ec_fulfillment(wh_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS uk_ff_order_seq ON ec_fulfillment(order_id, seq);

-- 接口幂等：同一 Idempotency-Key 只执行一次（支付/发货/退款防重复）
CREATE TABLE IF NOT EXISTS sys_idempotency (
  idem_key    TEXT PRIMARY KEY,
  method      TEXT NOT NULL,
  path        TEXT NOT NULL,
  user_id     INTEGER,
  state       INTEGER NOT NULL DEFAULT 0,
  status_code INTEGER,
  response    TEXT,
  create_time TEXT NOT NULL,
  finish_time TEXT
);
CREATE INDEX IF NOT EXISTS idx_idem_time ON sys_idempotency(create_time);

-- 任务队列：把非强一致动作（自动并波、清理）移出请求链路，峰值削峰
CREATE TABLE IF NOT EXISTS sys_task_queue (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  task_type    TEXT NOT NULL,
  payload      TEXT,
  status       INTEGER NOT NULL DEFAULT 0,    -- 0待处理 1处理中 2成功 3失败
  attempts     INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 5,
  available_at TEXT NOT NULL,
  locked_by    TEXT,
  last_error   TEXT,
  create_time  TEXT NOT NULL,
  update_time  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_pick ON sys_task_queue(status, available_at);
CREATE INDEX IF NOT EXISTS idx_task_type ON sys_task_queue(task_type, create_time);

-- 发货单（物流单号回传/客户查询的凭据）
CREATE TABLE IF NOT EXISTS ec_shipment (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  shipment_no TEXT NOT NULL UNIQUE,
  order_id    INTEGER NOT NULL REFERENCES ec_order(id),
  carrier     TEXT,                             -- 顺丰/中通/京东/自提
  tracking_no TEXT,
  status      INTEGER NOT NULL DEFAULT 1,       -- 1已发货 2已签收
  fulfillment_id INTEGER,                       -- 对应履约单（按仓拆单）
  wh_id       INTEGER,                          -- 发货仓（拆单维度）
  item_qty    REAL NOT NULL DEFAULT 0,          -- 本单发货件数（<=0 视为空单异常）
  create_time TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  create_by   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_shipment_order ON ec_shipment(order_id);

-- 售后（退货退款 / 仅退款），退货收货时回写库存
CREATE TABLE IF NOT EXISTS ec_after_sale (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  after_no   TEXT NOT NULL UNIQUE,
  order_id   INTEGER NOT NULL REFERENCES ec_order(id),
  item_id    INTEGER,
  goods_id   INTEGER NOT NULL,
  qty        REAL    NOT NULL DEFAULT 0,
  type       INTEGER NOT NULL DEFAULT 1,        -- 1退货退款 2仅退款（不回收货）
  status     INTEGER NOT NULL DEFAULT 0,        -- 0待审核 1同意待收货 2已完成 3已拒绝
  reason     TEXT,
  loc_id     INTEGER,                           -- 退回哪个库位（默认原发货库位）
  batch_no   TEXT NOT NULL DEFAULT '',
  create_time TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  finish_time TEXT,
  create_by  INTEGER,
  CHECK(qty > 0)
);
CREATE INDEX IF NOT EXISTS idx_aftersale_order ON ec_after_sale(order_id);

-- 补货政策（安全库存 / 补货点 / 补货批量），补货建议由库存实时计算
CREATE TABLE IF NOT EXISTS wms_stock_policy (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  goods_id      INTEGER NOT NULL REFERENCES base_goods(id),
  wh_id         INTEGER NOT NULL DEFAULT 0,     -- 0 = 该商品所有仓库汇总口径
  safety_stock  REAL    NOT NULL DEFAULT 0,     -- 低于它就要补货
  replenish_qty REAL    NOT NULL DEFAULT 0,     -- 一次补多少（0 表示按安全库存差额补）
  lead_days     INTEGER NOT NULL DEFAULT 7,     -- 供应商交期（天）
  update_time   TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE(goods_id, wh_id)
);

-- 系统参数（运维层可改，业务代码读它而非硬编码）
CREATE TABLE IF NOT EXISTS sys_config (
  config_key   TEXT PRIMARY KEY,
  config_value TEXT NOT NULL,
  remark       TEXT,
  update_time  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- =============================================================================
-- V4：RBAC 真正启用（V2 只在 MySQL 脚本里画了表，演示库一直没有这些表）
--   权限模型 = 菜单/按钮权限（perm_code）+ 仓库数据权限（data_scope + sys_user_warehouse）
--   校验落点：接口层统一拦（ROUTE_PERM 映射），业务代码只关心 has_perm 的结果
-- =============================================================================
CREATE TABLE IF NOT EXISTS sys_role (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  role_code  TEXT NOT NULL UNIQUE,
  role_name  TEXT NOT NULL,
  data_scope INTEGER NOT NULL DEFAULT 3,   -- 1全部 2自定义 3本仓库 4本仓库及下级
  status     INTEGER NOT NULL DEFAULT 1,
  remark     TEXT
);

CREATE TABLE IF NOT EXISTS sys_permission (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  perm_code TEXT NOT NULL UNIQUE,          -- 如 ec:order:ship / inventory:view
  perm_name TEXT NOT NULL,
  perm_type INTEGER NOT NULL DEFAULT 1,    -- 1菜单 2按钮/操作
  module    TEXT NOT NULL DEFAULT '',      -- 所属模块（前端按模块分组渲染）
  sort_no   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sys_user_role (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  role_id INTEGER NOT NULL,
  UNIQUE(user_id, role_id)
);

CREATE TABLE IF NOT EXISTS sys_role_permission (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  role_id       INTEGER NOT NULL,
  permission_id INTEGER NOT NULL,
  UNIQUE(role_id, permission_id)
);

-- 仓库数据权限：用户能看/能操作哪些仓库（data_scope=1 时忽略本表）
CREATE TABLE IF NOT EXISTS sys_user_warehouse (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  wh_id   INTEGER NOT NULL,
  UNIQUE(user_id, wh_id)
);

CREATE TABLE IF NOT EXISTS sys_login_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER,
  username    TEXT NOT NULL,
  login_time  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  ip          TEXT,
  result      TEXT NOT NULL DEFAULT 'SUCCESS',
  fail_reason TEXT,
  user_agent  TEXT
);
CREATE INDEX IF NOT EXISTS idx_login_time ON sys_login_log(login_time);

-- =============================================================================
-- V5：补齐主流 WMS 的四个作业/核算模块
--   盘点（全盘/抽盘 + 差异审批入账）、调拨（仓间/库位间）、
--   移动加权成本（流水带成本）、批次效期（FEFO + 临期预警）
--   列级增量（avg_cost / expire_date / unit_cost / cost_amount）由 ensure_column 补。
-- =============================================================================

-- 盘点单
CREATE TABLE IF NOT EXISTS wms_stocktake_order (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  order_no       TEXT NOT NULL UNIQUE,          -- PD + yyyyMMdd + 4 位
  stocktake_type INTEGER NOT NULL DEFAULT 1,    -- 1全盘（仓内全部库存） 2抽盘（指定库位）
  wh_id          INTEGER NOT NULL,
  status         INTEGER NOT NULL DEFAULT 0,    -- 0草稿 1盘点中 2待审核 3已完成 4已作废
  plan_count     INTEGER NOT NULL DEFAULT 0,    -- 应盘行数
  actual_count   INTEGER NOT NULL DEFAULT 0,    -- 已录入行数
  diff_count     INTEGER NOT NULL DEFAULT 0,    -- 有差异行数
  diff_in_qty    REAL NOT NULL DEFAULT 0,       -- 盘盈合计
  diff_out_qty   REAL NOT NULL DEFAULT 0,       -- 盘亏合计
  audit_by       INTEGER,
  audit_time     TEXT,
  remark         TEXT,
  create_by      INTEGER NOT NULL,
  create_time    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  update_time    TEXT
);
CREATE INDEX IF NOT EXISTS idx_st_status ON wms_stocktake_order(status, create_time);

CREATE TABLE IF NOT EXISTS wms_stocktake_detail (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id    INTEGER NOT NULL REFERENCES wms_stocktake_order(id),
  goods_id    INTEGER NOT NULL,
  loc_id      INTEGER NOT NULL,
  batch_no    TEXT NOT NULL DEFAULT '',
  book_qty    REAL NOT NULL DEFAULT 0,          -- 账面数（生成时快照）
  actual_qty  REAL,                             -- 实盘数（NULL = 未盘）
  diff_qty    REAL NOT NULL DEFAULT 0,          -- 盈亏（正=盘盈，负=盘亏）
  diff_reason TEXT,
  update_time TEXT,
  UNIQUE(order_id, goods_id, loc_id, batch_no)
);
CREATE INDEX IF NOT EXISTS idx_std_order ON wms_stocktake_detail(order_id);

-- 调拨单（仓间/库位间）
CREATE TABLE IF NOT EXISTS wms_transfer_order (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  order_no     TEXT NOT NULL UNIQUE,            -- DB + yyyyMMdd + 4 位
  from_wh_id   INTEGER NOT NULL,
  to_wh_id     INTEGER NOT NULL,
  status       INTEGER NOT NULL DEFAULT 0,      -- 0草稿 1已审核(调出已预占) 2已调拨 3已作废
  total_qty    REAL NOT NULL DEFAULT 0,
  request_id   TEXT UNIQUE,                     -- 过账幂等键
  audit_by     INTEGER,
  audit_time   TEXT,
  cancel_reason TEXT,
  remark       TEXT,
  create_by    INTEGER NOT NULL,
  create_time  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  update_time  TEXT
);
CREATE INDEX IF NOT EXISTS idx_tf_status ON wms_transfer_order(status, create_time);

CREATE TABLE IF NOT EXISTS wms_transfer_detail (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id    INTEGER NOT NULL REFERENCES wms_transfer_order(id),
  goods_id    INTEGER NOT NULL,
  from_loc_id INTEGER NOT NULL,
  to_loc_id   INTEGER NOT NULL,
  batch_no    TEXT NOT NULL DEFAULT '',
  qty         REAL NOT NULL DEFAULT 0,
  CHECK(qty > 0)
);
CREATE INDEX IF NOT EXISTS idx_tfd_order ON wms_transfer_detail(order_id);

-- 批处理任务日志（超时关单等；运维层可手动触发）
CREATE TABLE IF NOT EXISTS sys_job_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  job_code   TEXT NOT NULL,
  run_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  affected   INTEGER NOT NULL DEFAULT 0,
  detail     TEXT,
  result     TEXT NOT NULL DEFAULT 'SUCCESS'
);
CREATE INDEX IF NOT EXISTS idx_job_time ON sys_job_log(run_at);

-- =============================================================================
-- V4：电商作业形态（订单驱动）
--   与 V3 的差别不是「多几张表」，而是作业主轴变了：
--     V3（ERP 味）：人工开单 → 审核 → 过账
--     V4（电商味）：渠道订单进订单池 → 合成波次 → 按库位路径拣货 → 复核 → 打包 → 发货过账 → 售后
--   商品也升到 SPU/SKU 两级：SPU 是「款」（中性笔 0.5mm），SKU 是「可卖单位」
--   （中性笔 0.5mm 黑色 / 红色）——库存、订单行、上下架价都挂在 SKU 上（base_goods 即 SKU）。
-- =============================================================================

-- SPU（款）
CREATE TABLE IF NOT EXISTS base_spu (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  spu_code    TEXT NOT NULL UNIQUE,
  spu_name    TEXT NOT NULL,
  category_id INTEGER,
  brand       TEXT,
  unit        TEXT,
  description TEXT,
  status      INTEGER NOT NULL DEFAULT 1,
  create_time TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 波次：一批订单合成一次拣货作业（电商仓的核心单位，不是「一张单一次作业」）
CREATE TABLE IF NOT EXISTS ec_wave (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  wave_no     TEXT NOT NULL UNIQUE,          -- BC + yyyyMMdd + 4位
  shop_id     INTEGER,
  wh_id       INTEGER,
  status      INTEGER NOT NULL DEFAULT 0,    -- 0待拣 1拣货中 2待打包 3已完成 4已取消
  order_count INTEGER NOT NULL DEFAULT 0,
  item_count  INTEGER NOT NULL DEFAULT 0,
  total_qty   REAL NOT NULL DEFAULT 0,
  create_by   INTEGER,
  create_time TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  finish_time TEXT
);
CREATE INDEX IF NOT EXISTS idx_wave_status ON ec_wave(status, create_time);

CREATE TABLE IF NOT EXISTS ec_wave_order (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  wave_id  INTEGER NOT NULL REFERENCES ec_wave(id),
  order_id INTEGER NOT NULL REFERENCES ec_order(id),
  status   INTEGER NOT NULL DEFAULT 0,       -- 0待拣 1已拣 2待打包 3已发货
  UNIQUE(wave_id, order_id)
);
CREATE INDEX IF NOT EXISTS idx_wave_order_order ON ec_wave_order(order_id);

-- 拣货任务：一行 = 一个「库位×批次」的取货动作，按 loc_code 排序就是拣货路径
CREATE TABLE IF NOT EXISTS ec_pick_task (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  wave_id    INTEGER NOT NULL,
  order_id   INTEGER NOT NULL,
  alloc_id   INTEGER NOT NULL,
  goods_id   INTEGER NOT NULL,
  loc_id     INTEGER NOT NULL,
  batch_no   TEXT    NOT NULL DEFAULT '',
  qty        REAL    NOT NULL DEFAULT 0,
  picked_qty REAL    NOT NULL DEFAULT 0,
  status     INTEGER NOT NULL DEFAULT 0,     -- 0待拣 1已拣 2缺货
  create_time TEXT   NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE(alloc_id),
  CHECK(qty > 0)
);
CREATE INDEX IF NOT EXISTS idx_pick_wave ON ec_pick_task(wave_id, status);

-- 包裹：打包动作的产物（称重/包材），发货时挂在发货单上
CREATE TABLE IF NOT EXISTS ec_package (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  package_no TEXT NOT NULL UNIQUE,           -- BZ + yyyyMMdd + 4位
  order_id   INTEGER NOT NULL,
  weight_g   INTEGER NOT NULL DEFAULT 0,
  remark     TEXT,
  create_by  INTEGER,
  create_time TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_package_order ON ec_package(order_id);
-- 日汇总：报表/看板只读这张表（分片后跨库查询也走它）
CREATE TABLE IF NOT EXISTS wms_daily_stat (
  stat_date    TEXT NOT NULL,
  goods_id     INTEGER NOT NULL,
  wh_id        INTEGER NOT NULL DEFAULT 0,
  in_qty       REAL NOT NULL DEFAULT 0,
  out_qty      REAL NOT NULL DEFAULT 0,
  adjust_qty   REAL NOT NULL DEFAULT 0,
  cost_amount  REAL NOT NULL DEFAULT 0,
  sales_amount REAL NOT NULL DEFAULT 0,
  end_qty      REAL NOT NULL DEFAULT 0,
  update_time  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (stat_date, goods_id, wh_id)
);
CREATE INDEX IF NOT EXISTS idx_stat_goods ON wms_daily_stat(goods_id, stat_date);

-- 多实例限流回退（无 Redis 时用共享计数，按分钟窗口）
CREATE TABLE IF NOT EXISTS sys_rate_limit (
  bucket      TEXT NOT NULL,
  window_key  TEXT NOT NULL,
  hits        INTEGER NOT NULL DEFAULT 0,
  update_time TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (bucket, window_key)
);
-- 单号序列（原子自增，替代 COUNT(*) 扫表；分库时按前缀/日期分键）
CREATE TABLE IF NOT EXISTS sys_seq (
  seq_key  TEXT PRIMARY KEY,
  next_val INTEGER NOT NULL DEFAULT 0
);
