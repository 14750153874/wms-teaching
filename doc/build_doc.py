# -*- coding: utf-8 -*-
"""把各章节 Markdown 合并成整册，并转换成 Word 可导入的 HTML。

用法：
    .\\.venv\\Scripts\\python.exe wms\\src\\build_doc.py

输出：
    wms/src/_book.md          合并后的完整 Markdown（便于人工校对）
    wms/src/_book.html        Word/WPS 导入用 HTML（图片用相对路径 figures/*.png）
    wms/build/_headings.tsv   标题清单（层级 + 文本），供 build_docx.ps1 套用 Word 标题样式
"""
import html
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent
BUILD = SRC / "build"
BUILD.mkdir(parents=True, exist_ok=True)

# 表格/表名统一（把第 6—8 章里沿用的旧表名换成整册统一的命名）
RENAMES = [
    ("wms_stock_flow_history", "wms_inventory_txn_history"),
    ("wms_stock_flow", "wms_inventory_txn"),
    ("sys_goods", "base_goods"),
]

CSS = """
body { font-family: "Microsoft YaHei", "Segoe UI", sans-serif; font-size: 10.5pt; color: #1c2028; line-height: 1.55; }
p { margin: 5pt 0; }
h1 { font-size: 16pt; color: #17335c; margin: 16pt 0 8pt 0; }
h2 { font-size: 13.5pt; color: #1f4a7a; margin: 12pt 0 6pt 0; }
h3 { font-size: 12pt; color: #24557f; margin: 10pt 0 5pt 0; }
h4 { font-size: 11pt; color: #2b5f8a; margin: 8pt 0 4pt 0; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0 10pt 0; }
th, td { border: 1px solid #9aa7b8; padding: 3pt 5pt; font-size: 9pt; vertical-align: top; }
th { background: #e8eef7; font-weight: bold; text-align: center; }
ul, ol { margin: 4pt 0 8pt 0; }
li { margin: 2pt 0; font-size: 10.5pt; }
pre { font-family: Consolas, "Courier New", monospace; font-size: 8.5pt; background: #f5f7fa;
      border: 1px solid #c8d2e0; padding: 5pt; margin: 6pt 0; white-space: pre-wrap; }
code { font-family: Consolas, "Courier New", monospace; font-size: 9pt; background: #f2f4f8; }
blockquote { margin: 5pt 0 8pt 0; padding-left: 8pt; border-left: 3px solid #9ab; color: #33445c; }
.fig { text-align: center; margin: 8pt 0 10pt 0; }
.cover { text-align: center; }
.cover-title { font-size: 24pt; font-weight: bold; color: #17335c; margin: 90pt 0 6pt 0; }
.cover-sub { font-size: 15pt; color: #1f4a7a; margin: 0 0 40pt 0; }
.cover-meta { font-size: 11pt; margin: 4pt 0; }
.h1-like { font-size: 16pt; font-weight: bold; color: #17335c; margin: 16pt 0 8pt 0; }
.toc-hint { font-size: 9pt; color: #666; }
"""


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def apply_renames(text: str) -> str:
    for old, new in RENAMES:
        text = text.replace(old, new)
    return text


def shift_headings(text: str) -> str:
    """把每个标题提升一级（子代理章节从 ## 6 开始，需与整册的一级章节对齐）。"""
    out = []
    for line in text.split("\n"):
        m = re.match(r"^(#{1,5})(\s+.*)$", line)
        if m:
            out.append("#" * (len(m.group(1)) - 1) + m.group(2))
        else:
            out.append(line)
    return "\n".join(out)


def drop_rules(text: str) -> str:
    """删除 Markdown 水平分隔线（章节之间已有分页）。"""
    return "\n".join(line for line in text.split("\n") if line.strip() not in ("---", "***", "___"))


def split_at(text: str, heading: str):
    """在指定一级标题处切分，返回 (前, 后)；找不到时返回 (text, '')。"""
    idx = text.find("\n" + heading)
    if idx < 0:
        return text, ""
    return text[:idx], text[idx + 1:]


