#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WMS 本地演示后端 —— Python 标准库 + SQLite，无需安装任何第三方包。

启动：
    python app.py                 # 默认 http://127.0.0.1:8000
    python app.py --port 8080
    python app.py --reset         # 重置演示数据（删库重建并重新灌入种子数据）

与《WMS 项目方案与技术文档 V2.0》的对应关系：
    · 接口路径 /api/v1/**、统一响应体 {code,message,data}、错误码 —— 与文档第 5 章一致
    · 库存并发控制：可用量预占 + 条件更新 + version 乐观锁 + request_id 幂等 —— 与文档 4.6 一致
    · 库存任何变动都同时写 wms_inventory 与 wms_inventory_txn（同一事务内）
    · 生产环境按文档用 Spring Boot + MySQL + Redis；本演示用 Python + SQLite 以便零配置跑起来
"""
import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
SCHEMA_PATH = os.path.join(BASE_DIR, "db", "schema_sqlite.sql")
import wms_infra                        # 生产化基础设施：迁移/幂等/限流/队列/指标/结构化日志

WRITE_LOCK = threading.Lock()          # 单进程写事务串行化（跨进程由 BEGIN IMMEDIATE + WAL 保证）
METRICS = wms_infra.Metrics()
RATE_LIMITER = wms_infra.RateLimiter(
    per_minute=int(os.environ.get("WMS_RATE_PER_MINUTE", "1200")),
    burst=int(os.environ.get("WMS_RATE_BURST", "200")))
IDEMPOTENT_METHODS = ("POST", "PUT", "DELETE")
# 并发闸门：超过上限直接 503（快速失败），避免线程数失控把机器拖死
MAX_CONCURRENT_REQUESTS = int(os.environ.get("WMS_MAX_CONCURRENCY", "128"))
INFLIGHT = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)


def task_queue():
    """延迟构造（db 在后面才定义），保证模块级无循环依赖。"""
    return wms_infra.TaskQueue(db)


def task_auto_wave(payload):
    """异步任务：订单支付后自动并波（失败不影响支付，重试走队列）。"""
    order_id = int(payload.get("order_id") or 0)
    if not order_id:
        return
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute("SELECT * FROM ec_order WHERE id=?", (order_id,)).fetchone()
        if not order or order["status"] not in (1, 2):
            conn.rollback()
            return
        if conn.execute("SELECT 1 FROM ec_wave_order WHERE order_id=? AND status<3", (order_id,)).fetchone():
            conn.rollback()
            return
        admin = conn.execute("SELECT * FROM sys_user WHERE username=? ", ("admin",)).fetchone()
        if not admin:
            conn.rollback()
            return
        create_wave(conn, [order_id], {"id": admin["id"], "username": admin["username"]})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def task_idem_cleanup(payload):
    """异步任务：清理过期幂等键（默认 24h），避免表无限膨胀。"""
    conn = db()
    try:
        removed = wms_infra.Idempotency(conn).cleanup(payload.get("hours"))
        conn.close()
        return {"removed": removed}
    except Exception:
        conn.close()
        raise
DEC2 = lambda v: round(float(v or 0), 3)
MAX_RETRY = 3                          # 乐观锁冲突重试次数


# --------------------------------------------------------------------------- #
# 环境配置（密钥与可变参数一律来自环境变量 / .env，不写进代码）
#   优先级：系统环境变量 > 就近的 .env 文件；.env 不进代码库，线上单独配置
# --------------------------------------------------------------------------- #
def _load_env_file(path):
    """极简 .env 解析：KEY=VALUE，支持 # 注释、引号与 export 前缀；不覆盖已存在的系统环境变量。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key.startswith("export "):
                    key = key[7:].strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


def _find_env_file():
    """先找 app.py 同目录，再找上级目录，方便 .env 就近放置。"""
    for candidate in (os.path.join(BASE_DIR, ".env"), os.path.join(os.path.dirname(BASE_DIR), ".env")):
        if os.path.isfile(candidate):
            return candidate
    return None


def env_int(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit("[FATAL] 环境变量 %s 必须是整数，当前值：%r" % (name, raw))


ENV_FILE = _find_env_file()
if ENV_FILE:
    _load_env_file(ENV_FILE)

WMS_ENV = (os.environ.get("WMS_ENV", "development").strip() or "development").lower()
IS_PRODUCTION = WMS_ENV == "production"

SECRET_TEXT = os.environ.get("WMS_SECRET_KEY", "").strip()
if not SECRET_TEXT:
    if IS_PRODUCTION:
        raise SystemExit(
            "[FATAL] 生产环境（WMS_ENV=production）必须配置 WMS_SECRET_KEY。\n"
            "        生成方式：openssl rand -base64 48（>=32 字节），写入 .env 或系统环境变量。")
    SECRET_TEXT = "wms-demo-dev-secret-not-for-production"
    sys.stderr.write("[WARN] 未设置 WMS_SECRET_KEY，使用开发默认密钥；上线前请务必配置。\n")
if IS_PRODUCTION and len(SECRET_TEXT) < 32:
    raise SystemExit("[FATAL] 生产环境的 WMS_SECRET_KEY 长度必须 >= 32 字节（建议 openssl rand -base64 48）。")
SECRET = SECRET_TEXT.encode("utf-8")

DB_PATH = os.environ.get("WMS_DB_PATH", "").strip() or os.path.join("data", "wms.db")
if not os.path.isabs(DB_PATH):
    DB_PATH = os.path.normpath(os.path.join(BASE_DIR, DB_PATH))

TOKEN_EXPIRE_SECONDS = env_int("WMS_TOKEN_EXPIRE_HOURS", 8) * 3600
DEFAULT_HOST = os.environ.get("WMS_HOST", "").strip() or "127.0.0.1"
DEFAULT_PORT = env_int("WMS_PORT", 8000)
# 前后端分离部署时前端是另一个源（Vite 8001 / Nginx），必须放行跨域；默认 * 仅用于本地演示，
# 生产环境务必写成白名单：WMS_CORS_ORIGINS=https://wms.example.com
CORS_ORIGINS = [item.strip() for item in os.environ.get("WMS_CORS_ORIGINS", "*").split(",") if item.strip()]


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    def __init__(self, code, message, http=400):
        super().__init__(message)
        self.code, self.message, self.http = code, message, http


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def db_readonly() -> sqlite3.Connection:
    """报表/导出走只读连接：WAL 下不阻塞写；生产换从库只需改这一个函数。"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL：读不阻塞写、写不阻塞读（默认 rollback journal 下报表一跑就锁库）
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")     # WAL 下足够安全，比 FULL 快很多
    # 压测实测：10 并发写时会出现 5~6s 的尾延迟，正好撞上原来的 5s 上限 -> 500。
    # 这里放宽到 15s（SQLite 单写者，宁可排队也不要报错），同时减少检查点抖动。
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA wal_autocheckpoint = 4000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def hash_password(password: str, salt: str = None):
    salt = salt or secrets.token_hex(8)
    digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return digest, salt


def make_token(user_id: int, username: str) -> str:
    exp = int(time.time()) + TOKEN_EXPIRE_SECONDS
    raw = "%d|%s|%d" % (user_id, username, exp)
    sig = hmac.new(SECRET, raw.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(("%s|%s" % (raw, sig)).encode()).decode()


def parse_token(token: str):
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        user_id, username, exp, sig = raw.split("|")
        expect = hmac.new(SECRET, ("%s|%s|%s" % (user_id, username, exp)).encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(expect, sig) or int(exp) < time.time():
            return None
        return {"id": int(user_id), "username": username}
    except Exception:
        return None


SENSITIVE_LOG_KEYS = ("password", "secret", "token", "apikey", "api_key")


def mask_params(params):
    """日志脱敏：口令、密钥、token 等敏感字段不落库（对应文档 6.3.6）。"""
    if not params:
        return None
    masked = {}
    for key, value in params.items():
        lowered = str(key).lower()
        masked[key] = "***" if any(word in lowered for word in SENSITIVE_LOG_KEYS) else value
    return json.dumps(masked, ensure_ascii=False)[:500]


def write_log(conn, user, module, op_type, biz_id=None, biz_no=None, params=None, result="SUCCESS",
              error_msg=None, cost_ms=None, ip=None):
    conn.execute(
        "INSERT INTO sys_operation_log(user_id,user_name,ip,module,op_type,biz_id,biz_no,params,result,error_msg,cost_ms)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (user["id"] if user else 0, user["username"] if user else "anonymous", ip, module, op_type,
         biz_id, biz_no, mask_params(params),
         result, error_msg, cost_ms),
    )


def positive_qty(value, field="数量"):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ApiError(40000, "%s必须是数字" % field)
    if not math.isfinite(number) or number <= 0:
        raise ApiError(40000, "%s必须大于 0" % field)
    return DEC2(number)


def warehouse_for_location(conn, loc_id):
    """沿库位树向上解析仓库，避免依赖冗余 wh_id 的填写质量。"""
    current = conn.execute("SELECT * FROM base_location WHERE id=?", (loc_id,)).fetchone()
    if not current or current["status"] != 1 or current["loc_type"] != 4:
        raise ApiError(40000, "明细库位不存在、已停用或不是末级库位")
    visited = set()
    while current and current["loc_type"] != 1:
        if current["id"] in visited:
            raise ApiError(40000, "库位树存在循环引用")
        visited.add(current["id"])
        current = conn.execute("SELECT * FROM base_location WHERE id=?", (current["parent_id"],)).fetchone()
    if not current or current["status"] != 1:
        raise ApiError(40000, "明细库位所属仓库不存在或已停用")
    return current


def validate_order(conn, body, kind):
    """校验演示版单据边界，确保库存操作不会跨仓库或写入脏数量。"""
    try:
        wh_id = int(body.get("wh_id"))
    except (TypeError, ValueError):
        raise ApiError(40000, "请选择有效仓库")
    warehouse = conn.execute("SELECT * FROM base_location WHERE id=? AND loc_type=1 AND status=1", (wh_id,)).fetchone()
    if not warehouse:
        raise ApiError(40000, "仓库不存在或已停用")
    partner_id = body.get("partner_id")
    partner_type = "supplier" if kind == "in" else "customer"
    if partner_id:
        try:
            partner_id = int(partner_id)
        except (TypeError, ValueError):
            raise ApiError(40000, "往来单位无效")
        partner = conn.execute("SELECT id FROM base_partner WHERE id=? AND partner_type=? AND status=1",
                               (partner_id, partner_type)).fetchone()
        if not partner:
            raise ApiError(40000, "往来单位不存在、已停用或类型不匹配")
    lines = body.get("lines") or []
    if not lines:
        raise ApiError(40000, "至少一行明细")
    seen = set()
    for index, line in enumerate(lines, start=1):
        try:
            goods_id = int(line.get("goods_id"))
            loc_id = int(line.get("loc_id"))
        except (TypeError, ValueError):
            raise ApiError(40000, "第 %d 行商品或库位无效" % index)
        goods = conn.execute("SELECT id FROM base_goods WHERE id=? AND status=1", (goods_id,)).fetchone()
        if not goods:
            raise ApiError(40000, "第 %d 行商品不存在或已停用" % index)
        location_warehouse = warehouse_for_location(conn, loc_id)
        if location_warehouse["id"] != wh_id:
            raise ApiError(40000, "第 %d 行库位不属于所选仓库" % index)
        quantity = positive_qty(line.get("plan_num"), "第 %d 行数量" % index)
        actual = line.get("actual_num")
        if actual is not None and actual != "":
            actual_quantity = positive_qty(actual, "第 %d 行实际数量" % index)
            if actual_quantity != quantity:
                raise ApiError(40000, "当前演示版不支持部分收发货，实际数量必须等于计划数量")
        key = (goods_id, loc_id, (line.get("batch_no") or "").strip())
        if key in seen:
            raise ApiError(40000, "第 %d 行与前面明细重复，请合并数量" % index)
        seen.add(key)
    return wh_id


def next_order_no(conn, prefix: str) -> str:
    """单号：前缀 + yyyyMMdd + 4 位日内序列"""
    day = datetime.now().strftime("%Y%m%d")
    row = conn.execute(
        "SELECT COUNT(*) c FROM ("
        "  SELECT order_no FROM wms_in_order WHERE order_no LIKE ?"
        "  UNION ALL SELECT order_no FROM wms_out_order WHERE order_no LIKE ?)",
        (prefix + day + "%", prefix + day + "%"),
    ).fetchone()
    return "%s%s%04d" % (prefix, day, row["c"] + 1)


def next_txn_no(conn) -> str:
    day = datetime.now().strftime("%Y%m%d")
    key = "LS:%s" % day
    pat = "LS" + day + "%"
    seq = next_seq(conn, key,
                   lambda: conn.execute("SELECT COUNT(*) FROM wms_inventory_txn WHERE txn_no LIKE ?",
                                        (pat,)).fetchone()[0])
    return "LS%s%06d" % (day, seq)


# --------------------------------------------------------------------------- #
# 建库 + 种子数据
# --------------------------------------------------------------------------- #
CONFIG_DEFAULTS = [
    ("ec.free.shipping.amount", "99", "满多少免运费（元）"),
    ("ec.freight.fee", "8", "运费（未满免邮门槛，元）"),
    ("ec.auto.cancel.hours", "24", "未支付订单超时自动关闭（小时）"),
    ("inventory.fifo.enabled", "true", "出库/发货是否按 FIFO 推荐批次"),
    ("inventory.negative.allowed", "false", "是否允许负库存（演示必须 false）"),
    ("ec.channel.self.enabled", "true", "自营商城渠道开关（运维层可关停渠道）"),
]


def ensure_column(conn, table, column, ddl):
    """SQLite 增量迁移：CREATE TABLE IF NOT EXISTS 不会给老表加列，这里补。

    只加列、不改列、不删列，可反复执行；老库（demo/data/wms.db）启动即可升级，不需要 --reset。
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
    if column not in cols:
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, ddl))


def repair_location_wh(conn):
    """修正库位表的冗余 wh_id：沿 parent 找到根仓库并回写。

    V2 的演示种子把 wh_id 写成了「库区 id」，平时没人发现，
    一旦按仓库做数据权限过滤（WHERE wh_id IN ...）就会全部查不到——属于静默失效，必须自愈。
    """
    rows = conn.execute("SELECT id,parent_id,loc_type,wh_id FROM base_location").fetchall()
    by_id = {r["id"]: r for r in rows}
    fixed = 0
    for r in rows:
        cur, seen = r, set()
        while cur and cur["loc_type"] != 1:
            if cur["id"] in seen:
                break
            seen.add(cur["id"])
            cur = by_id.get(cur["parent_id"])
        target = cur["id"] if cur and cur["loc_type"] == 1 else 0
        if r["wh_id"] != target:
            conn.execute("UPDATE base_location SET wh_id=? WHERE id=?", (target, r["id"]))
            fixed += 1
    return fixed


def ensure_ec_baseline(conn):
    repair_location_wh(conn)          # 先修数据，再补基线
    """增量初始化电商基线数据：老库（未重置）启动时也能补齐店铺/上下架/补货政策/参数。

    全部用 INSERT OR IGNORE / 补列，可反复执行；不删除、不改写已有业务数据。
    """
    # ---- 增量列（V4：SPU/SKU、订单类型、售后质检）----
    ensure_column(conn, "base_goods", "spu_id", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "base_goods", "spec_attrs", "TEXT NOT NULL DEFAULT '{}'")
    ensure_column(conn, "ec_order", "order_kind", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(conn, "ec_order", "parent_order_id", "INTEGER")
    ensure_column(conn, "ec_after_sale", "good_qty", "REAL NOT NULL DEFAULT 0")
    ensure_column(conn, "ec_after_sale", "bad_qty", "REAL NOT NULL DEFAULT 0")
    ensure_column(conn, "ec_after_sale", "check_time", "TEXT")
    ensure_column(conn, "ec_after_sale", "check_by", "INTEGER")
    ensure_column(conn, "ec_after_sale", "ref_order_no", "TEXT")
    # V5：成本（移动加权）、批次效期（FEFO/临期）、流水成本
    ensure_column(conn, "base_goods", "avg_cost", "REAL NOT NULL DEFAULT 0")
    ensure_column(conn, "wms_inventory", "expire_date", "TEXT")
    ensure_column(conn, "wms_inventory_txn", "unit_cost", "REAL NOT NULL DEFAULT 0")
    ensure_column(conn, "wms_inventory_txn", "cost_amount", "REAL NOT NULL DEFAULT 0")
    ensure_column(conn, "wms_in_order_detail", "produce_date", "TEXT")
    ensure_column(conn, "wms_in_order_detail", "expire_date", "TEXT")
    # V6：商品图片（SPU 主图 + SKU 覆盖图），空则前端回落分类占位图
    ensure_column(conn, "base_spu", "image_url", "TEXT")
    ensure_column(conn, "base_goods", "image_url", "TEXT")
    # V7：商品逻辑删除（归档）。物理删除会破坏流水/台账的引用，一律走逻辑删除
    ensure_column(conn, "base_goods", "is_deleted", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "base_spu", "is_deleted", "INTEGER NOT NULL DEFAULT 0")
    # 报表按时间范围扫描：已有索引都以 goods_id/loc_id 打头，时间列单独补索引
    for ddl in (
        "CREATE INDEX IF NOT EXISTS idx_txn_time ON wms_inventory_txn(create_time, biz_type)",
        "CREATE INDEX IF NOT EXISTS idx_ec_order_time ON ec_order(create_time)",
        "CREATE INDEX IF NOT EXISTS idx_ec_order_pay ON ec_order(pay_time)",
        "CREATE INDEX IF NOT EXISTS idx_ec_item_goods ON ec_order_item(goods_id)",
        "CREATE INDEX IF NOT EXISTS idx_aftersale_time ON ec_after_sale(create_time)",
    ):
        conn.execute(ddl)

    conn.execute("INSERT OR IGNORE INTO base_shop(shop_code,shop_name,channel_type,remark)"
                 " VALUES('DS001','官方商城','self','自营渠道（演示默认店铺）')")
    conn.execute("INSERT OR IGNORE INTO base_shop(shop_code,shop_name,channel_type,status,remark)"
                 " VALUES('TB001','淘宝旗舰店','taobao',0,'预留渠道：接入平台开放接口后启用')")
    for key, value, remark in CONFIG_DEFAULTS:
        conn.execute("INSERT OR IGNORE INTO sys_config(config_key,config_value,remark) VALUES(?,?,?)",
                     (key, value, remark))

    # ---- 次品库位（退货质检的残次品专用，不参与正常可售）----
    wh = conn.execute("SELECT id FROM base_location WHERE loc_type=1 AND status=1 ORDER BY id LIMIT 1").fetchone()
    if wh and not conn.execute("SELECT 1 FROM base_location WHERE loc_code='WH01-QC'").fetchone():
        conn.execute("INSERT INTO base_location(loc_code,loc_name,parent_id,loc_type,wh_id,status)"
                     " VALUES('WH01-QC','次品待处理区',?,2,?,1)", (wh["id"], wh["id"]))
        zone = conn.execute("SELECT id FROM base_location WHERE loc_code='WH01-QC'").fetchone()["id"]
        conn.execute("INSERT INTO base_location(loc_code,loc_name,parent_id,loc_type,wh_id,status)"
                     " VALUES('WH01-QC-01','次品库位',?,4,?,1)", (zone, wh["id"]))

    # ---- 老商品补 SPU（一货一款）：先把 SPU 建齐，再派生多规格 SKU，最后统一建上下架/补货政策 ----
    for g in conn.execute("SELECT id, goods_code, goods_name, category_id, unit, spu_id"
                          " FROM base_goods WHERE status=1").fetchall():
        if g["spu_id"]:
            continue
        conn.execute("INSERT OR IGNORE INTO base_spu(spu_code,spu_name,category_id,unit)"
                     " VALUES(?,?,?,?)", ("SPU" + g["goods_code"], g["goods_name"],
                                          g["category_id"], g["unit"]))
        row = conn.execute("SELECT id FROM base_spu WHERE spu_code=?",
                           ("SPU" + g["goods_code"],)).fetchone()
        if row:
            conn.execute("UPDATE base_goods SET spu_id=? WHERE id=?", (row["id"], g["id"]))
            conn.execute("UPDATE base_spu SET image_url='/img/cat-'||?||'.svg'"
                         " WHERE id=? AND (image_url IS NULL OR image_url='')",
                         (g["category_id"], row["id"]))

    # ---- 多规格示范 SKU（同一 SPU 下的不同颜色/尺寸）----
    _ensure_demo_variants(conn)

    shop = conn.execute("SELECT id FROM base_shop WHERE shop_code='DS001'").fetchone()
    if not shop:
        return
    for g in conn.execute("SELECT id, goods_code, cost_price FROM base_goods WHERE status=1").fetchall():
        price = DEC2(max(1.0, float(g["cost_price"] or 0) * 1.6))
        conn.execute("INSERT OR IGNORE INTO ec_listing(shop_id,goods_id,sale_price,sale_status,reserve_qty)"
                     " VALUES(?,?,?,?,0)", (shop["id"], g["id"], price,
                                            0 if g["goods_code"] in ("SP011", "SP014") else 1))
        conn.execute("INSERT OR IGNORE INTO wms_stock_policy(goods_id,wh_id,safety_stock,replenish_qty,lead_days)"
                     " VALUES(?,0,?,?,7)", (g["id"], 150, 300))


def _ensure_demo_variants(conn):
    """给「中性笔」和「瓦楞纸箱」各补一个规格，演示 SPU/SKU 两级模型（可重复执行）。"""
    conn.execute("UPDATE base_spu SET spu_name='瓦楞纸箱' WHERE spu_code='SPUSP012'"
                 " AND spu_name LIKE '瓦楞纸箱%'")
    cat = conn.execute("SELECT id FROM base_goods_category ORDER BY id LIMIT 1").fetchone()
    cat_id = cat["id"] if cat else 1
    variants = [
        ("SP013", "中性笔 0.5mm 红色", "SP005", "6902004", '{"颜色":"红色","笔芯":"0.5mm"}', 15.5),
        ("SP014", "瓦楞纸箱 5号", "SP012", "6904004", '{"规格":"5号 290×170×190mm"}', 2.8),
    ]
    for code, name, base_code, barcode, attrs, cost in variants:
        base = conn.execute("SELECT * FROM base_goods WHERE goods_code=?", (base_code,)).fetchone()
        if not base:
            continue
        # 基准 SKU 也要有规格属性，商城才能渲染出「颜色/规格」选择器
        base_attrs = '{"颜色":"黑色","笔芯":"0.5mm"}' if base_code == "SP005" else \
            '{"规格":"3号 430×210×270mm"}'
        if (base["spec_attrs"] or "{}") in ("{}", ""):
            conn.execute("UPDATE base_goods SET spec_attrs=? WHERE id=?", (base_attrs, base["id"]))
        if conn.execute("SELECT 1 FROM base_goods WHERE goods_code=?", (code,)).fetchone():
            continue
        conn.execute("INSERT INTO base_goods(goods_code,goods_name,category_id,spec,unit,barcode,cost_price,"
                     "status,spu_id,spec_attrs) VALUES(?,?,?,?,?,?,?,1,?,?)",
                     (code, name, base["category_id"], name, base["unit"], barcode, cost, base["spu_id"], attrs))
        conn.execute("INSERT OR IGNORE INTO wms_stock_policy(goods_id,wh_id,safety_stock,replenish_qty,lead_days)"
                     " VALUES(?,0,?,?,7)",
                     (conn.execute("SELECT id FROM base_goods WHERE goods_code=?", (code,)).fetchone()["id"],
                      120, 240))


def init_db(reset: bool = False):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    fresh = not os.path.exists(DB_PATH)
    conn = db()
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    # 迁移必须早于种子数据：seed 会走支付逻辑，而支付要写 ec_fulfillment
    applied = wms_infra.apply_migrations(conn, log=lambda s: sys.stderr.write(s + "\n"))
    if applied:
        sys.stderr.write("[migrate] applied %d migration(s)\n" % applied)
    if fresh:
        seed(conn)
    else:
        ensure_ec_baseline(conn)
        ensure_rbac_baseline(conn)
        backfill_fulfillments(conn)      # 老库补齐履约单，保证拆单口径一致
    conn.commit()
    conn.close()
    return fresh


def seed(conn):
    """灌入演示数据：用户 / 分类 / 商品 / 库位树 / 往来单位 / 期初库存 / 几张历史单据"""
    pwd, salt = hash_password("123456")
    conn.execute("INSERT INTO sys_user(username,password_hash,salt,real_name,phone) VALUES(?,?,?,?,?)",
                 ("admin", pwd, salt, "系统管理员", "13800000000"))

    cats = [("C01", "五金件"), ("C02", "办公用品"), ("C03", "电子元件"), ("C04", "包装耗材")]
    for code, name in cats:
        conn.execute("INSERT INTO base_goods_category(category_code,category_name) VALUES(?,?)", (code, name))
    cat_id = {r["category_code"]: r["id"] for r in conn.execute("SELECT id,category_code FROM base_goods_category")}

    goods = [
        ("SP001", "不锈钢螺丝 M6×20", "C01", "M6×20 / 100只装", "包", "6901001", 12.5),
        ("SP002", "不锈钢螺母 M6", "C01", "M6 / 200只装", "包", "6901002", 9.8),
        ("SP003", "内六角扳手 3mm", "C01", "3mm 加硬", "把", "6901003", 6.5),
        ("SP004", "A4 复印纸", "C02", "70g / 500张", "包", "6902001", 18.0),
        ("SP005", "中性笔 0.5mm", "C02", "黑色 / 12支装", "盒", "6902002", 15.5),
        ("SP006", "文件夹 A4", "C02", "双强力夹", "个", "6902003", 4.2),
        ("SP007", "贴片电阻 10kΩ", "C03", "0805 精度1%", "盘", "6903001", 32.0),
        ("SP008", "贴片电容 100nF", "C03", "0805 50V", "盘", "6903002", 28.0),
        ("SP009", "杜邦线 20cm", "C03", "母对母 40根", "排", "6903003", 5.6),
        ("SP010", "气泡膜", "C04", "50cm×100m", "卷", "6904001", 26.0),
        ("SP011", "封箱胶带", "C04", "透明 4.5cm×100m", "卷", "6904002", 7.8),
        ("SP012", "瓦楞纸箱 3号", "C04", "430×210×270mm", "个", "6904003", 3.6),
    ]
    for code, name, cat, spec, unit, bar, cost in goods:
        conn.execute(
            "INSERT INTO base_goods(goods_code,goods_name,category_id,spec,unit,barcode,cost_price)"
            " VALUES(?,?,?,?,?,?,?)",
            (code, name, cat_id[cat], spec, unit, bar, cost))

    # 库位树：2 个仓库 → 库区 → 货架 → 库位
    def add_loc(code, name, parent, ltype, wh):
        cur = conn.execute("INSERT INTO base_location(loc_code,loc_name,parent_id,loc_type,wh_id) VALUES(?,?,?,?,?)",
                           (code, name, parent, ltype, wh))
        return cur.lastrowid

    wh1 = add_loc("WH01", "一号仓", 0, 1, 0)
    zone_a = add_loc("WH01-A", "A 库区", wh1, 2, wh1)
    zone_b = add_loc("WH01-B", "B 库区", wh1, 2, wh1)
    wh2 = add_loc("WH02", "二号仓", 0, 1, 0)
    zone_c = add_loc("WH02-C", "C 库区", wh2, 2, wh2)
    loc_ids = []
    for zone, racks, wh in ((zone_a, ["A01", "A02"], wh1), (zone_b, ["B01", "B02"], wh1),
                            (zone_c, ["C01"], wh2)):
        for rack in racks:
            # 冗余 wh_id 必须写「仓库」id（数据权限按它过滤），写成库区 id 会让仓库隔离全部失效
            rack_id = add_loc(rack, "%s 货架" % rack, zone, 3, wh)
            for lv in ("01", "02", "03"):
                loc_ids.append(add_loc("%s-%s" % (rack, lv), "%s-%s 库位" % (rack, lv), rack_id, 4, wh))

    for code, name, contact, phone in [("GYS001", "东莞精密五金有限公司", "李经理", "13900000001"),
                                       ("GYS002", "深圳华强电子供应商", "王经理", "13900000002")]:
        conn.execute("INSERT INTO base_partner(partner_type,partner_code,partner_name,contact,phone)"
                     " VALUES('supplier',?,?,?,?)", (code, name, contact, phone))
    for code, name, contact, phone in [("KH001", "广州贸易有限公司", "张主管", "13700000001"),
                                       ("KH002", "东莞制造厂", "刘主管", "13700000002")]:
        conn.execute("INSERT INTO base_partner(partner_type,partner_code,partner_name,contact,phone)"
                     " VALUES('customer',?,?,?,?)", (code, name, contact, phone))

    admin = {"id": 1, "username": "admin"}
    gid = {r["goods_code"]: r["id"] for r in conn.execute("SELECT id,goods_code FROM base_goods")}

    # 期初库存：直接写库存 + 期初流水（biz_type=11）
    opening = [("SP001", 0, 600), ("SP002", 0, 800), ("SP003", 1, 150), ("SP004", 2, 320),
               ("SP005", 3, 260), ("SP006", 4, 400), ("SP007", 5, 120), ("SP008", 6, 90),
               ("SP009", 7, 300), ("SP010", 8, 60), ("SP011", 9, 220), ("SP012", 10, 500),
               # 二号仓（WH02 / C 库区）也放一点货：用来演示「按仓库隔离的数据权限」
               ("SP004", 12, 80), ("SP010", 13, 40)]
    for idx, (code, loc_idx, qty) in enumerate(opening, start=1):
        loc = loc_ids[loc_idx]
        conn.execute("INSERT INTO wms_inventory(goods_id,loc_id,batch_no,quantity,locked_qty,available_qty,version,in_time)"
                     " VALUES(?,?,'',?,0,?,0,?)", (gid[code], loc, qty, qty, now()))
        conn.execute(
            "INSERT INTO wms_inventory_txn(txn_no,biz_type,biz_id,biz_no,detail_id,goods_id,loc_id,batch_no,"
            "change_qty,before_qty,after_qty,create_by) VALUES(?,11,0,'期初导入',?,?,?,'',?,0,?,1)",
            (next_txn_no(conn), idx, gid[code], loc, qty, qty))

    # 一张已完成的入库单（含批次），让台账有数据
    order_no = "RK" + datetime.now().strftime("%Y%m%d") + "0001"
    cur = conn.execute(
        "INSERT INTO wms_in_order(order_no,order_type,partner_id,wh_id,status,total_plan_num,total_actual_num,"
        "audit_by,audit_time,remark,create_by,request_id) VALUES(?,1,1,?,3,?,?,1,?,'种子数据',1,'seed-in-1')",
        (order_no, wh1, 300, 300, now()))
    oid = cur.lastrowid
    for code, loc_idx, qty, batch in [("SP001", 0, 200, "B20260901"), ("SP004", 2, 100, "B20260905")]:
        loc = loc_ids[loc_idx]
        cur = conn.execute("INSERT INTO wms_in_order_detail(order_id,goods_id,loc_id,plan_num,actual_num,batch_no,unit)"
                           " VALUES(?,?,?,?,?,?,'')", (oid, gid[code], loc, qty, qty, batch))
        did = cur.lastrowid
        inv = conn.execute("SELECT * FROM wms_inventory WHERE goods_id=? AND loc_id=? AND batch_no=?",
                           (gid[code], loc, batch)).fetchone()
        if inv:
            before = inv["quantity"]
            after = DEC2(before + qty)
            conn.execute("UPDATE wms_inventory SET quantity=?,available_qty=?,version=version+1,in_time=?,"
                         "update_time=? WHERE id=?", (after, after, now(), now(), inv["id"]))
        else:
            before = 0
            after = qty
            conn.execute("INSERT INTO wms_inventory(goods_id,loc_id,batch_no,quantity,locked_qty,available_qty,"
                         "version,in_time) VALUES(?,?,?,?,0,?,0,?)", (gid[code], loc, batch, qty, qty, now()))
        conn.execute(
            "INSERT INTO wms_inventory_txn(txn_no,biz_type,biz_id,biz_no,detail_id,goods_id,loc_id,batch_no,"
            "change_qty,before_qty,after_qty,create_by) VALUES(?,1,?,?,?,?,?,?,?,?,?,1)",
            (next_txn_no(conn), oid, order_no, did, gid[code], loc, batch, qty, before, after))

    # 一张已完成的出库单
    out_no = "CK" + datetime.now().strftime("%Y%m%d") + "0001"
    cur = conn.execute(
        "INSERT INTO wms_out_order(order_no,order_type,partner_id,wh_id,status,total_plan_num,total_actual_num,"
        "audit_by,audit_time,remark,create_by,request_id) VALUES(?,1,1,?,3,?,?,1,?,'种子数据',1,'seed-out-1')",
        (out_no, wh1, 80, 80, now()))
    ooid = cur.lastrowid
    for code, loc_idx, qty in [("SP001", 0, 30), ("SP004", 2, 50)]:
        loc = loc_ids[loc_idx]
        cur = conn.execute("INSERT INTO wms_out_order_detail(order_id,goods_id,loc_id,plan_num,actual_num,batch_no,inv_version)"
                           " VALUES(?,?,?,?,?,'',0)", (ooid, gid[code], loc, qty, qty))
        did = cur.lastrowid
        inv = conn.execute("SELECT * FROM wms_inventory WHERE goods_id=? AND loc_id=? AND batch_no=''",
                           (gid[code], loc)).fetchone()
        before = inv["quantity"]
        after = DEC2(before - qty)
        conn.execute("UPDATE wms_inventory SET quantity=?,available_qty=?,version=version+1,update_time=? WHERE id=?",
                     (after, after, now(), inv["id"]))
        conn.execute(
            "INSERT INTO wms_inventory_txn(txn_no,biz_type,biz_id,biz_no,detail_id,goods_id,loc_id,batch_no,"
            "change_qty,before_qty,after_qty,create_by) VALUES(?,4,?,?,?,?,?,'',?,?,?,1)",
            (next_txn_no(conn), ooid, out_no, did, gid[code], loc, -qty, before, after))
    # ---- 电商基线（店铺 / 上下架 / 补货政策 / 参数）----
    ensure_ec_baseline(conn)
    # ---- 权限基线（权限目录 / 角色 / 演示账号）----
    ensure_rbac_baseline(conn)
    shop = conn.execute("SELECT id FROM base_shop WHERE shop_code='DS001'").fetchone()
    buyer = {"id": 1, "username": "admin"}

    # 演示订单 1：已支付待发货（库存已按 FIFO 分配到库位/批次并预占，实物还没动）
    o1 = create_ec_order(conn, {
        "shop_id": shop["id"], "buyer_name": "张伟", "buyer_phone": "13800001111",
        "receiver_name": "张伟", "receiver_phone": "13800001111", "receiver_address": "广东省深圳市南山区科技园 1 号",
        "items": [{"goods_id": gid["SP001"], "qty": 2}, {"goods_id": gid["SP004"], "qty": 1}],
    }, buyer)
    pay_ec_order(conn, o1["id"], "seed-pay-1", buyer)

    # 演示订单 2：已支付 → 已拣货 → 已发货（扣实物 + 写 21 流水 + 生成发货单）→ 申请退货（售后中）
    o2 = create_ec_order(conn, {
        "shop_id": shop["id"], "buyer_name": "李娜", "buyer_phone": "13900002222",
        "receiver_name": "李娜", "receiver_phone": "13900002222", "receiver_address": "上海市浦东新区张江路 88 号",
        "items": [{"goods_id": gid["SP010"], "qty": 3}],
    }, buyer)
    pay_ec_order(conn, o2["id"], "seed-pay-2", buyer)
    pick_ec_order(conn, o2["id"], buyer)
    ship_ec_order(conn, o2["id"], "顺丰速运", "SF1234567890123", buyer)
    create_after_sale(conn, {"order_id": o2["id"], "goods_id": gid["SP010"], "qty": 1,
                             "type": 1, "reason": "商品有划痕，申请退货"}, buyer)

    # V5 种子：期初均价 = 成本价；给两个批次一个到期日（演示 FEFO 与临期预警）；补齐种子流水的成本
    conn.execute("UPDATE base_goods SET avg_cost=cost_price")
    conn.execute("UPDATE wms_inventory SET expire_date=date('now','+18 day') WHERE batch_no='B20260901'")
    conn.execute("UPDATE wms_inventory SET expire_date=date('now','+240 day') WHERE batch_no='B20260905'")
    conn.execute(
        "UPDATE wms_inventory_txn SET unit_cost=(SELECT avg_cost FROM base_goods g WHERE g.id=goods_id),"
        " cost_amount=ABS(change_qty)*(SELECT avg_cost FROM base_goods g WHERE g.id=goods_id)")
    write_log(conn, admin, "系统", "INIT", params={"note": "演示种子数据初始化（含电商订单/发货/售后）"})


# --------------------------------------------------------------------------- #
# 业务逻辑
# --------------------------------------------------------------------------- #
def post_in_order(conn, order_id, request_id, user):
    """确认入库（过账）：库存增加 + 流水 + 单据状态，三者同一事务；幂等"""
    order = conn.execute("SELECT * FROM wms_in_order WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ApiError(40400, "入库单不存在", 404)
    if order["status"] == 3:
        return {"orderNo": order["order_no"], "status": 3, "duplicated": True}
    if order["status"] not in (1, 2):
        raise ApiError(40001, "单据状态不允许确认入库", 409)

    details = conn.execute("SELECT * FROM wms_in_order_detail WHERE order_id=? ORDER BY goods_id,loc_id",
                           (order_id,)).fetchall()
    if not details:
        raise ApiError(40001, "单据没有明细", 409)

    total = 0
    for d in details:
        qty = DEC2(d["actual_num"] if d["actual_num"] else d["plan_num"])
        if qty <= 0:
            continue
        for attempt in range(MAX_RETRY):
            inv = conn.execute("SELECT * FROM wms_inventory WHERE goods_id=? AND loc_id=? AND batch_no=?",
                               (d["goods_id"], d["loc_id"], d["batch_no"])).fetchone()
            if not inv:
                try:
                    conn.execute(
                        "INSERT INTO wms_inventory(goods_id,loc_id,batch_no,quantity,locked_qty,available_qty,version,in_time)"
                        " VALUES(?,?,?,0,0,0,0,?)", (d["goods_id"], d["loc_id"], d["batch_no"], now()))
                    inv = conn.execute("SELECT * FROM wms_inventory WHERE goods_id=? AND loc_id=? AND batch_no=?",
                                       (d["goods_id"], d["loc_id"], d["batch_no"])).fetchone()
                except sqlite3.IntegrityError:
                    continue
            before = DEC2(inv["quantity"])
            after = DEC2(before + qty)
            cur = conn.execute(
                "UPDATE wms_inventory SET quantity=?, available_qty=? - locked_qty, version=version+1,"
                " in_time=?, update_time=? WHERE id=? AND version=?",
                (after, after, now(), now(), inv["id"], inv["version"]))
            if cur.rowcount == 0:
                time.sleep(0.05 * (attempt + 1))
                continue
            conn.execute(
                "INSERT INTO wms_inventory_txn(txn_no,biz_type,biz_id,biz_no,detail_id,goods_id,loc_id,batch_no,"
                "change_qty,before_qty,after_qty,create_by) VALUES(?,1,?,?,?,?,?,?,?,?,?,?)",
                (next_txn_no(conn), order_id, order["order_no"], d["id"], d["goods_id"], d["loc_id"],
                 d["batch_no"], qty, before, after, user["id"]))
            post_txn_cost(conn, 1, order_id, d["id"], d["goods_id"], qty, before, after)
            if d["expire_date"]:
                conn.execute("UPDATE wms_inventory SET expire_date=? WHERE goods_id=? AND loc_id=?"
                             " AND batch_no=?", (d["expire_date"], d["goods_id"], d["loc_id"], d["batch_no"]))
            total += qty
            break
        else:
            raise ApiError(50202, "库存并发冲突，请刷新后重试", 409)

    conn.execute("UPDATE wms_in_order SET status=3, total_actual_num=?, request_id=? WHERE id=?",
                 (DEC2(total), request_id, order_id))
    write_log(conn, user, "入库管理", "CONFIRM", order_id, order["order_no"], {"total": total})
    return {"orderNo": order["order_no"], "status": 3, "totalActualNum": DEC2(total), "txnCount": len(details)}


def audit_out_order(conn, order_id, user):
    """审核出库单：可用量校验 + 预占（locked_qty 增加）"""
    order = conn.execute("SELECT * FROM wms_out_order WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ApiError(40400, "出库单不存在", 404)
    if order["status"] != 0:
        raise ApiError(40001, "只有草稿单可以审核", 409)
    details = conn.execute("SELECT * FROM wms_out_order_detail WHERE order_id=? ORDER BY goods_id,loc_id",
                           (order_id,)).fetchall()
    for d in details:
        qty = DEC2(d["plan_num"])
        for attempt in range(MAX_RETRY):
            inv = conn.execute("SELECT * FROM wms_inventory WHERE goods_id=? AND loc_id=? AND batch_no=?",
                               (d["goods_id"], d["loc_id"], d["batch_no"])).fetchone()
            if not inv or DEC2(inv["available_qty"]) < qty:
                avail = DEC2(inv["available_qty"]) if inv else 0
                g = conn.execute("SELECT goods_code,goods_name FROM base_goods WHERE id=?", (d["goods_id"],)).fetchone()
                l = conn.execute("SELECT loc_code FROM base_location WHERE id=?", (d["loc_id"],)).fetchone()
                raise ApiError(50201, "可用库存不足：%s 在库位 %s 可用 %s，需出库 %s"
                               % (g["goods_name"], l["loc_code"], avail, qty), 409)
            cur = conn.execute(
                "UPDATE wms_inventory SET locked_qty=locked_qty+?, available_qty=quantity-(locked_qty+?),"
                " version=version+1, update_time=? WHERE id=? AND version=? AND available_qty>=?",
                (qty, qty, now(), inv["id"], inv["version"], qty))
            if cur.rowcount == 0:
                time.sleep(0.05 * (attempt + 1))
                continue
            break
        else:
            raise ApiError(50202, "库存并发冲突，请刷新后重试", 409)
    conn.execute("UPDATE wms_out_order SET status=1, audit_by=?, audit_time=? WHERE id=?",
                 (user["id"], now(), order_id))
    write_log(conn, user, "出库管理", "AUDIT", order_id, order["order_no"], {"action": "审核并预占"})
    return {"orderNo": order["order_no"], "status": 1}


def post_out_order(conn, order_id, request_id, user):
    """确认出库（过账）：扣减 + 释放预占 + 流水 + 状态；幂等 + 乐观锁"""
    order = conn.execute("SELECT * FROM wms_out_order WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ApiError(40400, "出库单不存在", 404)
    if order["status"] == 3:
        return {"orderNo": order["order_no"], "status": 3, "duplicated": True}
    if order["status"] not in (1, 2):
        raise ApiError(40001, "单据未审核，不能确认出库", 409)
    details = conn.execute("SELECT * FROM wms_out_order_detail WHERE order_id=? ORDER BY goods_id,loc_id",
                           (order_id,)).fetchall()
    total = 0
    for d in details:
        qty = DEC2(d["actual_num"] if d["actual_num"] else d["plan_num"])
        if qty <= 0:
            continue
        for attempt in range(MAX_RETRY):
            inv = conn.execute("SELECT * FROM wms_inventory WHERE goods_id=? AND loc_id=? AND batch_no=?",
                               (d["goods_id"], d["loc_id"], d["batch_no"])).fetchone()
            if not inv:
                raise ApiError(50201, "库存记录不存在", 409)
            before = DEC2(inv["quantity"])
            after = DEC2(before - qty)
            if before < qty or DEC2(inv["locked_qty"]) < qty:
                raise ApiError(50201, "库存不足或预占已释放，请刷新后重试", 409)
            cur = conn.execute(
                "UPDATE wms_inventory SET quantity=?, locked_qty=locked_qty-?, available_qty=?-locked_qty+?,"
                " version=version+1, update_time=? WHERE id=? AND version=? AND quantity>=? AND locked_qty>=?",
                (after, qty, after, qty, now(), inv["id"], inv["version"], qty, qty))
            if cur.rowcount == 0:
                time.sleep(0.05 * (attempt + 1))
                continue
            conn.execute(
                "INSERT INTO wms_inventory_txn(txn_no,biz_type,biz_id,biz_no,detail_id,goods_id,loc_id,batch_no,"
                "change_qty,before_qty,after_qty,create_by) VALUES(?,4,?,?,?,?,?,?,?,?,?,?)",
                (next_txn_no(conn), order_id, order["order_no"], d["id"], d["goods_id"], d["loc_id"],
                 d["batch_no"], -qty, before, after, user["id"]))
            post_txn_cost(conn, 4, order_id, d["id"], d["goods_id"], -qty, before, after)
            total += qty
            break
        else:
            raise ApiError(50202, "库存并发冲突，请刷新后重试", 409)
    conn.execute("UPDATE wms_out_order SET status=3, total_actual_num=?, request_id=? WHERE id=?",
                 (DEC2(total), request_id, order_id))
    write_log(conn, user, "出库管理", "CONFIRM", order_id, order["order_no"], {"total": total})
    return {"orderNo": order["order_no"], "status": 3, "totalActualNum": DEC2(total), "txnCount": len(details)}


def cancel_out_order(conn, order_id, user):
    """作废出库单：释放预占"""
    order = conn.execute("SELECT * FROM wms_out_order WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ApiError(40400, "出库单不存在", 404)
    if order["status"] == 3:
        raise ApiError(50301, "单据已过账，不能作废", 409)
    if order["status"] in (1, 2):
        for d in conn.execute("SELECT * FROM wms_out_order_detail WHERE order_id=?", (order_id,)):
            qty = DEC2(d["plan_num"])
            conn.execute(
                "UPDATE wms_inventory SET locked_qty=locked_qty-?, available_qty=quantity-locked_qty+?,"
                " version=version+1, update_time=? WHERE goods_id=? AND loc_id=? AND batch_no=?",
                (qty, qty, now(), d["goods_id"], d["loc_id"], d["batch_no"]))
    conn.execute("UPDATE wms_out_order SET status=4 WHERE id=?", (order_id,))
    write_log(conn, user, "出库管理", "CANCEL", order_id, order["order_no"])
    return {"orderNo": order["order_no"], "status": 4}


# --------------------------------------------------------------------------- #
# 电商域：渠道 / 上下架 / 商城订单 / 库存分配 / 拣货发货 / 售后 / 补货
#
# 一句话原则（贯穿全文件）：
#   商城只能「下单」，它不碰库存；物理库存只在两个动作上变动 ——
#     ① 支付：校验可售量 → 按 FIFO 拆到库位×批次 → 预占（locked_qty+，实物不变）
#     ② 发货：扣实物（quantity−）+ 释放预占（locked_qty−）+ 写流水，同一事务
#   这样「用户购买影响仓库数量」这件事才有唯一、可审计的落点。
# --------------------------------------------------------------------------- #
VERSION = "5.0"
START_TIME = time.time()
EC_STATUS = {0: "待付款", 1: "待发货", 2: "拣货中", 3: "已发货", 4: "已完成", 5: "已取消",
             6: "售后中", 7: "待打包", 8: "部分发货"}
WAVE_STATUS = {0: "待拣货", 1: "拣货中", 2: "待打包", 3: "已完成", 4: "已取消"}
AFTER_SALE_TYPE = {1: "退货退款", 2: "仅退款", 3: "换货", 4: "补发"}
ORDER_KIND = {1: "销售单", 2: "换货单", 3: "补发单"}
AFTER_SALE_STATUS = {0: "待审核", 1: "同意待收货", 2: "已完成", 3: "已拒绝"}
CARRIERS = ["顺丰速运", "中通快递", "圆通速递", "韵达快递", "京东物流", "自提"]
BIZ_EC_SHIP, BIZ_EC_RETURN = 21, 22          # 电商发货出库 / 电商退货入库（独立 biz_type，见 schema 注释）


def get_config(conn, key, default=""):
    row = conn.execute("SELECT config_value FROM sys_config WHERE config_key=?", (key,)).fetchone()
    return row["config_value"] if row else default

def get_config_int(conn, key, default=0):
    """读整数参数；非法值回落默认值（参数是运维手改的，别让脏数据把服务打挂）。"""
    try:
        return int(str(get_config(conn, key, default)).strip())
    except (TypeError, ValueError):
        return int(default)



def set_config(conn, key, value, remark=None):
    conn.execute(
        "INSERT INTO sys_config(config_key,config_value,remark) VALUES(?,?,?)"
        " ON CONFLICT(config_key) DO UPDATE SET config_value=excluded.config_value,"
        " update_time=datetime('now','localtime')", (key, str(value), remark))


def next_seq(conn, key, boot_fn=None):
    """原子取号：UPDATE ... RETURNING，单条语句完成自增，不会撞号也不会扫表。

    boot_fn 只在该 key 第一次出现时调用（用现有单据数做续号，兼容老库）。
    """
    row = conn.execute("SELECT next_val FROM sys_seq WHERE seq_key=?", (key,)).fetchone()
    if row is None:
        boot = 0
        if boot_fn:
            try:
                boot = int(boot_fn() or 0)
            except Exception:  # noqa: BLE001
                boot = 0
        try:
            cur = conn.execute("INSERT INTO sys_seq(seq_key,next_val) VALUES(?,?) RETURNING next_val",
                               (key, boot + 1))
            return cur.fetchone()[0]
        except sqlite3.IntegrityError:
            pass                                  # 并发下别的线程刚插入，落到下面的自增
    cur = conn.execute("UPDATE sys_seq SET next_val=next_val+1 WHERE seq_key=? RETURNING next_val", (key,))
    got = cur.fetchone()
    if not got:                                   # 极端竞态：行被删/未插上，重试一次
        conn.execute("INSERT OR IGNORE INTO sys_seq(seq_key,next_val) VALUES(?,1)", (key,))
        got = conn.execute("UPDATE sys_seq SET next_val=next_val+1 WHERE seq_key=? RETURNING next_val",
                           (key,)).fetchone()
    return got[0]


def next_no(conn, table, column, prefix):
    """单号：前缀 + yyyyMMdd + 4 位日内序列（演示用；正式版用 Redis 原子自增或序列表）"""
    day = datetime.now().strftime("%Y%m%d")
    key = "%s:%s" % (prefix, day)
    sql = "SELECT COUNT(*) FROM %s WHERE %s LIKE ?" % (table, column)
    pat = prefix + day + "%"
    seq = next_seq(conn, key, lambda: conn.execute(sql, (pat,)).fetchone()[0])
    return "%s%s%04d" % (prefix, day, seq)


def goods_available_total(conn, goods_id):
    row = conn.execute(
        "SELECT IFNULL(SUM(i.available_qty),0) FROM wms_inventory i JOIN base_location l ON l.id=i.loc_id"
        " WHERE i.goods_id=? AND l.status=1 AND l.loc_type=4", (goods_id,)).fetchone()
    return DEC2(row[0])


def shop_saleable(conn, shop_id, goods_id):
    """商城可售量 = 全仓可用量 − 该渠道保留量（下架商品可售按 0 处理）"""
    listing = conn.execute("SELECT * FROM ec_listing WHERE shop_id=? AND goods_id=?", (shop_id, goods_id)).fetchone()
    if not listing or not listing["sale_status"]:
        return 0.0
    return max(0.0, DEC2(goods_available_total(conn, goods_id) - DEC2(listing["reserve_qty"])))


def try_lock_inventory(conn, inv_id, want):
    """在指定库存行上预占；返回实际锁到的数量（0 = 该行已无可用）。乐观锁 + 条件更新。"""
    for _ in range(MAX_RETRY):
        row = conn.execute("SELECT * FROM wms_inventory WHERE id=?", (inv_id,)).fetchone()
        if not row:
            return 0.0
        take = DEC2(min(DEC2(row["available_qty"]), DEC2(want)))
        if take <= 0:
            return 0.0
        cur = conn.execute(
            "UPDATE wms_inventory SET locked_qty=locked_qty+?, available_qty=available_qty-?,"
            " version=version+1, update_time=? WHERE id=? AND version=? AND available_qty>=?",
            (take, take, now(), inv_id, row["version"], take))
        if cur.rowcount:
            return take
        time.sleep(0.02)
    raise ApiError(50202, "库存并发冲突，请稍后重试", 409)


def allocate_for_item(conn, item, order_id):
    """按 FIFO（入库时间）把订单行拆到具体库位×批次，并逐行预占，落 ec_order_alloc。"""
    need = DEC2(item["qty"])
    # 出库分配顺序：开启 FEFO 时先按到期日（近的先出），否则纯 FIFO（按入库时间）
    fefo = 1 if str(get_config(conn, "inventory.fefo.enabled", "false")).lower() == "true" else 0
    candidates = conn.execute(
        "SELECT i.id, i.loc_id, i.batch_no FROM wms_inventory i JOIN base_location l ON l.id=i.loc_id"
        " WHERE i.goods_id=? AND i.available_qty>0 AND l.status=1 AND l.loc_type=4"
        " ORDER BY (CASE WHEN ?=1 THEN IFNULL(i.expire_date,'9999-12-31') ELSE '9999-12-31' END),"
        " IFNULL(i.in_time,''), i.id", (item["goods_id"], fefo)).fetchall()
    for cand in candidates:
        if need <= 0:
            break
        got = try_lock_inventory(conn, cand["id"], need)
        if got <= 0:
            continue
        conn.execute("INSERT INTO ec_order_alloc(order_id,item_id,goods_id,loc_id,batch_no,alloc_qty)"
                     " VALUES(?,?,?,?,?,?)",
                     (order_id, item["id"], item["goods_id"], cand["loc_id"], cand["batch_no"], got))
        need = DEC2(need - got)
    if need > 0:
        raise ApiError(50201, "库存不足：%s 还差 %s，请补货或拆单后再支付"
                       % (item["goods_name"], need), 409)


def split_into_fulfillments(conn, order_id):
    """拆单：按分配落到的仓库切成履约单（ec_fulfillment），并把分配挂到履约单上。

    为什么必须按仓拆：一个订单的货可能分布在多个仓，各仓要独立拣货/打包/发货。
    不拆的话就会出现「A 仓发了、B 仓的货还在库里，订单却显示已发货」——典型漏发/空单。
    """
    allocs = conn.execute(
        "SELECT a.id, a.alloc_qty, l.wh_id FROM ec_order_alloc a"
        " JOIN base_location l ON l.id=a.loc_id WHERE a.order_id=? AND a.status IN (1,2)",
        (order_id,)).fetchall()
    if not allocs:
        return 0
    by_wh = {}
    for a in allocs:
        by_wh.setdefault(a["wh_id"], []).append(a)
    existing = {f["wh_id"]: f for f in conn.execute(
        "SELECT * FROM ec_fulfillment WHERE order_id=?", (order_id,)).fetchall()}
    seq = len(existing)
    for wh_id, items in sorted(by_wh.items()):
        total = DEC2(sum(DEC2(i["alloc_qty"]) for i in items))
        ff = existing.get(wh_id)
        if ff:
            conn.execute("UPDATE ec_fulfillment SET total_qty=? WHERE id=?", (total, ff["id"]))
            fid = ff["id"]
        else:
            seq += 1
            cur = conn.execute(
                "INSERT INTO ec_fulfillment(order_id,wh_id,seq,status,total_qty,create_time)"
                " VALUES(?,?,?,1,?,?)", (order_id, wh_id, seq, total, now()))
            fid = cur.lastrowid
        for it in items:
            conn.execute("UPDATE ec_order_alloc SET fulfillment_id=?, wh_id=? WHERE id=?",
                         (fid, wh_id, it["id"]))
    conn.execute("UPDATE ec_order SET split_count=? WHERE id=?", (seq, order_id))
    return seq


def backfill_fulfillments(conn):
    """老库/种子数据补齐：已支付但无履约单的订单，按现有分配补出来（幂等）。"""
    # ① 分配未挂履约单：按仓库重新分组
    conn.execute("UPDATE ec_order_alloc SET fulfillment_id=NULL WHERE fulfillment_id IS NOT NULL"
                 " AND fulfillment_id NOT IN (SELECT id FROM ec_fulfillment)")
    rows = conn.execute(
        "SELECT o.id FROM ec_order o WHERE o.status IN (1,2,3,4,7,8)"
        " AND (IFNULL(o.split_count,0)=0 OR EXISTS (SELECT 1 FROM ec_order_alloc a"
        "     WHERE a.order_id=o.id AND a.status IN (1,2) AND IFNULL(a.fulfillment_id,0)=0))").fetchall()
    fixed = 0
    for r in rows:
        if split_into_fulfillments(conn, r["id"]):
            fixed += 1
    if fixed:
        conn.commit()
    return fixed


def pick_fulfillment(conn, order_id, fulfillment_id=None):
    """选出本次要发的履约单：显式指定 > 唯一待发 > 报错让调用方指定（不让系统替人猜）。"""
    ffs = conn.execute("SELECT * FROM ec_fulfillment WHERE order_id=? ORDER BY seq",
                       (order_id,)).fetchall()
    pending = [f for f in ffs if f["status"] == 1]
    if fulfillment_id:
        for f in pending:
            if int(f["id"]) == int(fulfillment_id):
                return f
        raise ApiError(40001, "指定的履约单不存在或已发货", 409)
    if not pending:
        raise ApiError(40001, "该订单的履约单都已发货", 409)
    if len(pending) == 1:
        return pending[0]
    detail = "、".join("#%d(仓库%s %s件)" % (f["id"], f["wh_id"], DEC2(f["total_qty"])) for f in pending)
    raise ApiError(40001, "订单拆成 %d 个仓库发货，请指定 fulfillmentId：%s" % (len(pending), detail), 409)


def create_ec_order(conn, body, user):
    """商城下单（待付款）：只生成订单，不碰库存。价格一律取服务端上下架价，不信任前端传值。"""
    try:
        shop_id = int(body.get("shop_id"))
    except (TypeError, ValueError):
        raise ApiError(40000, "请选择店铺")
    if not conn.execute("SELECT 1 FROM base_shop WHERE id=? AND status=1", (shop_id,)).fetchone():
        raise ApiError(40000, "店铺不存在或已停用")
    items = body.get("items") or []
    if not items:
        raise ApiError(40000, "订单没有商品")
    lines, amount = [], 0.0
    for idx, it in enumerate(items, start=1):
        try:
            goods_id = int(it.get("goods_id"))
            qty = positive_qty(it.get("qty"), "第 %d 行数量" % idx)
        except (TypeError, ValueError, AttributeError):
            raise ApiError(40000, "第 %d 行商品或数量无效" % idx)
        listing = conn.execute("SELECT * FROM ec_listing WHERE shop_id=? AND goods_id=?",
                               (shop_id, goods_id)).fetchone()
        if not listing or not listing["sale_status"]:
            raise ApiError(40002, "第 %d 行商品已下架，无法下单" % idx, 409)
        goods = conn.execute("SELECT * FROM base_goods WHERE id=? AND status=1", (goods_id,)).fetchone()
        if not goods:
            raise ApiError(40000, "第 %d 行商品不存在或已停用" % idx)
        price = DEC2(listing["sale_price"])
        lines.append({"goods_id": goods_id, "code": goods["goods_code"], "name": goods["goods_name"],
                      "price": price, "qty": qty, "amount": DEC2(price * qty)})
        amount = DEC2(amount + price * qty)
    free_at = float(get_config(conn, "ec.free.shipping.amount", "99") or 99)
    freight = 0.0 if amount >= free_at else float(get_config(conn, "ec.freight.fee", "8") or 8)
    pay_amount = DEC2(amount + freight)
    order_no = next_no(conn, "ec_order", "order_no", "DS")
    cur = conn.execute(
        "INSERT INTO ec_order(order_no,channel_order_no,shop_id,buyer_name,buyer_phone,receiver_name,"
        "receiver_phone,receiver_address,status,goods_amount,freight,pay_amount,create_by)"
        " VALUES(?,?,?,?,?,?,?,?,0,?,?,?,?)",
        (order_no, body.get("channel_order_no"), shop_id,
         (body.get("buyer_name") or "游客").strip(), body.get("buyer_phone"),
         (body.get("receiver_name") or body.get("buyer_name") or "游客").strip(),
         body.get("receiver_phone") or body.get("buyer_phone"),
         body.get("receiver_address"), amount, DEC2(freight), pay_amount, user["id"]))
    oid = cur.lastrowid
    for ln in lines:
        conn.execute("INSERT INTO ec_order_item(order_id,goods_id,goods_code,goods_name,price,qty,amount)"
                     " VALUES(?,?,?,?,?,?,?)",
                     (oid, ln["goods_id"], ln["code"], ln["name"], ln["price"], ln["qty"], ln["amount"]))
    write_log(conn, user, "商城订单", "CREATE", oid, order_no, {"payAmount": pay_amount, "items": len(lines)})
    return {"id": oid, "orderNo": order_no, "goodsAmount": amount,
            "freight": DEC2(freight), "payAmount": pay_amount, "status": 0}


def pay_ec_order(conn, order_id, request_id, user):
    """支付：幂等 + 可售量校验 + FIFO 分配预占。库存不足直接拒付（不产生半截分配）。"""
    order = conn.execute("SELECT * FROM ec_order WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ApiError(40400, "订单不存在", 404)
    if order["status"] >= 1 or order["status"] == 5:
        return {"orderNo": order["order_no"], "status": order["status"], "duplicated": True}
    if order["status"] != 0:
        raise ApiError(40001, "订单当前状态不允许支付", 409)
    items = conn.execute("SELECT * FROM ec_order_item WHERE order_id=? ORDER BY id", (order_id,)).fetchall()
    if not items:
        raise ApiError(40001, "订单没有明细", 409)
    for it in items:
        saleable = shop_saleable(conn, order["shop_id"], it["goods_id"])
        if saleable < DEC2(it["qty"]):
            raise ApiError(50201, "支付失败：%s 可售 %s，本单需要 %s，库存不足"
                           % (it["goods_name"], saleable, DEC2(it["qty"])), 409)
    for it in items:
        allocate_for_item(conn, it, order_id)
    split_count = split_into_fulfillments(conn, order_id)
    conn.execute("UPDATE ec_order SET status=1, pay_time=?, request_id=?, split_count=? WHERE id=?",
                 (now(), request_id, split_count, order_id))
    write_log(conn, user, "商城订单", "PAY", order_id, order["order_no"],
              {"payAmount": order["pay_amount"], "splitCount": split_count})
    return {"orderNo": order["order_no"], "status": 1, "duplicated": False,
            "splitCount": split_count}


def pick_ec_order(conn, order_id, user):
    """拣货：待发货 → 拣货中；分配明细标记为已拣（实物仍未动）"""
    order = conn.execute("SELECT * FROM ec_order WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ApiError(40400, "订单不存在", 404)
    if order["status"] == 2:
        return {"orderNo": order["order_no"], "status": 2, "duplicated": True}
    if order["status"] == 0:
        raise ApiError(40001, "订单未支付，不能拣货", 409)
    if order["status"] not in (1,):
        raise ApiError(40001, "订单状态不允许拣货", 409)
    conn.execute("UPDATE ec_order_alloc SET status=2 WHERE order_id=? AND status=1", (order_id,))
    conn.execute("UPDATE ec_order SET status=2 WHERE id=?", (order_id,))
    write_log(conn, user, "电商履约", "PICK", order_id, order["order_no"])
    return {"orderNo": order["order_no"], "status": 2}


def ship_ec_order(conn, order_id, carrier, tracking_no, user, fulfillment_id=None):
    """按「履约单（仓）」发货过账：扣实物 + 释放预占 + 流水（21）+ 发货单，同一事务。

    生产级防错（针对「发空单」「买了没发货」）：
    ① 多仓拆单时必须显式指定履约单，系统不替人猜；
    ② 该履约单必须有分配明细且数量 > 0（否则拒绝，空单不可能过账）；
    ③ 运单号必填（物流追溯与售后判定的凭据）；
    ④ 全部分配发完才置「已发货」，否则置「部分发货」并保留剩余履约单；
    ⑤ 发货单落 item_qty，供对账与校验（item_qty<=0 视为异常）。
    """
    order = conn.execute("SELECT * FROM ec_order WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ApiError(40400, "订单不存在", 404)
    if order["status"] == 4:
        raise ApiError(40001, "订单已完成，不能再发货", 409)
    if order["status"] == 6:
        raise ApiError(40001, "订单售后中，请先处理售后", 409)
    if conn.execute("SELECT COUNT(*) FROM ec_fulfillment WHERE order_id=?", (order_id,)).fetchone()[0] == 0:
        split_into_fulfillments(conn, order_id)
    ff = pick_fulfillment(conn, order_id, fulfillment_id)
    in_wave = conn.execute("SELECT 1 FROM ec_wave_order WHERE order_id=? AND status<3", (order_id,)).fetchone()
    has_package = conn.execute("SELECT 1 FROM ec_package WHERE order_id=? LIMIT 1", (order_id,)).fetchone()
    if order["status"] == 7 and not has_package:
        raise ApiError(40001, "订单已复核，但还没打包（称重/包材），请先完成打包再发货", 409)
    if order["status"] in (1, 2) and not in_wave:
        if not has_package:
            conn.execute("INSERT INTO ec_package(package_no,order_id,weight_g,remark,create_by)"
                         " VALUES(?,?,0,'直发自动建包',?)",
                         (next_no(conn, "ec_package", "package_no", "BZ"), order_id, user["id"]))
    elif order["status"] not in (7, 8):
        raise ApiError(40001, "该订单还没走完「拣货 → 复核 → 打包」，不能发货（当前：%s）"
                       % EC_STATUS.get(order["status"], order["status"]), 409)
    carrier = (carrier or "").strip() or "顺丰速运"
    tracking_no = (tracking_no or "").strip()
    if not tracking_no:
        raise ApiError(40000, "请填写运单号（发货单是物流追溯与售后判定的凭据）")
    allocs = conn.execute(
        "SELECT * FROM ec_order_alloc WHERE order_id=? AND fulfillment_id=? AND status IN (1,2)"
        " ORDER BY id", (order_id, ff["id"])).fetchall()
    if not allocs:
        raise ApiError(40001, "该履约单没有可发货的库存分配（拒绝空单）", 409)
    to_ship = DEC2(sum(DEC2(a["alloc_qty"]) for a in allocs))
    if to_ship <= 0:
        raise ApiError(40001, "该履约单发货数量为 0，拒绝空单过账", 409)
    shipped = 0.0
    for a in allocs:
        qty = DEC2(a["alloc_qty"])
        for attempt in range(MAX_RETRY):
            inv = conn.execute("SELECT * FROM wms_inventory WHERE goods_id=? AND loc_id=? AND batch_no=?",
                               (a["goods_id"], a["loc_id"], a["batch_no"])).fetchone()
            if not inv:
                raise ApiError(50201, "库存记录不存在，请检查数据一致性", 409)
            q_after = DEC2(inv["quantity"] - qty)
            l_after = DEC2(inv["locked_qty"] - qty)
            if q_after < 0 or l_after < 0:
                raise ApiError(50201, "库存或预占不足，发货失败（请跑一致性校验）", 409)
            cur = conn.execute(
                "UPDATE wms_inventory SET quantity=?, locked_qty=?, available_qty=?, version=version+1,"
                " update_time=? WHERE id=? AND version=? AND quantity>=? AND locked_qty>=?",
                (q_after, l_after, DEC2(q_after - l_after), now(), inv["id"], inv["version"], qty, qty))
            if cur.rowcount:
                conn.execute(
                    "INSERT INTO wms_inventory_txn(txn_no,biz_type,biz_id,biz_no,detail_id,goods_id,loc_id,"
                    "batch_no,change_qty,before_qty,after_qty,create_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (next_txn_no(conn), BIZ_EC_SHIP, order_id, order["order_no"], a["id"], a["goods_id"],
                     a["loc_id"], a["batch_no"], -qty, DEC2(inv["quantity"]), q_after, user["id"]))
                post_txn_cost(conn, BIZ_EC_SHIP, order_id, a["id"], a["goods_id"], -qty,
                              DEC2(inv["quantity"]), q_after)
                break
            time.sleep(0.02 * (attempt + 1))
        else:
            raise ApiError(50202, "库存并发冲突，请重试", 409)
        conn.execute("UPDATE ec_order_alloc SET status=3 WHERE id=?", (a["id"],))
        shipped = DEC2(shipped + qty)
    ff_row = conn.execute("SELECT wh_id FROM ec_fulfillment WHERE id=?", (ff["id"],)).fetchone()
    conn.execute("UPDATE ec_fulfillment SET status=2, shipped_qty=?, ship_time=? WHERE id=?",
                 (shipped, now(), ff["id"]))
    shipment_no = next_no(conn, "ec_shipment", "shipment_no", "FH")
    conn.execute("INSERT INTO ec_shipment(shipment_no,order_id,carrier,tracking_no,status,create_by,"
                 "fulfillment_id,wh_id,item_qty) VALUES(?,?,?,?,1,?,?,?,?)",
                 (shipment_no, order_id, carrier, tracking_no, user["id"], ff["id"],
                  ff_row["wh_id"] if ff_row else None, shipped))
    left = conn.execute("SELECT COUNT(*) FROM ec_fulfillment WHERE order_id=? AND status=1",
                        (order_id,)).fetchone()[0]
    if left == 0:
        conn.execute("UPDATE ec_order SET status=3, ship_time=?, ship_qty=IFNULL(ship_qty,0)+? WHERE id=?",
                     (now(), shipped, order_id))
        for wo in conn.execute("SELECT * FROM ec_wave_order WHERE order_id=? AND status<3",
                               (order_id,)).fetchall():
            conn.execute("UPDATE ec_wave_order SET status=3 WHERE id=?", (wo["id"],))
            if conn.execute("SELECT COUNT(*) FROM ec_wave_order WHERE wave_id=? AND status<3",
                            (wo["wave_id"],)).fetchone()[0] == 0:
                conn.execute("UPDATE ec_wave SET status=3, finish_time=? WHERE id=?", (now(), wo["wave_id"]))
    else:
        conn.execute("UPDATE ec_order SET status=8, ship_qty=IFNULL(ship_qty,0)+? WHERE id=?",
                     (shipped, order_id))
    write_log(conn, user, "电商履约", "SHIP", order_id, order["order_no"],
              {"carrier": carrier, "trackingNo": tracking_no, "qty": shipped,
               "fulfillmentId": ff["id"], "remaining": left})
    return {"orderNo": order["order_no"], "status": 3 if left == 0 else 8,
            "fulfillmentId": ff["id"], "shipmentNo": shipment_no, "trackingNo": tracking_no,
            "shippedQty": shipped, "remainingFulfillments": left}


def cancel_ec_order(conn, order_id, reason, user):
    """取消订单：释放预占（实物从未变过）。已发货只能走售后。"""
    order = conn.execute("SELECT * FROM ec_order WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ApiError(40400, "订单不存在", 404)
    if order["status"] == 5:
        return {"orderNo": order["order_no"], "status": 5, "duplicated": True}
    if order["status"] in (3, 4, 6):
        raise ApiError(50301, "订单已发货，不能直接取消，请提交售后", 409)
    released = 0.0
    for a in conn.execute("SELECT * FROM ec_order_alloc WHERE order_id=? AND status IN (1,2)",
                          (order_id,)).fetchall():
        qty = DEC2(a["alloc_qty"])
        for attempt in range(MAX_RETRY):
            inv = conn.execute("SELECT * FROM wms_inventory WHERE goods_id=? AND loc_id=? AND batch_no=?",
                               (a["goods_id"], a["loc_id"], a["batch_no"])).fetchone()
            if not inv:
                break
            l_after = DEC2(max(0.0, inv["locked_qty"] - qty))
            cur = conn.execute(
                "UPDATE wms_inventory SET locked_qty=?, available_qty=?, version=version+1, update_time=?"
                " WHERE id=? AND version=?", (l_after, DEC2(inv["quantity"] - l_after), now(),
                                              inv["id"], inv["version"]))
            if cur.rowcount:
                break
            time.sleep(0.02 * (attempt + 1))
        conn.execute("UPDATE ec_order_alloc SET status=4 WHERE id=?", (a["id"],))
        released = DEC2(released + qty)
    conn.execute("DELETE FROM ec_fulfillment WHERE order_id=? AND status=1", (order_id,))
    conn.execute("UPDATE ec_order SET status=5, cancel_reason=?, split_count=0 WHERE id=?",
                 (reason, order_id))
    write_log(conn, user, "商城订单", "CANCEL", order_id, order["order_no"],
              {"reason": reason, "releasedQty": released})
    return {"orderNo": order["order_no"], "status": 5, "releasedQty": released}


def confirm_ec_receipt(conn, order_id, user):
    """确认收货：已发货 → 已完成（交易闭环，售后窗口从这里开始算）"""
    order = conn.execute("SELECT * FROM ec_order WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ApiError(40400, "订单不存在", 404)
    if order["status"] == 4:
        return {"orderNo": order["order_no"], "status": 4, "duplicated": True}
    if order["status"] != 3:
        raise ApiError(40001, "只有已发货的订单可以确认收货", 409)
    conn.execute("UPDATE ec_order SET status=4, finish_time=? WHERE id=?", (now(), order_id))
    conn.execute("UPDATE ec_shipment SET status=2 WHERE order_id=?", (order_id,))
    write_log(conn, user, "商城订单", "RECEIVE", order_id, order["order_no"])
    return {"orderNo": order["order_no"], "status": 4}


# ---- 波次拣货 / 复核 / 打包（电商仓的作业形态，一次拣一批单）----
def pick_wave_tasks(conn, wave_id, user, task_id=None, picked_qty=None, all_tasks=False):
    """拣货确认：可以逐条拣（PDA 扫一个库位点一次），也可以「整波一键拣完」。"""
    wave = conn.execute("SELECT * FROM ec_wave WHERE id=?", (wave_id,)).fetchone()
    if not wave:
        raise ApiError(40400, "波次不存在", 404)
    if wave["status"] not in (0, 1):
        raise ApiError(40001, "波次当前状态（%s）不能拣货" % WAVE_STATUS.get(wave["status"]), 409)
    if all_tasks:
        targets = conn.execute("SELECT * FROM ec_pick_task WHERE wave_id=? AND (status<>1 OR picked_qty<qty)",
                               (wave_id,)).fetchall()
    else:
        if not task_id:
            raise ApiError(40000, "请指定拣货任务")
        task = conn.execute("SELECT * FROM ec_pick_task WHERE id=? AND wave_id=?", (int(task_id), wave_id)).fetchone()
        if not task:
            raise ApiError(40400, "拣货任务不存在或不属于该波次", 404)
        targets = [task]
    picked_total = 0.0
    for t in targets:
        if t["status"] == 1 and DEC2(t["picked_qty"]) >= DEC2(t["qty"]):
            continue
        qty = DEC2(t["qty"])
        got = DEC2(picked_qty if (picked_qty not in (None, "") and not all_tasks) else qty)
        if got <= 0:
            raise ApiError(40000, "拣货数量必须大于 0")
        if got > qty:
            raise ApiError(40001, "拣货数量不能超过分配数量 %s（短拣请走缺货登记）" % qty, 409)
        conn.execute("UPDATE ec_pick_task SET picked_qty=?, status=1 WHERE id=?", (got, t["id"]))
        picked_total = DEC2(picked_total + got)
    conn.execute("UPDATE ec_wave SET status=1 WHERE id=? AND status=0", (wave_id,))
    # 订单级推进：该订单在波次里的任务都拣完了 → 波次订单标记「已拣」
    for row in conn.execute("SELECT DISTINCT order_id FROM ec_pick_task WHERE wave_id=?", (wave_id,)):
        left = conn.execute("SELECT COUNT(*) FROM ec_pick_task WHERE wave_id=? AND order_id=? AND status<>1",
                            (wave_id, row["order_id"])).fetchone()[0]
        if left == 0:
            conn.execute("UPDATE ec_wave_order SET status=1 WHERE wave_id=? AND order_id=? AND status=0",
                         (wave_id, row["order_id"]))
    write_log(conn, user, "波次作业", "PICK", wave_id, wave["wave_no"],
              {"tasks": len(targets), "qty": picked_total})
    return {"waveNo": wave["wave_no"], "pickedTasks": len(targets), "pickedQty": picked_total}


def review_wave_order(conn, wave_id, order_id, user):
    """复核：拣货数量必须等于分配数量才能进打包（电商的「扫码复核」防错发）"""
    wo = conn.execute("SELECT * FROM ec_wave_order WHERE wave_id=? AND order_id=?",
                      (wave_id, order_id)).fetchone()
    if not wo:
        raise ApiError(40400, "该订单不在这个波次里", 404)
    if wo["status"] >= 2:
        return {"orderId": order_id, "status": wo["status"], "duplicated": True}
    short = conn.execute(
        "SELECT p.id, p.qty, p.picked_qty, p.status, g.goods_name, l.loc_code FROM ec_pick_task p"
        " LEFT JOIN base_goods g ON g.id=p.goods_id LEFT JOIN base_location l ON l.id=p.loc_id"
        " WHERE p.wave_id=? AND p.order_id=? AND (p.status<>1 OR p.picked_qty<p.qty)",
        (wave_id, order_id)).fetchall()
    if short:
        detail = "、".join("%s@%s 拣 %s/%s" % (r["goods_name"] or r["id"], r["loc_code"] or "",
                                              DEC2(r["picked_qty"]), DEC2(r["qty"])) for r in short[:3])
        raise ApiError(50001, "复核不通过：%d 条任务未拣完或短拣（%s）" % (len(short), detail), 409)
    conn.execute("UPDATE ec_wave_order SET status=2 WHERE id=?", (wo["id"],))
    conn.execute("UPDATE ec_order SET status=7 WHERE id=?", (order_id,))
    left = conn.execute("SELECT COUNT(*) FROM ec_wave_order WHERE wave_id=? AND status<2",
                        (wave_id,)).fetchone()[0]
    if left == 0:
        conn.execute("UPDATE ec_wave SET status=2 WHERE id=?", (wave_id,))
    wave = conn.execute("SELECT wave_no FROM ec_wave WHERE id=?", (wave_id,)).fetchone()
    write_log(conn, user, "波次作业", "AUDIT", wave_id, wave["wave_no"], {"reviewedOrder": order_id})
    return {"orderId": order_id, "status": 2}


def pack_ec_order(conn, order_id, weight_g, remark, user):
    """打包：生成包裹（称重/包材），订单进入待发货交接。"""
    order = conn.execute("SELECT * FROM ec_order WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ApiError(40400, "订单不存在", 404)
    exists = conn.execute("SELECT * FROM ec_package WHERE order_id=? ORDER BY id DESC LIMIT 1",
                          (order_id,)).fetchone()
    if exists:
        return {"packageNo": exists["package_no"], "duplicated": True}
    if order["status"] != 7:
        raise ApiError(40001, "只有「待打包」的订单可以打包（当前：%s）" % EC_STATUS.get(order["status"]), 409)
    package_no = next_no(conn, "ec_package", "package_no", "BZ")
    conn.execute("INSERT INTO ec_package(package_no,order_id,weight_g,remark,create_by) VALUES(?,?,?,?,?)",
                 (package_no, order_id, int(weight_g or 0), remark, user["id"]))
    write_log(conn, user, "电商履约", "PACK", order_id, order["order_no"],
              {"packageNo": package_no, "weightG": int(weight_g or 0)})
    return {"packageNo": package_no, "orderNo": order["order_no"]}


def cancel_wave(conn, wave_id, user):
    """取消波次：只回滚作业状态，不动库存（预占仍在，订单回到待发货）"""
    wave = conn.execute("SELECT * FROM ec_wave WHERE id=?", (wave_id,)).fetchone()
    if not wave:
        raise ApiError(40400, "波次不存在", 404)
    if wave["status"] in (3, 4):
        raise ApiError(40001, "波次已完成或已取消", 409)
    conn.execute("DELETE FROM ec_pick_task WHERE wave_id=?", (wave_id,))
    for wo in conn.execute("SELECT * FROM ec_wave_order WHERE wave_id=?", (wave_id,)).fetchall():
        if wo["status"] < 3:
            conn.execute("UPDATE ec_order SET status=1 WHERE id=? AND status IN (2,7)", (wo["order_id"],))
    conn.execute("UPDATE ec_wave_order SET status=0 WHERE wave_id=?", (wave_id,))
    conn.execute("UPDATE ec_wave SET status=4 WHERE id=?", (wave_id,))
    write_log(conn, user, "波次作业", "CANCEL", wave_id, wave["wave_no"])
    return {"waveNo": wave["wave_no"], "status": 4}


def create_wave(conn, order_ids, user):
    """把一批「待发货」订单合成波次：生成拣货任务（按库位编码排序 = 拣货路径）。"""
    if not order_ids:
        raise ApiError(40000, "请勾选要合并波次的订单")
    orders, seen = [], set()
    for raw in order_ids:
        try:
            oid = int(raw)
        except (TypeError, ValueError):
            raise ApiError(40000, "订单号无效")
        if oid in seen:
            continue
        seen.add(oid)
        o = conn.execute("SELECT * FROM ec_order WHERE id=?", (oid,)).fetchone()
        if not o:
            raise ApiError(40400, "订单 %s 不存在" % oid, 404)
        if o["status"] != 1:
            raise ApiError(40001, "订单 %s 当前是「%s」，只有待发货订单能进波次"
                           % (o["order_no"], EC_STATUS.get(o["status"], o["status"])), 409)
        busy = conn.execute("SELECT w.wave_no FROM ec_wave_order wo JOIN ec_wave w ON w.id=wo.wave_id"
                            " WHERE wo.order_id=? AND w.status IN (0,1,2)", (oid,)).fetchone()
        if busy:
            raise ApiError(40001, "订单 %s 已在波次 %s 中" % (o["order_no"], busy["wave_no"]), 409)
        orders.append(o)
    wh_ids, allocs_by_order = set(), {}
    for o in orders:
        allocs = conn.execute(
            "SELECT a.*, l.loc_code FROM ec_order_alloc a JOIN base_location l ON l.id=a.loc_id"
            " WHERE a.order_id=? AND a.status IN (1,2) ORDER BY l.loc_code, a.id", (o["id"],)).fetchall()
        if not allocs:
            raise ApiError(40001, "订单 %s 没有待拣的库存分配" % o["order_no"], 409)
        for a in allocs:
            wh_ids.add(warehouse_for_location(conn, a["loc_id"])["id"])
        allocs_by_order[o["id"]] = allocs
    if len(wh_ids) > 1:
        raise ApiError(40001, "一个波次只能含同一仓库的订单（当前跨 %d 个仓库，请分仓建波次）"
                       % len(wh_ids), 409)
    check_wh_scope(user, list(wh_ids)[0])
    wave_no = next_no(conn, "ec_wave", "wave_no", "BC")
    item_count = sum(len(v) for v in allocs_by_order.values())
    total_qty = DEC2(sum(DEC2(a["alloc_qty"]) for v in allocs_by_order.values() for a in v))
    cur = conn.execute(
        "INSERT INTO ec_wave(wave_no,shop_id,wh_id,status,order_count,item_count,total_qty,create_by)"
        " VALUES(?,?,?,0,?,?,?,?)",
        (wave_no, orders[0]["shop_id"], list(wh_ids)[0], len(orders), item_count, total_qty, user["id"]))
    wave_id = cur.lastrowid
    for o in orders:
        conn.execute("INSERT INTO ec_wave_order(wave_id,order_id,status) VALUES(?,?,0)", (wave_id, o["id"]))
        for a in allocs_by_order[o["id"]]:
            conn.execute("INSERT INTO ec_pick_task(wave_id,order_id,alloc_id,goods_id,loc_id,batch_no,qty)"
                         " VALUES(?,?,?,?,?,?,?)",
                         (wave_id, o["id"], a["id"], a["goods_id"], a["loc_id"], a["batch_no"],
                          DEC2(a["alloc_qty"])))
        conn.execute("UPDATE ec_order SET status=2 WHERE id=?", (o["id"],))
    write_log(conn, user, "波次作业", "CREATE", wave_id, wave_no,
              {"orders": len(orders), "items": item_count, "qty": total_qty})
    return {"id": wave_id, "waveNo": wave_no, "orderCount": len(orders),
            "itemCount": item_count, "totalQty": total_qty}


def defect_location(conn):
    """次品库位（退货质检判残次的货进这里，不计入正常可售）"""
    row = conn.execute("SELECT id FROM base_location WHERE loc_code='WH01-QC-01' AND status=1").fetchone()
    if row:
        return row["id"]
    row = conn.execute("SELECT id FROM base_location WHERE loc_type=4 AND status=1 ORDER BY loc_code LIMIT 1").fetchone()
    return row["id"] if row else None


def restock_inventory(conn, goods_id, loc_id, batch_no, qty, biz_id, biz_no, user, detail_id=0):
    """退货回架：库存 + 流水（biz_type=22），与发货的扣减完全对称。

    detail_id 必须区分同一张售后单的不同回架行（良品行 / 残次行），
    否则会撞 wms_inventory_txn 的 (biz_type,biz_id,detail_id) 唯一索引——这是流水防重的保险丝。
    """
    qty = DEC2(qty)
    if qty <= 0:
        return 0.0
    inv = conn.execute("SELECT * FROM wms_inventory WHERE goods_id=? AND loc_id=? AND batch_no=?",
                       (goods_id, loc_id, batch_no)).fetchone()
    if inv:
        before = DEC2(inv["quantity"])
        q_after = DEC2(before + qty)
        cur = conn.execute(
            "UPDATE wms_inventory SET quantity=?, available_qty=?, version=version+1, update_time=?"
            " WHERE id=? AND version=? AND locked_qty<=?",
            (q_after, DEC2(q_after - inv["locked_qty"]), now(), inv["id"], inv["version"], q_after))
        if cur.rowcount == 0:
            raise ApiError(50202, "库存并发冲突，请重试", 409)
    else:
        before = 0.0
        conn.execute("INSERT INTO wms_inventory(goods_id,loc_id,batch_no,quantity,locked_qty,available_qty,"
                     "version,in_time) VALUES(?,?,?,?,0,?,0,?)",
                     (goods_id, loc_id, batch_no, qty, qty, now()))
    conn.execute(
        "INSERT INTO wms_inventory_txn(txn_no,biz_type,biz_id,biz_no,detail_id,goods_id,loc_id,batch_no,"
        "change_qty,before_qty,after_qty,create_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (next_txn_no(conn), BIZ_EC_RETURN, biz_id, biz_no, detail_id, goods_id, loc_id, batch_no,
         qty, before, DEC2(before + qty), user["id"]))
    post_txn_cost(conn, BIZ_EC_RETURN, biz_id, detail_id, goods_id, qty, before, DEC2(before + qty))
    return qty


def create_replacement_order(conn, parent, goods_id, qty, kind, user):
    """换货/补发：不走商城上下架校验，直接生成一张「已支付」订单并分配库存。"""
    goods = conn.execute("SELECT * FROM base_goods WHERE id=?", (goods_id,)).fetchone()
    if not goods:
        raise ApiError(40000, "商品不存在")
    order_no = next_no(conn, "ec_order", "order_no", "DS")
    cur = conn.execute(
        "INSERT INTO ec_order(order_no,shop_id,buyer_name,buyer_phone,receiver_name,receiver_phone,"
        "receiver_address,status,goods_amount,freight,pay_amount,pay_time,order_kind,parent_order_id,create_by)"
        " VALUES(?,?,?,?,?,?,?,1,0,0,0,?,?,?,?)",
        (order_no, parent["shop_id"], parent["buyer_name"], parent["buyer_phone"], parent["receiver_name"],
         parent["receiver_phone"], parent["receiver_address"], now(), kind, parent["id"], user["id"]))
    oid = cur.lastrowid
    conn.execute("INSERT INTO ec_order_item(order_id,goods_id,goods_code,goods_name,price,qty,amount)"
                 " VALUES(?,?,?,?,0,?,0)", (oid, goods_id, goods["goods_code"], goods["goods_name"], DEC2(qty)))
    item = conn.execute("SELECT * FROM ec_order_item WHERE order_id=? ORDER BY id LIMIT 1", (oid,)).fetchone()
    allocate_for_item(conn, item, oid)
    split_into_fulfillments(conn, oid)      # 换货/补发单同样是「已支付」，必须按仓拆出履约单
    write_log(conn, user, "售后服务", "CREATE", oid, order_no,
              {"orderKind": kind, "parentOrder": parent["order_no"]})
    return order_no


def create_after_sale(conn, body, user):
    """申请售后：退货退款 / 仅退款 / 换货 / 补发，四类的共同约束是「不能退超」。"""
    try:
        order_id = int(body.get("order_id"))
        qty = positive_qty(body.get("qty"), "售后数量")
    except (TypeError, ValueError):
        raise ApiError(40000, "订单或数量无效")
    as_type = int(body.get("type") or 1)
    if as_type not in AFTER_SALE_TYPE:
        raise ApiError(40000, "售后类型无效")
    order = conn.execute("SELECT * FROM ec_order WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ApiError(40400, "订单不存在", 404)
    if order["status"] not in (3, 4, 6):
        raise ApiError(40001, "只有已发货及以后的订单可以申请售后（当前：%s）"
                       % EC_STATUS.get(order["status"]), 409)
    try:
        goods_id = int(body.get("goods_id"))
    except (TypeError, ValueError):
        raise ApiError(40000, "请选择售后商品")
    item = conn.execute("SELECT * FROM ec_order_item WHERE order_id=? AND goods_id=?",
                        (order_id, goods_id)).fetchone()
    if not item:
        raise ApiError(40000, "该商品不在这张订单里")
    shipped = DEC2(conn.execute(
        "SELECT IFNULL(SUM(alloc_qty),0) FROM ec_order_alloc WHERE order_id=? AND goods_id=? AND status=3",
        (order_id, goods_id)).fetchone()[0])
    applying = DEC2(conn.execute(
        "SELECT IFNULL(SUM(qty),0) FROM ec_after_sale WHERE order_id=? AND goods_id=? AND status<>3",
        (order_id, goods_id)).fetchone()[0])
    if qty > DEC2(shipped - applying):
        raise ApiError(50201, "可售后数量不足：已发 %s，已申请 %s，本次最多 %s"
                       % (shipped, applying, DEC2(shipped - applying)), 409)
    alloc = conn.execute("SELECT * FROM ec_order_alloc WHERE order_id=? AND goods_id=? AND status=3"
                         " ORDER BY id LIMIT 1", (order_id, goods_id)).fetchone()
    after_no = next_no(conn, "ec_after_sale", "after_no", "SH")
    cur = conn.execute(
        "INSERT INTO ec_after_sale(after_no,order_id,item_id,goods_id,qty,type,status,reason,loc_id,"
        "batch_no,create_by) VALUES(?,?,?,?,?,?,0,?,?,?,?)",
        (after_no, order_id, item["id"], goods_id, qty, as_type, body.get("reason"),
         alloc["loc_id"] if alloc else None, alloc["batch_no"] if alloc else "", user["id"]))
    conn.execute("UPDATE ec_order SET status=6 WHERE id=?", (order_id,))
    write_log(conn, user, "售后服务", "CREATE", cur.lastrowid, after_no,
              {"orderNo": order["order_no"], "qty": qty, "type": as_type})
    return {"id": cur.lastrowid, "afterNo": after_no, "status": 0,
            "typeText": AFTER_SALE_TYPE.get(as_type)}


def approve_after_sale(conn, after_id, user, approved=True):
    """审核售后：
       拒绝 → 直接关闭；仅退款 → 直接完成；换货/补发 → 自动生成出库用的「换货单/补发单」；
       退货退款/换货 → 进入「同意待收货」，等退回后质检入库。
    """
    row = conn.execute("SELECT * FROM ec_after_sale WHERE id=?", (after_id,)).fetchone()
    if not row:
        raise ApiError(40400, "售后单不存在", 404)
    if row["status"] != 0:
        raise ApiError(40001, "只有待审核的售后单可以审核", 409)
    as_type = int(row["type"] or 1)
    if not approved:
        conn.execute("UPDATE ec_after_sale SET status=3, finish_time=? WHERE id=?", (now(), after_id))
        write_log(conn, user, "售后服务", "REJECT", after_id, row["after_no"])
        _close_after_sale_if_done(conn, row["order_id"])
        return {"id": after_id, "status": 3}
    order = conn.execute("SELECT * FROM ec_order WHERE id=?", (row["order_id"],)).fetchone()
    ref_no, status, finish = None, 1, None
    if as_type == 2:                                   # 仅退款：不回收货，直接完成
        status, finish = 2, now()
    elif as_type in (3, 4):                            # 换货 / 补发：自动生成替换单
        ref_no = create_replacement_order(conn, order, row["goods_id"], DEC2(row["qty"]),
                                          2 if as_type == 3 else 3, user)
        status = 1 if as_type == 3 else 2               # 换货要等原件退回质检
        finish = now() if as_type == 4 else None
    conn.execute("UPDATE ec_after_sale SET status=?, ref_order_no=?, finish_time=? WHERE id=?",
                 (status, ref_no, finish, after_id))
    write_log(conn, user, "售后服务", "AUDIT", after_id, row["after_no"],
              {"type": as_type, "refOrderNo": ref_no, "status": status})
    if status == 2:
        _close_after_sale_if_done(conn, row["order_id"])
    return {"id": after_id, "status": status, "refOrderNo": ref_no}


def _close_after_sale_if_done(conn, order_id):
    open_count = conn.execute("SELECT COUNT(*) FROM ec_after_sale WHERE order_id=? AND status IN (0,1)",
                              (order_id,)).fetchone()[0]
    if open_count == 0:
        order = conn.execute("SELECT * FROM ec_order WHERE id=?", (order_id,)).fetchone()
        if order and order["status"] == 6:
            back = 4 if order["finish_time"] else 3
            conn.execute("UPDATE ec_order SET status=? WHERE id=?", (back, order_id))


def receive_after_sale(conn, after_id, good_qty, bad_qty, user):
    """退货收货 + 质检分拣：良品回原库位（可再售），残次进次品处理区（不可售）。
       这一步是电商和 ERP 最大的差别之一：退货不是「一律加库存」，而是要质检定性。
    """
    row = conn.execute("SELECT * FROM ec_after_sale WHERE id=?", (after_id,)).fetchone()
    if not row:
        raise ApiError(40400, "售后单不存在", 404)
    if row["status"] == 2:
        return {"id": after_id, "status": 2, "duplicated": True}
    if row["status"] != 1:
        raise ApiError(40001, "只有已同意待收货的售后单可以确认收货", 409)
    qty = DEC2(row["qty"])
    good = DEC2(qty if good_qty in (None, "") else good_qty)
    bad = DEC2(0 if bad_qty in (None, "") else bad_qty)
    if good < 0 or bad < 0:
        raise ApiError(40000, "良品数与残次数不能为负")
    if DEC2(good + bad) != qty:
        raise ApiError(40000, "良品数 + 残次数 必须等于退货数量 %s（当前 %s + %s）" % (qty, good, bad))
    loc_id = row["loc_id"]
    if not loc_id or not conn.execute("SELECT 1 FROM base_location WHERE id=? AND loc_type=4 AND status=1",
                                      (loc_id,)).fetchone():
        loc_id = conn.execute("SELECT id FROM base_location WHERE loc_type=4 AND status=1"
                              " ORDER BY loc_code LIMIT 1").fetchone()["id"]
    batch_no = row["batch_no"] or ""
    if good > 0:
        restock_inventory(conn, row["goods_id"], loc_id, batch_no, good, after_id, "退货入库-良品", user,
                          detail_id=1)
    if bad > 0:
        restock_inventory(conn, row["goods_id"], defect_location(conn), batch_no, bad, after_id,
                          "退货入库-残次", user, detail_id=2)
    conn.execute("UPDATE ec_after_sale SET status=2, good_qty=?, bad_qty=?, check_time=?, check_by=?,"
                 " finish_time=? WHERE id=?", (good, bad, now(), user["id"], now(), after_id))
    _close_after_sale_if_done(conn, row["order_id"])
    write_log(conn, user, "售后服务", "CONFIRM", after_id, row["after_no"],
              {"goodQty": good, "badQty": bad})
    return {"id": after_id, "status": 2, "goodQty": good, "badQty": bad}


def replenish_suggestions(conn, only_below=True):
    """补货建议：可用量低于安全库存即建议补货（补货量取政策值，未设则补到安全库存）。"""
    rows = conn.execute(
        "SELECT g.id AS goods_id, g.goods_code, g.goods_name, g.unit, g.cost_price,"
        " IFNULL((SELECT SUM(i.quantity) FROM wms_inventory i WHERE i.goods_id=g.id),0) AS total_qty,"
        " IFNULL((SELECT SUM(i.available_qty) FROM wms_inventory i WHERE i.goods_id=g.id),0) AS avail_qty,"
        " IFNULL((SELECT SUM(i.locked_qty) FROM wms_inventory i WHERE i.goods_id=g.id),0) AS locked_qty,"
        " IFNULL(p.safety_stock,0) AS safety_stock, IFNULL(p.replenish_qty,0) AS replenish_qty,"
        " IFNULL(p.lead_days,7) AS lead_days"
        " FROM base_goods g LEFT JOIN wms_stock_policy p ON p.goods_id=g.id AND p.wh_id=0"
        " WHERE g.status=1 ORDER BY g.goods_code").fetchall()
    out = []
    for r in rows:
        item = dict(r)
        gap = DEC2(item["safety_stock"] - item["avail_qty"])
        item["gap_qty"] = gap
        item["suggest_qty"] = DEC2(item["replenish_qty"] or max(0.0, gap))
        item["urgent"] = bool(gap > 0 and item["avail_qty"] <= 0)
        item["status"] = "缺货" if item["avail_qty"] <= 0 else ("低于安全库存" if gap > 0 else "正常")
        if only_below and gap <= 0:
            continue
        out.append(item)
    return out


# --------------------------------------------------------------------------- #
# 运维域：健康检查 / 指标 / 一致性校验 / 备份 / 参数
# --------------------------------------------------------------------------- #
def consistency_checks(conn):
    """六项硬校验：任何一项不通过都说明账实/账账不一致，必须人工介入（只读，不修数据）。"""
    checks = []

    def add(name, sql, hint, args=()):
        bad = conn.execute(sql, args).fetchone()[0]
        checks.append({"name": name, "passed": bad == 0, "badRows": bad, "hint": hint})

    add("可用量恒等式",
        "SELECT COUNT(*) FROM wms_inventory WHERE ABS(available_qty-(quantity-locked_qty))>0.0005",
        "available_qty 必须等于 quantity-locked_qty")
    add("库存非负",
        "SELECT COUNT(*) FROM wms_inventory WHERE quantity<-0.0005 OR locked_qty<-0.0005 OR available_qty<-0.0005",
        "任何数量字段出现负数都说明过账逻辑或并发控制有漏洞")
    add("流水可累加 = 库存",
        "SELECT COUNT(*) FROM (SELECT goods_id,loc_id,batch_no,SUM(change_qty) f FROM wms_inventory_txn"
        " GROUP BY goods_id,loc_id,batch_no) t"
        " JOIN wms_inventory i ON i.goods_id=t.goods_id AND i.loc_id=t.loc_id AND i.batch_no=t.batch_no"
        " WHERE ABS(t.f-i.quantity)>0.0005",
        "每一笔库存变动都必须有流水，流水之和必须等于库存实物")
    add("已过账入库单明细数 = 流水数",
        "SELECT COUNT(*) FROM (SELECT o.id, COUNT(d.id) dc,"
        " (SELECT COUNT(*) FROM wms_inventory_txn t WHERE t.biz_type=1 AND t.biz_id=o.id) tc"
        " FROM wms_in_order o JOIN wms_in_order_detail d ON d.order_id=o.id"
        " WHERE o.status=3 GROUP BY o.id) x WHERE x.dc<>x.tc",
        "过账必须逐行写流水，缺一行就是少记账")
    add("预占总额可解释",
        "SELECT ABS((SELECT IFNULL(SUM(locked_qty),0) FROM wms_inventory)"
        " - ((SELECT IFNULL(SUM(d.plan_num),0) FROM wms_out_order o JOIN wms_out_order_detail d ON d.order_id=o.id"
        "     WHERE o.status IN (1,2))"
        "  + (SELECT IFNULL(SUM(alloc_qty),0) FROM ec_order_alloc WHERE status IN (1,2))"
        "  + (SELECT IFNULL(SUM(td.qty),0) FROM wms_transfer_order t"
        "     JOIN wms_transfer_detail td ON td.order_id=t.id WHERE t.status=1))) > 0.0005",
        "locked_qty 必须能由「未过账出库单 + 未发货电商分配 + 已审核待调拨」完全解释"
        "（防止预占泄漏/重复释放）")
    add("电商已发货订单有流水",
        "SELECT COUNT(*) FROM (SELECT o.id,"
        " (SELECT COUNT(*) FROM ec_order_alloc a WHERE a.order_id=o.id AND a.status=3) ac,"
        " (SELECT COUNT(*) FROM wms_inventory_txn t WHERE t.biz_type=21 AND t.biz_id=o.id) tc"
        " FROM ec_order o WHERE o.status IN (3,4,6)) x WHERE x.ac<>x.tc",
        "每一条发货分配都必须对应一条出库流水")
    add("已发货必有发货单",
        "SELECT COUNT(*) FROM ec_order o WHERE o.status IN (3,4,6)"
        " AND NOT EXISTS (SELECT 1 FROM ec_shipment s WHERE s.order_id=o.id)",
        "已发货/已完成订单必须有发货单，否则物流不可追溯（防发空单）")
    add("发货单数量为正",
        "SELECT COUNT(*) FROM ec_shipment WHERE IFNULL(item_qty,0)<=0",
        "发货单必须是正数量（item_qty<=0 视为空单过账）")
    add("订单发货数量等于已发分配",
        "SELECT COUNT(*) FROM ec_order o WHERE ABS(IFNULL(o.ship_qty,0) - IFNULL("
        "(SELECT SUM(a.alloc_qty) FROM ec_order_alloc a WHERE a.order_id=o.id AND a.status=3),0))>0.0005",
        "订单 ship_qty 必须等于已发货分配之和（防多发/少发）")
    add("拆单数量与履约单一致",
        "SELECT COUNT(*) FROM ec_order o WHERE o.status IN (1,2,3,4,7,8)"
        " AND IFNULL(o.split_count,0) <> (SELECT COUNT(*) FROM ec_fulfillment f WHERE f.order_id=o.id)",
        "订单 split_count 必须等于履约单数量（拆单口径漂移说明有漏建/漏删）")
    add("已付款订单必须有分配",
        "SELECT COUNT(*) FROM ec_order o WHERE o.status IN (1,2,7,8) AND NOT EXISTS ("
        " SELECT 1 FROM ec_order_alloc a WHERE a.order_id=o.id AND a.status IN (1,2,3))",
        "已支付订单必须持有库存分配（防「买了没发货」：支付成功却没有任何预占）")
    add("有效分配必须挂履约单",
        "SELECT COUNT(*) FROM ec_order_alloc a JOIN ec_order o ON o.id=a.order_id"
        " WHERE a.status IN (1,2,3) AND IFNULL(a.fulfillment_id,0)=0 AND o.status<>5",
        "每条有效分配都要归属一个履约单（拆单维度），否则无法按仓发货")
    return checks


def db_size_bytes():
    try:
        return os.path.getsize(DB_PATH)
    except OSError:
        return 0


# --------------------------------------------------------------------------- #
# HTTP 路由
# --------------------------------------------------------------------------- #
ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, pattern, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


def current_user(handler):
    """每个请求只查一次库：认证 + 角色 + 权限码 + 可见仓库，缓存在 handler 上。"""
    cached = getattr(handler, "auth_user", None)
    if cached is not None:
        return cached
    auth = handler.headers.get("Authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    user = parse_token(token) if token else None
    if not user:
        raise ApiError(40100, "未登录或登录已过期", 401)
    conn = db()
    try:
        row = conn.execute("SELECT id, username, real_name FROM sys_user WHERE id=? AND username=? AND status=1",
                           (user["id"], user["username"])).fetchone()
        if not row:
            raise ApiError(40103, "用户不存在或已停用", 401)
        extra = user_auth(conn, row["id"])
        result = {"id": row["id"], "username": row["username"], "realName": row["real_name"],
                  "roles": extra["roles"], "perms": extra["perms"], "whIds": extra["whIds"]}
        handler.auth_user = result
        return result
    finally:
        conn.close()


def paging(q):
    page = max(1, int(q.get("page", ["1"])[0]))
    size = min(200, max(1, int(q.get("size", ["20"])[0])))
    return page, size, (page - 1) * size


# --------------------------------------------------------------------------- #
# 权限（RBAC）：菜单/按钮权限码 + 仓库数据权限
#
# 分工：
#   · 接口层（dispatch）统一拦权限码 —— 业务函数里不写 if 判断，避免漏拦；
#   · 数据权限用 scope_wh_ids(user) + add_wh_scope() 拼到列表 SQL 上；
#   · admin 角色用通配符 "*"，不参与逐条配置。
# --------------------------------------------------------------------------- #
PERM_CATALOG = [
    # (perm_code, perm_name, module, perm_type)  type: 1菜单 2按钮/操作
    ("dashboard:view",     "经营看板",         "总览", 1),
    ("shop:view",          "商城前台",         "用户层", 1),
    ("shop:buy",           "商城下单/支付",    "用户层", 2),
    ("myorder:view",       "我的订单",         "用户层", 1),
    ("ec:order:view",      "订单池",           "电商履约", 1),
    ("ec:order:edit",      "订单处理(打包/取消)", "电商履约", 2),
    ("ec:order:ship",      "发货过账",         "电商履约", 2),
    ("ec:wave:view",       "波次拣货",         "电商履约", 1),
    ("ec:wave:create",     "生成/取消波次",    "电商履约", 2),
    ("ec:wave:pick",       "拣货确认",         "电商履约", 2),
    ("ec:wave:review",     "复核",             "电商履约", 2),
    ("ec:aftersale:view",  "售后工作台",       "售后", 1),
    ("ec:aftersale:audit", "售后审核/代申请",  "售后", 2),
    ("ec:aftersale:receive", "退货质检收货",   "售后", 2),
    ("goods:view",         "商品 SPU/SKU",     "商品", 1),
    ("goods:edit",         "商品维护",         "商品", 2),
    ("listing:view",       "商品上下架",       "商品", 1),
    ("listing:edit",       "上下架/改价",      "商品", 2),
    ("replenish:view",     "补货建议",         "商品", 1),
    ("replenish:edit",     "补货政策/生成采购", "商品", 2),
    ("in:view",            "入库管理",         "仓储", 1),
    ("in:edit",            "入库建单/审核/作废", "仓储", 2),
    ("in:confirm",         "入库过账",         "仓储", 2),
    ("out:view",           "出库管理",         "仓储", 1),
    ("out:edit",           "出库建单/审核/作废", "仓储", 2),
    ("out:confirm",        "出库过账",         "仓储", 2),
    ("inventory:view",     "库存查询",         "库存报表", 1),
    ("txn:view",           "库存流水",         "库存报表", 1),
    ("ledger:view",        "库存台账",         "库存报表", 1),
    ("location:view",      "仓库库位",         "基础资料", 1),
    ("ops:view",           "系统运维",         "运维", 1),
    ("ops:manage",         "备份/参数",        "运维", 2),
    ("log:view",           "操作日志",         "运维", 1),
    ("sys:view",           "系统管理",         "系统", 1),
    ("sys:manage",         "用户/角色维护",    "系统", 2),
    ("stocktake:view",     "库存盘点",         "库存作业", 1),
    ("stocktake:edit",     "盘点建单/录入",    "库存作业", 2),
    ("stocktake:audit",    "盘点审核过账",     "库存作业", 2),
    ("transfer:view",      "库存调拨",         "库存作业", 1),
    ("transfer:edit",      "调拨建单/审核",    "库存作业", 2),
    ("transfer:confirm",   "调拨确认过账",     "库存作业", 2),
    ("report:view",        "报表中心",         "报表", 1),
]
PERM_NAME = {code: name for code, name, _, _ in PERM_CATALOG}

ROLE_DEFS = [
    # (role_code, role_name, data_scope, remark, perms)
    ("admin", "系统管理员", 1, "全部权限（含系统管理与运维）", ["*"]),
    ("ec_operator", "电商运营", 1, "订单池/上下架/补货/售后审核；不做仓储作业", [
        "dashboard:view", "shop:view", "ec:order:view", "ec:order:edit", "ec:aftersale:view",
        "ec:aftersale:audit", "goods:view", "listing:view", "listing:edit",
        "replenish:view", "replenish:edit", "inventory:view", "txn:view", "ledger:view",
        "report:view"]),
    ("warehouse_manager", "仓库主管", 3, "本仓库全部仓储作业 + 电商履约 + 报表", [
        "dashboard:view", "ec:order:view", "ec:order:edit", "ec:order:ship", "ec:wave:view",
        "ec:wave:create", "ec:wave:pick", "ec:wave:review", "goods:view",
        "in:view", "in:edit", "in:confirm", "out:view", "out:edit", "out:confirm",
        "inventory:view", "txn:view", "ledger:view", "location:view", "replenish:view",
        "stocktake:view", "stocktake:edit", "stocktake:audit",
        "transfer:view", "transfer:edit", "transfer:confirm", "report:view",
        "ops:view", "log:view"]),
    ("warehouse_keeper", "仓管员", 3, "收发货录入与确认、拣货复核；无取消/系统管理权", [
        "dashboard:view", "in:view", "in:edit", "in:confirm", "out:view", "out:edit", "out:confirm",
        "inventory:view", "txn:view", "ledger:view", "location:view",
        "stocktake:view", "stocktake:edit", "transfer:view", "transfer:edit", "transfer:confirm",
        "report:view", "ec:order:view", "ec:wave:view", "ec:wave:pick", "ec:wave:review"]),
    ("picker", "拣货员", 3, "波次拣货与打包；不能审核、不能取消", [
        "dashboard:view", "ec:order:view", "ec:wave:view", "ec:wave:pick",
        "inventory:view", "location:view"]),
    ("packer", "打包员", 3, "复核后打包称重与发货交接", [
        "dashboard:view", "ec:order:view", "ec:order:edit", "ec:order:ship",
        "ec:wave:view", "inventory:view"]),
    ("stocktaker", "盘点员", 3, "盘点录入与库存查询，无审核权", [
        "dashboard:view", "inventory:view", "txn:view", "ledger:view", "location:view",
        "stocktake:view", "stocktake:edit", "transfer:view"]),
    ("viewer", "只读访客", 1, "全部只读（可看全仓，不能改任何数据；不含系统管理）", [
        code for code, _, _, ptype in PERM_CATALOG if ptype == 1 and code != "sys:view"]),
]
# 演示账号（密码统一 123456；正好用来验证「同一套接口、不同人看到的数据不同」）
DEMO_USERS = [
    ("admin", "系统管理员", "admin", []),
    ("ops01", "电商运营-小王", "ec_operator", []),
    ("keeper01", "仓管员-老李", "warehouse_keeper", ["WH01"]),
    ("keeper02", "仓管员-二号仓", "warehouse_keeper", ["WH02"]),
    ("picker01", "拣货员-小赵", "picker", ["WH01"]),
    ("viewer01", "只读-审计", "viewer", []),
]


def user_auth(conn, user_id):
    """一次查出用户的角色、权限码集合、可见仓库；返回给 current_user / 登录响应用。"""
    roles = conn.execute(
        "SELECT r.id, r.role_code, r.role_name, r.data_scope FROM sys_user_role ur"
        " JOIN sys_role r ON r.id=ur.role_id WHERE ur.user_id=? AND r.status=1 ORDER BY r.id",
        (user_id,)).fetchall()
    perms, scopes = set(), []
    for r in roles:
        scopes.append(r["data_scope"])
        if r["role_code"] == "admin":
            perms.add("*")
        for row in conn.execute(
                "SELECT p.perm_code FROM sys_role_permission rp JOIN sys_permission p ON p.id=rp.permission_id"
                " WHERE rp.role_id=?", (r["id"],)):
            perms.add(row["perm_code"])
    wh_ids = [row["wh_id"] for row in conn.execute(
        "SELECT wh_id FROM sys_user_warehouse WHERE user_id=? ORDER BY wh_id", (user_id,))]
    if 1 in scopes:                       # 任一角色是「全部数据」
        scope_wh = None
    elif not scopes:                      # 没有启用角色 → 什么都不给
        scope_wh = []
    else:
        scope_wh = wh_ids
    return {"roles": [{"code": r["role_code"], "name": r["role_name"], "dataScope": r["data_scope"]}
                      for r in roles],
            "perms": sorted(perms), "whIds": scope_wh}


def has_perm(user, code):
    if not user:
        return False
    perms = user.get("perms") or []
    return "*" in perms or code in perms


def require_perm(user, code):
    if not has_perm(user, code):
        raise ApiError(40300, "无权限：需要「%s」" % PERM_NAME.get(code, code), 403)


def add_wh_scope(where, args, user, column):
    """把仓库数据权限拼进 WHERE；column 形如 'o.wh_id' 或 'l.wh_id'。"""
    ids = user.get("whIds") if user else None
    if ids is None:
        return where, args
    if not ids:
        return where + " AND 1=0", args
    return where + " AND %s IN (%s)" % (column, ",".join("?" * len(ids))), args + list(ids)


def check_wh_scope(user, wh_id):
    """写操作时校验目标仓库是否在数据权限内（列表过滤只防"看见"，写操作还要防"改到"）。"""
    ids = user.get("whIds") if user else None
    if ids is None:
        return
    if int(wh_id) not in ids:
        raise ApiError(40300, "无权限：该仓库不在你的数据权限范围内", 403)


def ensure_rbac_baseline(conn):
    """幂等补齐权限目录/角色/演示账号（老库启动也能直接启用 RBAC，不用清库）。"""
    for sort_no, (code, name, module, ptype) in enumerate(PERM_CATALOG, start=1):
        conn.execute("INSERT OR IGNORE INTO sys_permission(perm_code,perm_name,perm_type,module,sort_no)"
                     " VALUES(?,?,?,?,?)", (code, name, ptype, module, sort_no))
    perm_ids = {r["perm_code"]: r["id"] for r in conn.execute("SELECT id,perm_code FROM sys_permission")}
    for role_code, role_name, scope, remark, perms in ROLE_DEFS:
        conn.execute("INSERT OR IGNORE INTO sys_role(role_code,role_name,data_scope,remark) VALUES(?,?,?,?)",
                     (role_code, role_name, scope, remark))
        role = conn.execute("SELECT id FROM sys_role WHERE role_code=?", (role_code,)).fetchone()
        for code in perms:
            if code != "*" and code in perm_ids:
                conn.execute("INSERT OR IGNORE INTO sys_role_permission(role_id,permission_id) VALUES(?,?)",
                             (role["id"], perm_ids[code]))
    # 演示账号：首次创建后不再改密码（避免覆盖用户自己改过的口令）
    for username, real_name, role_code, wh_codes in DEMO_USERS:
        if conn.execute("SELECT 1 FROM sys_user WHERE username=?", (username,)).fetchone():
            continue
        pwd, salt = hash_password("123456")
        cur = conn.execute("INSERT INTO sys_user(username,password_hash,salt,real_name,phone,status)"
                           " VALUES(?,?,?,?,?,1)", (username, pwd, salt, real_name, None))
        uid = cur.lastrowid
        role = conn.execute("SELECT id FROM sys_role WHERE role_code=?", (role_code,)).fetchone()
        if role:
            conn.execute("INSERT OR IGNORE INTO sys_user_role(user_id,role_id) VALUES(?,?)", (uid, role["id"]))
        for code in wh_codes:
            wh = conn.execute("SELECT id FROM base_location WHERE loc_code=? AND loc_type=1", (code,)).fetchone()
            if wh:
                conn.execute("INSERT OR IGNORE INTO sys_user_warehouse(user_id,wh_id) VALUES(?,?)",
                             (uid, wh["id"]))
    # 老库里的 admin 若还没绑角色，补上 admin 角色
    admin = conn.execute("SELECT id FROM sys_user WHERE username='admin'").fetchone()
    admin_role = conn.execute("SELECT id FROM sys_role WHERE role_code='admin'").fetchone()
    if admin and admin_role:
        conn.execute("INSERT OR IGNORE INTO sys_user_role(user_id,role_id) VALUES(?,?)",
                     (admin["id"], admin_role["id"]))


# 接口 → 权限码映射（接口层统一拦；没列出来的接口只要求登录）
ROUTE_PERM = {
    "GET /api/v1/stats/summary": "dashboard:view",
    "GET /api/v1/goods": "goods:view", "POST /api/v1/goods": "goods:edit",
    "PUT /api/v1/goods/(\\d+)": "goods:edit",
    "GET /api/v1/categories": "goods:view", "GET /api/v1/partners": "goods:view",
    "GET /api/v1/locations": "location:view",
    "GET /api/v1/spus": "goods:view",
    "GET /api/v1/in-orders": "in:view", "GET /api/v1/in-orders/(\\d+)": "in:view",
    "POST /api/v1/in-orders": "in:edit", "POST /api/v1/in-orders/(\\d+)/audit": "in:edit",
    "POST /api/v1/in-orders/(\\d+)/confirm": "in:confirm",
    "POST /api/v1/in-orders/(\\d+)/cancel": "in:edit",
    "GET /api/v1/out-orders": "out:view", "GET /api/v1/out-orders/(\\d+)": "out:view",
    "POST /api/v1/out-orders": "out:edit", "POST /api/v1/out-orders/(\\d+)/audit": "out:edit",
    "POST /api/v1/out-orders/(\\d+)/confirm": "out:confirm",
    "POST /api/v1/out-orders/(\\d+)/cancel": "out:edit",
    "GET /api/v1/inventory": "inventory:view", "GET /api/v1/inventory/txn": "txn:view",
    "GET /api/v1/reports/ledger": "ledger:view", "GET /api/v1/logs": "log:view",
    "POST /api/v1/admin/reset-demo": "sys:manage",
    "GET /api/v1/shops": "shop:view", "GET /api/v1/shop/goods": "shop:view",
    "GET /api/v1/shop/orders": "myorder:view",
    "POST /api/v1/shop/orders": "shop:buy", "POST /api/v1/shop/orders/(\\d+)/pay": "shop:buy",
    "POST /api/v1/shop/orders/(\\d+)/cancel": "shop:buy",
    "POST /api/v1/shop/orders/(\\d+)/receive": "shop:buy",
    "GET /api/v1/ec/orders": "ec:order:view", "GET /api/v1/ec/orders/(\\d+)": "ec:order:view",
    "POST /api/v1/ec/orders/(\\d+)/pick": "ec:wave:pick",
    "POST /api/v1/ec/orders/(\\d+)/pack": "ec:order:edit",
    "POST /api/v1/ec/orders/(\\d+)/ship": "ec:order:ship",
    "GET /api/v1/ec/shipments": "ec:order:view",
    "GET /api/v1/listings": "listing:view", "POST /api/v1/listings": "listing:edit",
    "PUT /api/v1/listings/(\\d+)": "listing:edit",
    "GET /api/v1/replenish/suggestions": "replenish:view",
    "PUT /api/v1/replenish/policy": "replenish:edit",
    "POST /api/v1/replenish/generate": "replenish:edit",
    "GET /api/v1/ec/after-sales": "ec:aftersale:view",
    "POST /api/v1/ec/after-sales": "ec:aftersale:audit",
    "POST /api/v1/ec/after-sales/(\\d+)/approve": "ec:aftersale:audit",
    "POST /api/v1/ec/after-sales/(\\d+)/reject": "ec:aftersale:audit",
    "POST /api/v1/ec/after-sales/(\\d+)/receive": "ec:aftersale:receive",
    "GET /api/v1/ec/waves": "ec:wave:view", "GET /api/v1/ec/waves/(\\d+)": "ec:wave:view",
    "POST /api/v1/ec/waves": "ec:wave:create",
    "POST /api/v1/ec/waves/(\\d+)/pick": "ec:wave:pick",
    "POST /api/v1/ec/waves/(\\d+)/review": "ec:wave:review",
    "POST /api/v1/ec/waves/(\\d+)/cancel": "ec:wave:create",
    "GET /api/v1/ops/health": "ops:view", "GET /api/v1/ops/metrics": "ops:view",
    "GET /api/v1/ops/checks": "ops:view", "GET /api/v1/ops/backups": "ops:view",
    "GET /api/v1/ops/config": "ops:view",
    "POST /api/v1/ops/backup": "ops:manage", "PUT /api/v1/ops/config": "ops:manage",
    "GET /api/v1/sys/users": "sys:view", "POST /api/v1/sys/users": "sys:manage",
    "PUT /api/v1/sys/users/(\\d+)": "sys:manage",
    "GET /api/v1/sys/roles": "sys:view", "PUT /api/v1/sys/roles/(\\d+)": "sys:manage",
    "GET /api/v1/sys/perms": "sys:view",
}

ROUTE_PERM.update({
    "GET /api/v1/stocktakes": "stocktake:view", "GET /api/v1/stocktakes/(\\d+)": "stocktake:view",
    "POST /api/v1/stocktakes": "stocktake:edit",
    "POST /api/v1/stocktakes/(\\d+)/save": "stocktake:edit",
    "POST /api/v1/stocktakes/(\\d+)/submit": "stocktake:edit",
    "POST /api/v1/stocktakes/(\\d+)/audit": "stocktake:audit",
    "POST /api/v1/stocktakes/(\\d+)/cancel": "stocktake:edit",
    "GET /api/v1/transfers": "transfer:view", "GET /api/v1/transfers/(\\d+)": "transfer:view",
    "POST /api/v1/transfers": "transfer:edit",
    "POST /api/v1/transfers/(\\d+)/audit": "transfer:edit",
    "POST /api/v1/transfers/(\\d+)/confirm": "transfer:confirm",
    "POST /api/v1/transfers/(\\d+)/cancel": "transfer:edit",
    "POST /api/v1/ops/jobs/run": "ops:manage", "GET /api/v1/ops/jobs": "ops:view",
    "GET /api/v1/reports/stock-summary": "report:view",
    "GET /api/v1/reports/profit": "report:view",
    "GET /api/v1/reports/turnover": "report:view",
    "GET /api/v1/reports/expiring": "report:view",
    "GET /api/v1/reports/trend": "report:view",
    "GET /api/v1/reports/shop-sales": "report:view",
    "GET /api/v1/reports/warehouse-stock": "report:view",
    "GET /api/v1/reports/advice": "report:view",
    "POST /api/v1/spus": "goods:edit", "PUT /api/v1/spus/(\\d+)": "goods:edit",
    "POST /api/v1/catalog/onboard": "goods:edit",
    "GET /api/v1/goods/(\\d+)/delete-check": "goods:view",
    "POST /api/v1/goods/(\\d+)/archive": "goods:edit",
    "POST /api/v1/goods/(\\d+)/restore": "goods:edit",
    "GET /api/v1/ops/queue": "ops:view",
    "GET /api/v1/ops/migrations": "ops:view",
    "GET /api/v1/ops/runtime": "ops:view",
})


# ---------------- 认证 ----------------
@route("POST", r"/api/v1/auth/login")
def api_login(handler, m, q, body):
    username, password = (body.get("username") or "").strip(), body.get("password") or ""
    conn = db()
    try:
        limit, lock_min = get_config_int(conn, "login.fail.lock.count", 5), \
            get_config_int(conn, "login.fail.lock.minutes", 15)
        fails = conn.execute(
            "SELECT COUNT(*) FROM sys_login_log l WHERE l.username=? AND l.result='FAIL'"
            " AND datetime(l.login_time) >= datetime('now', ?)"
            " AND datetime(l.login_time) > IFNULL((SELECT MAX(login_time) FROM sys_login_log"
            "      WHERE username=l.username AND result='SUCCESS'), '1970-01-01')",
            (username, "-%d minute" % lock_min)).fetchone()[0]
        if fails >= limit:
            raise ApiError(40102, "密码连续错误 %d 次，账号已临时锁定 %d 分钟" % (fails, lock_min), 423)
        row = conn.execute("SELECT * FROM sys_user WHERE username=? AND status=1", (username,)).fetchone()
        if not row or hash_password(password, row["salt"])[0] != row["password_hash"]:
            conn.execute("INSERT INTO sys_login_log(username,ip,result,fail_reason,user_agent)"
                         " VALUES(?,?,'FAIL','用户名或密码错误',?)",
                         (username, handler.client_address[0] if handler.client_address else "",
                          handler.headers.get("User-Agent", "")[:200]))
            conn.commit()
            raise ApiError(40101, "用户名或密码错误", 401)
        token = make_token(row["id"], row["username"])
        extra = user_auth(conn, row["id"])
        conn.execute("INSERT INTO sys_login_log(user_id,username,ip,result,user_agent)"
                     " VALUES(?,?,?,'SUCCESS',?)",
                     (row["id"], row["username"],
                      handler.client_address[0] if handler.client_address else "",
                      handler.headers.get("User-Agent", "")[:200]))
        write_log(conn, {"id": row["id"], "username": row["username"]}, "认证", "LOGIN")
        conn.commit()
        return {"accessToken": token, "expiresIn": TOKEN_EXPIRE_SECONDS,
                "user": {"id": row["id"], "username": row["username"], "realName": row["real_name"],
                         "roles": extra["roles"], "perms": extra["perms"], "whIds": extra["whIds"]}}
    finally:
        conn.close()


@route("GET", r"/api/v1/auth/me")
def api_me(handler, m, q, body):
    return {"user": current_user(handler)}


@route("GET", r"/api/v1/health")
def api_health(handler, m, q, body):
    """不需要登录的存活探针（给负载均衡/监控用）；详细自检在 /api/v1/ops/health。"""
    return {"service": "wms-demo", "status": "ok", "version": VERSION, "env": WMS_ENV}


# ---------------- 仪表盘 ----------------
@route("GET", r"/api/v1/stats/summary")
def api_stats(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        one = lambda sql, args=(): conn.execute(sql, args).fetchone()[0]
        return {
            "goodsCount": one("SELECT COUNT(*) FROM base_goods WHERE status=1"),
            "locCount": one("SELECT COUNT(*) FROM base_location WHERE loc_type=4"),
            "stockQty": DEC2(one("SELECT SUM(quantity) FROM wms_inventory")),
            "stockLines": one("SELECT COUNT(*) FROM wms_inventory WHERE quantity>0"),
            "todayIn": one("SELECT COUNT(*) FROM wms_in_order WHERE substr(create_time,1,10)=?", (today,)),
            "todayOut": one("SELECT COUNT(*) FROM wms_out_order WHERE substr(create_time,1,10)=?", (today,)),
            "pendingIn": one("SELECT COUNT(*) FROM wms_in_order WHERE status IN (0,1,2)"),
            "pendingOut": one("SELECT COUNT(*) FROM wms_out_order WHERE status IN (0,1,2)"),
            # ---- 电商视角（用户层真正关心的四个数）----
            "todayOrders": one("SELECT COUNT(*) FROM ec_order WHERE substr(create_time,1,10)=?", (today,)),
            "todaySales": DEC2(one("SELECT IFNULL(SUM(pay_amount),0) FROM ec_order"
                                   " WHERE substr(create_time,1,10)=? AND status IN (1,2,3,4,6)", (today,))),
            "pendingShip": one("SELECT COUNT(*) FROM ec_order WHERE status IN (1,2)"),
            "afterSaleOpen": one("SELECT COUNT(*) FROM ec_after_sale WHERE status IN (0,1)"),
            "listedCount": one("SELECT COUNT(*) FROM ec_listing WHERE sale_status=1"),
            "lowStockSku": one("SELECT COUNT(*) FROM base_goods g JOIN wms_stock_policy p ON p.goods_id=g.id"
                               " AND p.wh_id=0 WHERE p.safety_stock>0 AND IFNULL((SELECT SUM(available_qty)"
                               " FROM wms_inventory WHERE goods_id=g.id),0) < p.safety_stock"),
            "lockedQty": DEC2(one("SELECT IFNULL(SUM(locked_qty),0) FROM wms_inventory")),
        }
    finally:
        conn.close()


# ---------------- 基础资料 ----------------
@route("GET", r"/api/v1/goods")
def api_goods_list(handler, m, q, body):
    current_user(handler)
    page, size, offset = paging(q)
    kw = (q.get("keyword", [""])[0] or "").strip()
    conn = db()
    try:
        where, args = "WHERE 1=1", []
        if q.get("includeDeleted", ["0"])[0] != "1":
            where += " AND IFNULL(g.is_deleted,0)=0"
        if kw:
            where += " AND (g.goods_code LIKE ? OR g.goods_name LIKE ?)"
            args += ["%" + kw + "%", "%" + kw + "%"]
        total = conn.execute("SELECT COUNT(*) FROM base_goods g " + where, args).fetchone()[0]
        rows = conn.execute(
            "SELECT g.*, c.category_name, s.spu_name, s.spu_code,"
            " IFNULL((SELECT SUM(quantity) FROM wms_inventory i WHERE i.goods_id=g.id),0) AS stock_qty"
            " FROM base_goods g LEFT JOIN base_goods_category c ON c.id=g.category_id"
            " LEFT JOIN base_spu s ON s.id=g.spu_id " + where +
            " ORDER BY IFNULL(s.spu_code,''), g.goods_code LIMIT ? OFFSET ?", args + [size, offset]).fetchall()
        return {"total": total, "page": page, "size": size, "list": [dict(r) for r in rows]}
    finally:
        conn.close()


@route("POST", r"/api/v1/goods")
def api_goods_create(handler, m, q, body):
    user = current_user(handler)
    for f in ("goods_code", "goods_name", "unit"):
        if not (body.get(f) or "").strip():
            raise ApiError(40000, "字段 %s 必填" % f)
    conn = db()
    try:
        if float(body.get("cost_price") or 0) < 0:
            raise ApiError(40000, "成本价不能为负数")
        if body.get("category_id") and not conn.execute(
                "SELECT 1 FROM base_goods_category WHERE id=? AND status=1", (int(body["category_id"]),)).fetchone():
            raise ApiError(40000, "商品分类不存在或已停用")
        try:
            cur = conn.execute(
                "INSERT INTO base_goods(goods_code,goods_name,category_id,spec,unit,barcode,cost_price,remark,"
                "spu_id,spec_attrs) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (body["goods_code"].strip(), body["goods_name"].strip(), int(body.get("category_id") or 1),
                 body.get("spec"), body["unit"].strip(), body.get("barcode"),
                 float(body.get("cost_price") or 0), body.get("remark"),
                 int(body.get("spu_id") or 0), (body.get("spec_attrs") or "{}")))
        except sqlite3.IntegrityError:
            raise ApiError(30001, "商品编码已存在", 409)
        write_log(conn, user, "基础资料", "CREATE", cur.lastrowid, body["goods_code"], body)
        conn.commit()
        return {"id": cur.lastrowid}
    finally:
        conn.close()


@route("PUT", r"/api/v1/goods/(\d+)")
def api_goods_update(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("UPDATE base_goods SET goods_name=?,spec=?,unit=?,barcode=?,cost_price=?,status=?,remark=?,"
                     " spu_id=IFNULL(?,spu_id), spec_attrs=IFNULL(?,spec_attrs) WHERE id=?",
                     (body.get("goods_name"), body.get("spec"), body.get("unit"), body.get("barcode"),
                      float(body.get("cost_price") or 0), int(body.get("status", 1)), body.get("remark"),
                      body.get("spu_id") if body.get("spu_id") not in (None, "") else None,
                      body.get("spec_attrs") if body.get("spec_attrs") not in (None, "") else None,
                      int(m.group(1))))
        write_log(conn, user, "基础资料", "UPDATE", int(m.group(1)), body.get("goods_code"), body)
        conn.commit()
        return {"id": int(m.group(1))}
    finally:
        conn.close()


@route("GET", r"/api/v1/categories")
def api_categories(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        return {"list": [dict(r) for r in conn.execute("SELECT * FROM base_goods_category ORDER BY category_code")]}
    finally:
        conn.close()


@route("GET", r"/api/v1/locations")
def api_locations(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM base_location ORDER BY loc_code")]
        return {"list": rows, "leafList": [r for r in rows if r["loc_type"] == 4]}
    finally:
        conn.close()


@route("GET", r"/api/v1/partners")
def api_partners(handler, m, q, body):
    current_user(handler)
    ptype = q.get("type", ["supplier"])[0]
    conn = db()
    try:
        return {"list": [dict(r) for r in conn.execute(
            "SELECT * FROM base_partner WHERE partner_type=? AND status=1 ORDER BY partner_code", (ptype,))]}
    finally:
        conn.close()


# ---------------- 入库 ----------------
@route("GET", r"/api/v1/in-orders")
def api_in_list(handler, m, q, body):
    user = current_user(handler)
    page, size, offset = paging(q)
    conn = db()
    try:
        status = q.get("status", [""])[0]
        where, args = "WHERE 1=1", []
        if status != "":
            where += " AND o.status=?"
            args.append(int(status))
        where, args = add_wh_scope(where, args, user, "o.wh_id")
        total = conn.execute("SELECT COUNT(*) FROM wms_in_order o " + where, args).fetchone()[0]
        rows = conn.execute(
            "SELECT o.*, p.partner_name FROM wms_in_order o LEFT JOIN base_partner p ON p.id=o.partner_id "
            + where + " ORDER BY o.id DESC LIMIT ? OFFSET ?", args + [size, offset]).fetchall()
        return {"total": total, "page": page, "size": size, "list": [dict(r) for r in rows]}
    finally:
        conn.close()


@route("GET", r"/api/v1/in-orders/(\d+)")
def api_in_detail(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        oid = int(m.group(1))
        order = conn.execute("SELECT o.*, p.partner_name FROM wms_in_order o"
                             " LEFT JOIN base_partner p ON p.id=o.partner_id WHERE o.id=?", (oid,)).fetchone()
        if not order:
            raise ApiError(40400, "入库单不存在", 404)
        lines = conn.execute(
            "SELECT d.*, g.goods_code, g.goods_name, g.unit AS goods_unit, l.loc_code, l.loc_name"
            " FROM wms_in_order_detail d JOIN base_goods g ON g.id=d.goods_id"
            " JOIN base_location l ON l.id=d.loc_id WHERE d.order_id=? ORDER BY d.id", (oid,)).fetchall()
        data = dict(order)
        data["lines"] = [dict(r) for r in lines]
        return data
    finally:
        conn.close()


@route("POST", r"/api/v1/in-orders")
def api_in_create(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        lines = body.get("lines") or []
        wh_id = validate_order(conn, body, "in")
        check_wh_scope(user, wh_id)
        conn.execute("BEGIN IMMEDIATE")
        order_no = next_order_no(conn, "RK")
        total = sum(DEC2(x.get("plan_num") or x.get("planNum")) for x in lines)
        cur = conn.execute(
            "INSERT INTO wms_in_order(order_no,order_type,partner_id,wh_id,status,total_plan_num,remark,create_by)"
            " VALUES(?,?,?,?,0,?,?,?)",
            (order_no, int(body.get("order_type") or 1), body.get("partner_id"), int(body.get("wh_id") or 1),
             DEC2(total), body.get("remark"), user["id"]))
        oid = cur.lastrowid
        for x in lines:
            conn.execute("INSERT INTO wms_in_order_detail(order_id,goods_id,loc_id,plan_num,actual_num,"
                         "batch_no,unit,expire_date) VALUES(?,?,?,?,?,?,?,?)",
                         (oid, int(x["goods_id"]), int(x["loc_id"]), DEC2(x.get("plan_num")),
                          DEC2(x.get("actual_num") or x.get("plan_num")), (x.get("batch_no") or "").strip(),
                          x.get("unit"), (x.get("expire_date") or "").strip() or None))
        write_log(conn, user, "入库管理", "CREATE", oid, order_no, {"lines": len(lines), "total": DEC2(total)})
        conn.commit()
        return {"id": oid, "orderNo": order_no, "status": 0}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/in-orders/(\d+)/audit")
def api_in_audit(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        oid = int(m.group(1))
        row = conn.execute("SELECT * FROM wms_in_order WHERE id=?", (oid,)).fetchone()
        if not row:
            raise ApiError(40400, "入库单不存在", 404)
        if row["status"] != 0:
            raise ApiError(40001, "只有草稿单可以审核", 409)
        conn.execute("UPDATE wms_in_order SET status=1, audit_by=?, audit_time=? WHERE id=?", (user["id"], now(), oid))
        write_log(conn, user, "入库管理", "AUDIT", oid, row["order_no"])
        conn.commit()
        return {"id": oid, "status": 1}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/in-orders/(\d+)/confirm")
def api_in_confirm(handler, m, q, body):
    user = current_user(handler)
    request_id = handler.headers.get("X-Idempotency-Key") or body.get("requestId") or ("in-%s-%d" % (m.group(1), int(time.time() * 1000)))
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        seen = conn.execute("SELECT * FROM wms_in_order WHERE request_id=? AND status=3", (request_id,)).fetchone()
        if seen:
            if seen["id"] != int(m.group(1)):
                raise ApiError(40901, "幂等键已用于其他入库单", 409)
            conn.commit()
            return {"orderNo": seen["order_no"], "status": 3, "duplicated": True}
        result = post_in_order(conn, int(m.group(1)), request_id, user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/in-orders/(\d+)/cancel")
def api_in_cancel(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        oid = int(m.group(1))
        row = conn.execute("SELECT * FROM wms_in_order WHERE id=?", (oid,)).fetchone()
        if not row:
            raise ApiError(40400, "入库单不存在", 404)
        if row["status"] == 3:
            raise ApiError(50301, "单据已过账，不能作废", 409)
        conn.execute("UPDATE wms_in_order SET status=4 WHERE id=?", (oid,))
        write_log(conn, user, "入库管理", "CANCEL", oid, row["order_no"])
        conn.commit()
        return {"id": oid, "status": 4}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------- 出库 ----------------
@route("GET", r"/api/v1/out-orders")
def api_out_list(handler, m, q, body):
    user = current_user(handler)
    page, size, offset = paging(q)
    conn = db()
    try:
        status = q.get("status", [""])[0]
        where, args = "WHERE 1=1", []
        if status != "":
            where += " AND o.status=?"
            args.append(int(status))
        where, args = add_wh_scope(where, args, user, "o.wh_id")
        total = conn.execute("SELECT COUNT(*) FROM wms_out_order o " + where, args).fetchone()[0]
        rows = conn.execute(
            "SELECT o.*, p.partner_name FROM wms_out_order o LEFT JOIN base_partner p ON p.id=o.partner_id "
            + where + " ORDER BY o.id DESC LIMIT ? OFFSET ?", args + [size, offset]).fetchall()
        return {"total": total, "page": page, "size": size, "list": [dict(r) for r in rows]}
    finally:
        conn.close()


@route("GET", r"/api/v1/out-orders/(\d+)")
def api_out_detail(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        oid = int(m.group(1))
        order = conn.execute("SELECT o.*, p.partner_name FROM wms_out_order o"
                             " LEFT JOIN base_partner p ON p.id=o.partner_id WHERE o.id=?", (oid,)).fetchone()
        if not order:
            raise ApiError(40400, "出库单不存在", 404)
        lines = conn.execute(
            "SELECT d.*, g.goods_code, g.goods_name, l.loc_code, l.loc_name,"
            " IFNULL((SELECT available_qty FROM wms_inventory i WHERE i.goods_id=d.goods_id AND i.loc_id=d.loc_id"
            "         AND i.batch_no=d.batch_no),0) AS available_qty"
            " FROM wms_out_order_detail d JOIN base_goods g ON g.id=d.goods_id"
            " JOIN base_location l ON l.id=d.loc_id WHERE d.order_id=? ORDER BY d.id", (oid,)).fetchall()
        data = dict(order)
        data["lines"] = [dict(r) for r in lines]
        return data
    finally:
        conn.close()


@route("POST", r"/api/v1/out-orders")
def api_out_create(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        lines = body.get("lines") or []
        wh_id = validate_order(conn, body, "out")
        check_wh_scope(user, wh_id)
        conn.execute("BEGIN IMMEDIATE")
        order_no = next_order_no(conn, "CK")
        total = sum(DEC2(x.get("plan_num")) for x in lines)
        cur = conn.execute(
            "INSERT INTO wms_out_order(order_no,order_type,partner_id,wh_id,status,total_plan_num,remark,create_by)"
            " VALUES(?,?,?,?,0,?,?,?)",
            (order_no, int(body.get("order_type") or 1), body.get("partner_id"), int(body.get("wh_id") or 1),
             DEC2(total), body.get("remark"), user["id"]))
        oid = cur.lastrowid
        for x in lines:
            conn.execute("INSERT INTO wms_out_order_detail(order_id,goods_id,loc_id,plan_num,actual_num,batch_no,unit)"
                         " VALUES(?,?,?,?,?,?,?)",
                         (oid, int(x["goods_id"]), int(x["loc_id"]), DEC2(x.get("plan_num")),
                          DEC2(x.get("actual_num") or x.get("plan_num")), (x.get("batch_no") or "").strip(), x.get("unit")))
        write_log(conn, user, "出库管理", "CREATE", oid, order_no, {"lines": len(lines), "total": DEC2(total)})
        conn.commit()
        return {"id": oid, "orderNo": order_no, "status": 0}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/out-orders/(\d+)/audit")
def api_out_audit(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = audit_out_order(conn, int(m.group(1)), user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/out-orders/(\d+)/confirm")
def api_out_confirm(handler, m, q, body):
    user = current_user(handler)
    request_id = handler.headers.get("X-Idempotency-Key") or body.get("requestId") or ("out-%s-%d" % (m.group(1), int(time.time() * 1000)))
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        seen = conn.execute("SELECT * FROM wms_out_order WHERE request_id=? AND status=3", (request_id,)).fetchone()
        if seen:
            if seen["id"] != int(m.group(1)):
                raise ApiError(40901, "幂等键已用于其他出库单", 409)
            conn.commit()
            return {"orderNo": seen["order_no"], "status": 3, "duplicated": True}
        result = post_out_order(conn, int(m.group(1)), request_id, user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/out-orders/(\d+)/cancel")
def api_out_cancel(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = cancel_out_order(conn, int(m.group(1)), user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------- 库存 / 流水 / 报表 ----------------
@route("GET", r"/api/v1/inventory")
def api_inventory(handler, m, q, body):
    user = current_user(handler)
    page, size, offset = paging(q)
    kw = (q.get("keyword", [""])[0] or "").strip()
    conn = db()
    try:
        where, args = "WHERE 1=1", []
        if kw:
            where += " AND (g.goods_code LIKE ? OR g.goods_name LIKE ? OR l.loc_code LIKE ?)"
            args += ["%" + kw + "%"] * 3
        if q.get("onlyStock", ["1"])[0] == "1":
            where += " AND i.quantity > 0"
        where, args = add_wh_scope(where, args, user, "l.wh_id")
        total = conn.execute("SELECT COUNT(*) FROM wms_inventory i JOIN base_goods g ON g.id=i.goods_id"
                             " JOIN base_location l ON l.id=i.loc_id " + where, args).fetchone()[0]
        rows = conn.execute(
            "SELECT i.*, g.goods_code, g.goods_name, g.unit, g.cost_price, l.loc_code, l.loc_name,"
            " (i.quantity * g.cost_price) AS amount"
            " FROM wms_inventory i JOIN base_goods g ON g.id=i.goods_id JOIN base_location l ON l.id=i.loc_id "
            + where + " ORDER BY g.goods_code, l.loc_code LIMIT ? OFFSET ?", args + [size, offset]).fetchall()
        total_amt = conn.execute(
            "SELECT IFNULL(SUM(i.quantity*g.cost_price),0) FROM wms_inventory i"
            " JOIN base_goods g ON g.id=i.goods_id JOIN base_location l ON l.id=i.loc_id " + where, args).fetchone()[0]
        return {"total": total, "page": page, "size": size, "totalAmount": DEC2(total_amt),
                "list": [dict(r) for r in rows]}
    finally:
        conn.close()


@route("GET", r"/api/v1/inventory/txn")
def api_txn(handler, m, q, body):
    current_user(handler)
    page, size, offset = paging(q)
    conn = db()
    try:
        where, args = "WHERE 1=1", []
        if q.get("goodsId", [""])[0]:
            where += " AND t.goods_id=?"
            args.append(int(q["goodsId"][0]))
        if q.get("bizType", [""])[0]:
            where += " AND t.biz_type=?"
            args.append(int(q["bizType"][0]))
        total = conn.execute("SELECT COUNT(*) FROM wms_inventory_txn t " + where, args).fetchone()[0]
        rows = conn.execute(
            "SELECT t.*, g.goods_code, g.goods_name, l.loc_code FROM wms_inventory_txn t"
            " JOIN base_goods g ON g.id=t.goods_id JOIN base_location l ON l.id=t.loc_id " + where +
            " ORDER BY t.id DESC LIMIT ? OFFSET ?", args + [size, offset]).fetchall()
        return {"total": total, "page": page, "size": size, "list": [dict(r) for r in rows]}
    finally:
        conn.close()


@route("GET", r"/api/v1/reports/ledger")
def api_ledger(handler, m, q, body):
    """库存台账：期初 + 入 − 出 ± 调整 = 期末"""
    current_user(handler)
    begin = q.get("begin", [datetime.now().strftime("%Y-%m-01")])[0]
    end = q.get("end", [datetime.now().strftime("%Y-%m-%d")])[0]
    try:
        begin_date = datetime.strptime(begin, "%Y-%m-%d")
        end_date = datetime.strptime(end, "%Y-%m-%d")
    except ValueError:
        raise ApiError(40000, "台账日期格式必须为 YYYY-MM-DD")
    if begin_date > end_date:
        raise ApiError(40000, "台账开始日期不能晚于结束日期")
    conn = db()
    try:
        rows = conn.execute(
            "SELECT g.goods_code, g.goods_name, g.unit, l.loc_code,"
            " (SELECT IFNULL(SUM(change_qty),0) FROM wms_inventory_txn t WHERE t.goods_id=i.goods_id"
            "    AND t.loc_id=i.loc_id AND t.batch_no=i.batch_no"
            "    AND (t.biz_type=11 OR date(t.create_time) < ?)) AS begin_qty,"
            " (SELECT IFNULL(SUM(CASE WHEN change_qty>0 THEN change_qty ELSE 0 END),0) FROM wms_inventory_txn t"
            "    WHERE t.goods_id=i.goods_id AND t.loc_id=i.loc_id AND t.batch_no=i.batch_no"
            "    AND t.biz_type<>11 AND date(t.create_time) BETWEEN ? AND ?) AS in_qty,"
            " (SELECT IFNULL(SUM(CASE WHEN change_qty<0 THEN -change_qty ELSE 0 END),0) FROM wms_inventory_txn t"
            "    WHERE t.goods_id=i.goods_id AND t.loc_id=i.loc_id AND t.batch_no=i.batch_no"
            "    AND t.biz_type<>11 AND date(t.create_time) BETWEEN ? AND ?) AS out_qty,"
            " (SELECT IFNULL(SUM(change_qty),0) FROM wms_inventory_txn t WHERE t.goods_id=i.goods_id"
            "    AND t.loc_id=i.loc_id AND t.batch_no=i.batch_no"
            "    AND (t.biz_type=11 OR date(t.create_time) <= ?)) AS end_qty,"
            " i.available_qty AS current_available_qty, i.locked_qty AS current_locked_qty"
            " FROM wms_inventory i JOIN base_goods g ON g.id=i.goods_id JOIN base_location l ON l.id=i.loc_id"
            " ORDER BY g.goods_code, l.loc_code", (begin, begin, end, begin, end, end)).fetchall()
        return {"begin": begin, "end": end, "list": [dict(r) for r in rows]}
    finally:
        conn.close()


@route("GET", r"/api/v1/logs")
def api_logs(handler, m, q, body):
    current_user(handler)
    page, size, offset = paging(q)
    conn = db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM sys_operation_log").fetchone()[0]
        rows = conn.execute("SELECT * FROM sys_operation_log ORDER BY id DESC LIMIT ? OFFSET ?",
                            (size, offset)).fetchall()
        return {"total": total, "page": page, "size": size, "list": [dict(r) for r in rows]}
    finally:
        conn.close()


@route("POST", r"/api/v1/admin/reset-demo")
def api_reset(handler, m, q, body):
    user = current_user(handler)
    if user["username"] != "admin":
        raise ApiError(40300, "只有演示管理员可以重置数据", 403)
    with WRITE_LOCK:
        init_db(reset=True)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 电商域接口：用户层（商城前台）
# --------------------------------------------------------------------------- #
@route("GET", r"/api/v1/shops")
def api_shops(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM base_shop WHERE status=1 ORDER BY id")]
        for r in rows:
            r["listingCount"] = conn.execute(
                "SELECT COUNT(*) FROM ec_listing WHERE shop_id=? AND sale_status=1", (r["id"],)).fetchone()[0]
        return {"list": rows}
    finally:
        conn.close()


@route("GET", r"/api/v1/shop/goods")
def api_shop_goods(handler, m, q, body):
    """商城商品列表：只返回上架商品，库存严格按「可售量」展示（渠道保留量已扣）。"""
    current_user(handler)
    shop_id = int(q.get("shopId", ["1"])[0])
    kw = (q.get("keyword", [""])[0] or "").strip()
    conn = db()
    try:
        where, args = "WHERE l.shop_id=? AND g.status=1 AND IFNULL(g.is_deleted,0)=0", [shop_id]
        if q.get("categoryId", [""])[0]:
            where += " AND g.category_id=?"
            args.append(int(q["categoryId"][0]))
        if kw:
            where += " AND (g.goods_code LIKE ? OR g.goods_name LIKE ?)"
            args += ["%" + kw + "%"] * 2
        rows = conn.execute(
            "SELECT l.id AS listing_id, l.sale_price, l.sale_status, l.reserve_qty, g.id AS goods_id,"
            " g.goods_code, g.goods_name, g.spec, g.unit, g.category_id, g.spu_id, g.spec_attrs,"
            " c.category_name, s.spu_name, s.spu_code,"
            " (CASE WHEN IFNULL(g.image_url,'')<>'' THEN g.image_url"
            "       WHEN IFNULL((SELECT image_url FROM base_spu WHERE id=g.spu_id),'')<>''"
            "            THEN (SELECT image_url FROM base_spu WHERE id=g.spu_id)"
            "       ELSE '/img/cat-'||IFNULL(g.category_id,0)||'.svg' END) AS image_url,"
            " IFNULL((SELECT SUM(i.available_qty) FROM wms_inventory i JOIN base_location lb ON lb.id=i.loc_id"
            "         WHERE i.goods_id=g.id AND lb.status=1 AND lb.loc_type=4),0) AS available_total"
            " FROM ec_listing l JOIN base_goods g ON g.id=l.goods_id"
            " LEFT JOIN base_goods_category c ON c.id=g.category_id"
            " LEFT JOIN base_spu s ON s.id=g.spu_id " + where +
            " ORDER BY l.sale_status DESC, l.sort_no, g.goods_code", args).fetchall()
        lst = []
        for r in rows:
            item = dict(r)
            item["saleable"] = max(0.0, DEC2(item["available_total"] - DEC2(item["reserve_qty"])))
            item["sold_count"] = conn.execute(
                "SELECT IFNULL(SUM(a.alloc_qty),0) FROM ec_order_alloc a JOIN ec_order o ON o.id=a.order_id"
                " WHERE o.shop_id=? AND a.goods_id=? AND a.status=3", (shop_id, item["goods_id"])).fetchone()[0]
            lst.append(item)
        return {"list": lst, "shopId": shop_id}
    finally:
        conn.close()


@route("POST", r"/api/v1/shop/orders")
def api_shop_order_create(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = create_ec_order(conn, body, user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/shop/orders/(\d+)/pay")
def api_shop_order_pay(handler, m, q, body):
    user = current_user(handler)
    request_id = handler.headers.get("X-Idempotency-Key") or body.get("requestId")
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        oid = int(m.group(1))
        if request_id:
            seen = conn.execute("SELECT * FROM ec_order WHERE request_id=? AND status>=1", (request_id,)).fetchone()
            if seen:
                if seen["id"] != oid:
                    raise ApiError(40901, "幂等键已用于其他订单", 409)
                conn.commit()
                return {"orderNo": seen["order_no"], "status": seen["status"], "duplicated": True}
        result = pay_ec_order(conn, oid, request_id, user)
        split_into_fulfillments(conn, oid)      # 幂等补齐：任何支付路径都必须有履约单
        conn.commit()
        # 支付已提交，再异步并波：队列失败不影响支付（幂等重试）
        if not result.get("duplicated"):
            try:
                task_queue().enqueue("AUTO_WAVE", {"order_id": oid}, delay_seconds=2)
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write("[WARN] 入队失败：%s\n" % exc)
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("GET", r"/api/v1/shop/orders")
def api_shop_orders(handler, m, q, body):
    """我的订单（用户层视角：只看自己的单，字段不含内部成本/库位）"""
    current_user(handler)
    page, size, offset = paging(q)
    conn = db()
    try:
        where, args = "WHERE 1=1", []
        if q.get("status", [""])[0] != "":
            where += " AND o.status=?"
            args.append(int(q["status"][0]))
        if q.get("shopId", [""])[0]:
            where += " AND o.shop_id=?"
            args.append(int(q["shopId"][0]))
        total = conn.execute("SELECT COUNT(*) FROM ec_order o " + where, args).fetchone()[0]
        rows = conn.execute(
            "SELECT o.*, s.shop_name, (SELECT COUNT(*) FROM ec_order_item i WHERE i.order_id=o.id) AS item_count,"
            " (SELECT IFNULL(SUM(qty),0) FROM ec_order_item i WHERE i.order_id=o.id) AS total_qty"
            " FROM ec_order o JOIN base_shop s ON s.id=o.shop_id " + where +
            " ORDER BY o.id DESC LIMIT ? OFFSET ?", args + [size, offset]).fetchall()
        return {"total": total, "page": page, "size": size, "list": [dict(r) for r in rows]}
    finally:
        conn.close()


@route("GET", r"/api/v1/ec/orders/(\d+)")
def api_ec_order_detail(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        oid = int(m.group(1))
        order = conn.execute("SELECT o.*, s.shop_name FROM ec_order o JOIN base_shop s ON s.id=o.shop_id"
                             " WHERE o.id=?", (oid,)).fetchone()
        if not order:
            raise ApiError(40400, "订单不存在", 404)
        data = dict(order)
        data["statusText"] = EC_STATUS.get(order["status"], "?")
        data["kindText"] = ORDER_KIND.get(order["order_kind"] or 1, "销售单")
        data["items"] = [dict(r) for r in conn.execute(
            "SELECT i.*, g.spec, g.unit, g.spec_attrs FROM ec_order_item i LEFT JOIN base_goods g ON g.id=i.goods_id"
            " WHERE i.order_id=? ORDER BY i.id", (oid,))]
        data["allocs"] = [dict(r) for r in conn.execute(
            "SELECT a.*, l.loc_code, l.loc_name, g.goods_code, g.goods_name, g.spec_attrs FROM ec_order_alloc a"
            " JOIN base_location l ON l.id=a.loc_id JOIN base_goods g ON g.id=a.goods_id"
            " WHERE a.order_id=? ORDER BY a.id", (oid,))]
        ship = conn.execute("SELECT * FROM ec_shipment WHERE order_id=? ORDER BY id DESC LIMIT 1", (oid,)).fetchone()
        data["shipment"] = dict(ship) if ship else None
        pkg = conn.execute("SELECT * FROM ec_package WHERE order_id=? ORDER BY id DESC LIMIT 1", (oid,)).fetchone()
        data["package"] = dict(pkg) if pkg else None
        wave = conn.execute("SELECT w.wave_no, w.status FROM ec_wave_order wo JOIN ec_wave w ON w.id=wo.wave_id"
                            " WHERE wo.order_id=? ORDER BY w.id DESC LIMIT 1", (oid,)).fetchone()
        data["wave"] = dict(wave) if wave else None
        data["afterSales"] = [dict(r) for r in conn.execute(
            "SELECT a.*, g.goods_code, g.goods_name FROM ec_after_sale a JOIN base_goods g ON g.id=a.goods_id"
            " WHERE a.order_id=? ORDER BY a.id DESC", (oid,))]
        for a in data["afterSales"]:
            a["statusText"] = AFTER_SALE_STATUS.get(a["status"], "?")
            a["typeText"] = AFTER_SALE_TYPE.get(a["type"] or 1, "?")
        return data
    finally:
        conn.close()


@route("POST", r"/api/v1/shop/orders/(\d+)/cancel")
def api_shop_order_cancel(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = cancel_ec_order(conn, int(m.group(1)), (body.get("reason") or "用户取消"), user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/shop/orders/(\d+)/receive")
def api_shop_order_receive(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = confirm_ec_receipt(conn, int(m.group(1)), user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 电商域接口：运营层（订单履约 / 上下架 / 补货 / 售后）
# --------------------------------------------------------------------------- #
@route("GET", r"/api/v1/ec/orders")
def api_ec_orders(handler, m, q, body):
    current_user(handler)
    page, size, offset = paging(q)
    conn = db()
    try:
        where, args = "WHERE 1=1", []
        if q.get("status", [""])[0] != "":
            where += " AND o.status=?"
            args.append(int(q["status"][0]))
        kw = (q.get("keyword", [""])[0] or "").strip()
        if kw:
            where += " AND (o.order_no LIKE ? OR o.buyer_name LIKE ? OR IFNULL(o.channel_order_no,'') LIKE ?)"
            args += ["%" + kw + "%"] * 3
        total = conn.execute("SELECT COUNT(*) FROM ec_order o " + where, args).fetchone()[0]
        rows = conn.execute(
            "SELECT o.*, s.shop_name,"
            " (SELECT IFNULL(SUM(qty),0) FROM ec_order_item i WHERE i.order_id=o.id) AS total_qty,"
            " (SELECT COUNT(*) FROM ec_order_alloc a WHERE a.order_id=o.id AND a.status IN (1,2)) AS open_allocs"
            " FROM ec_order o JOIN base_shop s ON s.id=o.shop_id " + where +
            " ORDER BY CASE o.status WHEN 1 THEN 0 WHEN 2 THEN 1 WHEN 6 THEN 2 ELSE 3 END, o.id DESC"
            " LIMIT ? OFFSET ?", args + [size, offset]).fetchall()
        lst = []
        for r in rows:
            item = dict(r)
            item["statusText"] = EC_STATUS.get(item["status"], "?")
            item["kindText"] = ORDER_KIND.get(item["order_kind"] or 1, "销售单")
            lst.append(item)
        return {"total": total, "page": page, "size": size, "list": lst}
    finally:
        conn.close()


@route("POST", r"/api/v1/ec/orders/(\d+)/pick")
def api_ec_pick(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = pick_ec_order(conn, int(m.group(1)), user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("GET", r"/api/v1/ec/orders/(\d+)/fulfillments")
def api_ec_order_fulfillments(handler, m, q, body):
    """订单的履约单（按仓拆单）：前端发货时选仓、看部分发货进度都靠它。"""
    current_user(handler)
    order_id = int(m.group(1))
    conn = db()
    try:
        order = conn.execute("SELECT id, order_no, status, split_count, ship_qty FROM ec_order WHERE id=?",
                             (order_id,)).fetchone()
        if not order:
            raise ApiError(40400, "订单不存在", 404)
        rows = conn.execute(
            "SELECT f.*, l.loc_name AS wh_name,"
            " (SELECT COUNT(*) FROM ec_order_alloc a WHERE a.fulfillment_id=f.id) AS alloc_lines"
            " FROM ec_fulfillment f LEFT JOIN base_location l ON l.id=f.wh_id"
            " WHERE f.order_id=? ORDER BY f.seq", (order_id,)).fetchall()
        return {"orderId": order_id, "orderNo": order["order_no"],
                "status": order["status"], "statusText": EC_STATUS.get(order["status"], order["status"]),
                "splitCount": order["split_count"], "shipQty": DEC2(order["ship_qty"]),
                "list": [dict(r) for r in rows]}
    finally:
        conn.close()


@route("POST", r"/api/v1/ec/orders/(\d+)/ship")
def api_ec_ship(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = ship_ec_order(conn, int(m.group(1)), body.get("carrier"), body.get("tracking_no"), user,
                               body.get("fulfillment_id") or body.get("fulfillmentId"))
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("GET", r"/api/v1/ec/shipments")
def api_ec_shipments(handler, m, q, body):
    current_user(handler)
    page, size, offset = paging(q)
    conn = db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM ec_shipment").fetchone()[0]
        rows = conn.execute(
            "SELECT s.*, o.order_no, o.buyer_name, o.receiver_name, o.receiver_address"
            " FROM ec_shipment s JOIN ec_order o ON o.id=s.order_id"
            " ORDER BY s.id DESC LIMIT ? OFFSET ?", (size, offset)).fetchall()
        return {"total": total, "page": page, "size": size, "list": [dict(r) for r in rows]}
    finally:
        conn.close()


@route("GET", r"/api/v1/listings")
def api_listings(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        shop_id = q.get("shopId", [""])[0]
        where, args = "WHERE IFNULL(g.is_deleted,0)=0", []
        if shop_id:
            where += " AND l.shop_id=?"
            args.append(int(shop_id))
        rows = conn.execute(
            "SELECT l.*, g.goods_code, g.goods_name, g.unit, g.cost_price, g.spec_attrs, s.shop_name,"
            " (CASE WHEN IFNULL(g.image_url,'')<>'' THEN g.image_url"
            "       WHEN IFNULL((SELECT image_url FROM base_spu WHERE id=g.spu_id),'')<>''"
            "            THEN (SELECT image_url FROM base_spu WHERE id=g.spu_id)"
            "       ELSE '/img/cat-'||IFNULL(g.category_id,0)||'.svg' END) AS image_url,"
            " IFNULL((SELECT SUM(i.available_qty) FROM wms_inventory i WHERE i.goods_id=g.id),0) AS available_total"
            " FROM ec_listing l JOIN base_goods g ON g.id=l.goods_id JOIN base_shop s ON s.id=l.shop_id "
            + where + " ORDER BY l.sale_status DESC, g.goods_code", args).fetchall()
        lst = []
        for r in rows:
            item = dict(r)
            item["saleable"] = max(0.0, DEC2(item["available_total"] - DEC2(item["reserve_qty"])))
            item["margin"] = DEC2(DEC2(item["sale_price"]) - DEC2(item["cost_price"]))
            lst.append(item)
        goods = [dict(r) for r in conn.execute(
            "SELECT id, goods_code, goods_name, unit, cost_price FROM base_goods WHERE status=1 ORDER BY goods_code")]
        shops = [dict(r) for r in conn.execute("SELECT * FROM base_shop WHERE status=1")]
        return {"list": lst, "goods": goods, "shops": shops}
    finally:
        conn.close()


@route("POST", r"/api/v1/listings")
def api_listing_create(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        try:
            shop_id, goods_id = int(body.get("shop_id")), int(body.get("goods_id"))
        except (TypeError, ValueError):
            raise ApiError(40000, "请选择店铺与商品")
        if float(body.get("sale_price") or 0) <= 0:
            raise ApiError(40000, "售价必须大于 0")
        conn.execute(
            "INSERT INTO ec_listing(shop_id,goods_id,sale_price,sale_status,reserve_qty) VALUES(?,?,?,?,?)"
            " ON CONFLICT(shop_id,goods_id) DO UPDATE SET sale_price=excluded.sale_price,"
            " sale_status=excluded.sale_status, reserve_qty=excluded.reserve_qty,"
            " update_time=datetime('now','localtime')",
            (shop_id, goods_id, DEC2(body.get("sale_price")), int(body.get("sale_status", 1)),
             DEC2(body.get("reserve_qty") or 0)))
        write_log(conn, user, "商品上下架", "CREATE", goods_id, None,
                  {"shopId": shop_id, "price": body.get("sale_price")})
        conn.commit()
        return {"ok": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("PUT", r"/api/v1/listings/(\d+)")
def api_listing_update(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        lid = int(m.group(1))
        row = conn.execute("SELECT * FROM ec_listing WHERE id=?", (lid,)).fetchone()
        if not row:
            raise ApiError(40400, "上架记录不存在", 404)
        price = body.get("sale_price")
        conn.execute("UPDATE ec_listing SET sale_price=?, sale_status=?, reserve_qty=?,"
                     " update_time=datetime('now','localtime') WHERE id=?",
                     (DEC2(price if price not in (None, "") else row["sale_price"]),
                      int(body.get("sale_status", row["sale_status"])),
                      DEC2(body.get("reserve_qty", row["reserve_qty"])), lid))
        write_log(conn, user, "商品上下架", "UPDATE", lid, None,
                  {"sale_status": body.get("sale_status"), "price": price,
                   "reserve": body.get("reserve_qty")})
        conn.commit()
        return {"id": lid}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("GET", r"/api/v1/replenish/suggestions")
def api_replenish(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        return {"list": replenish_suggestions(conn, only_below=q.get("all", ["0"])[0] != "1")}
    finally:
        conn.close()


@route("PUT", r"/api/v1/replenish/policy")
def api_replenish_policy(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        try:
            goods_id = int(body.get("goods_id"))
        except (TypeError, ValueError):
            raise ApiError(40000, "请选择商品")
        conn.execute(
            "INSERT INTO wms_stock_policy(goods_id,wh_id,safety_stock,replenish_qty,lead_days) VALUES(?,0,?,?,?)"
            " ON CONFLICT(goods_id,wh_id) DO UPDATE SET safety_stock=excluded.safety_stock,"
            " replenish_qty=excluded.replenish_qty, lead_days=excluded.lead_days,"
            " update_time=datetime('now','localtime')",
            (goods_id, DEC2(body.get("safety_stock") or 0), DEC2(body.get("replenish_qty") or 0),
             int(body.get("lead_days") or 7)))
        write_log(conn, user, "补货管理", "UPDATE", goods_id, None, body)
        conn.commit()
        return {"ok": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/replenish/generate")
def api_replenish_generate(handler, m, q, body):
    """把补货建议一键落成采购入库草稿（不直接改库存，仍需收货过账）"""
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        wants = body.get("goods_ids") or []
        suggestions = [s for s in replenish_suggestions(conn) if not wants or s["goods_id"] in wants]
        if not suggestions:
            raise ApiError(40001, "当前没有需要补货的商品", 409)
        wh = conn.execute("SELECT * FROM base_location WHERE loc_type=1 AND status=1 ORDER BY id LIMIT 1").fetchone()
        if not wh:
            raise ApiError(40000, "没有可用仓库")
        supplier = conn.execute("SELECT id FROM base_partner WHERE partner_type='supplier' AND status=1"
                                " ORDER BY id LIMIT 1").fetchone()
        order_no = next_order_no(conn, "RK")
        total = DEC2(sum(s["suggest_qty"] for s in suggestions))
        cur = conn.execute(
            "INSERT INTO wms_in_order(order_no,order_type,partner_id,wh_id,status,total_plan_num,remark,create_by)"
            " VALUES(?,1,?,?,0,?,'补货建议自动生成',?)",
            (order_no, supplier["id"] if supplier else None, wh["id"], total, user["id"]))
        oid = cur.lastrowid
        for s in suggestions:
            loc = conn.execute(
                "SELECT i.loc_id FROM wms_inventory i JOIN base_location l ON l.id=i.loc_id"
                " WHERE i.goods_id=? AND l.status=1 AND l.loc_type=4 ORDER BY i.quantity DESC LIMIT 1",
                (s["goods_id"],)).fetchone()
            if loc:
                loc_id = loc["loc_id"]
            else:
                fallback = conn.execute("SELECT id FROM base_location WHERE loc_type=4 AND status=1"
                                        " ORDER BY loc_code LIMIT 1").fetchone()
                loc_id = fallback["id"] if fallback else wh["id"]
            conn.execute("INSERT INTO wms_in_order_detail(order_id,goods_id,loc_id,plan_num,actual_num,unit)"
                         " VALUES(?,?,?,?,?,?)",
                         (oid, s["goods_id"], loc_id, s["suggest_qty"], s["suggest_qty"], s["unit"]))
        write_log(conn, user, "补货管理", "CREATE", oid, order_no, {"lines": len(suggestions), "total": total})
        conn.commit()
        return {"id": oid, "orderNo": order_no, "lines": len(suggestions), "total": total}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("GET", r"/api/v1/ec/after-sales")
def api_after_sales(handler, m, q, body):
    current_user(handler)
    page, size, offset = paging(q)
    conn = db()
    try:
        where, args = "WHERE 1=1", []
        if q.get("status", [""])[0] != "":
            where += " AND a.status=?"
            args.append(int(q["status"][0]))
        total = conn.execute("SELECT COUNT(*) FROM ec_after_sale a " + where, args).fetchone()[0]
        rows = conn.execute(
            "SELECT a.*, o.order_no, o.buyer_name, g.goods_code, g.goods_name, g.spec_attrs, l.loc_code"
            " FROM ec_after_sale a JOIN ec_order o ON o.id=a.order_id JOIN base_goods g ON g.id=a.goods_id"
            " LEFT JOIN base_location l ON l.id=a.loc_id " + where +
            " ORDER BY a.id DESC LIMIT ? OFFSET ?", args + [size, offset]).fetchall()
        lst = []
        for r in rows:
            item = dict(r)
            item["statusText"] = AFTER_SALE_STATUS.get(item["status"], "?")
            item["typeText"] = AFTER_SALE_TYPE.get(item["type"] or 1, "?")
            lst.append(item)
        return {"total": total, "page": page, "size": size, "list": lst}
    finally:
        conn.close()


@route("POST", r"/api/v1/ec/after-sales")
def api_after_sale_create(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = create_after_sale(conn, body, user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/ec/after-sales/(\d+)/approve")
def api_after_sale_approve(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = approve_after_sale(conn, int(m.group(1)), user, approved=True)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/ec/after-sales/(\d+)/reject")
def api_after_sale_reject(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = approve_after_sale(conn, int(m.group(1)), user, approved=False)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/ec/after-sales/(\d+)/receive")
def api_after_sale_receive(handler, m, q, body):
    """退货收货 + 质检：body 传 good_qty / bad_qty（不传则全部按良品入库）"""
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = receive_after_sale(conn, int(m.group(1)), body.get("good_qty"), body.get("bad_qty"), user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 波次拣货 / 复核 / 打包接口（订单驱动的作业核心）
# --------------------------------------------------------------------------- #
@route("GET", r"/api/v1/ec/waves")
def api_waves(handler, m, q, body):
    user = current_user(handler)
    page, size, offset = paging(q)
    conn = db()
    try:
        where, args = "WHERE 1=1", []
        if q.get("status", [""])[0] != "":
            where += " AND w.status=?"
            args.append(int(q["status"][0]))
        where, args = add_wh_scope(where, args, user, "w.wh_id")
        total = conn.execute("SELECT COUNT(*) FROM ec_wave w " + where, args).fetchone()[0]
        rows = conn.execute(
            "SELECT w.*, s.shop_name, l.loc_name AS wh_name,"
            " (SELECT COUNT(*) FROM ec_pick_task p WHERE p.wave_id=w.id AND p.status=0) AS open_tasks"
            " FROM ec_wave w LEFT JOIN base_shop s ON s.id=w.shop_id"
            " LEFT JOIN base_location l ON l.id=w.wh_id " + where +
            " ORDER BY CASE w.status WHEN 0 THEN 0 WHEN 1 THEN 1 WHEN 2 THEN 2 ELSE 3 END, w.id DESC"
            " LIMIT ? OFFSET ?", args + [size, offset]).fetchall()
        lst = []
        for r in rows:
            item = dict(r)
            item["statusText"] = WAVE_STATUS.get(item["status"], "?")
            lst.append(item)
        return {"total": total, "page": page, "size": size, "list": lst}
    finally:
        conn.close()


@route("GET", r"/api/v1/ec/waves/(\d+)")
def api_wave_detail(handler, m, q, body):
    """波次详情：按库位编码排序的拣货任务（= 拣货路径）+ 波次内订单"""
    current_user(handler)
    conn = db()
    try:
        wid = int(m.group(1))
        wave = conn.execute("SELECT w.*, l.loc_name AS wh_name FROM ec_wave w"
                            " LEFT JOIN base_location l ON l.id=w.wh_id WHERE w.id=?", (wid,)).fetchone()
        if not wave:
            raise ApiError(40400, "波次不存在", 404)
        data = dict(wave)
        data["statusText"] = WAVE_STATUS.get(wave["status"], "?")
        data["tasks"] = [dict(r) for r in conn.execute(
            "SELECT p.*, g.goods_code, g.goods_name, g.spec, g.unit, l.loc_code, l.loc_name,"
            " o.order_no, o.buyer_name, o.status AS order_status"
            " FROM ec_pick_task p LEFT JOIN base_goods g ON g.id=p.goods_id"
            " LEFT JOIN base_location l ON l.id=p.loc_id LEFT JOIN ec_order o ON o.id=p.order_id"
            " WHERE p.wave_id=? ORDER BY l.loc_code, p.id", (wid,))]
        data["orders"] = [dict(r) for r in conn.execute(
            "SELECT wo.*, o.order_no, o.buyer_name, o.pay_amount, o.status AS order_status,"
            " (SELECT COUNT(*) FROM ec_order_item i WHERE i.order_id=o.id) AS item_count,"
            " (SELECT IFNULL(SUM(qty),0) FROM ec_order_item i WHERE i.order_id=o.id) AS total_qty"
            " FROM ec_wave_order wo JOIN ec_order o ON o.id=wo.order_id"
            " WHERE wo.wave_id=? ORDER BY o.id", (wid,))]
        return data
    finally:
        conn.close()


@route("POST", r"/api/v1/ec/waves")
def api_wave_create(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = create_wave(conn, body.get("order_ids") or [], user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/ec/waves/(\d+)/pick")
def api_wave_pick(handler, m, q, body):
    """body: {all:true} 整波拣完；或 {task_id: 1, picked_qty: 2} 逐条拣"""
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = pick_wave_tasks(conn, int(m.group(1)), user, task_id=body.get("task_id"),
                                 picked_qty=body.get("picked_qty"), all_tasks=bool(body.get("all")))
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/ec/waves/(\d+)/review")
def api_wave_review(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = review_wave_order(conn, int(m.group(1)), int(body.get("order_id")), user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/ec/waves/(\d+)/cancel")
def api_wave_cancel(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = cancel_wave(conn, int(m.group(1)), user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/ec/orders/(\d+)/pack")
def api_ec_pack(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = pack_ec_order(conn, int(m.group(1)), body.get("weight_g"), body.get("remark"), user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# SPU / SKU（商品两级模型）
# --------------------------------------------------------------------------- #
@route("GET", r"/api/v1/spus")
def api_spus(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT s.*, c.category_name FROM base_spu s LEFT JOIN base_goods_category c ON c.id=s.category_id"
            " ORDER BY s.spu_code")]
        for r in rows:
            r["skus"] = [dict(x) for x in conn.execute(
                "SELECT g.id, g.goods_code, g.goods_name, g.spec, g.unit, g.cost_price, g.spec_attrs, g.status,"
                " IFNULL((SELECT SUM(quantity) FROM wms_inventory i WHERE i.goods_id=g.id),0) AS stock_qty"
                " FROM base_goods g WHERE g.spu_id=? ORDER BY g.goods_code", (r["id"],))]
        return {"list": rows}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 运维域接口：健康 / 指标 / 一致性校验 / 备份 / 参数
# --------------------------------------------------------------------------- #
@route("GET", r"/api/v1/ops/health")
def api_ops_health(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        tables = ["base_goods", "base_location", "wms_inventory", "wms_inventory_txn", "wms_in_order",
                  "wms_out_order", "ec_order", "ec_order_alloc", "ec_shipment", "ec_after_sale",
                  "sys_operation_log", "sys_config"]
        counts = {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in tables}
        usage = shutil.disk_usage(os.path.dirname(DB_PATH) or ".")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        return {
            "service": "wms", "version": VERSION, "status": "ok" if integrity == "ok" else "degraded",
            "env": WMS_ENV, "python": sys.version.split()[0],
            "uptimeSeconds": int(time.time() - START_TIME),
            "startedAt": datetime.fromtimestamp(START_TIME).strftime("%Y-%m-%d %H:%M:%S"),
            "dbPath": DB_PATH, "dbSizeBytes": db_size_bytes(),
            "diskFreeBytes": usage.free, "diskTotalBytes": usage.total,
            "sqliteIntegrity": integrity, "tableRows": counts,
        }
    finally:
        conn.close()


@route("GET", r"/api/v1/ops/metrics")
def api_ops_metrics(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        month = datetime.now().strftime("%Y-%m")
        one = lambda sql, args=(): conn.execute(sql, args).fetchone()[0]
        ship_rows = conn.execute(
            "SELECT ship_time, pay_time FROM ec_order WHERE status IN (3,4,6) AND ship_time IS NOT NULL"
            " AND pay_time IS NOT NULL").fetchall()
        hours = []
        for r in ship_rows:
            try:
                a = datetime.strptime(r["pay_time"], "%Y-%m-%d %H:%M:%S")
                b = datetime.strptime(r["ship_time"], "%Y-%m-%d %H:%M:%S")
                hours.append((b - a).total_seconds() / 3600.0)
            except (TypeError, ValueError):
                continue
        low_stock = conn.execute(
            "SELECT COUNT(*) FROM base_goods g JOIN wms_stock_policy p ON p.goods_id=g.id AND p.wh_id=0"
            " WHERE p.safety_stock>0 AND IFNULL((SELECT SUM(available_qty) FROM wms_inventory"
            " WHERE goods_id=g.id),0) < p.safety_stock").fetchone()[0]
        dead = conn.execute(
            "SELECT COUNT(*) FROM wms_inventory i WHERE i.quantity>0 AND NOT EXISTS ("
            " SELECT 1 FROM wms_inventory_txn t WHERE t.goods_id=i.goods_id AND t.loc_id=i.loc_id"
            " AND t.batch_no=i.batch_no AND t.change_qty<0"
            " AND date(t.create_time) >= date('now','-30 day'))").fetchone()[0]
        stale = conn.execute(
            "SELECT COUNT(*) FROM ec_order WHERE status IN (1,2)"
            " AND datetime(IFNULL(pay_time,create_time)) <= datetime('now','-1 day')").fetchone()[0]
        return {
            "todayOrders": one("SELECT COUNT(*) FROM ec_order WHERE date(create_time)=?", (today,)),
            "todaySales": DEC2(one("SELECT IFNULL(SUM(pay_amount),0) FROM ec_order WHERE date(create_time)=?"
                                   " AND status IN (1,2,3,4,6)", (today,))),
            "pendingShip": one("SELECT COUNT(*) FROM ec_order WHERE status IN (1,2)"),
            "pendingAfterSale": one("SELECT COUNT(*) FROM ec_after_sale WHERE status IN (0,1)"),
            "monthSales": DEC2(one("SELECT IFNULL(SUM(pay_amount),0) FROM ec_order"
                                   " WHERE substr(create_time,1,7)=? AND status IN (1,2,3,4,6)", (month,))),
            "avgShipHours": round(sum(hours) / len(hours), 2) if hours else 0,
            "lowStockSku": low_stock, "deadStockLines": dead, "staleAllocOrders": stale,
            "pendingOutOrders": one("SELECT COUNT(*) FROM wms_out_order WHERE status IN (0,1,2)"),
            "lockedQty": DEC2(one("SELECT IFNULL(SUM(locked_qty),0) FROM wms_inventory")),
            "canceledToday": one("SELECT COUNT(*) FROM ec_order WHERE date(create_time)=? AND status=5", (today,)),
        }
    finally:
        conn.close()


@route("GET", r"/api/v1/ops/checks")
def api_ops_checks(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        checks = consistency_checks(conn)
        for c in checks:
            c["level"] = "PASS" if c["passed"] else "FAIL"
        return {"allPassed": all(c["passed"] for c in checks), "checks": checks,
                "checkedAt": now()}
    finally:
        conn.close()


@route("GET", r"/api/v1/ops/backups")
def api_ops_backups(handler, m, q, body):
    current_user(handler)
    return {"list": _list_backups()}


def _backup_dir():
    return os.path.join(os.path.dirname(DB_PATH), "backups")


def _list_backups():
    path = _backup_dir()
    if not os.path.isdir(path):
        return []
    items = []
    for name in sorted(os.listdir(path), reverse=True):
        if not name.endswith(".db"):
            continue
        full = os.path.join(path, name)
        items.append({"name": name, "sizeBytes": os.path.getsize(full),
                      "createdAt": datetime.fromtimestamp(os.path.getmtime(full)).strftime("%Y-%m-%d %H:%M:%S")})
    return items


@route("POST", r"/api/v1/ops/backup")
def api_ops_backup(handler, m, q, body):
    user = current_user(handler)
    path = _backup_dir()
    os.makedirs(path, exist_ok=True)
    name = "wms-%s.db" % datetime.now().strftime("%Y%m%d-%H%M%S")
    full = os.path.join(path, name)
    src = db()
    try:
        dest = sqlite3.connect(full)
        try:
            src.backup(dest)
        finally:
            dest.close()
        conn = db()
        try:
            write_log(conn, user, "运维", "BACKUP", None, name)
            conn.commit()
        finally:
            conn.close()
    finally:
        src.close()
    return {"name": name, "sizeBytes": os.path.getsize(full),
            "createdAt": datetime.fromtimestamp(os.path.getmtime(full)).strftime("%Y-%m-%d %H:%M:%S")}


@route("GET", r"/api/v1/ops/config")
def api_ops_config(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        return {"list": [dict(r) for r in conn.execute("SELECT * FROM sys_config ORDER BY config_key")]}
    finally:
        conn.close()


@route("PUT", r"/api/v1/ops/config")
def api_ops_config_update(handler, m, q, body):
    """参数只能在白名单内改，避免运维误改出无法启动的配置"""
    user = current_user(handler)
    allowed = {"ec.free.shipping.amount", "ec.freight.fee", "ec.auto.cancel.hours",
               "inventory.fifo.enabled", "inventory.negative.allowed", "ec.channel.self.enabled"}
    conn = db()
    try:
        changes = {}
        for key, value in (body or {}).items():
            if key not in allowed:
                raise ApiError(40000, "参数 %s 不在可改白名单内" % key)
            set_config(conn, key, value)
            changes[key] = value
        if not changes:
            raise ApiError(40000, "没有要修改的参数")
        write_log(conn, user, "运维", "UPDATE", None, "config", changes)
        conn.commit()
        return {"changed": changes}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 系统管理接口（RBAC：用户 / 角色 / 权限）
# --------------------------------------------------------------------------- #
def _set_user_roles(conn, user_id, role_codes):
    conn.execute("DELETE FROM sys_user_role WHERE user_id=?", (user_id,))
    for code in role_codes or []:
        role = conn.execute("SELECT id FROM sys_role WHERE role_code=? AND status=1", (code,)).fetchone()
        if role:
            conn.execute("INSERT OR IGNORE INTO sys_user_role(user_id,role_id) VALUES(?,?)",
                         (user_id, role["id"]))


def _set_user_warehouses(conn, user_id, wh_ids):
    conn.execute("DELETE FROM sys_user_warehouse WHERE user_id=?", (user_id,))
    for wh in wh_ids or []:
        try:
            wh_id = int(wh)
        except (TypeError, ValueError):
            continue
        if conn.execute("SELECT 1 FROM base_location WHERE id=? AND loc_type=1", (wh_id,)).fetchone():
            conn.execute("INSERT OR IGNORE INTO sys_user_warehouse(user_id,wh_id) VALUES(?,?)", (user_id, wh_id))


@route("GET", r"/api/v1/sys/users")
def api_sys_users(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        users = []
        for r in conn.execute("SELECT id,username,real_name,phone,status,create_time FROM sys_user ORDER BY id"):
            item = dict(r)
            item["roles"] = [dict(x) for x in conn.execute(
                "SELECT r.role_code, r.role_name FROM sys_user_role ur JOIN sys_role r ON r.id=ur.role_id"
                " WHERE ur.user_id=? ORDER BY r.id", (r["id"],))]
            item["roleCodes"] = [x["role_code"] for x in item["roles"]]
            item["whIds"] = [x["wh_id"] for x in conn.execute(
                "SELECT wh_id FROM sys_user_warehouse WHERE user_id=? ORDER BY wh_id", (r["id"],))]
            users.append(item)
        roles = []
        for r in conn.execute("SELECT * FROM sys_role WHERE status=1 ORDER BY id"):
            item = dict(r)
            item["userCount"] = conn.execute("SELECT COUNT(*) FROM sys_user_role WHERE role_id=?",
                                             (r["id"],)).fetchone()[0]
            item["permCount"] = conn.execute("SELECT COUNT(*) FROM sys_role_permission WHERE role_id=?",
                                             (r["id"],)).fetchone()[0]
            item["perms"] = [x["perm_code"] for x in conn.execute(
                "SELECT p.perm_code FROM sys_role_permission rp JOIN sys_permission p ON p.id=rp.permission_id"
                " WHERE rp.role_id=? ORDER BY p.sort_no", (r["id"],))]
            roles.append(item)
        warehouses = [dict(r) for r in conn.execute(
            "SELECT id, loc_code, loc_name FROM base_location WHERE loc_type=1 AND status=1 ORDER BY loc_code")]
        return {"list": users, "roles": roles, "warehouses": warehouses}
    finally:
        conn.close()


@route("POST", r"/api/v1/sys/users")
def api_sys_user_create(handler, m, q, body):
    user = current_user(handler)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    real_name = (body.get("real_name") or "").strip() or username
    if not username:
        raise ApiError(40000, "登录名必填")
    if len(password) < 6:
        raise ApiError(40000, "密码至少 6 位")
    conn = db()
    try:
        if conn.execute("SELECT 1 FROM sys_user WHERE username=?", (username,)).fetchone():
            raise ApiError(30001, "登录名已存在", 409)
        pwd, salt = hash_password(password)
        cur = conn.execute("INSERT INTO sys_user(username,password_hash,salt,real_name,phone,status)"
                           " VALUES(?,?,?,?,?,?)",
                           (username, pwd, salt, real_name, body.get("phone"), int(body.get("status", 1))))
        uid = cur.lastrowid
        _set_user_roles(conn, uid, body.get("role_codes") or ["viewer"])
        _set_user_warehouses(conn, uid, body.get("wh_ids") or [])
        write_log(conn, user, "系统管理", "CREATE", uid, username,
                  {"roles": body.get("role_codes"), "whIds": body.get("wh_ids")})
        conn.commit()
        return {"id": uid, "username": username}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("PUT", r"/api/v1/sys/users/(\d+)")
def api_sys_user_update(handler, m, q, body):
    user = current_user(handler)
    uid = int(m.group(1))
    conn = db()
    try:
        row = conn.execute("SELECT * FROM sys_user WHERE id=?", (uid,)).fetchone()
        if not row:
            raise ApiError(40400, "用户不存在", 404)
        if uid == user["id"] and body.get("status") is not None and int(body.get("status")) == 0:
            raise ApiError(40001, "不能停用当前登录账号", 409)
        role_codes = body.get("role_codes")
        if row["username"] == "admin" and role_codes is not None and "admin" not in role_codes:
            raise ApiError(40001, "内置 admin 账号必须保留系统管理员角色", 409)
        conn.execute("UPDATE sys_user SET real_name=?, phone=?, status=? WHERE id=?",
                     (body.get("real_name") or row["real_name"], body.get("phone", row["phone"]),
                      int(body.get("status", row["status"])), uid))
        if body.get("password"):
            if len(body["password"]) < 6:
                raise ApiError(40000, "密码至少 6 位")
            pwd, salt = hash_password(body["password"])
            conn.execute("UPDATE sys_user SET password_hash=?, salt=?, pwd_update_time=? WHERE id=?",
                         (pwd, salt, now(), uid))
        if role_codes is not None:
            _set_user_roles(conn, uid, role_codes)
        if body.get("wh_ids") is not None:
            _set_user_warehouses(conn, uid, body.get("wh_ids"))
        write_log(conn, user, "系统管理", "UPDATE", uid, row["username"],
                  {"roles": role_codes, "status": body.get("status"),
                   "resetPwd": bool(body.get("password"))})
        conn.commit()
        return {"id": uid}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("GET", r"/api/v1/sys/roles")
def api_sys_roles(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        roles = []
        for r in conn.execute("SELECT * FROM sys_role ORDER BY id"):
            item = dict(r)
            item["perms"] = [x["perm_code"] for x in conn.execute(
                "SELECT p.perm_code FROM sys_role_permission rp JOIN sys_permission p ON p.id=rp.permission_id"
                " WHERE rp.role_id=? ORDER BY p.sort_no", (r["id"],))]
            item["userCount"] = conn.execute("SELECT COUNT(*) FROM sys_user_role WHERE role_id=?",
                                             (r["id"],)).fetchone()[0]
            roles.append(item)
        catalog = [dict(r) for r in conn.execute(
            "SELECT perm_code, perm_name, perm_type, module FROM sys_permission ORDER BY sort_no")]
        return {"list": roles, "catalog": catalog}
    finally:
        conn.close()


@route("PUT", r"/api/v1/sys/roles/(\d+)")
def api_sys_role_update(handler, m, q, body):
    user = current_user(handler)
    rid = int(m.group(1))
    conn = db()
    try:
        row = conn.execute("SELECT * FROM sys_role WHERE id=?", (rid,)).fetchone()
        if not row:
            raise ApiError(40400, "角色不存在", 404)
        if row["role_code"] == "admin":
            raise ApiError(40001, "系统管理员角色固定为全部权限，不可修改", 409)
        codes = body.get("perm_codes")
        if codes is None:
            raise ApiError(40000, "请提交权限码列表 perm_codes")
        conn.execute("DELETE FROM sys_role_permission WHERE role_id=?", (rid,))
        for code in codes:
            perm = conn.execute("SELECT id FROM sys_permission WHERE perm_code=?", (code,)).fetchone()
            if perm:
                conn.execute("INSERT OR IGNORE INTO sys_role_permission(role_id,permission_id) VALUES(?,?)",
                             (rid, perm["id"]))
        write_log(conn, user, "系统管理", "UPDATE", rid, row["role_name"], {"permCount": len(codes)})
        conn.commit()
        return {"id": rid, "permCount": len(codes)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("GET", r"/api/v1/sys/perms")
def api_sys_perms(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT perm_code, perm_name, perm_type, module FROM sys_permission ORDER BY sort_no")]
        grouped = {}
        for r in rows:
            grouped.setdefault(r["module"], []).append(r)
        return {"list": rows, "groups": [{"module": k, "perms": v} for k, v in grouped.items()]}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# V5 作业与核算：成本 / 盘点 / 调拨 / 批处理任务 / 报表
#
# 三个主线在这里补齐（主流 WMS 必备，之前只有表或完全没有）：
#   ① 成本：移动加权平均价（base_goods.avg_cost），入库更新均价、出库按均价结转，
#      流水表记 unit_cost / cost_amount —— 这样毛利才算得出来。
#   ② 盘点：生成账面快照 → 录实盘 → 提交 → 审核（差异按盘盈/盘亏过账，8/9 流水）。
#   ③ 调拨：审核即预占调出量 → 确认时同事务「调出减、调入加」两条流水（6/7），幂等。
# --------------------------------------------------------------------------- #
ST_STATUS = {0: "草稿", 1: "盘点中", 2: "待审核", 3: "已完成", 4: "已作废"}
TF_STATUS = {0: "草稿", 1: "已审核(待调拨)", 2: "已调拨", 3: "已作废"}
BIZ_TRANSFER_OUT, BIZ_TRANSFER_IN, BIZ_GAIN, BIZ_LOSS = 6, 7, 8, 9


def sku_avg_cost(conn, goods_id):
    row = conn.execute("SELECT avg_cost FROM base_goods WHERE id=?", (goods_id,)).fetchone()
    return DEC2(row["avg_cost"]) if row else 0.0


def post_txn_cost(conn, biz_type, biz_id, detail_id, goods_id, change_qty, before_qty, after_qty,
                  unit_cost=None):
    """给刚写入的流水补成本，并维护移动加权均价。

    · 入库（change>0）：new_avg = (before*avg + qty*unit) / after；unit 默认取商品成本价
    · 出库（change<0）：按当前均价结转，均价不变
    · 调拨（6/7）是仓内移动：调出按均价、调入同价，均价因此保持不变
    """
    avg = sku_avg_cost(conn, goods_id)
    if DEC2(change_qty) > 0:
        unit = DEC2(unit_cost if unit_cost not in (None, "") else avg)
        after = DEC2(after_qty)
        new_avg = DEC2((DEC2(before_qty) * avg + DEC2(change_qty) * unit) / after) if after > 0 else avg
        conn.execute("UPDATE base_goods SET avg_cost=? WHERE id=?", (new_avg, goods_id))
        amount = DEC2(DEC2(change_qty) * unit)
    else:
        unit = avg
        amount = DEC2(-DEC2(change_qty) * avg)
    conn.execute("UPDATE wms_inventory_txn SET unit_cost=?, cost_amount=?"
                 " WHERE biz_type=? AND biz_id=? AND detail_id=?",
                 (unit, amount, biz_type, biz_id, detail_id))
    return unit, amount


# ------------------------------ 盘点 ------------------------------
def _stocktake_numbers(conn):
    return next_no(conn, "wms_stocktake_order", "order_no", "PD")


def create_stocktake(conn, body, user):
    try:
        wh_id = int(body.get("wh_id"))
    except (TypeError, ValueError):
        raise ApiError(40000, "请选择盘点仓库")
    wh = conn.execute("SELECT * FROM base_location WHERE id=? AND loc_type=1 AND status=1", (wh_id,)).fetchone()
    if not wh:
        raise ApiError(40000, "仓库不存在或已停用")
    check_wh_scope(user, wh_id)
    stype = int(body.get("stocktake_type") or 1)
    if stype not in (1, 2):
        raise ApiError(40000, "盘点类型只能是 1 全盘 / 2 抽盘")
    loc_ids = [int(x) for x in (body.get("loc_ids") or [])] if stype == 2 else []
    if stype == 2 and not loc_ids:
        raise ApiError(40000, "抽盘请选择至少一个库位")
    where, args = "WHERE l.wh_id=? AND l.loc_type=4 AND i.quantity>0", [wh_id]
    if stype == 2:
        where += " AND i.loc_id IN (%s)" % ",".join("?" * len(loc_ids))
        args += loc_ids
    rows = conn.execute(
        "SELECT i.goods_id, i.loc_id, i.batch_no, i.quantity FROM wms_inventory i"
        " JOIN base_location l ON l.id=i.loc_id " + where + " ORDER BY l.loc_code, i.goods_id", args).fetchall()
    if not rows:
        raise ApiError(40001, "该范围没有可盘点的库存（先收货或换一个仓库）", 409)
    order_no = _stocktake_numbers(conn)
    cur = conn.execute(
        "INSERT INTO wms_stocktake_order(order_no,stocktake_type,wh_id,status,plan_count,remark,create_by)"
        " VALUES(?,?,?,0,?,?,?)",
        (order_no, stype, wh_id, len(rows), body.get("remark"), user["id"]))
    oid = cur.lastrowid
    for r in rows:
        conn.execute("INSERT INTO wms_stocktake_detail(order_id,goods_id,loc_id,batch_no,book_qty)"
                     " VALUES(?,?,?,?,?)", (oid, r["goods_id"], r["loc_id"], r["batch_no"], DEC2(r["quantity"])))
    write_log(conn, user, "库存盘点", "CREATE", oid, order_no, {"plan": len(rows), "type": stype})
    return {"id": oid, "orderNo": order_no, "planCount": len(rows), "status": 0}


def save_stocktake(conn, order_id, lines, user):
    """录入实盘数（可多次保存）；差异 = 实盘 − 账面。"""
    order = conn.execute("SELECT * FROM wms_stocktake_order WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ApiError(40400, "盘点单不存在", 404)
    if order["status"] not in (0, 1):
        raise ApiError(40001, "只有草稿/盘点中的单可以录入实盘数", 409)
    if not lines:
        raise ApiError(40000, "没有要保存的盘点行")
    for line in lines:
        try:
            did = int(line.get("detail_id"))
            actual = DEC2(line.get("actual_qty"))
        except (TypeError, ValueError):
            raise ApiError(40000, "盘点行或实盘数无效")
        if actual < 0:
            raise ApiError(40000, "实盘数不能为负")
        d = conn.execute("SELECT * FROM wms_stocktake_detail WHERE id=? AND order_id=?",
                         (did, order_id)).fetchone()
        if not d:
            raise ApiError(40400, "盘点明细不存在", 404)
        diff = DEC2(actual - DEC2(d["book_qty"]))
        conn.execute("UPDATE wms_stocktake_detail SET actual_qty=?, diff_qty=?, diff_reason=?, update_time=?"
                     " WHERE id=?", (actual, diff, line.get("diff_reason"), now(), did))
    _refresh_stocktake_stat(conn, order_id)
    conn.execute("UPDATE wms_stocktake_order SET status=1 WHERE id=? AND status=0", (order_id,))
    write_log(conn, user, "库存盘点", "UPDATE", order_id, order["order_no"], {"lines": len(lines)})
    return {"id": order_id, "saved": len(lines)}


def _refresh_stocktake_stat(conn, order_id):
    stat = conn.execute(
        "SELECT COUNT(*) total, SUM(CASE WHEN actual_qty IS NOT NULL THEN 1 ELSE 0 END) done,"
        " SUM(CASE WHEN actual_qty IS NOT NULL AND diff_qty<>0 THEN 1 ELSE 0 END) diff,"
        " IFNULL(SUM(CASE WHEN diff_qty>0 THEN diff_qty ELSE 0 END),0) gin,"
        " IFNULL(SUM(CASE WHEN diff_qty<0 THEN -diff_qty ELSE 0 END),0) gout"
        " FROM wms_stocktake_detail WHERE order_id=?", (order_id,)).fetchone()
    conn.execute("UPDATE wms_stocktake_order SET actual_count=?, diff_count=?, diff_in_qty=?, diff_out_qty=?"
                 " WHERE id=?",
                 (int(stat["done"] or 0), int(stat["diff"] or 0), DEC2(stat["gin"]), DEC2(stat["gout"]),
                  order_id))
    return stat


def submit_stocktake(conn, order_id, user):
    order = conn.execute("SELECT * FROM wms_stocktake_order WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ApiError(40400, "盘点单不存在", 404)
    if order["status"] not in (0, 1):
        raise ApiError(40001, "单据状态不允许提交", 409)
    stat = _refresh_stocktake_stat(conn, order_id)
    if (stat["done"] or 0) < (stat["total"] or 0):
        raise ApiError(40001, "还有 %d 行没有录入实盘数，不能提交"
                       % ((stat["total"] or 0) - (stat["done"] or 0)), 409)
    conn.execute("UPDATE wms_stocktake_order SET status=2, update_time=? WHERE id=?", (now(), order_id))
    write_log(conn, user, "库存盘点", "SUBMIT", order_id, order["order_no"],
              {"diff": int(stat["diff"] or 0)})
    return {"id": order_id, "status": 2}


def audit_stocktake(conn, order_id, user):
    """审核：把差异过账——盘盈加库存(8)，盘亏减库存(9)，同时写流水。"""
    order = conn.execute("SELECT * FROM wms_stocktake_order WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ApiError(40400, "盘点单不存在", 404)
    if order["status"] == 3:
        return {"id": order_id, "status": 3, "duplicated": True}
    if order["status"] != 2:
        raise ApiError(40001, "只有待审核的盘点单可以审核", 409)
    details = conn.execute("SELECT * FROM wms_stocktake_detail WHERE order_id=? AND diff_qty<>0 ORDER BY id",
                           (order_id,)).fetchall()
    posted = 0
    for d in details:
        diff = DEC2(d["diff_qty"])
        inv = conn.execute("SELECT * FROM wms_inventory WHERE goods_id=? AND loc_id=? AND batch_no=?",
                           (d["goods_id"], d["loc_id"], d["batch_no"])).fetchone()
        if not inv:
            raise ApiError(50001, "库存行已不存在（可能被其他单据改动），请重新建盘点单", 409)
        before = DEC2(inv["quantity"])
        after = DEC2(before + diff)
        if after < DEC2(inv["locked_qty"]):
            raise ApiError(40001, "盘亏 %.3f 后可用量会变成负数（预占 %s），请先处理预占再审核"
                           % (-diff, DEC2(inv["locked_qty"])), 409)
        cur = conn.execute(
            "UPDATE wms_inventory SET quantity=?, available_qty=?, version=version+1, update_time=?"
            " WHERE id=? AND version=?",
            (after, DEC2(after - inv["locked_qty"]), now(), inv["id"], inv["version"]))
        if cur.rowcount == 0:
            raise ApiError(50202, "库存并发冲突，请重试", 409)
        biz = BIZ_GAIN if diff > 0 else BIZ_LOSS
        conn.execute(
            "INSERT INTO wms_inventory_txn(txn_no,biz_type,biz_id,biz_no,detail_id,goods_id,loc_id,batch_no,"
            "change_qty,before_qty,after_qty,create_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (next_txn_no(conn), biz, order_id, order["order_no"], d["id"], d["goods_id"], d["loc_id"],
             d["batch_no"], diff, before, after, user["id"]))
        post_txn_cost(conn, biz, order_id, d["id"], d["goods_id"], diff, before, after)
        posted += 1
    conn.execute("UPDATE wms_stocktake_order SET status=3, audit_by=?, audit_time=?, update_time=? WHERE id=?",
                 (user["id"], now(), now(), order_id))
    write_log(conn, user, "库存盘点", "AUDIT", order_id, order["order_no"],
              {"postedLines": posted, "diffIn": DEC2(order["diff_in_qty"]), "diffOut": DEC2(order["diff_out_qty"])})
    return {"id": order_id, "status": 3, "postedLines": posted}


def cancel_stocktake(conn, order_id, user):
    order = conn.execute("SELECT * FROM wms_stocktake_order WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ApiError(40400, "盘点单不存在", 404)
    if order["status"] == 3:
        raise ApiError(50301, "单据已完成，不能作废", 409)
    conn.execute("UPDATE wms_stocktake_order SET status=4, update_time=? WHERE id=?", (now(), order_id))
    write_log(conn, user, "库存盘点", "CANCEL", order_id, order["order_no"])
    return {"id": order_id, "status": 4}


# ------------------------------ 调拨 ------------------------------
def create_transfer(conn, body, user):
    try:
        from_loc = int(body.get("from_loc_id"))
        to_loc = int(body.get("to_loc_id"))
        goods_id = int(body.get("goods_id"))
        qty = positive_qty(body.get("qty"), "调拨数量")
    except (TypeError, ValueError):
        raise ApiError(40000, "调拨参数不完整")
    if from_loc == to_loc:
        raise ApiError(40000, "调出库位与调入库位不能相同")
    from_wh = warehouse_for_location(conn, from_loc)
    to_wh = warehouse_for_location(conn, to_loc)
    check_wh_scope(user, from_wh["id"])
    if not conn.execute("SELECT 1 FROM base_goods WHERE id=? AND status=1", (goods_id,)).fetchone():
        raise ApiError(40000, "商品不存在或已停用")
    batch_no = (body.get("batch_no") or "").strip()
    inv = conn.execute("SELECT * FROM wms_inventory WHERE goods_id=? AND loc_id=? AND batch_no=?",
                       (goods_id, from_loc, batch_no)).fetchone()
    if not inv or DEC2(inv["available_qty"]) < qty:
        raise ApiError(50201, "调出库位可用量不足（可用 %s，需 %s）"
                       % (DEC2(inv["available_qty"]) if inv else 0, qty), 409)
    order_no = next_no(conn, "wms_transfer_order", "order_no", "DB")
    cur = conn.execute(
        "INSERT INTO wms_transfer_order(order_no,from_wh_id,to_wh_id,status,total_qty,remark,create_by)"
        " VALUES(?,?,?,0,?,?,?)", (order_no, from_wh["id"], to_wh["id"], qty, body.get("remark"), user["id"]))
    oid = cur.lastrowid
    conn.execute("INSERT INTO wms_transfer_detail(order_id,goods_id,from_loc_id,to_loc_id,batch_no,qty)"
                 " VALUES(?,?,?,?,?,?)", (oid, goods_id, from_loc, to_loc, batch_no, qty))
    write_log(conn, user, "库存调拨", "CREATE", oid, order_no, {"qty": qty})
    return {"id": oid, "orderNo": order_no, "status": 0}


def audit_transfer(conn, order_id, user):
    """审核 = 锁定调出量（locked_qty+），避免确认时被别的单抢走。"""
    order = conn.execute("SELECT * FROM wms_transfer_order WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ApiError(40400, "调拨单不存在", 404)
    if order["status"] != 0:
        raise ApiError(40001, "只有草稿单可以审核", 409)
    for d in conn.execute("SELECT * FROM wms_transfer_detail WHERE order_id=?", (order_id,)):
        qty = DEC2(d["qty"])
        inv = conn.execute("SELECT * FROM wms_inventory WHERE goods_id=? AND loc_id=? AND batch_no=?",
                           (d["goods_id"], d["from_loc_id"], d["batch_no"])).fetchone()
        if not inv or DEC2(inv["available_qty"]) < qty:
            raise ApiError(50201, "可用量不足，审核失败（可用 %s，需 %s）"
                           % (DEC2(inv["available_qty"]) if inv else 0, qty), 409)
        cur = conn.execute(
            "UPDATE wms_inventory SET locked_qty=locked_qty+?, available_qty=available_qty-?,"
            " version=version+1, update_time=? WHERE id=? AND version=? AND available_qty>=?",
            (qty, qty, now(), inv["id"], inv["version"], qty))
        if cur.rowcount == 0:
            raise ApiError(50202, "库存并发冲突，请重试", 409)
    conn.execute("UPDATE wms_transfer_order SET status=1, audit_by=?, audit_time=?, update_time=? WHERE id=?",
                 (user["id"], now(), now(), order_id))
    write_log(conn, user, "库存调拨", "AUDIT", order_id, order["order_no"])
    return {"id": order_id, "status": 1}


def confirm_transfer(conn, order_id, request_id, user):
    """确认调拨：同一事务内调出减、调入加，写 6/7 两条流水。幂等。"""
    order = conn.execute("SELECT * FROM wms_transfer_order WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ApiError(40400, "调拨单不存在", 404)
    if order["status"] == 2:
        return {"id": order_id, "status": 2, "duplicated": True}
    if order["status"] != 1:
        raise ApiError(40001, "只有已审核的调拨单可以确认", 409)
    details = conn.execute("SELECT * FROM wms_transfer_detail WHERE order_id=? ORDER BY id", (order_id,)).fetchall()
    moved = 0.0
    for d in details:
        qty = DEC2(d["qty"])
        # 调出
        src = conn.execute("SELECT * FROM wms_inventory WHERE goods_id=? AND loc_id=? AND batch_no=?",
                           (d["goods_id"], d["from_loc_id"], d["batch_no"])).fetchone()
        if not src or DEC2(src["locked_qty"]) < qty:
            raise ApiError(50201, "调出预占已失效，请重新审核", 409)
        q_after = DEC2(src["quantity"] - qty)
        l_after = DEC2(src["locked_qty"] - qty)
        cur = conn.execute(
            "UPDATE wms_inventory SET quantity=?, locked_qty=?, available_qty=?, version=version+1,"
            " update_time=? WHERE id=? AND version=? AND quantity>=? AND locked_qty>=?",
            (q_after, l_after, DEC2(q_after - l_after), now(), src["id"], src["version"], qty, qty))
        if cur.rowcount == 0:
            raise ApiError(50202, "库存并发冲突，请重试", 409)
        conn.execute(
            "INSERT INTO wms_inventory_txn(txn_no,biz_type,biz_id,biz_no,detail_id,goods_id,loc_id,batch_no,"
            "change_qty,before_qty,after_qty,create_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (next_txn_no(conn), BIZ_TRANSFER_OUT, order_id, order["order_no"], d["id"], d["goods_id"],
             d["from_loc_id"], d["batch_no"], -qty, DEC2(src["quantity"]), q_after, user["id"]))
        unit, _amount = post_txn_cost(conn, BIZ_TRANSFER_OUT, order_id, d["id"], d["goods_id"],
                                      -qty, DEC2(src["quantity"]), q_after)
        # 调入
        dst = conn.execute("SELECT * FROM wms_inventory WHERE goods_id=? AND loc_id=? AND batch_no=?",
                           (d["goods_id"], d["to_loc_id"], d["batch_no"])).fetchone()
        if dst:
            d_after = DEC2(dst["quantity"] + qty)
            cur = conn.execute(
                "UPDATE wms_inventory SET quantity=?, available_qty=?, version=version+1, update_time=?"
                " WHERE id=? AND version=?",
                (d_after, DEC2(d_after - dst["locked_qty"]), now(), dst["id"], dst["version"]))
            if cur.rowcount == 0:
                raise ApiError(50202, "库存并发冲突，请重试", 409)
            d_before = DEC2(dst["quantity"])
        else:
            d_before = 0.0
            conn.execute("INSERT INTO wms_inventory(goods_id,loc_id,batch_no,quantity,locked_qty,"
                         "available_qty,version,in_time) VALUES(?,?,?,?,0,?,0,?)",
                         (d["goods_id"], d["to_loc_id"], d["batch_no"], qty, qty, now()))
        conn.execute(
            "INSERT INTO wms_inventory_txn(txn_no,biz_type,biz_id,biz_no,detail_id,goods_id,loc_id,batch_no,"
            "change_qty,before_qty,after_qty,create_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (next_txn_no(conn), BIZ_TRANSFER_IN, order_id, order["order_no"], d["id"], d["goods_id"],
             d["to_loc_id"], d["batch_no"], qty, d_before, DEC2(d_before + qty), user["id"]))
        post_txn_cost(conn, BIZ_TRANSFER_IN, order_id, d["id"], d["goods_id"], qty, d_before,
                      DEC2(d_before + qty), unit_cost=unit)
        moved = DEC2(moved + qty)
    conn.execute("UPDATE wms_transfer_order SET status=2, request_id=?, update_time=? WHERE id=?",
                 (request_id, now(), order_id))
    write_log(conn, user, "库存调拨", "CONFIRM", order_id, order["order_no"], {"moved": moved})
    return {"id": order_id, "status": 2, "movedQty": moved}


def cancel_transfer(conn, order_id, reason, user):
    order = conn.execute("SELECT * FROM wms_transfer_order WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ApiError(40400, "调拨单不存在", 404)
    if order["status"] == 2:
        raise ApiError(50301, "已调拨，不能作废", 409)
    if order["status"] == 1:
        for d in conn.execute("SELECT * FROM wms_transfer_detail WHERE order_id=?", (order_id,)):
            qty = DEC2(d["qty"])
            inv = conn.execute("SELECT * FROM wms_inventory WHERE goods_id=? AND loc_id=? AND batch_no=?",
                               (d["goods_id"], d["from_loc_id"], d["batch_no"])).fetchone()
            if not inv:
                continue
            l_after = DEC2(max(0.0, DEC2(inv["locked_qty"]) - qty))
            conn.execute("UPDATE wms_inventory SET locked_qty=?, available_qty=?, version=version+1,"
                         " update_time=? WHERE id=?",
                         (l_after, DEC2(DEC2(inv["quantity"]) - l_after), now(), inv["id"]))
    conn.execute("UPDATE wms_transfer_order SET status=3, cancel_reason=?, update_time=? WHERE id=?",
                 (reason, now(), order_id))
    write_log(conn, user, "库存调拨", "CANCEL", order_id, order["order_no"], {"reason": reason})
    return {"id": order_id, "status": 3}


# ------------------------------ 批处理任务 ------------------------------
def run_jobs(conn, user=None):
    """启动时与运维手动触发都会跑：超时未支付关单（释放预占）。返回执行摘要。"""
    results = []
    # ① 未支付超时关单
    hours = float(get_config(conn, "ec.auto.cancel.hours", "24") or 24)
    rows = conn.execute(
        "SELECT * FROM ec_order WHERE status=0 AND datetime(create_time) <= datetime('now', ?)",
        ("-%d hours" % int(hours),)).fetchall()
    closed = 0
    for o in rows:
        cancel_ec_order(conn, o["id"], "超过 %d 小时未支付，系统自动关闭" % int(hours), user or {"id": 1})
        closed += 1
    conn.execute("INSERT INTO sys_job_log(job_code,affected,detail) VALUES('AUTO_CANCEL_UNPAID',?,?)",
                 (closed, "阈值 %d 小时" % int(hours)))
    results.append({"job": "AUTO_CANCEL_UNPAID", "affected": closed})
    # ② 预占超时提醒（只统计，不自动释放——释放预占必须人工确认）
    lock_hours = float(get_config(conn, "inventory.lock.expire.hours", "24") or 24)
    stale = conn.execute(
        "SELECT COUNT(*) FROM ec_order WHERE status IN (1,2) AND datetime(IFNULL(pay_time,create_time))"
        " <= datetime('now', ?)", ("-%d hours" % int(lock_hours),)).fetchone()[0]
    conn.execute("INSERT INTO sys_job_log(job_code,affected,detail) VALUES('STALE_ALLOC_SCAN',?,?)",
                 (stale, "阈值 %d 小时，需人工处理" % int(lock_hours)))
    results.append({"job": "STALE_ALLOC_SCAN", "affected": stale})
    # ③ 临期批次扫描（30 天内到期且还有库存）
    expiring = conn.execute(
        "SELECT COUNT(*) FROM wms_inventory WHERE quantity>0 AND expire_date IS NOT NULL"
        " AND date(expire_date) <= date('now','+30 day')").fetchone()[0]
    conn.execute("INSERT INTO sys_job_log(job_code,affected,detail) VALUES('EXPIRY_SCAN',?,?)",
                 (expiring, "30 天内到期"))
    results.append({"job": "EXPIRY_SCAN", "affected": expiring})
    # ④ WAL 检查点（长压测后 WAL 会变大，及时截断避免读放大）
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        results.append({"job": "WAL_CHECKPOINT", "affected": 0, "detail": "已做被动检查点"})
    except Exception as exc:  # noqa: BLE001
        results.append({"job": "WAL_CHECKPOINT", "affected": 0, "detail": str(exc)[:120]})

    # ⑤ 日汇总重算（今天 + 昨天，补齐跨零点提交的流水）
    try:
        days = [datetime.now().strftime("%Y-%m-%d"),
                (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")]
        total = sum(rebuild_daily_stat(conn, d) for d in days)
        conn.commit()
        results.append({"job": "STAT_REFRESH", "affected": total, "detail": "日汇总已重算"})
    except Exception as exc:  # noqa: BLE001
        results.append({"job": "STAT_REFRESH", "affected": 0, "detail": str(exc)[:120]})

    return results


# ------------------------------ 报表 ------------------------------
def report_stock_summary(conn, begin, end):
    """收发存汇总：期初 + 期间入 − 期间出 ± 调整 = 期末（并与当前库存对账）。"""
    rows = conn.execute(
        "SELECT g.id goods_id, g.goods_code, g.goods_name, g.unit,"
        " IFNULL((SELECT SUM(change_qty) FROM wms_inventory_txn t WHERE t.goods_id=g.id"
        "         AND date(t.create_time) < ?),0) AS begin_qty,"
        " IFNULL((SELECT SUM(change_qty) FROM wms_inventory_txn t WHERE t.goods_id=g.id"
        "         AND date(t.create_time) BETWEEN ? AND ? AND t.change_qty>0),0) AS in_qty,"
        " IFNULL((SELECT SUM(-change_qty) FROM wms_inventory_txn t WHERE t.goods_id=g.id"
        "         AND date(t.create_time) BETWEEN ? AND ? AND t.change_qty<0),0) AS out_qty,"
        " IFNULL((SELECT SUM(change_qty) FROM wms_inventory_txn t WHERE t.goods_id=g.id"
        "         AND date(t.create_time) <= ?),0) AS end_qty,"
        " IFNULL((SELECT SUM(quantity) FROM wms_inventory i WHERE i.goods_id=g.id),0) AS current_qty,"
        " g.avg_cost"
        " FROM base_goods g WHERE g.status=1 ORDER BY g.goods_code",
        (begin, begin, end, begin, end, end)).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["diff_qty"] = DEC2(DEC2(item["end_qty"]) - DEC2(item["current_qty"]))
        item["amount"] = DEC2(DEC2(item["end_qty"]) * DEC2(item["avg_cost"]))
        out.append(item)
    return out


def report_profit(conn, begin, end):
    """订单毛利：销售额（支付金额）− 出库成本（发货流水的 cost_amount）。"""
    rows = conn.execute(
        "SELECT o.id, o.order_no, o.buyer_name, o.pay_amount, o.ship_time, o.status,"
        " IFNULL((SELECT SUM(t.cost_amount) FROM wms_inventory_txn t WHERE t.biz_type=21 AND t.biz_id=o.id),0)"
        "   AS cost_amount,"
        " IFNULL((SELECT SUM(-t.change_qty) FROM wms_inventory_txn t WHERE t.biz_type=21 AND t.biz_id=o.id),0)"
        "   AS shipped_qty"
        " FROM ec_order o WHERE o.status IN (3,4,6) AND date(IFNULL(o.ship_time, o.create_time)) BETWEEN ? AND ?"
        " ORDER BY o.ship_time DESC", (begin, end)).fetchall()
    lst, total_sales, total_cost = [], 0.0, 0.0
    for r in rows:
        sales = DEC2(r["pay_amount"])
        cost = DEC2(r["cost_amount"])
        profit = DEC2(sales - cost)
        total_sales = DEC2(total_sales + sales)
        total_cost = DEC2(total_cost + cost)
        item = dict(r)
        item["profit"] = profit
        item["margin"] = DEC2(profit / sales * 100) if sales else 0
        lst.append(item)
    return {"list": lst, "totalSales": total_sales, "totalCost": total_cost,
            "totalProfit": DEC2(total_sales - total_cost),
            "profitRate": DEC2((total_sales - total_cost) / total_sales * 100) if total_sales else 0}


def report_turnover(conn, days=30):
    """库存周转/动销：近 N 天出库量、日均、可周转天数、是否呆滞。"""
    rows = conn.execute(
        "SELECT g.id goods_id, g.goods_code, g.goods_name, g.unit, g.avg_cost,"
        " IFNULL((SELECT SUM(quantity) FROM wms_inventory i WHERE i.goods_id=g.id),0) AS stock_qty,"
        " IFNULL((SELECT SUM(-change_qty) FROM wms_inventory_txn t WHERE t.goods_id=g.id AND t.change_qty<0"
        "         AND date(t.create_time) >= date('now', ?)),0) AS out_qty"
        " FROM base_goods g WHERE g.status=1 ORDER BY g.goods_code", ("-%d day" % days,)).fetchall()
    lst, active = [], 0
    for r in rows:
        item = dict(r)
        out_qty = DEC2(item["out_qty"])
        daily = DEC2(out_qty / days)
        item["dailyOut"] = daily
        item["turnoverDays"] = DEC2(DEC2(item["stock_qty"]) / daily) if daily > 0 else None
        item["slowMoving"] = out_qty <= 0 and DEC2(item["stock_qty"]) > 0
        if out_qty > 0:
            active += 1
        lst.append(item)
    total = len(rows)
    return {"list": lst, "days": days, "skuCount": total, "activeSku": active,
            "activeRate": DEC2(active / total * 100) if total else 0}


def report_expiring(conn, days=30):
    rows = conn.execute(
        "SELECT i.*, g.goods_code, g.goods_name, g.unit, l.loc_code, l.loc_name,"
        " CAST(julianday(date(i.expire_date)) - julianday(date('now')) AS INTEGER) AS days_left"
        " FROM wms_inventory i JOIN base_goods g ON g.id=i.goods_id JOIN base_location l ON l.id=i.loc_id"
        " WHERE i.quantity>0 AND i.expire_date IS NOT NULL AND date(i.expire_date) <= date('now', ?)"
        " ORDER BY i.expire_date", ("+%d day" % days,)).fetchall()
    lst = []
    for r in rows:
        item = dict(r)
        item["expired"] = (item["days_left"] or 0) < 0
        lst.append(item)
    return {"list": lst, "days": days,
            "expiredCount": len([x for x in lst if x["expired"]])}


# --------------------------------------------------------------------------- #
# V5 接口：盘点 / 调拨 / 批处理 / 报表
# --------------------------------------------------------------------------- #
@route("GET", r"/api/v1/stocktakes")
def api_stocktakes(handler, m, q, body):
    user = current_user(handler)
    page, size, offset = paging(q)
    conn = db()
    try:
        where, args = "WHERE 1=1", []
        if q.get("status", [""])[0] != "":
            where += " AND o.status=?"
            args.append(int(q["status"][0]))
        where, args = add_wh_scope(where, args, user, "o.wh_id")
        total = conn.execute("SELECT COUNT(*) FROM wms_stocktake_order o " + where, args).fetchone()[0]
        rows = conn.execute(
            "SELECT o.*, l.loc_name AS wh_name, u.real_name AS audit_name FROM wms_stocktake_order o"
            " LEFT JOIN base_location l ON l.id=o.wh_id LEFT JOIN sys_user u ON u.id=o.audit_by "
            + where + " ORDER BY o.id DESC LIMIT ? OFFSET ?", args + [size, offset]).fetchall()
        lst = []
        for r in rows:
            item = dict(r)
            item["statusText"] = ST_STATUS.get(item["status"], "?")
            item["typeText"] = "全盘" if item["stocktake_type"] == 1 else "抽盘"
            lst.append(item)
        return {"total": total, "page": page, "size": size, "list": lst}
    finally:
        conn.close()


@route("GET", r"/api/v1/stocktakes/(\d+)")
def api_stocktake_detail(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        sid = int(m.group(1))
        order = conn.execute("SELECT o.*, l.loc_name AS wh_name FROM wms_stocktake_order o"
                             " LEFT JOIN base_location l ON l.id=o.wh_id WHERE o.id=?", (sid,)).fetchone()
        if not order:
            raise ApiError(40400, "盘点单不存在", 404)
        data = dict(order)
        data["statusText"] = ST_STATUS.get(order["status"], "?")
        data["lines"] = [dict(r) for r in conn.execute(
            "SELECT d.*, g.goods_code, g.goods_name, g.unit, l.loc_code, l.loc_name"
            " FROM wms_stocktake_detail d JOIN base_goods g ON g.id=d.goods_id"
            " JOIN base_location l ON l.id=d.loc_id WHERE d.order_id=? ORDER BY l.loc_code, g.goods_code",
            (sid,))]
        return data
    finally:
        conn.close()


@route("POST", r"/api/v1/stocktakes")
def api_stocktake_create(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = create_stocktake(conn, body, user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/stocktakes/(\d+)/save")
def api_stocktake_save(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = save_stocktake(conn, int(m.group(1)), body.get("lines") or [], user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/stocktakes/(\d+)/submit")
def api_stocktake_submit(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = submit_stocktake(conn, int(m.group(1)), user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/stocktakes/(\d+)/audit")
def api_stocktake_audit(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = audit_stocktake(conn, int(m.group(1)), user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/stocktakes/(\d+)/cancel")
def api_stocktake_cancel(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = cancel_stocktake(conn, int(m.group(1)), user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("GET", r"/api/v1/transfers")
def api_transfers(handler, m, q, body):
    user = current_user(handler)
    page, size, offset = paging(q)
    conn = db()
    try:
        where, args = "WHERE 1=1", []
        if q.get("status", [""])[0] != "":
            where += " AND t.status=?"
            args.append(int(q["status"][0]))
        where, args = add_wh_scope(where, args, user, "t.from_wh_id")
        total = conn.execute("SELECT COUNT(*) FROM wms_transfer_order t " + where, args).fetchone()[0]
        rows = conn.execute(
            "SELECT t.*, lf.loc_name AS from_wh, lt.loc_name AS to_wh FROM wms_transfer_order t"
            " LEFT JOIN base_location lf ON lf.id=t.from_wh_id LEFT JOIN base_location lt ON lt.id=t.to_wh_id "
            + where + " ORDER BY t.id DESC LIMIT ? OFFSET ?", args + [size, offset]).fetchall()
        lst = []
        for r in rows:
            item = dict(r)
            item["statusText"] = TF_STATUS.get(item["status"], "?")
            lst.append(item)
        return {"total": total, "page": page, "size": size, "list": lst}
    finally:
        conn.close()


@route("GET", r"/api/v1/transfers/(\d+)")
def api_transfer_detail(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        tid = int(m.group(1))
        order = conn.execute(
            "SELECT t.*, lf.loc_name AS from_wh, lt.loc_name AS to_wh FROM wms_transfer_order t"
            " LEFT JOIN base_location lf ON lf.id=t.from_wh_id LEFT JOIN base_location lt ON lt.id=t.to_wh_id"
            " WHERE t.id=?", (tid,)).fetchone()
        if not order:
            raise ApiError(40400, "调拨单不存在", 404)
        data = dict(order)
        data["statusText"] = TF_STATUS.get(order["status"], "?")
        data["lines"] = [dict(r) for r in conn.execute(
            "SELECT d.*, g.goods_code, g.goods_name, g.unit, lf.loc_code AS from_loc_code,"
            " lt.loc_code AS to_loc_code,"
            " IFNULL((SELECT available_qty FROM wms_inventory i WHERE i.goods_id=d.goods_id"
            "         AND i.loc_id=d.from_loc_id AND i.batch_no=d.batch_no),0) AS available_qty"
            " FROM wms_transfer_detail d JOIN base_goods g ON g.id=d.goods_id"
            " JOIN base_location lf ON lf.id=d.from_loc_id JOIN base_location lt ON lt.id=d.to_loc_id"
            " WHERE d.order_id=? ORDER BY d.id", (tid,))]
        return data
    finally:
        conn.close()


@route("POST", r"/api/v1/transfers")
def api_transfer_create(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = create_transfer(conn, body, user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/transfers/(\d+)/audit")
def api_transfer_audit(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = audit_transfer(conn, int(m.group(1)), user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/transfers/(\d+)/confirm")
def api_transfer_confirm(handler, m, q, body):
    user = current_user(handler)
    request_id = handler.headers.get("X-Idempotency-Key") or body.get("requestId")
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = confirm_transfer(conn, int(m.group(1)), request_id, user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/transfers/(\d+)/cancel")
def api_transfer_cancel(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = cancel_transfer(conn, int(m.group(1)), body.get("reason"), user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/ops/jobs/run")
def api_jobs_run(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        results = run_jobs(conn, user)
        conn.commit()
        return {"results": results}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("GET", r"/api/v1/ops/jobs")
def api_jobs_list(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        return {"list": [dict(r) for r in conn.execute(
            "SELECT * FROM sys_job_log ORDER BY id DESC LIMIT 30")]}
    finally:
        conn.close()


@route("GET", r"/api/v1/reports/stock-summary")
def api_report_stock(handler, m, q, body):
    current_user(handler)
    begin = q.get("begin", [datetime.now().strftime("%Y-%m-01")])[0]
    end = q.get("end", [datetime.now().strftime("%Y-%m-%d")])[0]
    conn = db()
    try:
        return {"begin": begin, "end": end, "list": report_stock_summary(conn, begin, end)}
    finally:
        conn.close()


@route("GET", r"/api/v1/reports/profit")
def api_report_profit(handler, m, q, body):
    current_user(handler)
    begin = q.get("begin", [datetime.now().strftime("%Y-%m-01")])[0]
    end = q.get("end", [datetime.now().strftime("%Y-%m-%d")])[0]
    conn = db()
    try:
        data = report_profit(conn, begin, end)
        data.update({"begin": begin, "end": end})
        return data
    finally:
        conn.close()


@route("GET", r"/api/v1/reports/turnover")
def api_report_turnover(handler, m, q, body):
    current_user(handler)
    try:
        days = int(q.get("days", ["30"])[0])
    except ValueError:
        raise ApiError(40000, "days 必须是整数")
    conn = db()
    try:
        return report_turnover(conn, days)
    finally:
        conn.close()


@route("GET", r"/api/v1/reports/expiring")
def api_report_expiring(handler, m, q, body):
    current_user(handler)
    try:
        days = int(q.get("days", ["30"])[0])
    except ValueError:
        raise ApiError(40000, "days 必须是整数")
    conn = db()
    try:
        return report_expiring(conn, days)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# V6：商品图片 / 一体化上架 / 经营分析（营收趋势 + 店铺 & 仓库对比 + 规则建议）
# --------------------------------------------------------------------------- #

def _spu_payload(body):
    name = (body.get("spu_name") or "").strip()
    if not name:
        raise ApiError(40000, "款名必填")
    return {
        "spu_code": (body.get("spu_code") or "").strip(),
        "spu_name": name,
        "category_id": int(body.get("category_id") or 1),
        "brand": (body.get("brand") or "").strip() or None,
        "unit": (body.get("unit") or "个").strip(),
        "description": (body.get("description") or "").strip() or None,
        "image_url": (body.get("image_url") or "").strip() or None,
    }


def onboard_product(conn, body, user):
    """一体化上架：款(SPU) + SKU + 渠道价(上架) + 可选初始库存，全部在一个事务里。

    为什么要一体化：分开建很容易出现「有商品没上架」「上架了没库存」的半截数据；
    这里要么全成，要么全回滚，并且初始库存走的是正式入库过账（有流水、有成本）。
    """
    spu_id = body.get("spu_id")
    spu_info = None
    if spu_id:
        row = conn.execute("SELECT * FROM base_spu WHERE id=?", (int(spu_id),)).fetchone()
        if not row:
            raise ApiError(40400, "所属款不存在", 404)
        spu_id = row["id"]
        spu_info = dict(row)
    else:
        info = _spu_payload(body)
        if not info["spu_code"]:
            info["spu_code"] = "SPU" + datetime.now().strftime("%m%d%H%M%S")
        if conn.execute("SELECT 1 FROM base_spu WHERE spu_code=?", (info["spu_code"],)).fetchone():
            raise ApiError(30001, "款号已存在", 409)
        cur = conn.execute(
            "INSERT INTO base_spu(spu_code,spu_name,category_id,brand,unit,description,image_url)"
            " VALUES(?,?,?,?,?,?,?)",
            (info["spu_code"], info["spu_name"], info["category_id"], info["brand"], info["unit"],
             info["description"], info["image_url"]))
        spu_id = cur.lastrowid
        spu_info = dict(info, id=spu_id)

    goods_code = (body.get("goods_code") or "").strip()
    goods_name = (body.get("goods_name") or "").strip() or spu_info["spu_name"]
    if not goods_code:
        raise ApiError(40000, "SKU 编码必填")
    if not conn.execute("SELECT 1 FROM base_goods_category WHERE id=?",
                        (spu_info["category_id"],)).fetchone():
        raise ApiError(40000, "商品分类不存在")
    if conn.execute("SELECT 1 FROM base_goods WHERE goods_code=?", (goods_code,)).fetchone():
        raise ApiError(30001, "SKU 编码已存在", 409)
    unit = (body.get("unit") or spu_info.get("unit") or "个").strip()
    cost_price = DEC2(body.get("cost_price") or 0)
    spec_attrs = body.get("spec_attrs") or "{}"
    if isinstance(spec_attrs, dict):
        spec_attrs = json.dumps(spec_attrs, ensure_ascii=False)
    cur = conn.execute(
        "INSERT INTO base_goods(goods_code,goods_name,spu_id,category_id,spec,spec_attrs,unit,barcode,"
        "cost_price,avg_cost,status,remark,image_url) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)",
        (goods_code, goods_name, spu_id, spu_info["category_id"], (body.get("spec") or "").strip() or None,
         spec_attrs, unit, (body.get("barcode") or "").strip() or None, cost_price, cost_price,
         (body.get("remark") or "").strip() or None, (body.get("image_url") or "").strip() or None))
    sku_id = cur.lastrowid

    shop_id = int(body.get("shop_id") or 1)
    if not conn.execute("SELECT 1 FROM base_shop WHERE id=? AND status=1", (shop_id,)).fetchone():
        raise ApiError(40000, "店铺不存在或已停用")
    sale_price = DEC2(body.get("sale_price") or 0)
    if sale_price <= 0:
        raise ApiError(40000, "售价必须大于 0")
    conn.execute(
        "INSERT INTO ec_listing(shop_id,goods_id,sale_price,sale_status,reserve_qty) VALUES(?,?,?,1,?)"
        " ON CONFLICT(shop_id,goods_id) DO UPDATE SET sale_price=excluded.sale_price, sale_status=1,"
        " reserve_qty=excluded.reserve_qty, update_time=datetime('now','localtime')",
        (shop_id, sku_id, sale_price, DEC2(body.get("reserve_qty") or 0)))
    conn.execute("INSERT OR IGNORE INTO wms_stock_policy(goods_id,wh_id,safety_stock,replenish_qty,lead_days)"
                 " VALUES(?,0,?,?,7)", (sku_id, DEC2(body.get("safety_stock") or 100), 200))

    order_no, init_qty = None, DEC2(body.get("init_qty") or 0)
    if init_qty > 0:
        try:
            loc_id = int(body.get("init_loc_id"))
        except (TypeError, ValueError):
            raise ApiError(40000, "填了初始库存就必须选入库库位")
        wh = warehouse_for_location(conn, loc_id)
        check_wh_scope(user, wh["id"])
        supplier = conn.execute("SELECT id FROM base_partner WHERE partner_type='supplier' AND status=1"
                                " ORDER BY id LIMIT 1").fetchone()
        order_no = next_order_no(conn, "RK")
        cur = conn.execute(
            "INSERT INTO wms_in_order(order_no,order_type,partner_id,wh_id,status,total_plan_num,remark,"
            "create_by,audit_by,audit_time) VALUES(?,1,?,?,1,?,'新品建档初始库存',?,?,?)",
            (order_no, supplier["id"] if supplier else None, wh["id"], init_qty, user["id"], user["id"], now()))
        oid = cur.lastrowid
        conn.execute("INSERT INTO wms_in_order_detail(order_id,goods_id,loc_id,plan_num,actual_num,batch_no,unit)"
                     " VALUES(?,?,?,?,?,?,?)", (oid, sku_id, loc_id, init_qty, init_qty, "", unit))
        post_in_order(conn, oid, "onboard-%d" % sku_id, user)

    write_log(conn, user, "商品上下架", "CREATE", sku_id, goods_code,
              {"spuId": spu_id, "shopId": shop_id, "price": sale_price, "initQty": init_qty})
    return {"spuId": spu_id, "skuId": sku_id, "goodsCode": goods_code, "shopId": shop_id,
            "salePrice": sale_price, "initQty": init_qty, "orderNo": order_no}


def rebuild_daily_stat(conn, day=None):
    """重算某天（默认今天）的日汇总：按 SKU × 仓 汇总入库/出库/调整/成本/销售额/期末。

    为什么要汇总表：报表若每次扫流水，数据量一大就超时；分片以后跨库更不可能实时 JOIN。
    汇总表按天增量重算（幂等 UPSERT），报表只读它。
    """
    day = day or datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT t.goods_id, IFNULL(l.wh_id,0) wh_id,"
        " SUM(CASE WHEN t.change_qty>0 AND t.biz_type IN (1,2,3,7,8,11) THEN t.change_qty ELSE 0 END) in_qty,"
        " SUM(CASE WHEN t.change_qty<0 AND t.biz_type IN (4,5,6,9) THEN -t.change_qty ELSE 0 END) out_qty,"
        " SUM(CASE WHEN t.biz_type IN (8,9,10) THEN t.change_qty ELSE 0 END) adjust_qty,"
        " SUM(CASE WHEN t.change_qty<0 THEN IFNULL(t.cost_amount,0) ELSE 0 END) cost_amount"
        " FROM wms_inventory_txn t LEFT JOIN base_location l ON l.id=t.loc_id"
        " WHERE date(t.create_time)=? GROUP BY t.goods_id, IFNULL(l.wh_id,0)", (day,)).fetchall()
    sales = {r["goods_id"]: DEC2(r["amt"]) for r in conn.execute(
        "SELECT i.goods_id, SUM(i.amount) amt FROM ec_order_item i JOIN ec_order o ON o.id=i.order_id"
        " WHERE date(IFNULL(o.pay_time,o.create_time))=? AND o.status IN (1,2,3,4,6,7,8)"
        " GROUP BY i.goods_id", (day,)).fetchall()}
    end_qty = {r["goods_id"]: DEC2(r["qty"]) for r in conn.execute(
        "SELECT t.goods_id, SUM(t.change_qty) qty FROM wms_inventory_txn t"
        " WHERE date(t.create_time)<=? GROUP BY t.goods_id", (day,)).fetchall()}
    for r in rows:
        conn.execute(
            "INSERT INTO wms_daily_stat(stat_date,goods_id,wh_id,in_qty,out_qty,adjust_qty,cost_amount,"
            "sales_amount,end_qty,update_time) VALUES(?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(stat_date,goods_id,wh_id) DO UPDATE SET in_qty=excluded.in_qty,"
            " out_qty=excluded.out_qty, adjust_qty=excluded.adjust_qty, cost_amount=excluded.cost_amount,"
            " sales_amount=excluded.sales_amount, end_qty=excluded.end_qty, update_time=excluded.update_time",
            (day, r["goods_id"], r["wh_id"], DEC2(r["in_qty"]), DEC2(r["out_qty"]),
             DEC2(r["adjust_qty"]), DEC2(r["cost_amount"]), sales.get(r["goods_id"], 0.0),
             end_qty.get(r["goods_id"], 0.0), now()))
    write_job_stat(conn, day, len(rows))
    return len(rows)


def write_job_stat(conn, day, rows):
    conn.execute("INSERT INTO sys_job_log(job_code,run_at,affected,detail,result)"
                 " VALUES('STAT_REFRESH',?,?,?,'SUCCESS')", (now(), rows, "汇总日期 %s" % day))


def report_trend(conn, days=14):
    """近 N 天营收趋势：销售额（下单）、出库成本（发货）、毛利。"""
    rows = conn.execute(
        "SELECT date(create_time) d, COUNT(*) orders,"
        " SUM(CASE WHEN status IN (1,2,3,4,6) THEN pay_amount ELSE 0 END) sales"
        " FROM ec_order WHERE date(create_time) >= date('now', ?) GROUP BY d", ("-%d day" % days,)).fetchall()
    if conn.execute("SELECT 1 FROM wms_daily_stat LIMIT 1").fetchone():
        # 汇总表已就绪：销售额/成本按天从汇总取（比扫订单+流水快一个量级）
        agg = {r["d"]: r for r in conn.execute(
            "SELECT stat_date d, SUM(sales_amount) sales, SUM(cost_amount) cost FROM wms_daily_stat"
            " WHERE stat_date >= date('now', ?) GROUP BY stat_date", ("-%d day" % days,)).fetchall()}
    else:
        agg = {}
    costs = conn.execute(
        "SELECT date(create_time) d, SUM(cost_amount) cost FROM wms_inventory_txn"
        " WHERE biz_type=21 AND date(create_time) >= date('now', ?) GROUP BY d", ("-%d day" % days,)).fetchall()
    sales = {r["d"]: (DEC2(r["sales"]), r["orders"]) for r in rows}
    cost = {r["d"]: DEC2(r["cost"]) for r in costs}
    out = []
    today = datetime.now().date()
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        s, o = sales.get(d, (0.0, 0))
        c = cost.get(d, 0.0)
        out.append({"date": d, "sales": s, "cost": c, "profit": DEC2(s - c), "orders": o})
    return out


def report_shop_sales(conn, days=30):
    rows = conn.execute(
        "SELECT s.id, s.shop_code, s.shop_name, s.channel_type,"
        " IFNULL(SUM(CASE WHEN o.status IN (1,2,3,4,6) THEN o.pay_amount ELSE 0 END),0) sales,"
        " IFNULL(SUM(CASE WHEN o.status IN (1,2,3,4,6) THEN 1 ELSE 0 END),0) orders,"
        " IFNULL(SUM(CASE WHEN o.status=5 THEN 1 ELSE 0 END),0) canceled,"
        " IFNULL((SELECT COUNT(*) FROM ec_after_sale a JOIN ec_order o2 ON o2.id=a.order_id"
        "         WHERE o2.shop_id=s.id AND date(a.create_time) >= date('now', ?)),0) after_sales"
        " FROM base_shop s LEFT JOIN ec_order o ON o.shop_id=s.id AND date(o.create_time) >= date('now', ?)"
        " GROUP BY s.id ORDER BY sales DESC", ("-%d day" % days, "-%d day" % days)).fetchall()
    return [dict(r) for r in rows]


def report_warehouse_stock(conn):
    """按仓库：SKU 数、实物、库存金额（移动加权）、近 30 天出库量、周转天数。"""
    out = []
    for wh in conn.execute("SELECT id, loc_code, loc_name FROM base_location WHERE loc_type=1"
                           " AND status=1 ORDER BY loc_code").fetchall():
        row = conn.execute(
            "SELECT COUNT(DISTINCT i.goods_id) skus, IFNULL(SUM(i.quantity),0) qty,"
            " IFNULL(SUM(i.quantity * g.avg_cost),0) amount, IFNULL(SUM(i.locked_qty),0) locked"
            " FROM wms_inventory i JOIN base_location l ON l.id=i.loc_id JOIN base_goods g ON g.id=i.goods_id"
            " WHERE l.wh_id=?", (wh["id"],)).fetchone()
        out30 = DEC2(conn.execute(
            "SELECT IFNULL(SUM(-t.change_qty),0) FROM wms_inventory_txn t"
            " JOIN base_location l ON l.id=t.loc_id"
            " WHERE l.wh_id=? AND t.change_qty<0 AND date(t.create_time) >= date('now','-30 day')",
            (wh["id"],)).fetchone()[0])
        qty = DEC2(row["qty"])
        daily = DEC2(out30 / 30)
        out.append({"whId": wh["id"], "whCode": wh["loc_code"], "whName": wh["loc_name"],
                    "skus": row["skus"], "qty": qty, "amount": DEC2(row["amount"]),
                    "locked": DEC2(row["locked"]), "out30": out30,
                    "turnoverDays": DEC2(qty / daily) if daily > 0 else None})
    return out


def report_category_sales(conn, days=30):
    rows = conn.execute(
        "SELECT IFNULL(c.category_name,'未分类') category, IFNULL(SUM(i.amount),0) sales,"
        " IFNULL(SUM(i.qty),0) qty"
        " FROM ec_order_item i JOIN ec_order o ON o.id=i.order_id"
        " JOIN base_goods g ON g.id=i.goods_id LEFT JOIN base_goods_category c ON c.id=g.category_id"
        " WHERE o.status IN (1,2,3,4,6) AND date(o.create_time) >= date('now', ?)"
        " GROUP BY c.id ORDER BY sales DESC", ("-%d day" % days,)).fetchall()
    total = sum(DEC2(r["sales"]) for r in rows) or 1
    out = []
    for r in rows:
        item = dict(r)
        item["share"] = DEC2(DEC2(item["sales"]) / total * 100)
        out.append(item)
    return out


def report_advice(conn):
    """规则化经营建议：库存/履约/毛利/退货/周转五类，给出可执行动作。"""
    advice = []

    low = conn.execute(
        "SELECT g.goods_code, g.goods_name, IFNULL(p.safety_stock,0) safety,"
        " IFNULL((SELECT SUM(available_qty) FROM wms_inventory i WHERE i.goods_id=g.id),0) avail"
        " FROM base_goods g JOIN wms_stock_policy p ON p.goods_id=g.id AND p.wh_id=0"
        " WHERE g.status=1 AND p.safety_stock>0"
        "   AND IFNULL((SELECT SUM(available_qty) FROM wms_inventory i WHERE i.goods_id=g.id),0) < p.safety_stock"
        " ORDER BY (p.safety_stock - IFNULL((SELECT SUM(available_qty) FROM wms_inventory i"
        "   WHERE i.goods_id=g.id),0)) DESC LIMIT 3").fetchall()
    if low:
        detail = "；".join("%s 可用 %s / 安全 %s" % (r["goods_name"], DEC2(r["avail"]), DEC2(r["safety"]))
                          for r in low)
        advice.append({"level": "high", "topic": "补货",
                       "title": "%d 个 SKU 低于安全库存" % len(low), "detail": detail,
                       "action": "去「补货建议」一键生成采购入库草稿，按交期倒排下单"})

    stale = conn.execute(
        "SELECT COUNT(*) c, IFNULL(SUM(i.quantity * g.avg_cost),0) amt FROM wms_inventory i"
        " JOIN base_goods g ON g.id=i.goods_id WHERE i.quantity>0 AND NOT EXISTS ("
        "  SELECT 1 FROM wms_inventory_txn t WHERE t.goods_id=i.goods_id AND t.loc_id=i.loc_id"
        "  AND t.batch_no=i.batch_no AND t.change_qty<0 AND date(t.create_time) >= date('now','-30 day'))"
    ).fetchone()
    if stale["c"]:
        advice.append({"level": "mid", "topic": "呆滞",
                       "title": "%d 个库存行 30 天没有出库" % stale["c"],
                       "detail": "占用资金约 ￥%s" % yuan_plain(stale["amt"]),
                       "action": "做组合促销/调拨到动销仓，或与供应商谈退换；报表中心可看呆滞 SKU 清单"})

    expiring = conn.execute(
        "SELECT COUNT(*) c FROM wms_inventory WHERE quantity>0 AND expire_date IS NOT NULL"
        " AND date(expire_date) <= date('now','+30 day')").fetchone()
    if expiring["c"]:
        advice.append({"level": "high", "topic": "效期",
                       "title": "%d 个批次 30 天内到期" % expiring["c"], "detail": "过期不可再售，只能报损或退供",
                       "action": "打开参数 inventory.fefo.enabled=true 让近效期先出，并对临期批次做促销"})

    ship = conn.execute(
        "SELECT COUNT(*) c FROM ec_order WHERE status IN (1,2)"
        " AND datetime(IFNULL(pay_time,create_time)) <= datetime('now','-1 day')").fetchone()
    if ship["c"]:
        advice.append({"level": "high", "topic": "履约",
                       "title": "%d 笔订单支付后超过 24h 未发货" % ship["c"],
                       "detail": "电商超时发货会拉低店铺评分，也可能触发平台罚则",
                       "action": "去「订单池」合成波次优先处理，或检查拣货人手与库存分配"})

    profit = report_profit(conn, datetime.now().strftime("%Y-%m-01"),
                           datetime.now().strftime("%Y-%m-%d"))
    if profit["totalSales"] > 0 and profit["profitRate"] < 20:
        advice.append({"level": "mid", "topic": "毛利",
                       "title": "本月毛利率 %.1f%%，低于 20%% 基线" % profit["profitRate"],
                       "detail": "销售额 ￥%s / 出库成本 ￥%s" % (yuan_plain(profit["totalSales"]),
                                                            yuan_plain(profit["totalCost"])),
                       "action": "核查高价采购与低价促销；毛利低的 SKU 考虑调价或换供应商"})

    as_count = conn.execute("SELECT COUNT(*) c FROM ec_after_sale WHERE status<>3").fetchone()["c"]
    sold = conn.execute("SELECT COUNT(*) c FROM ec_order WHERE status IN (3,4,6)").fetchone()["c"]
    if sold and as_count / sold > 0.1:
        advice.append({"level": "mid", "topic": "售后",
                       "title": "退货率 %.1f%%，高于 10%%" % (as_count / sold * 100),
                       "detail": "%d 张已发货订单里有 %d 张售后" % (sold, as_count),
                       "action": "复盘退货原因（尺寸/质量/描述不符），必要时改详情页与质检标准"})

    turn = report_turnover(conn, 30)
    slow = [x for x in turn["list"] if x["turnoverDays"] is None and DEC2(x["stock_qty"]) > 0]
    if len(slow) > turn["skuCount"] * 0.5 and turn["skuCount"] > 0:
        advice.append({"level": "low", "topic": "周转",
                       "title": "%d/%d 个 SKU 近 30 天没有动销" % (len(slow), turn["skuCount"]),
                       "detail": "动销率仅 %.0f%%，库存周转会持续恶化" % turn["activeRate"],
                       "action": "上架新品或做组合套餐；把长尾 SKU 从商城下架，减少无效库存"})

    if not advice:
        advice.append({"level": "low", "topic": "健康", "title": "各项指标正常，没有需要立刻处理的风险",
                       "detail": "库存水位、履约时效与毛利都在基线上", "action": "保持当前节奏即可"})
    level_order = {"high": 0, "mid": 1, "low": 2}
    advice.sort(key=lambda x: level_order.get(x["level"], 3))
    return advice


def yuan_plain(v):
    return "{:,.2f}".format(float(v or 0))


# --------------------------------------------------------------------------- #
# V6 接口：款(SPU)维护 / 一体化上架 / 经营分析
# --------------------------------------------------------------------------- #
@route("POST", r"/api/v1/spus")
def api_spu_create(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        info = _spu_payload(body)
        if not info["spu_code"]:
            info["spu_code"] = "SPU" + datetime.now().strftime("%m%d%H%M%S")
        if conn.execute("SELECT 1 FROM base_spu WHERE spu_code=?", (info["spu_code"],)).fetchone():
            raise ApiError(30001, "款号已存在", 409)
        cur = conn.execute(
            "INSERT INTO base_spu(spu_code,spu_name,category_id,brand,unit,description,image_url)"
            " VALUES(?,?,?,?,?,?,?)",
            (info["spu_code"], info["spu_name"], info["category_id"], info["brand"], info["unit"],
             info["description"], info["image_url"]))
        write_log(conn, user, "商品上下架", "CREATE", cur.lastrowid, info["spu_code"], {"spu": True})
        conn.commit()
        return {"id": cur.lastrowid, "spuCode": info["spu_code"]}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("PUT", r"/api/v1/spus/(\d+)")
def api_spu_update(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        sid = int(m.group(1))
        row = conn.execute("SELECT * FROM base_spu WHERE id=?", (sid,)).fetchone()
        if not row:
            raise ApiError(40400, "款不存在", 404)
        conn.execute("UPDATE base_spu SET spu_name=?, category_id=?, brand=?, unit=?, description=?,"
                     " image_url=?, status=? WHERE id=?",
                     ((body.get("spu_name") or row["spu_name"]).strip(),
                      int(body.get("category_id") or row["category_id"]),
                      (body.get("brand") if body.get("brand") is not None else row["brand"]),
                      (body.get("unit") or row["unit"]),
                      (body.get("description") if body.get("description") is not None else row["description"]),
                      (body.get("image_url") if body.get("image_url") is not None else row["image_url"]),
                      int(body.get("status", row["status"])), sid))
        write_log(conn, user, "商品上下架", "UPDATE", sid, row["spu_code"], {"image": body.get("image_url")})
        conn.commit()
        return {"id": sid}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/catalog/onboard")
def api_catalog_onboard(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = onboard_product(conn, body, user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("GET", r"/api/v1/reports/trend")
def api_report_trend(handler, m, q, body):
    current_user(handler)
    try:
        days = max(3, min(90, int(q.get("days", ["14"])[0])))
    except ValueError:
        raise ApiError(40000, "days 必须是整数")
    conn = db()
    try:
        return {"days": days, "list": report_trend(conn, days)}
    finally:
        conn.close()


@route("GET", r"/api/v1/reports/shop-sales")
def api_report_shop_sales(handler, m, q, body):
    current_user(handler)
    try:
        days = max(1, min(365, int(q.get("days", ["30"])[0])))
    except ValueError:
        raise ApiError(40000, "days 必须是整数")
    conn = db()
    try:
        return {"days": days, "list": report_shop_sales(conn, days),
                "byCategory": report_category_sales(conn, days)}
    finally:
        conn.close()


@route("GET", r"/api/v1/reports/warehouse-stock")
def api_report_wh(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        return {"list": report_warehouse_stock(conn)}
    finally:
        conn.close()


@route("GET", r"/api/v1/reports/advice")
def api_report_advice(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        return {"list": report_advice(conn)}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# V7：商品归档 / 彻底删除（逻辑删除 + 前置校验）
#
# 为什么不能物理删除：库存、流水、订单分配、盘点/调拨明细都按 goods_id 引用商品，
# 台账与流水是审计凭证（不许改、不许删）。所以「彻底删除」= 停用 + 逻辑删除，
# 行还在（历史流水照样能 join 出名称），只是所有列表/商城不再出现。
# 真正物理 DELETE 只允许在「无库存、无预占、无未完成单据」时由运维手工执行。
# --------------------------------------------------------------------------- #
def goods_blockers(conn, goods_id):
    """返回 [{code, text}] 形式的删除阻塞原因；空数组表示可以归档。"""
    blockers = []
    row = conn.execute(
        "SELECT IFNULL(SUM(quantity),0) qty, IFNULL(SUM(locked_qty),0) locked"
        " FROM wms_inventory WHERE goods_id=?", (goods_id,)).fetchone()
    if DEC2(row["qty"]) > 0:
        blockers.append({"code": "STOCK", "text": "还有库存 %s 件，需先盘点清零或报损处理"
                         % DEC2(row["qty"])})
    if DEC2(row["locked"]) > 0:
        blockers.append({"code": "LOCKED", "text": "存在预占 %s 件（未发货订单/未确认调拨）"
                         % DEC2(row["locked"])})
    open_alloc = conn.execute(
        "SELECT COUNT(*) FROM ec_order_alloc WHERE goods_id=? AND status IN (1,2)",
        (goods_id,)).fetchone()[0]
    if open_alloc:
        blockers.append({"code": "ORDER_ALLOC",
                         "text": "有 %d 条未发货订单分配占用该商品，先发货或取消订单" % open_alloc})
    open_in = conn.execute(
        "SELECT COUNT(*) FROM wms_in_order_detail d JOIN wms_in_order o ON o.id=d.order_id"
        " WHERE d.goods_id=? AND o.status IN (0,1,2)", (goods_id,)).fetchone()[0]
    if open_in:
        blockers.append({"code": "IN_ORDER",
                         "text": "有 %d 张未完成的入库单包含该商品，先过账或作废" % open_in})
    open_out = conn.execute(
        "SELECT COUNT(*) FROM wms_out_order_detail d JOIN wms_out_order o ON o.id=d.order_id"
        " WHERE d.goods_id=? AND o.status IN (0,1,2)", (goods_id,)).fetchone()[0]
    if open_out:
        blockers.append({"code": "OUT_ORDER",
                         "text": "有 %d 张未完成的出库单包含该商品，先过账或作废" % open_out})
    open_st = conn.execute(
        "SELECT COUNT(*) FROM wms_stocktake_detail d JOIN wms_stocktake_order o ON o.id=d.order_id"
        " WHERE d.goods_id=? AND o.status IN (0,1,2)", (goods_id,)).fetchone()[0]
    if open_st:
        blockers.append({"code": "STOCKTAKE",
                         "text": "有 %d 张进行中的盘点单包含该商品，先完成或作废盘点" % open_st})
    open_tf = conn.execute(
        "SELECT COUNT(*) FROM wms_transfer_detail d JOIN wms_transfer_order o ON o.id=d.order_id"
        " WHERE d.goods_id=? AND o.status IN (0,1)", (goods_id,)).fetchone()[0]
    if open_tf:
        blockers.append({"code": "TRANSFER",
                         "text": "有 %d 张未完成的调拨单包含该商品，先确认或作废" % open_tf})
    return blockers


def archive_goods(conn, goods_id, reason, user):
    """归档（= 彻底删除的业务口径）：停用 + 逻辑删除 + 全渠道下架。有阻塞则整体拒绝。"""
    g = conn.execute("SELECT * FROM base_goods WHERE id=?", (goods_id,)).fetchone()
    if not g:
        raise ApiError(40400, "商品不存在", 404)
    if g["is_deleted"]:
        return {"id": goods_id, "duplicated": True}
    blockers = goods_blockers(conn, goods_id)
    if blockers:
        raise ApiError(50002, "该商品不能删除：" + "；".join(b["text"] for b in blockers), 409)
    conn.execute("UPDATE base_goods SET status=0, is_deleted=1, remark=? WHERE id=?",
                 ("已归档（删除原因：%s）" % ((reason or "商品下线").strip()), goods_id))
    conn.execute("UPDATE ec_listing SET sale_status=0, update_time=datetime('now','localtime')"
                 " WHERE goods_id=?", (goods_id,))
    conn.execute("DELETE FROM wms_stock_policy WHERE goods_id=?", (goods_id,))
    # 该款下若已无其它在售 SKU，把款也归档
    spu_left = conn.execute("SELECT COUNT(*) FROM base_goods WHERE spu_id=? AND is_deleted=0"
                            " AND status=1", (g["spu_id"],)).fetchone()[0]
    if g["spu_id"] and spu_left == 0:
        conn.execute("UPDATE base_spu SET status=0, is_deleted=1 WHERE id=?", (g["spu_id"],))
    write_log(conn, user, "商品管理", "ARCHIVE", goods_id, g["goods_code"],
              {"reason": reason, "blockers": []})
    return {"id": goods_id, "goodsCode": g["goods_code"], "spuArchived": bool(g["spu_id"]) and spu_left == 0}


def restore_goods(conn, goods_id, user):
    g = conn.execute("SELECT * FROM base_goods WHERE id=?", (goods_id,)).fetchone()
    if not g:
        raise ApiError(40400, "商品不存在", 404)
    conn.execute("UPDATE base_goods SET status=1, is_deleted=0 WHERE id=?", (goods_id,))
    if g["spu_id"]:
        conn.execute("UPDATE base_spu SET status=1, is_deleted=0 WHERE id=?", (g["spu_id"],))
    write_log(conn, user, "商品管理", "RESTORE", goods_id, g["goods_code"])
    return {"id": goods_id, "status": 1}


# --------------------------------------------------------------------------- #
# V7 接口：删除前检查 / 归档 / 恢复
# --------------------------------------------------------------------------- #
@route("GET", r"/api/v1/goods/(\d+)/delete-check")
def api_goods_delete_check(handler, m, q, body):
    current_user(handler)
    conn = db()
    try:
        gid = int(m.group(1))
        g = conn.execute("SELECT id, goods_code, goods_name, status, is_deleted FROM base_goods WHERE id=?",
                         (gid,)).fetchone()
        if not g:
            raise ApiError(40400, "商品不存在", 404)
        blockers = goods_blockers(conn, gid)
        return {"id": gid, "goodsCode": g["goods_code"], "goodsName": g["goods_name"],
                "deletable": not blockers and not g["is_deleted"], "alreadyDeleted": bool(g["is_deleted"]),
                "blockers": blockers}
    finally:
        conn.close()


@route("POST", r"/api/v1/goods/(\d+)/archive")
def api_goods_archive(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = archive_goods(conn, int(m.group(1)), body.get("reason"), user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@route("POST", r"/api/v1/goods/(\d+)/restore")
def api_goods_restore(handler, m, q, body):
    user = current_user(handler)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = restore_goods(conn, int(m.group(1)), user)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# HTTP Server
# --------------------------------------------------------------------------- #
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".png": "image/png",
        ".svg": "image/svg+xml", ".ico": "image/x-icon", ".json": "application/json; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    # 压测实测：默认 backlog 只有 5，突发连接会被内核直接拒绝（表现为大量连接异常）
    request_queue_size = 512
    # 复用连接：默认 HTTP/1.0 每请求都断开，6000 次/8s 会把本机 TIME_WAIT 打满
    protocol_version = "HTTP/1.1"

    server_version = "WMS-Demo/1.0"

    def log_message(self, fmt, *args):        # 静音默认访问日志，只保留错误
        if not str(args[1] if len(args) > 1 else "").startswith("2"):
            sys.stderr.write("[http] " + fmt % args + "\n")

    # ---------- 工具 ----------
    def _cors_headers(self):
        """按 WMS_CORS_ORIGINS 白名单回显来源；* 仅用于本地开发。"""
        if "*" in CORS_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", "*")
        else:
            origin = self.headers.get("Origin", "")
            if origin and origin in CORS_ORIGINS:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")

    def send_json(self, payload, status=200):
        self.last_status = status
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _dispatch_idempotent(self, key, method, path, user, fn, mm, query, body):
        """同一 Idempotency-Key 只执行一次：重放返回首次结果，处理中返回 409。

        为什么必须有：支付/发货/退款这类接口，客户端超时重试是常态，
        没有幂等就会出现「重复扣库存」「重复生成发货单」——这是生产事故的高频来源。
        """
        conn = db()
        store = wms_infra.Idempotency(conn)
        try:
            state = store.begin(key, method, path, user["id"] if user else None)
            if state["state"] == "replay":
                raw = state["body"] or "{}"
                try:                       # 重放时标注 duplicated，前端/调用方可识别"这是重复请求"
                    replay = json.loads(raw)
                    if isinstance(replay.get("data"), dict):
                        replay["data"]["duplicated"] = True
                        replay["data"]["replayed"] = True
                        raw = json.dumps(replay, ensure_ascii=False, default=str)
                except (ValueError, AttributeError):
                    pass
                self.send_response(state["status"] or 200)
                self._cors_headers()
                data = raw.encode("utf-8")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Idempotent-Replay", "1")
                self.end_headers()
                self.wfile.write(data)
                self.last_status = state["status"] or 200
                return None
            if state["state"] == "in_flight":
                raise ApiError(40901, "同一请求正在处理中，请稍后重试", 409)
            try:
                data = fn(self, mm, query, body)
            except Exception:
                store.fail(key)
                raise
            payload = {"code": 200, "message": "操作成功", "data": data}
            store.finish(key, 200, json.dumps(payload, ensure_ascii=False, default=str))
            return data
        finally:
            conn.close()

    def send_file(self, path):
        if not os.path.isfile(path):
            self.send_json({"code": 40400, "message": "资源不存在", "data": None}, 404)
            return
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Idempotency-Key")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise ApiError(40001, "请求体不是合法 JSON")

    # ---------- 分发 ----------
    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        started = time.time()
        self.auth_user = None                    # 每个请求重新认证（避免 keep-alive 复用旧身份）
        parsed = urlparse(self.path)
        path = parsed.path
        self.trace_id = wms_infra.new_trace_id()
        acquired = INFLIGHT.acquire(timeout=5)
        if not acquired:
            self.send_json({"code": 50301, "message": "服务繁忙，请稍后重试", "data": None}, 503)
            return
        try:
            if path.startswith("/api/"):
                query = {k: v for k, v in parse_qs(parsed.query).items()}
                body = self.read_body() if method in ("POST", "PUT", "DELETE") else {}
                # ① 入口限流（按 IP）：峰值时快速失败，别把线程池打满
                client_ip = self.client_address[0] if self.client_address else "unknown"
                ok, retry_after = RATE_LIMITER.allow("ip:" + client_ip)
                if not ok:
                    raise ApiError(42901, "请求过于频繁，请 %d 秒后重试" % retry_after, 429)
                for m_method, p_str, p_re, fn in ROUTES:
                    mm = p_re.match(path)
                    if mm and m_method == method:
                        need = ROUTE_PERM.get(method + " " + p_str)
                        user = None
                        if need:                       # 接口层统一鉴权，业务函数不重复判断
                            user = current_user(self)
                            require_perm(user, need)
                            ok, retry_after = RATE_LIMITER.allow(
                                "user:%s:%s" % (user["username"], p_str.split("/")[3]))
                            if not ok:
                                raise ApiError(42901, "账号请求过于频繁，请 %d 秒后重试" % retry_after, 429)
                        idem_key = (self.headers.get("X-Idempotency-Key") or "").strip()
                        if idem_key and method in IDEMPOTENT_METHODS:
                            data = self._dispatch_idempotent(idem_key, method, path, user, fn, mm, query, body)
                        else:
                            data = fn(self, mm, query, body)
                        self.send_json({"code": 200, "message": "操作成功", "data": data})
                        return
                raise ApiError(40400, "接口不存在：%s %s" % (method, path), 404)
            # 静态资源（单文件版由本服务直出；独立前后端版没有 web/ 目录时回落为服务信息 JSON）
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            target = os.path.normpath(os.path.join(WEB_DIR, rel))
            if not target.startswith(WEB_DIR):
                raise ApiError(40300, "非法路径", 403)
            if path in ("/", "") and not os.path.isfile(target):
                self.send_json({"code": 200, "message": "WMS 后端服务运行正常",
                                "data": {"service": "wms-demo-backend", "version": VERSION, "env": WMS_ENV,
                                         "docs": "/api/v1/ops/health（需登录）"}})
                return
            self.send_file(target)
        except ApiError as e:
            self.send_json({"code": e.code, "message": e.message, "data": None}, e.http)
        except BrokenPipeError:
            pass
        except Exception as e:                                  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.send_json({"code": 50000, "message": "系统内部错误：%s" % e, "data": None}, 500)
        finally:
            INFLIGHT.release()
            if path.startswith("/api/"):
                elapsed = (time.time() - started) * 1000
                status = getattr(self, "last_status", 200)
                METRICS.observe(status, elapsed)
                # 采样：错误全记，正常请求只记慢的（>=300ms）。逐请求打日志在 180QPS 下会显著抬高 P99
                if status >= 500 or elapsed >= 300:
                    wms_infra.log_event("api", trace=getattr(self, "trace_id", "-"), method=method,
                                        path=path, status=status, ms=int(elapsed),
                                        ip=self.client_address[0] if self.client_address else "-")


@route("GET", r"/api/v1/ops/queue")
def api_ops_queue(handler, m, q, body):
    """任务队列水位（积压/失败），削峰是否健康看这里。"""
    current_user(handler)
    stats = task_queue().stats()
    stats["backlog"] = stats["byStatus"].get("0", 0) + stats["byStatus"].get("1", 0)
    return stats


@route("GET", r"/api/v1/ops/migrations")
def api_ops_migrations(handler, m, q, body):
    """已应用的迁移版本（回答"这个库是哪个版本"）。"""
    current_user(handler)
    conn = db()
    try:
        rows = conn.execute("SELECT version,name,applied_at,duration_ms FROM schema_migrations"
                            " ORDER BY version").fetchall()
        latest = max(v for v, _, _ in wms_infra.MIGRATIONS) if wms_infra.MIGRATIONS else 0
        return {"current": rows[-1]["version"] if rows else 0, "latest": latest,
                "list": [dict(r) for r in rows]}
    finally:
        conn.close()


@route("GET", r"/api/v1/ops/runtime")
def api_ops_runtime(handler, m, q, body):
    """进程运行时指标：QPS / P95 / 错误率 / 队列积压 / 分片数。"""
    current_user(handler)
    stats = task_queue().stats()
    return {"metrics": METRICS.snapshot(),
            "queue": {"backlog": stats["byStatus"].get("0", 0) + stats["byStatus"].get("1", 0),
                      "failed": stats["byStatus"].get("3", 0), "byStatus": stats["byStatus"]},
            "shards": wms_infra.SHARD_COUNT,
            "rateLimitPerMinute": RATE_LIMITER.per_minute}


def main():
    try:                                   # 控制台码页不是 UTF-8 时也不要因中文 banner 报错
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(description="WMS 本地演示（Python + SQLite，零依赖）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="默认取环境变量 WMS_PORT（8000）")
    parser.add_argument("--host", default=DEFAULT_HOST, help="默认取环境变量 WMS_HOST（127.0.0.1）")
    parser.add_argument("--reset", action="store_true", help="重置演示数据")
    args = parser.parse_args()

    fresh = init_db(reset=args.reset)
    try:                                   # 启动即跑一次批处理：超时未支付关单 / 预占与临期扫描
        boot_conn = db()
        try:
            jobs = run_jobs(boot_conn)
            boot_conn.commit()
            sys.stderr.write("[jobs] " + ", ".join("%s=%s" % (j["job"], j["affected"]) for j in jobs) + "\n")
        finally:
            boot_conn.close()
    except Exception as exc:               # noqa: BLE001
        sys.stderr.write("[WARN] 启动批处理失败：%s\n" % exc)
    url = "http://%s:%d" % (args.host, args.port)
    env_hint = ("配置文件 %s" % ENV_FILE) if ENV_FILE else "未使用 .env（读取系统环境变量）"
    secret_hint = "已通过环境变量/.env 配置" if os.environ.get("WMS_SECRET_KEY") else "开发默认值（上线前必须配置）"
    print("=" * 68)
    print("  WMS 本地演示已启动")
    print("  地址   : %s" % url)
    print("  账号   : admin / 123456")
    print("  环境   : %s（%s）" % (WMS_ENV, env_hint))
    print("  密钥   : %s" % secret_hint)
    print("  数据库 : %s%s" % (DB_PATH, "（本次新建并灌入演示数据）" if fresh else ""))
    print("  停止   : Ctrl + C")
    print("=" * 68)
    # 任务 worker：把「自动建波、清理幂等键」等非强一致动作移出请求链路（削峰）
    handlers = {"AUTO_WAVE": task_auto_wave, "IDEM_CLEANUP": task_idem_cleanup}
    workers = [wms_infra.TaskWorker(task_queue(), handlers, concurrency=1,
                                    name="task-worker-%d" % i) for i in range(2)]
    for w in workers:
        w.start()
    ThreadingHTTPServer.request_queue_size = 512
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        for w in workers:
            w.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()







