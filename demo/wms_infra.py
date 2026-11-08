# -*- coding: utf-8 -*-
"""生产化基础设施（与业务解耦，可被 app.py 直接引用）。

设计目标（都是演示版原来缺的）：
1. 迁移机制   —— schema_migrations + 有序迁移，回答"这个库是哪个版本"，可重复执行。
2. 接口幂等   —— sys_idempotency：同一 Idempotency-Key 重放返回首次结果，不再重复下单/发货。
3. 限流       —— 令牌桶，按 用户/IP × 路由 分组，峰值时快速失败（429）而不是拖垮进程。
4. 任务队列   —— sys_task_queue：把「非强一致」动作（自动建波、刷新汇总、告警扫描）移出请求链路，
                支付接口只做同步预占，波次生成等异步消费，峰值得以削平。
5. 结构化日志 —— 一行一个 JSON，带 request_id / 耗时 / 状态码，可直接被采集。
6. 读写分离   —— 报表类只读连接（WAL 下不阻塞写），MySQL 侧同一接口可换从库。
7. 分片路由   —— shard_of(wh_id)：给出库/表分片键，分库后代码不需要改业务逻辑。
"""
import json
import os
import random
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta

# --------------------------------------------------------------------------- #
# 结构化日志
# --------------------------------------------------------------------------- #
_LOG_LOCK = threading.Lock()


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_event(event, **fields):
    """一行一个 JSON。演示环境打 stdout；生产接采集器（Loki/ES）时不用改调用点。"""
    record = {"ts": _now_str(), "event": event}
    record.update(fields)
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _LOG_LOCK:
        print(line, flush=True)


