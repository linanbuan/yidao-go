"""应用图标：画出来，不带 png 资源文件。

为什么不放一张现成 png：这个仓库里连音效都是生成的（`scripts/gen_sounds.py`），
理由是同一个 —— 二进制资源进不了代码评审，坏了也看不出坏在哪。画出来的图标
每一条颜色都能指回 `theme` 里的色板，而 `clipped_texts` 那类像素断言也照样能量它。

运行时用 `icon()`；打包 exe 要的 `.ico` 由 `scripts/gen_icons.py` 用它写出来
（实测 Qt 的 ico 编码器可用，`QIcon.write()` 能把多个尺寸装进同一个 .ico）。
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from ui import theme

#: .ico 里该有的尺寸（Windows 托盘 16、任务栏 24/32、alt-tab 48、缩略图 256）。
SIZES: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)
#: 图标里的棋盘是 5x5 路：真盘 19 路缩到 16px 会变成一坨灰，读不出是棋盘。
GRID_ROAD = 5


def pixmap(size: int = 64) -> QPixmap:
    """一枚方图标。所有尺寸走同一套比例，所以缩小不会丢结构。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    try:
        p.setRenderHint(QPainter.Antialiasing)
        s = float(size)
        r = max(1.0, s * 0.14)                        # 圆角：跟卡片那 8px 同一量级
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, s, s), r, r)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.BOARD_TOP))
        p.drawPath(path)
        # 木纹的上下两端（与棋盘同一对色），留一点边以便在小尺寸下还有形状
        p.setBrush(QColor(theme.BOARD_BOTTOM))
        p.drawRoundedRect(QRectF(s * 0.06, s * 0.5, s * 0.88, s * 0.44), r * 0.7, r * 0.7)

        m = s * 0.18                                   # 网格留白
        step = (s - 2 * m) / (GRID_ROAD - 1)
        pen = QPen(QColor(theme.GRID))
        pen.setWidthF(max(1.0, s * 0.02))
        p.setPen(pen)
        for i in range(GRID_ROAD):
            x = m + i * step
            p.drawLine(QPointF(x, m), QPointF(x, s - m))
            p.drawLine(QPointF(m, x), QPointF(s - m, x))
        # 两枚子一黑一白：只有一色的圆片会被读成「一个棋罐」而不是一盘棋
        p.setPen(Qt.NoPen)
        c = s / 2
        p.setBrush(QColor(theme.STONE_BLACK_OUT))
        p.drawEllipse(QPointF(c - step, c - step), step * 0.44, step * 0.44)
        p.setBrush(QColor(theme.STONE_WHITE_OUT))
        p.drawEllipse(QPointF(c + step, c + step), step * 0.44, step * 0.44)
    finally:
        p.end()
    return pm


def icon(sizes: tuple[int, ...] = SIZES) -> QIcon:
    ic = QIcon()
    for s in sizes:
        ic.addPixmap(pixmap(s))
    return ic
