# -*- coding: utf-8 -*-
"""把 PDF 每页渲染成 PNG，用于肉眼检查排版（QtPdf，离屏）。"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize                      # noqa: E402
from PySide6.QtGui import QGuiApplication             # noqa: E402
from PySide6.QtPdf import QPdfDocument                # noqa: E402


def main():
    pdf = Path(sys.argv[1])
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else pdf.parent / (pdf.stem + "_pages")
    max_pages = int(sys.argv[3]) if len(sys.argv) > 3 else 60
    width = int(sys.argv[4]) if len(sys.argv) > 4 else 1240
    out_dir.mkdir(parents=True, exist_ok=True)

    app = QGuiApplication([])                          # noqa: F841
    doc = QPdfDocument()
    status = doc.load(str(pdf))
    count = doc.pageCount()
    print("load status:", status, "| pages:", count)
    for i in range(min(count, max_pages)):
        size = doc.pagePointSize(i)
        height = int(width * size.height() / size.width())
        image = doc.render(i, QSize(width, height))
        path = out_dir / ("page_%02d.png" % (i + 1))
        image.save(str(path))
    print("rendered ->", out_dir)


if __name__ == "__main__":
    main()