def inline(text: str) -> str:
    """处理行内标记：先转义，再还原 code / bold。"""
    # 行内代码先占位，避免其中的 */_ 被处理
    codes = []

    def stash(m):
        codes.append(m.group(1))
        return "\x00%d\x00" % (len(codes) - 1)

    text = re.sub(r"`([^`]+)`", stash, text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    for i, c in enumerate(codes):
        text = text.replace("\x00%d\x00" % i, "<code>%s</code>" % html.escape(c, quote=False))
    return text


def split_row(line: str):
    """按未转义的 | 切分表格行。"""
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    cells = re.split(r"(?<!\\)\|", line)
    return [c.strip().replace("\\|", "|") for c in cells]


def is_sep(line: str) -> bool:
    return bool(re.match(r"^\|?[\s:\-|]+\|[\s:\-|]*$", line.strip())) and "-" in line


def md_to_html(md: str):
    lines = md.split("\n")
    out = []
    headings = []
    i = 0
    in_code = False
    list_type = None
    first_h1 = True

    def close_list():
        nonlocal list_type
        if list_type:
            out.append("</%s>" % list_type)
            list_type = None

    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        stripped = line.strip()

        # 代码块
        if stripped.startswith("```"):
            if in_code:
                out.append("</pre>")
                in_code = False
            else:
                close_list()
                out.append("<pre>")
                in_code = True
            i += 1
            continue
        if in_code:
            out.append(html.escape(raw, quote=False))
            i += 1
            continue

        # 原始 HTML 块（如封面 div）
        if stripped.startswith("<"):
            close_list()
            out.append(line)
            i += 1
            continue

        if not stripped:
            close_list()
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            close_list()
            level = len(m.group(1))
            text = m.group(2).strip()
            if level == 1:
                if not first_h1:
                    out.append('<div style="page-break-before: always;"></div>')
                first_h1 = False
            plain = re.sub(r"[*`]", "", text)
            headings.append((level, plain))
            out.append("<h%d>%s</h%d>" % (level, inline(text), level))
            i += 1
            continue

        # 目录占位
        if stripped == "{{TOC}}":
            close_list()
            out.append("<p class=\"toc-hint\">{{TOC}}</p>")
            i += 1
            continue

        # 图片：不直接输出 <img>（WPS 会把它变成指向本地文件的 INCLUDEPICTURE 链接域，
        # 文档一旦拷给别人就丢图），而是输出标记段落，由 build_docx.ps1 用 AddPicture 真实嵌入
        m = re.match(r"^!\[(.*?)\]\((.*?)\)\s*$", stripped)
        if m:
            close_list()
            alt, src = m.group(1), m.group(2)
            out.append('<p class="fig">{{FIG:%s|%s}}</p>' % (src, alt))
            i += 1
            continue

        # 表格
        if stripped.startswith("|") and i + 1 < len(lines) and is_sep(lines[i + 1]):
            close_list()
            header = split_row(stripped)
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(split_row(lines[i].strip()))
                i += 1
            out.append("<table>")
            out.append("<tr>" + "".join("<th>%s</th>" % inline(c) for c in header) + "</tr>")
            for r in rows:
                out.append("<tr>" + "".join("<td>%s</td>" % inline(c) for c in r) + "</tr>")
            out.append("</table>")
            continue

        # 引用
        if stripped.startswith(">"):
            close_list()
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append("<blockquote>%s</blockquote>" % "<br/>".join(inline(b) for b in buf))
            continue

        # 无序列表
        m = re.match(r"^[-*]\s+(.*)$", stripped)
        if m:
            if list_type != "ul":
                close_list()
                out.append("<ul>")
                list_type = "ul"
            out.append("<li>%s</li>" % inline(m.group(1)))
            i += 1
            continue

        # 有序列表
        m = re.match(r"^\d+[.、]\s+(.*)$", stripped)
        if m:
            if list_type != "ol":
                close_list()
                out.append("<ol>")
                list_type = "ol"
            out.append("<li>%s</li>" % inline(m.group(1)))
            i += 1
            continue

        # 普通段落
        close_list()
        out.append("<p>%s</p>" % inline(stripped))
        i += 1

    close_list()
    if in_code:
        out.append("</pre>")
    return "\n".join(out), headings


def main():
    ch_intro = apply_renames(read(SRC / "ch00_03.md"))
    ch_db_api = apply_renames(read(SRC / "ch04_05.md"))
    ch_678 = drop_rules(shift_headings(apply_renames(read(SRC / "第6-8章_部署运维_安全_非功能性指标.md"))))
    ch_11_all = apply_renames(read(SRC / "ch11_appendix.md"))
    ch11, appendix = split_at(ch_11_all, "# 附录")
    ch_9_12 = drop_rules(apply_renames(read(SRC / "ch09_10_12.md")))
    ch910, ch12 = split_at(ch_9_12, "# 12 商务说明")

    book = "\n\n".join(x.strip() for x in [ch_intro, ch_db_api, ch_678, ch910, ch11, ch12, appendix] if x.strip())
    (SRC / "_book.md").write_text(book, encoding="utf-8")

    body, headings = md_to_html(book)
    page = (
        "<html><head><meta charset=\"utf-8\"><title>智能仓库管理系统（WMS）项目方案与技术文档 V2.0</title>"
        "<style>%s</style></head><body>\n%s\n</body></html>" % (CSS, body)
    )
    (SRC / "_book.html").write_text(page, encoding="utf-8")

    with (BUILD / "_headings.tsv").open("w", encoding="utf-8", newline="\n") as f:
        for level, text in headings:
            f.write("%d\t%s\n" % (level, text))

    print("合并完成：")
    print("  _book.md   %d 字符" % len(book))
    print("  _book.html %d 字符" % len(page))
    print("  标题 %d 个（写入 build/_headings.tsv）" % len(headings))
    print("  一级标题：")
    for level, text in headings:
        if level == 1:
            print("    -", text)


if __name__ == "__main__":
    main()
