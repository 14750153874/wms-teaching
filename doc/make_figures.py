# -*- coding: utf-8 -*-
"""生成 WMS 方案文档需要的示意图（Pillow 绘制，中文字体用系统微软雅黑）。

输出到 src/figures/：
  fig1_layers.png   系统分层架构图
  fig2_deploy.png   联网部署架构图
  fig3_inbound.png  入库业务流程图
  fig4_outbound.png 出库业务流程图
  fig5_er.png       核心数据表关系图
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent / "figures"
OUT.mkdir(parents=True, exist_ok=True)

FONT_REG = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
SCALE = 2

INK = (34, 38, 46)
MUTED = (108, 116, 130)
LINE = (150, 158, 172)
WHITE = (255, 255, 255)
BLUE = (232, 240, 254)
BLUE_BD = (66, 118, 208)
GREEN = (232, 246, 236)
GREEN_BD = (56, 150, 96)
ORANGE = (255, 243, 224)
ORANGE_BD = (216, 142, 32)
PURPLE = (243, 238, 252)
PURPLE_BD = (132, 100, 200)
GREY = (243, 245, 248)
GREY_BD = (140, 148, 162)
RED = (253, 236, 236)
RED_BD = (200, 78, 78)


def font(size, bold=False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size * SCALE)


def canvas(w, h):
    img = Image.new("RGB", (w * SCALE, h * SCALE), WHITE)
    return img, ImageDraw.Draw(img)


def text_size(draw, s, f):
    box = draw.textbbox((0, 0), s, font=f)
    return box[2] - box[0], box[3] - box[1]


def center_text(draw, cx, cy, s, f, fill=INK):
    w, h = text_size(draw, s, f)
    draw.text((cx * SCALE - w / 2, cy * SCALE - h / 2), s, font=f, fill=fill)


def box(draw, x, y, w, h, fill, outline, radius=8, width=2):
    draw.rounded_rectangle(
        [x * SCALE, y * SCALE, (x + w) * SCALE, (y + h) * SCALE],
        radius=radius * SCALE,
        fill=fill,
        outline=outline,
        width=width * SCALE,
    )


def label_box(draw, x, y, w, h, title, sub=None, fill=BLUE, outline=BLUE_BD, ts=15, ss=12):
    box(draw, x, y, w, h, fill, outline)
    cx, cy = x + w / 2, y + h / 2
    if sub:
        center_text(draw, cx, cy - 10, title, font(ts, True))
        center_text(draw, cx, cy + 11, sub, font(ss), MUTED)
    else:
        center_text(draw, cx, cy, title, font(ts, True))


def arrow(draw, x1, y1, x2, y2, color=LINE, width=2, head=9, dash=False):
    p1 = (x1 * SCALE, y1 * SCALE)
    p2 = (x2 * SCALE, y2 * SCALE)
    if dash:
        # 简单虚线
        import math

        total = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        step = 10 * SCALE
        seg = 5 * SCALE
        for off in range(0, int(total), step):
            t0, t1 = off / total, min((off + seg) / total, 1.0)
            draw.line(
                [
                    (p1[0] + (p2[0] - p1[0]) * t0, p1[1] + (p2[1] - p1[1]) * t0),
                    (p1[0] + (p2[0] - p1[0]) * t1, p1[1] + (p2[1] - p1[1]) * t1),
                ],
                fill=color,
                width=width * SCALE,
            )
    else:
        draw.line([p1, p2], fill=color, width=width * SCALE)
    # 箭头三角
    import math

    ang = math.atan2(p2[1] - p1[1], p2[0] - p1[0])
    hs = head * SCALE
    draw.polygon(
        [
            p2,
            (p2[0] - hs * math.cos(ang - 0.42), p2[1] - hs * math.sin(ang - 0.42)),
            (p2[0] - hs * math.cos(ang + 0.42), p2[1] - hs * math.sin(ang + 0.42)),
        ],
        fill=color,
    )


def title_bar(draw, x, y, w, s, fill=GREY, outline=GREY_BD, ts=14):
    box(draw, x, y, w, 30, fill, outline)
    center_text(draw, x + w / 2, y + 15, s, font(ts, True))


# ---------------------------------------------------------------- 图1 分层架构
def fig1():
    W, H = 900, 470
    img, d = canvas(W, H)
    center_text(d, W / 2, 24, "图 1  系统分层架构（前后端分离四层）", font(16, True))
    rows = [
        ("表现层", "用户界面", BLUE, BLUE_BD,
         [("PC 管理后台", "Vue 3 + Element Plus"), ("手机浏览器", "响应式页面"), ("PDA 浏览器", "扫码作业（预留）")]),
        ("业务层", "业务规则", GREEN, GREEN_BD,
         [("认证与权限", "Spring Security + JWT"), ("WMS 业务服务", "入库 / 出库 / 库存 / 盘点"), ("报表与导出", "EasyExcel")]),
        ("数据层", "数据与缓存", ORANGE, ORANGE_BD,
         [("MySQL 8", "业务数据 / 台账 / 日志"), ("Redis 7", "登录态 / 热点库存"), ("每日全量备份", "保留 30 天 + 异地副本")]),
        ("基础设施层", "运行环境", PURPLE, PURPLE_BD,
         [("云服务器", "Ubuntu 22.04 LTS"), ("Docker Compose", "一键编排与升级"), ("Nginx", "反向代理 / HTTPS")]),
    ]
    y = 50
    for name, desc, fill, bd, items in rows:
        box(d, 24, y, 130, 92, fill, bd)
        center_text(d, 24 + 65, y + 36, name, font(15, True))
        center_text(d, 24 + 65, y + 60, desc.split(" / ")[0], font(10), MUTED)
        for i, (t, s) in enumerate(items):
            label_box(d, 176 + i * 234, y + 14, 218, 64, t, s, fill=WHITE, outline=bd, ts=13, ss=10)
        y += 104
    arrow(d, 89, 142, 89, 154, LINE, 2)
    arrow(d, 89, 246, 89, 258, LINE, 2)
    arrow(d, 89, 350, 89, 362, LINE, 2)
    img.save(OUT / "fig1_layers.png")


# ---------------------------------------------------------------- 图2 部署架构
def fig2():
    W, H = 900, 520
    img, d = canvas(W, H)
    center_text(d, W / 2, 22, "图 2  联网部署架构（单台公网云服务器 + Docker Compose）", font(16, True))

    title_bar(d, 24, 48, 250, "客户端（B/S，免安装）", GREY, GREY_BD)
    label_box(d, 24, 86, 250, 44, "PC 浏览器（仓库办公室）", None, WHITE, GREY_BD, 12)
    label_box(d, 24, 138, 250, 44, "手机浏览器（主管 / 老板）", None, WHITE, GREY_BD, 12)
    label_box(d, 24, 190, 250, 44, "PDA 浏览器（可选，扫码作业）", None, WHITE, GREY_BD, 12)

    label_box(d, 24, 262, 250, 52, "互联网 / HTTPS 443", "全站加密传输", BLUE, BLUE_BD, 13, 10)

    # 服务器大框
    box(d, 320, 48, 556, 448, GREY, GREY_BD, radius=10)
    center_text(d, 598, 70, "公网云服务器（Ubuntu 22.04 LTS + Docker Compose）", font(13, True))
    box(d, 336, 88, 524, 30, WHITE, GREY_BD)
    center_text(d, 598, 103, "防火墙：仅开放 80 / 443（SSH 限来源 IP）", font(11), MUTED)

    label_box(d, 400, 132, 200, 52, "Nginx", "反向代理 / HTTPS 终止", ORANGE, ORANGE_BD, 13, 10)
    label_box(d, 620, 132, 220, 52, "前端静态资源", "Vue 3 打包产物", BLUE, BLUE_BD, 13, 10)
    label_box(d, 400, 216, 200, 56, "wms-api", "Spring Boot 3.2 / JDK 17", GREEN, GREEN_BD, 13, 10)
    label_box(d, 620, 216, 220, 56, "Redis 7", "登录态 / 热点库存（密码 + 仅内网）", RED, RED_BD, 13, 10)
    label_box(d, 400, 300, 200, 56, "MySQL 8", "业务库（不对外暴露端口）", ORANGE, ORANGE_BD, 14, 10)
    label_box(d, 620, 300, 220, 56, "数据卷 / 备份", "每日全量 + 每周异地副本", PURPLE, PURPLE_BD, 13, 10)

    arrow(d, 274, 288, 396, 158, LINE, 2)
    arrow(d, 600, 184, 600, 216, LINE, 2)
    arrow(d, 500, 272, 500, 300, LINE, 2)
    arrow(d, 620, 244, 600, 244, LINE, 2, dash=True)
    arrow(d, 600, 356, 620, 328, LINE, 2, dash=True)

    center_text(d, 598, 396, "所有业务数据集中存储，多终端实时一致；服务器只暴露 80/443，数据库与缓存仅容器内网可达",
                font(11), MUTED)
    center_text(d, 598, 424, "部署方式：前端静态资源 + 后端 Jar 打入镜像，docker compose up -d 一键启动 / 升级 / 回滚",
                font(11), MUTED)
    center_text(d, 598, 452, "备份：mysqldump 每日凌晨全量（保留 30 天），每周同步至对象存储（异地）",
                font(11), MUTED)
    img.save(OUT / "fig2_deploy.png")


# ---------------------------------------------------------------- 图3/4 流程图
def flow(filename, title, steps, notes, branch=None):
    W, H = 1160, 430
    img, d = canvas(W, H)
    center_text(d, W / 2, 22, title, font(16, True))
    n = len(steps)
    bw, gap = 168, 18
    x = (W - (n * bw + (n - 1) * gap)) / 2
    for i, s in enumerate(steps):
        fill, bd = (GREEN, GREEN_BD) if i < n - 1 else (ORANGE, ORANGE_BD)
        label_box(d, x + i * (bw + gap), 62, bw, 66, s[0], s[1], fill, bd, 12, 9)
        if i < n - 1:
            arrow(d, x + i * (bw + gap) + bw, 95, x + (i + 1) * (bw + gap), 95, LINE, 2, head=8)
    if branch:
        bx, by = x + branch[0] * (bw + gap), 152
        label_box(d, bx, by, bw, 48, branch[1], branch[2], RED, RED_BD, 11, 9)
        arrow(d, bx + bw / 2, 128, bx + bw / 2, by, RED_BD, 2, head=8)
    box(d, 40, 242, W - 80, 164, GREY, GREY_BD, radius=8)
    center_text(d, W / 2, 264, "关键规则与并发处理", font(13, True))
    y = 290
    for t in notes:
        d.text((62 * SCALE, y * SCALE), "· " + t, font=font(11), fill=INK)
        y += 27
    img.save(OUT / filename)


# ---------------------------------------------------------------- 图5 ER
def fig5():
    W, H = 940, 700
    img, d = canvas(W, H)
    center_text(d, W / 2, 22, "图 5  核心数据表关系（节选）", font(16, True))

    def table(x, y, w, name, fields, fill=BLUE, bd=BLUE_BD):
        h = 30 + len(fields) * 20
        box(d, x, y, w, h, fill, bd)
        d.rectangle([x * SCALE, y * SCALE, (x + w) * SCALE, (y + 26) * SCALE], fill=bd)
        center_text(d, x + w / 2, y + 13, name, font(11, True), WHITE)
        for i, f in enumerate(fields):
            d.text(((x + 10) * SCALE, (y + 32 + i * 20) * SCALE), f, font=font(10), fill=INK)
        return (x, y, w, h)

    goods = table(40, 60, 210, "base_goods 商品", ["id / goods_code（唯一）", "goods_name / category_id", "unit / warning_low / warning_high", "is_deleted / 公共字段"])
    loc = table(40, 210, 210, "base_location 库位树", ["id / loc_code（唯一）", "loc_name / parent_id", "loc_type 仓库/库区/货架/库位", "status"])
    inv = table(360, 130, 230, "wms_inventory 库存", ["id", "goods_id + loc_id + batch_no（唯一）", "quantity / locked_qty", "available_qty / version"], GREEN, GREEN_BD)
    txn = table(360, 300, 230, "wms_inventory_txn 流水", ["id / txn_no", "biz_type / biz_no", "goods_id / loc_id / qty 变动", "before_qty / after_qty"], GREEN, GREEN_BD)
    ino = table(680, 60, 220, "wms_in_order 入库单", ["id / order_no（唯一）", "order_type / supplier_id", "status / total_num", "create_by / create_time"])
    ind = table(680, 200, 220, "wms_in_order_detail", ["id / order_id", "goods_id / loc_id", "plan_num / actual_num", "batch_no / expire_date"])
    outo = table(680, 340, 220, "wms_out_order 出库单", ["id / order_no（唯一）", "order_type / customer_id", "status / total_num", "request_id（幂等）"], ORANGE, ORANGE_BD)
    outd = table(680, 470, 220, "wms_out_order_detail", ["id / order_id", "goods_id / loc_id", "plan_num / actual_num", "batch_no"], ORANGE, ORANGE_BD)
    stk = table(40, 360, 210, "wms_stocktake_order 盘点", ["id / order_no", "loc_id / status", "plan_qty / actual_qty", "diff_qty"], PURPLE, PURPLE_BD)
    user = table(40, 500, 210, "sys_user / sys_role", ["用户-角色-权限", "菜单 + 按钮 + 数据权限", "sys_operation_log 审计", "sys_config 参数"], GREY, GREY_BD)

    arrow(d, 250, 165, 356, 165, LINE, 2, head=8)
    arrow(d, 250, 250, 356, 200, LINE, 2, head=8)
    arrow(d, 470, 244, 470, 296, LINE, 2, head=8)
    arrow(d, 590, 180, 676, 120, LINE, 2, head=8)
    arrow(d, 680, 130, 596, 190, LINE, 2, head=8, dash=True)
    arrow(d, 590, 330, 676, 260, LINE, 2, head=8)
    arrow(d, 680, 270, 596, 340, LINE, 2, head=8, dash=True)
    arrow(d, 590, 400, 676, 390, LINE, 2, head=8)
    arrow(d, 680, 400, 596, 410, LINE, 2, head=8, dash=True)
    center_text(d, 450, 560, "实线＝业务写入方向（库存变动必须同时写 wms_inventory 与 wms_inventory_txn，同一事务内完成）",
                font(10), MUTED)
    center_text(d, 450, 584, "虚线＝外键关联查询方向；所有业务表含 create_time / update_time / is_deleted（逻辑删除）",
                font(10), MUTED)
    img.save(OUT / "fig5_er.png")


def main():
    fig1()
    fig2()
    flow(
        "fig3_inbound.png",
        "图 3  入库业务流程",
        [("创建入库单", "草稿态 / 选供应商"), ("单审核", "主管审核通过"), ("收货验货", "按单收货 / 部分收货"),
         ("录入明细", "实收数 / 库位 / 批次"), ("确认入库", "过账（幂等）"), ("库存增加", "库存 + 流水 + 单据归档")],
        [
            "过账在同一数据库事务内完成：写 wms_inventory（含 locked_qty / version 乐观锁）→ 写 wms_inventory_txn 流水 → 更新单据状态为「已入库」。",
            "确认入库存量支持幂等：客户端携带 request_id，服务端以唯一索引去重，网络重试不会重复加库存。",
            "超额收货自动提醒；部分收货时单据状态为「部分入库」，可继续收货直至关闭。",
            "支持采购入库、退货入库、其他入库；单据全生命周期（草稿 / 已审核 / 部分入库 / 已入库 / 已作废）可追溯。",
        ],
    )
    flow(
        "fig4_outbound.png",
        "图 4  出库业务流程",
        [("创建出库单", "草稿态 / 选客户"), ("单审核", "主管审核通过"), ("拣货核验", "按库位 / 批次拣货"),
         ("确认出库", "校验可用量 + 预占"), ("库存扣减", "FIFO / 批次优先"), ("流水归档", "台账 + 报表")],
        [
            "可用量校验：available_qty = quantity − locked_qty；确认出库时先校验再扣减，不足直接拦截并提示具体商品与缺口数量。",
            "并发控制：出库单审核后即预占（locked_qty 增加），过账时按 version 乐观锁提交，冲突则重试；避免两个门店同时抢最后一件导致超卖。",
            "幂等过账：确认出库接口同样以 request_id 唯一索引防重，重试不会重复扣减。",
            "出库策略：默认 FIFO（按入库时间 / 批次先进先出），支持指定批次出库；出库明细全程留痕，可按单反向追溯。",
        ],
        branch=(3, "库存不足 → 拦截", "提示缺口 / 建议调拨"),
    )
    fig5()
    print("figures written to", OUT)


if __name__ == "__main__":
    main()
