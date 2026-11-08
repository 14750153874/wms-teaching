#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零依赖压测脚本：测 QPS / P50 / P95 / P99 / 错误率。

用法：
    python scripts/loadtest.py --port 8010 --seconds 10 --threads 20 --mode read
    python scripts/loadtest.py --port 8010 --seconds 10 --threads 8  --mode order
    python scripts/loadtest.py --port 8010 --seconds 10 --threads 8  --mode pay

三种模式：
    read   GET /api/v1/shop/goods?shopId=1（商城列表，最热的读）
    order  POST /api/v1/shop/orders（下单，写但不扣库存）
    pay    POST /api/v1/shop/orders + /pay（真扣库存，会消耗库存，慎用）

输出包含：总请求、成功/失败、QPS、P50/P95/P99、错误码分布。
"""
import argparse
import json
import statistics
import threading
import time
import urllib.error
import urllib.request

LOCK = threading.Lock()
LAT = []
CODES = {}
PAID = []


def call(method, url, body=None, token=None, timeout=15):
    data = None
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            code = payload.get("code", resp.status)
            return code, payload, (time.time() - started) * 1000
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            code = payload.get("code", exc.code)
        except Exception:  # noqa: BLE001
            code, payload = exc.code, {}
        return code, payload, (time.time() - started) * 1000
    except Exception as exc:  # noqa: BLE001
        return -1, {"message": str(exc)}, (time.time() - started) * 1000


def worker(mode, base, token, stop_at, goods_id):
    while time.time() < stop_at:
        if mode == "read":
            code, _, ms = call("GET", base + "/api/v1/shop/goods?shopId=1", token=token)
        elif mode == "order":
            code, _, ms = call("POST", base + "/api/v1/shop/orders", {
                "shop_id": 1, "buyer_name": "压测买家", "receiver_name": "压测",
                "receiver_phone": "13800000000", "receiver_address": "压测地址",
                "items": [{"goods_id": goods_id, "qty": 1}]}, token=token)
        else:
            code, payload, ms = call("POST", base + "/api/v1/shop/orders", {
                "shop_id": 1, "buyer_name": "压测买家", "receiver_name": "压测",
                "receiver_phone": "13800000000", "receiver_address": "压测地址",
                "items": [{"goods_id": goods_id, "qty": 1}]}, token=token)
            if code == 200 and payload.get("data", {}).get("id"):
                oid = payload["data"]["id"]
                code, payload, ms2 = call("POST", base + "/api/v1/shop/orders/%d/pay" % oid, {}, token=token)
                ms += ms2
                if code == 200:
                    with LOCK:
                        PAID.append(oid)
        with LOCK:
            LAT.append(ms)
            CODES[code] = CODES.get(code, 0) + 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--seconds", type=int, default=10)
    ap.add_argument("--threads", type=int, default=10)
    ap.add_argument("--mode", default="read", choices=["read", "order", "pay"])
    args = ap.parse_args()
    base = "http://%s:%d" % (args.host, args.port)

    code, payload, _ = call("POST", base + "/api/v1/auth/login", {"username": "admin", "password": "123456"})
    token = payload.get("data", {}).get("accessToken")
    if not token:
        raise SystemExit("登录失败：%s" % payload)
    goods = call("GET", base + "/api/v1/shop/goods?shopId=1", token=token)[1]["data"]["list"]
    goods_id = next(g["goods_id"] for g in goods if float(g.get("saleable") or 0) > 2)

    print("[loadtest] mode=%s threads=%d seconds=%s goods=%s" % (args.mode, args.threads, args.seconds, goods_id))
    stop_at = time.time() + args.seconds
    threads = [threading.Thread(target=worker, args=(args.mode, base, token, stop_at, goods_id))
               for _ in range(args.threads)]
    started = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = max(0.001, time.time() - started)

    total = len(LAT)
    ok = CODES.get(200, 0)
    lat = sorted(LAT)
    pct = lambda p: lat[min(len(lat) - 1, int(len(lat) * p))] if lat else 0
    print("-" * 60)
    print("请求总数 : %d   成功 %d   失败 %d   耗时 %.1fs" % (total, ok, total - ok, elapsed))
    print("QPS      : %.1f" % (total / elapsed))
    print("延迟 ms  : P50=%.1f  P95=%.1f  P99=%.1f  max=%.1f" % (
        pct(0.5), pct(0.95), pct(0.99), lat[-1] if lat else 0))
    # 区分「系统故障」和「业务拒绝」：库存不足/重复请求是正常业务结果，不该算故障
    SYSTEM_BAD = {50000, 50301, 42901}
    sys_bad = sum(v for k, v in CODES.items() if k in SYSTEM_BAD or k < 0)
    biz_bad = sum(v for k, v in CODES.items() if k != 200 and k not in SYSTEM_BAD and k >= 0)
    print("错误码   : %s" % ", ".join("%s=%s" % (k, v) for k, v in sorted(CODES.items(), key=lambda x: -x[1])))
    print("系统错误 : %d   %s" % (sys_bad, "OK" if sys_bad == 0 else "<-- 需要排查"))
    print("业务拒绝 : %d   （库存不足/重复请求等，属正常结果）" % biz_bad)
    print("有效成功率: %.2f%%" % (ok * 100.0 / max(1, ok + sys_bad)))
    if PAID:
        print("实际支付成功订单数 : %d" % len(PAID))

    checks = call("GET", base + "/api/v1/ops/checks", token=token)[1].get("data") or {}
    print("压测后一致性校验   : allPassed=%s" % checks.get("allPassed"))

    # 追加一行 CSV，方便对比多次长压测
    try:
        import os
        os.makedirs("logs", exist_ok=True)
        path = os.path.join("logs", "loadtest.csv")
        newfile = not os.path.exists(path)
        with open(path, "a", encoding="utf-8-sig") as fh:
            if newfile:
                fh.write("time,mode,threads,seconds,total,ok,fail,qps,p50_ms,p95_ms,p99_ms,max_ms,checks\n")
            fh.write("%s,%s,%d,%d,%d,%d,%d,%.1f,%.1f,%.1f,%.1f,%.1f,%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), args.mode, args.threads, args.seconds,
                total, ok, total - ok, total / elapsed, pct(0.5), pct(0.95), pct(0.99),
                lat[-1] if lat else 0, checks.get("allPassed")))
        print("结果已追加 : logs/loadtest.csv")
    except Exception as exc:  # noqa: BLE001
        print("写 CSV 失败：%s" % exc)


if __name__ == "__main__":
    main()