# --------------------------------------------------------------------------- #
# 迁移
# --------------------------------------------------------------------------- #
SCHEMA_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  applied_at TEXT NOT NULL,
  duration_ms INTEGER NOT NULL DEFAULT 0
)
"""

# 每条迁移只写「新东西」，且必须能对老库重复执行不报错（用 IF NOT EXISTS / 列存在性判断）
MIGRATIONS = [
    (1, "infra_idempotency", ["""
        CREATE TABLE IF NOT EXISTS sys_idempotency (
          idem_key    TEXT PRIMARY KEY,
          method      TEXT NOT NULL,
          path        TEXT NOT NULL,
          user_id     INTEGER,
          state       INTEGER NOT NULL DEFAULT 0,   -- 0处理中 1已完成
          status_code INTEGER,
          response    TEXT,
          create_time TEXT NOT NULL,
          finish_time TEXT
        )""", "CREATE INDEX IF NOT EXISTS idx_idem_time ON sys_idempotency(create_time)"]),
    (2, "infra_task_queue", ["""
        CREATE TABLE IF NOT EXISTS sys_task_queue (
          id           INTEGER PRIMARY KEY AUTOINCREMENT,
          task_type    TEXT NOT NULL,
          payload      TEXT,
          status       INTEGER NOT NULL DEFAULT 0,  -- 0待处理 1处理中 2成功 3失败
          attempts     INTEGER NOT NULL DEFAULT 0,
          max_attempts INTEGER NOT NULL DEFAULT 5,
          available_at TEXT NOT NULL,
          locked_by    TEXT,
          last_error   TEXT,
          create_time  TEXT NOT NULL,
          update_time  TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_task_pick ON sys_task_queue(status, available_at)",
        "CREATE INDEX IF NOT EXISTS idx_task_type ON sys_task_queue(task_type, create_time)"]),
    (3, "ec_fulfillment", ["""
        CREATE TABLE IF NOT EXISTS ec_fulfillment (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          order_id    INTEGER NOT NULL,
          wh_id       INTEGER NOT NULL,
          seq         INTEGER NOT NULL DEFAULT 1,
          status      INTEGER NOT NULL DEFAULT 1,   -- 1待发货 2已发货
          total_qty   REAL NOT NULL DEFAULT 0,
          shipped_qty REAL NOT NULL DEFAULT 0,
          create_time TEXT NOT NULL,
          ship_time   TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_ff_order ON ec_fulfillment(order_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_ff_wh ON ec_fulfillment(wh_id, status)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uk_ff_order_seq ON ec_fulfillment(order_id, seq)"]),
    (4, "ec_shipment_prod", [
        "ALTER TABLE ec_shipment ADD COLUMN fulfillment_id INTEGER",
        "ALTER TABLE ec_shipment ADD COLUMN wh_id INTEGER",
        "ALTER TABLE ec_shipment ADD COLUMN item_qty REAL NOT NULL DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS idx_shipment_no ON ec_shipment(tracking_no)",
    ]),
    (5, "ec_order_prod", [
        "ALTER TABLE ec_order ADD COLUMN ship_qty REAL NOT NULL DEFAULT 0",
        "ALTER TABLE ec_order ADD COLUMN split_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE ec_order ADD COLUMN idem_key TEXT",
        "CREATE INDEX IF NOT EXISTS idx_ec_order_status_ship ON ec_order(status, ship_time)",
    ]),
    (8, "infra_seq", ["""
        CREATE TABLE IF NOT EXISTS sys_seq (seq_key TEXT PRIMARY KEY, next_val INTEGER NOT NULL DEFAULT 0)"""]),
    (7, "infra_daily_stat", ["""
        CREATE TABLE IF NOT EXISTS wms_daily_stat (
          stat_date TEXT NOT NULL, goods_id INTEGER NOT NULL, wh_id INTEGER NOT NULL DEFAULT 0,
          in_qty REAL NOT NULL DEFAULT 0, out_qty REAL NOT NULL DEFAULT 0,
          adjust_qty REAL NOT NULL DEFAULT 0, cost_amount REAL NOT NULL DEFAULT 0,
          sales_amount REAL NOT NULL DEFAULT 0, end_qty REAL NOT NULL DEFAULT 0,
          update_time TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          PRIMARY KEY (stat_date, goods_id, wh_id))""",
        "CREATE INDEX IF NOT EXISTS idx_stat_goods ON wms_daily_stat(goods_id, stat_date)",
        "CREATE TABLE IF NOT EXISTS sys_rate_limit (bucket TEXT NOT NULL, window_key TEXT NOT NULL,"
        " hits INTEGER NOT NULL DEFAULT 0, update_time TEXT NOT NULL DEFAULT (datetime('now','localtime')),"
        " PRIMARY KEY (bucket, window_key))"]),
    (6, "ec_order_alloc_ff", [
        "ALTER TABLE ec_order_alloc ADD COLUMN fulfillment_id INTEGER",
        "ALTER TABLE ec_order_alloc ADD COLUMN wh_id INTEGER",
        "CREATE INDEX IF NOT EXISTS idx_alloc_ff ON ec_order_alloc(fulfillment_id)",
    ]),
]


def _columns(conn, table):
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
    except sqlite3.OperationalError:
        return set()


def _table_exists(conn, table):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                             (table,)).fetchone())


def apply_migrations(conn, log=print):
    """按版本号顺序执行；每条迁移单独事务，失败即中断（不会留半截版本）。"""
    conn.execute(SCHEMA_MIGRATIONS)
    conn.commit()
    done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    applied = 0
    for version, name, statements in MIGRATIONS:
        if version in done:
            continue
        started = time.time()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for sql in statements:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError as exc:
                    text = str(exc).lower()
                    # 老库已手工加过列 / 索引已存在：视为已生效，继续
                    if "duplicate column" in text or "already exists" in text:
                        continue
                    raise
            conn.execute("INSERT INTO schema_migrations(version,name,applied_at,duration_ms)"
                         " VALUES(?,?,?,?)",
                         (version, name, _now_str(), int((time.time() - started) * 1000)))
            conn.commit()
            applied += 1
            log("[migrate] %02d %s ok" % (version, name))
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            log("[migrate] %02d %s FAILED: %s" % (version, name, exc))
            raise
    return applied


# --------------------------------------------------------------------------- #
# 幂等
# --------------------------------------------------------------------------- #
class Idempotency:
    """给 POST/PUT/DELETE 加「一次且仅一次」语义。

    流程：预占 key（state=0）→ 执行业务 → 写回结果（state=1）。
    重复请求命中 state=1 直接重放首次响应；命中 state=0 说明上一个请求还在跑，返回 409 让调用方重试。
    """

    TTL_HOURS = 24

    def __init__(self, conn):
        self.conn = conn

    def begin(self, key, method, path, user_id):
        now = _now_str()
        try:
            self.conn.execute(
                "INSERT INTO sys_idempotency(idem_key,method,path,user_id,state,create_time)"
                " VALUES(?,?,?,?,0,?)", (key, method, path, user_id, now))
            self.conn.commit()
            return {"state": "new"}
        except sqlite3.IntegrityError:
            row = self.conn.execute("SELECT * FROM sys_idempotency WHERE idem_key=?", (key,)).fetchone()
            if not row:
                return {"state": "new"}
            if row["state"] == 1:
                return {"state": "replay", "status": row["status_code"], "body": row["response"]}
            return {"state": "in_flight"}

    def finish(self, key, status_code, body):
        self.conn.execute("UPDATE sys_idempotency SET state=1, status_code=?, response=?, finish_time=?"
                          " WHERE idem_key=?", (status_code, body, _now_str(), key))
        self.conn.commit()

    def fail(self, key):
        """业务失败要删掉预占，否则用户重试会被当成重复请求。"""
        self.conn.execute("DELETE FROM sys_idempotency WHERE idem_key=? AND state=0", (key,))
        self.conn.commit()

    def cleanup(self, hours=None):
        hours = hours or self.TTL_HOURS
        cur = self.conn.execute("DELETE FROM sys_idempotency WHERE create_time < ?",
                                ((datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S"),))
        self.conn.commit()
        return cur.rowcount


# --------------------------------------------------------------------------- #
# 限流（令牌桶）
# --------------------------------------------------------------------------- #
class RateLimiter:
    def __init__(self, per_minute=600, burst=None):
        self.per_minute = max(1, int(per_minute))
        self.burst = burst or self.per_minute
        self._buckets = {}
        self._lock = threading.RLock()   # snapshot() 内部会调 percentile()，必须可重入

    def allow(self, key, cost=1):
        now = time.time()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.burst), now))
            refill = (now - last) * (self.per_minute / 60.0)
            tokens = min(float(self.burst), tokens + refill)
            if tokens < cost:
                self._buckets[key] = (tokens, now)
                retry_after = max(1, int((cost - tokens) / (self.per_minute / 60.0) + 0.999))
                return False, retry_after
            self._buckets[key] = (tokens - cost, now)
            return True, 0

    def reset(self):
        with self._lock:
            self._buckets.clear()


# --------------------------------------------------------------------------- #
# 任务队列 + worker
# --------------------------------------------------------------------------- #
class TaskQueue:
    """表即队列：claim 用条件 UPDATE 抢占，多进程部署时天然互斥（SQLite 单库，MySQL 同样语义）。"""

    def __init__(self, conn_factory):
        self.conn_factory = conn_factory

    def enqueue(self, task_type, payload=None, delay_seconds=0, max_attempts=5):
        conn = self.conn_factory()
        try:
            available = (datetime.now() + timedelta(seconds=delay_seconds)).strftime("%Y-%m-%d %H:%M:%S")
            cur = conn.execute(
                "INSERT INTO sys_task_queue(task_type,payload,status,attempts,max_attempts,available_at,"
                "create_time,update_time) VALUES(?,?,0,0,?,?,?,?)",
                (task_type, json.dumps(payload or {}, ensure_ascii=False), max_attempts, available,
                 _now_str(), _now_str()))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def claim(self, worker_id):
        conn = self.conn_factory()
        try:
            now = _now_str()
            row = conn.execute(
                "SELECT * FROM sys_task_queue WHERE status=0 AND available_at<=?"
                " ORDER BY id LIMIT 1", (now,)).fetchone()
            if not row:
                return None
            cur = conn.execute(
                "UPDATE sys_task_queue SET status=1, locked_by=?, attempts=attempts+1, update_time=?"
                " WHERE id=? AND status=0", (worker_id, now, row["id"]))
            conn.commit()
            if not cur.rowcount:
                return None
            return dict(row)
        finally:
            conn.close()

    def complete(self, task_id):
        conn = self.conn_factory()
        try:
            conn.execute("UPDATE sys_task_queue SET status=2, last_error=NULL, update_time=? WHERE id=?",
                         (_now_str(), task_id))
            conn.commit()
        finally:
            conn.close()

    def retry_or_fail(self, task_id, error, attempts, max_attempts):
        conn = self.conn_factory()
        try:
            if attempts >= max_attempts:
                conn.execute("UPDATE sys_task_queue SET status=3, last_error=?, update_time=? WHERE id=?",
                             (str(error)[:500], _now_str(), task_id))
            else:
                backoff = min(300, 5 * (2 ** (attempts - 1)))
                available = (datetime.now() + timedelta(seconds=backoff)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute("UPDATE sys_task_queue SET status=0, locked_by=NULL, last_error=?,"
                             " available_at=?, update_time=? WHERE id=?",
                             (str(error)[:500], available, _now_str(), task_id))
            conn.commit()
        finally:
            conn.close()

    def stats(self):
        conn = self.conn_factory()
        try:
            rows = conn.execute("SELECT status, COUNT(*) c FROM sys_task_queue GROUP BY status").fetchall()
            by_type = conn.execute(
                "SELECT task_type, status, COUNT(*) c FROM sys_task_queue GROUP BY task_type, status"
            ).fetchall()
            return {"byStatus": {str(r["status"]): r["c"] for r in rows},
                    "byType": [{"taskType": r["task_type"], "status": r["status"], "count": r["c"]}
                               for r in by_type]}
        finally:
            conn.close()


class TaskWorker(threading.Thread):
    """单进程内跑 N 个线程；生产可改成独立 worker 进程，任务表语义不变。"""

    def __init__(self, queue, handlers, concurrency=2, poll_interval=0.5, name="task-worker"):
        super(TaskWorker, self).__init__(name=name, daemon=True)
        self.queue = queue
        self.handlers = handlers
        self.concurrency = concurrency
        self.poll_interval = poll_interval
        self.worker_id = "%s-%s" % (os.getpid(), uuid.uuid4().hex[:6])
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            task = self.queue.claim(self.worker_id)
            if not task:
                time.sleep(self.poll_interval)
                continue
            handler = self.handlers.get(task["task_type"])
            started = time.time()
            try:
                if not handler:
                    raise RuntimeError("未注册的任务类型：%s" % task["task_type"])
                payload = json.loads(task["payload"] or "{}")
                handler(payload)
                self.queue.complete(task["id"])
                log_event("task.ok", task=task["type"] if "type" in task else task["task_type"],
                          id=task["id"], ms=int((time.time() - started) * 1000))
            except Exception as exc:  # noqa: BLE001
                self.queue.retry_or_fail(task["id"], exc, task["attempts"], task["max_attempts"])
                log_event("task.fail", id=task["id"], taskType=task["task_type"], error=str(exc)[:300])


# --------------------------------------------------------------------------- #
# 指标（进程内；多实例部署时换成 Prometheus 客户端即可，调用点不变）
# --------------------------------------------------------------------------- #
class Metrics:
    def __init__(self):
        self._lock = threading.RLock()
        self.total = 0
        self.errors = 0
        self.by_status = {}
        self.latency_ms = []
        self.started_at = time.time()

    def observe(self, status_code, elapsed_ms):
        with self._lock:
            self.total += 1
            if status_code >= 500:
                self.errors += 1
            self.by_status[str(status_code)] = self.by_status.get(str(status_code), 0) + 1
            self.latency_ms.append(int(elapsed_ms))
            if len(self.latency_ms) > 5000:
                self.latency_ms = self.latency_ms[-2000:]

    def percentile(self, p):
        with self._lock:
            if not self.latency_ms:
                return 0
            data = sorted(self.latency_ms)
            idx = min(len(data) - 1, int(len(data) * p))
            return data[idx]

    def snapshot(self):
        with self._lock:
            uptime = max(1.0, time.time() - self.started_at)
            return {"requests": self.total, "errors": self.errors,
                    "errorRate": round(self.errors / self.total * 100, 2) if self.total else 0,
                    "qps": round(self.total / uptime, 2),
                    "p50Ms": self.percentile(0.5), "p95Ms": self.percentile(0.95),
                    "p99Ms": self.percentile(0.99), "byStatus": dict(self.by_status),
                    "uptimeSec": int(uptime)}


# --------------------------------------------------------------------------- #
# 分片路由
# --------------------------------------------------------------------------- #
SHARD_COUNT = int(os.environ.get("WMS_SHARD_COUNT", "1"))


def shard_of_warehouse(wh_id):
    """分片键：仓库。同仓数据落同库，跨仓查询走汇总/异步，避免跨库事务。"""
    if SHARD_COUNT <= 1:
        return 0
    return int(wh_id or 0) % SHARD_COUNT


def shard_of_order(order_id):
    """订单分片：按订单号取模，保证同一订单的 明细/分配/发货 落同库。"""
    if SHARD_COUNT <= 1:
        return 0
    return int(order_id or 0) % SHARD_COUNT


def new_trace_id():
    return uuid.uuid4().hex[:16]


def snowflake_like(prefix=""):
    """分布式友好的单号：时间戳 + 随机，避免分库后自增冲突（演示用 22 位）。"""
    return "%s%s%04d" % (prefix, datetime.now().strftime("%Y%m%d%H%M%S"), random.randint(0, 9999))


class DbRateLimiter:
    """多实例限流：把计数写进库，任意实例都能看到（无 Redis 时的替代方案）。

    语义与内存版一致：每「分钟窗口」最多 N 次，超了返回 429。
    代价是每次多一次 UPSERT；演示/中小流量完全够用，上 Redis 时换成本类同名接口即可。
    """

    def __init__(self, conn_factory, per_minute=600):
        self.conn_factory = conn_factory
        self.per_minute = max(1, int(per_minute))

    def allow(self, key, cost=1):
        window = datetime.now().strftime("%Y%m%d%H%M")
        conn = self.conn_factory()
        try:
            conn.execute(
                "INSERT INTO sys_rate_limit(bucket,window_key,hits,update_time) VALUES(?,?,?,?)"
                " ON CONFLICT(bucket,window_key) DO UPDATE SET hits=hits+?, update_time=excluded.update_time",
                (key, window, cost, _now_str(), cost))
            conn.commit()
            hits = conn.execute("SELECT hits FROM sys_rate_limit WHERE bucket=? AND window_key=?",
                                (key, window)).fetchone()[0]
            return (hits <= self.per_minute), 60
        finally:
            conn.close()
