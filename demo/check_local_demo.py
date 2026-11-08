#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地演示回归检查脚本。

功能：
  1) 登录管理员账号
  2) 重置演示数据
  3) 验证关键接口与规则：统计、库存、日期校验、跨仓库校验、负数校验

运行方式：
  python check_local_demo.py
  python check_local_demo.py --base http://127.0.0.1:8000
"""

import argparse
import json
import sys
from urllib import error, request


def http_json(method, url, token=None, payload=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8") or "{}"
        return exc.code, json.loads(body) if body.startswith("{") else {"message": body}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    parser = argparse.ArgumentParser(description="WMS 本地演示回归检查")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="服务基地址")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    # 1) 登录
    status, payload = http_json(
        "POST",
        base + "/api/v1/auth/login",
        payload={"username": "admin", "password": "123456"},
    )
    require(status == 200 and payload.get("code") == 200, f"登录失败：{status} {payload}")
    token = payload["data"]["accessToken"]

    # 2) 重置数据
    status, payload = http_json("POST", base + "/api/v1/admin/reset-demo", token=token)
    require(status == 200 and payload.get("code") == 200, f"重置失败：{status} {payload}")

    # 3) 统计接口可用
    status, payload = http_json("GET", base + "/api/v1/stats/summary", token=token)
    require(status == 200 and payload.get("code") == 200, f"统计接口失败：{status} {payload}")
    require(payload["data"]["goodsCount"] > 0, "仪表盘统计数据为空")

    # 4) 获取商品和库位
    status, payload = http_json("GET", base + "/api/v1/goods?page=1&size=20", token=token)
    require(status == 200 and payload.get("code") == 200, f"商品查询失败：{status} {payload}")
    goods = payload["data"]["list"]
    require(len(goods) > 0, "未获取到商品列表")

    status, payload = http_json("GET", base + "/api/v1/locations", token=token)
    require(status == 200 and payload.get("code") == 200, f"库位查询失败：{status} {payload}")
    all_locs = payload["data"]["list"]
    leaf_locs = [loc for loc in all_locs if loc["loc_type"] == 4]
    require(len(leaf_locs) > 0, "未获取到末级库位")

    # 5) 台账日期范围校验：开始日期晚于结束日期应失败
    status, payload = http_json(
        "GET",
        base + "/api/v1/reports/ledger?begin=2024-02-05&end=2024-02-01",
        token=token,
    )
    require(status == 400 and payload.get("code") == 40000, f"台账日期校验失败：{status} {payload}")

    # 6) 跨仓库库位校验：在 WH01 下提交 WH02 的库位应失败
    wh2_locs = [loc for loc in all_locs if loc["wh_id"] == 2 and loc["loc_type"] == 4]
    require(len(wh2_locs) > 0, "未找到第二个仓库的末级库位")
    bad_payload = {
        "wh_id": 1,
        "partner_id": 2,
        "lines": [{
            "goods_id": goods[0]["id"],
            "loc_id": wh2_locs[0]["id"],
            "plan_num": 5,
            "batch_no": "B-TEST",
        }],
    }
    status, payload = http_json("POST", base + "/api/v1/out-orders", token=token, payload=bad_payload)
    require(status == 400 and payload.get("code") == 40000, f"跨仓库校验失败：{status} {payload}")

    # 7) 负数数量校验：必须被拒绝
    valid_loc = leaf_locs[0]
    bad_qty_payload = {
        "wh_id": 1,
        "partner_id": 1,
        "lines": [{
            "goods_id": goods[0]["id"],
            "loc_id": valid_loc["id"],
            "plan_num": -1,
            "batch_no": "B-NEGATIVE",
        }],
    }
    status, payload = http_json("POST", base + "/api/v1/out-orders", token=token, payload=bad_qty_payload)
    require(status == 400 and payload.get("code") == 40000, f"负数校验失败：{status} {payload}")

    print("[OK] WMS 本地演示关键回归检测已通过")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:  # pragma: no cover
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
