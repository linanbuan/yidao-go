"""棋盘控件：QPainter 自绘，绘制口径逐条对齐 `frontend/src/components/GoBoard.tsx`。

为什么自己画而不是用 QGraphicsScene 摆子：围棋棋盘是「一格一物、每手全变」的典型
立即模式场景，控件树（19x19=361 个子控件）在悔棋/复盘拖动进度条时会成为负担。

坐标系（与后端/网页版一致，**不是**屏幕坐标）：
    x 向右增大，y **向上**增大（y=0 是棋盘底边），所以 `py = pad + (size-1-y)*cell`。
    这一条是本项目踩过的老坑（引擎视角/行序），画错就会整盘上下翻转。

spike 暴露的 4 个缺陷，对应修法写在下面各处的注释里：
    ① 坐标标签被浮层压住 → 标签只画在 pad 带内，本控件不承载任何浮层卡片；
    ② 下边距放不下标签 → `pad = cell`，标签字号 `cell*0.34`；**带中心取 `LABEL_BAND`（0.33）
        而不是网页版的 `pad*0.45`** —— 0.45 在 9 路那种大 cell 下会被边行棋子压掉半截，
        推导见 `LABEL_BAND` 的注释；这一条是原生端**主动偏离**网页版的地方；
    ③ DPR 1.5 下 1px 线深浅不一 → 网格用 cosmetic 画笔（恒为 1 设备像素）+ 半设备像素对齐；
    ④ 星位与棋子描边叠成双圈 → 星位先画、棋子后画且**棋子不描边**（网页版就没有描边）。

第 32 轮（性能）在这里加了三层缓存与两处动效，动机是实测数字而不是感觉：
19 路 640x640、盘上 180 子时**整盘重绘 22.25 ms** —— 比一个 60fps 帧还长，
于是鼠标每划过一格（`mouseMoveEvent` → 整盘重绘）都能看见一次顿。
三层缓存的划分按「多久变一次」来切，从最不变到最常变：
    · 静态层 `_grid_layer()`：木底以外的网格/星位/坐标标签（4.20 ms/帧）；
    · 领地层 `_own_layer()`：平铺热力图（361 个 fillRect，只在数据换了才重建）；
    · 棋子精灵 `_stone_sprite()`：一颗子的「阴影+径向渐变」预渲染成一张小图，
      每颗子从「建一个 QRadialGradient + 两次 drawEllipse」变成一次 drawPixmap
      （实测 100 µs/子 → 个位数 µs/子）。
再加上一条：`paintEvent` 只画**曝光区里**的棋子（悬停时 Qt 只报那一格的脏区）。
三层都不是「另画一份」：像素口径与从前逐条相同，只是把重复的算式存了下来。
"""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter,
                           QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import QWidget

from .. import theme
from . import motion

LETTERS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"     # 围棋坐标没有 I
EMPTY, BLACK, WHITE = 0, 1, 2
#: 棋子半径（cell 的倍数）。抽成常量是因为坐标标签带要拿它算碰撞，不能两边各写一份。
STONE_R = 0.47
#: 标签带中心到 pad 外沿的距离（cell 的倍数）。原先是 0.45，看终局截图才发现它不对：
#: 子半径 0.47cell 会伸进 pad 带，标签中心 0.45 + 半字高 0.17 = 0.62 > 子的外沿 0.53。
#: 19 路 cell 只有 32px 时只啃掉几像素看不出来，9 路 cell 到 100px 时整排字母被
#: 首尾两行子吃掉一半 —— 只有真截图能发现这种事。
LABEL_BAND = 0.33

STARS = {
    19: [(3, 3), (15, 3), (3, 15), (15, 15), (9, 9), (9, 3), (9, 15), (3, 9), (15, 9)],
    13: [(3, 3), (9, 3), (3, 9), (9, 9), (6, 6)],
    9: [(2, 2), (6, 2), (2, 6), (6, 6), (4, 4)],
}


def star_points(size: int) -> list[tuple[int, int]]:
    """标准三路星位；非 9/13/19 时退化为四角 + 天元。"""
    if size in STARS:
        return [(x, y) for x, y in STARS[size] if x < size and y < size]
    if size < 5:
        return []
    lo, hi = 2, size - 3
    mid = size // 2
    return [(lo, lo), (hi, lo), (lo, hi), (hi, hi), (mid, mid)]


def _as_point(v):
    """`None` / `{x,y}` / `(x,y)` 三种写法统一成 tuple 或 None。"""
    if v is None:
        return None
    if isinstance(v, dict):
        if v.get("x") is None or v.get("y") is None:
            return None            # 虚手没有坐标，不是「画在原点」
        return int(v["x"]), int(v["y"])
    return int(v[0]), int(v[1])


class GoBoard(QWidget):
    """可交互棋盘。

    信号：
      pointClicked(x, y)      左键落子/选点（越界与空点之外不发）
      pointRightClicked(x, y) 右键，用于终局点选死子与悔棋反悔
      hoverChanged(point)     悬停点或 None
    """

    pointClicked = Signal(int, int)
    pointRightClicked = Signal(int, int)
    hoverChanged = Signal(object)

    def __init__(self, size: int = 19, interactive: bool = True, parent=None):
        super().__init__(parent)
        self._size = size
        self._board: list[list[int]] = [[EMPTY] * size for _ in range(size)]
        self._last_move: tuple[int, int] | None = None
        self._hints: list[dict] = []
        self._show_hints = True
        self._ownership: list[float] | None = None
        self._show_ownership = False
        self._dead: list[tuple[int, int]] = []
        self._variation: list[dict] = []
        self._marks: list[dict] = []
        self._moves: list[dict] = []
        self._show_move_numbers = False
        self._interactive = interactive
        self._dim = False
        self._hover: tuple[int, int] | None = None

        # ---- 缓存（第 32 轮）：键里都带着几何，改了尺寸/DPR 自然重建
        #: 盘上有子的点，`[(x, y, color)]`。`_board` 仍是唯一真值，这一份只是让
        #: 「画子」不必每帧扫 361 格 —— 曝光区判定也靠它。
        self._stones: list[tuple[int, int, int]] = []
        self._sprites: dict[tuple, QPixmap] = {}
        self._grid: QPixmap | None = None
        self._grid_key: tuple | None = None
        self._own_tex: QPixmap | None = None
        self._own_key: tuple | None = None
        #: 领地数据的版本号：`set_props(ownership=...)` 换了一份就 +1（同一份对象不算换）
        self._own_seq = 0

        # ---- 动效（第 32 轮）：落子淡入放大、最后一手落定圈
        self._place: dict | None = None       # {"x","y","color","t0"}
        self._ring: tuple | None = None       # (x, y, t0)
        self._hint_t0: float | None = None
        self._frame = motion.pulse_timer(self, self._on_frame)
        self._animations = motion.enabled()

        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor if interactive else Qt.ArrowCursor)
        self.setMinimumSize(*self._min_side_pair())

    # ---------------------------------------------------------------- 尺寸

    def _min_side_pair(self) -> tuple[int, int]:
        """cell 下限 12px 时整块棋盘需要的边长（含两侧 pad）。"""
        side = 12 * (self._size + 1)
        return side, side

    def sizeHint(self):                                     # noqa: N802
        side = 30 * (self._size + 1)                        # 与网页版 pixelSize=640/19 同量级
        return QSize(side, side)

    @property
    def board_size(self) -> int:
        """棋盘路数。叫这个名字而不是 `size`：`QWidget.size()` 已经存在且返回 QSize，
        把它遮成一个 int 的属性会在任何一句 `w.size().width()` 上炸掉。"""
        return self._size

    @property
    def shown_dead(self) -> list[tuple[int, int]]:
        """当前**画在盘上**的死子标记。页面自己那份 `dead_stones` 是待确认的判定，
        控件这一份才是「用户看得见的是什么」—— 测试要断言的是后者。"""
        return list(self._dead)

    def set_interactive(self, value: bool) -> None:
        value = bool(value)
        if value == self._interactive:
            return                        # 没变就别重绘：第 32 轮起「别白画」是硬口径
        self._interactive = value
        self.setCursor(Qt.PointingHandCursor if value else Qt.ArrowCursor)
        if not value:
            self._hover = None
        self.update()

    @property
    def interactive(self) -> bool:
        """`set_interactive` 的读侧。与 `shown_dead` 同一个理由：要断言的是
        「用户看得见 / 点得动的到底是什么」，而不是去翻控件的私有字段。"""
        return self._interactive

    @property
    def shown_marks(self) -> list[dict]:
        """当前**画在盘上**的问题手圈。复盘页的验收断这一份而不是页面自己算的那份。"""
        return list(self._marks)

    @property
    def shown_variation(self) -> list[dict]:
        """当前画在盘上的变化线（幽灵子）。"""
        return list(self._variation)

    def _ownership_layer(self) -> list[float] | None:
        """真的该叠上领地热力图时返回那份平铺数组，否则 None。

        判据只有一份：`paintEvent` 与 `shown_ownership` 都走这里。写成两份的话，
        哪天改了长度校验，测试读到的就是那个**没改的**副本 —— 而盘上什么都没变。
        长度要够 size*size：ownership 是**平铺的一维数组**（`[y*size+x]`），
        传成二维会让绘制处拿到一个 list 再 `abs()`，崩在画棋盘里。"""
        if not self._show_ownership or not self._ownership:
            return None
        if len(self._ownership) < self._size * self._size:
            return None
        return self._ownership

    @property
    def shown_ownership(self) -> list[float] | None:
        """当前真的在画的领地层；没勾开关或数据不够长就是 None。

        有了这个读口才测得出「有个复选框但点了没反应」—— 那是网页版复盘页的实情
        （它的分析里根本没有 ownership），原生端不许复制这个缺陷。"""
        layer = self._ownership_layer()
        return list(layer) if layer else None

    def set_size(self, size: int) -> None:
        """换棋盘规格（9/13/19）并清空。对局页切局时用。"""
        self._size = size
        self._board = [[EMPTY] * size for _ in range(size)]
        self._stones = []
        self._reset_overlays()
        self._invalidate_layers()
        self.setMinimumSize(*self._min_side_pair())
        self.update()

    def _invalidate_layers(self) -> None:
        """丢掉与几何/数据相关的缓存。换规格、换 DPR、换领地数据时都要叫一次。"""
        self._grid = None
        self._grid_key = None
        self._own_tex = None
        self._own_key = None
        self._sprites.clear()

    # ---------------------------------------------------------------- 状态

    def set_board(self, board: list[list[int]] | None) -> None:
        """`board[y][x]`，y=0 是底边 —— 与后端 snapshot 的 `board` 同构。"""
        if self._apply_board(board):
            self.update()

    def _apply_board(self, board: list[list[int]] | None) -> bool:
        """装局面，返回**画面上有没有变**。

        判据是 `_stones`（盘上真有子的点的集合）而不是「传进来的 list 是不是同一份」：
        页面每帧都送一份新算出来的 board 是常态（复盘页的回看重建），
        真正决定要不要重画的是「子有没有变」，与 list 对象无关。
        """
        size = self._size
        if not board:
            self._board = [[EMPTY] * size for _ in range(size)]
        else:
            # 后端给的行序就是 y=0..size-1，直接照抄；长度不足时补空行而不是报错，
            # 免得一个残缺 payload 把整个界面画崩
            self._board = [list(board[y]) if y < len(board) and board[y] else [EMPTY] * size
                           for y in range(size)]
        before = self._stones
        self._sync_stones()
        return self._stones != before

    def _sync_stones(self) -> None:
        """把 `_stones` 与 `_board` 对齐，并给**新出现的那一颗**起落子动效。

        只在「多了一颗」时起：一次 `set_board` 同时多出好几颗（重连补全局面、
        复盘跳手）时逐颗动画既慢又看不出是「落子」，那时直接给终态更诚实。
        `_stones` 是画子用的遍历表，顺序按 y/x 稳定，与 `_board` 的扫描顺序一致。
        """
        old = {(x, y): v for x, y, v in self._stones}
        stones: list[tuple[int, int, int]] = []
        added: list[tuple[int, int, int]] = []
        for y in range(self._size):
            row = self._board[y]
            for x in range(self._size):
                v = row[x] if x < len(row) else EMPTY
                if v == EMPTY:
                    continue
                stones.append((x, y, v))
                if old.get((x, y)) in (None, EMPTY):
                    added.append((x, y, v))
        self._stones = stones
        if len(added) == 1 and self._animations:
            x, y, v = added[0]
            self._start_place(x, y, v)
        elif added and self._place:
            self._place = None                       # 局面被整体换掉了，别再补上一颗的动画
            self._frame.stop()

    def _reset_overlays(self) -> None:
        self._last_move = None
        self._hints = []
        self._ownership = None
        self._show_ownership = False
        self._dead = []
        self._variation = []
        self._marks = []
        self._moves = []
        self._show_move_numbers = False
        self._dim = False
        self._place = None
        self._ring = None

    def set_props(self, **kw) -> None:
        """一次性设置叠加层。未知键直接报错 —— 拼错属性名不该静默失效。

        第 32 轮加的两条「别白画」：
        · 领地换了一份数据就 `_own_seq += 1`（领地层缓存按它失效）；
        · 画完**只在真的有东西变了**时 `update()` —— 页面把 `_paint` 挂在每个
          服务端事件上（对局页 9 处调用点），同一份状态被重复送进来时，
          从前每次都要整盘重绘一遍。
        """
        alias = {
            "lastMove": "last_move", "showHints": "show_hints",
            "showOwnership": "show_ownership", "deadStones": "dead",
            "showMoveNumbers": "show_move_numbers",
        }
        valid = {"board", "last_move", "hints", "show_hints", "ownership", "show_ownership",
                 "dead", "variation", "marks", "moves", "show_move_numbers",
                 "interactive", "dim"}
        dirty = False
        for key, value in kw.items():
            name = alias.get(key, key)
            if name not in valid:
                raise AttributeError(f"GoBoard 没有属性 {key!r}")
            if name == "board":
                dirty = self._apply_board(value) or dirty
                continue
            if name == "last_move":
                # 后端与网页版给的是 {"x":..,"y":..}，内部用 tuple；两种都接受
                value = _as_point(value)
            if name == "ownership":
                if value is not self._ownership:
                    self._own_seq += 1
                    self._own_tex = None
            if name == "last_move" and value is not None and value != self._last_move:
                self._start_ring(*value)
            old = getattr(self, f"_{name}")
            if old != value:
                dirty = True
                if name == "hints" and value:
                    self._start_hint_fade()
            setattr(self, f"_{name}", value)
        if "interactive" in kw or kw.get("interactive") is False:
            before = self._interactive
            self.set_interactive(bool(kw.get("interactive", self._interactive)))
            dirty = dirty or self._interactive != before
        if dirty:
            self.update()

    # ---------------------------------------------------------------- 动效

    @property
    def animations(self) -> bool:
        """动效开关的读口（测试要断言「关掉之后真的没有动画」）。"""
        return self._animations

    def set_animations(self, value: bool) -> None:
        """开关动效。关掉时把在跑的动画收到终态 —— 半透明的子不能留在盘上。"""
        self._animations = bool(value)
        if not self._animations:
            self._place = None
            self._ring = None
            self._hint_t0 = None
            self._frame.stop()
            self.update()

    @property
    def place_animation(self) -> tuple[int, int] | None:
        """正在淡入的那颗子 `(x, y)`，没有就是 None。给测试读，不给页面用。"""
        if not self._place:
            return None
        return (self._place["x"], self._place["y"])

    def _start_place(self, x: int, y: int, color: int) -> None:
        self._place = {"x": x, "y": y, "color": color, "t0": motion.now_ms()}
        if not self._frame.isActive():
            self._frame.start()
        self.update(self._cell_rect(x, y))

    def _start_ring(self, x: int, y: int) -> None:
        """最后一手的「落定圈」：从略大略淡收到位。只在这手**换了**时才起。"""
        if not self._animations:
            return
        self._ring = (x, y, motion.now_ms())
        if not self._frame.isActive():
            self._frame.start()
        self.update(self._cell_rect(x, y))

    def _start_hint_fade(self) -> None:
        if not self._animations:
            return
        self._hint_t0 = motion.now_ms()
        if not self._frame.isActive():
            self._frame.start()

    def _place_progress(self) -> tuple[int, int, int, float, float] | None:
        """落子动画的当前帧 `(x, y, color, 缩放, 透明度)`；不在动画中返回 None。"""
        pl = self._place
        ms = motion.duration("place")
        if not pl or ms <= 0:
            return None
        t = (motion.now_ms() - pl["t0"]) / ms
        if t >= 1.0:
            return None
        e = 1 - (1 - max(0.0, t)) ** 3          # OutCubic：起手快、收尾稳
        return (pl["x"], pl["y"], pl["color"], 0.72 + 0.28 * e, 0.25 + 0.75 * e)

    def _ring_progress(self) -> float | None:
        """最后一手圈的进度 0→1；不在动画中返回 None（调用方按 1.0 画）。"""
        if not self._ring:
            return None
        ms = motion.duration("ring")
        if ms <= 0:
            return None
        t = (motion.now_ms() - self._ring[2]) / ms
        if t >= 1.0:
            return None
        return 1 - (1 - max(0.0, t)) ** 3

    def _hint_alpha(self) -> float:
        if self._hint_t0 is None:
            return 1.0
        ms = motion.duration("hint")
        if ms <= 0:
            return 1.0
        return min(1.0, max(0.0, (motion.now_ms() - self._hint_t0) / ms))

    def _on_frame(self) -> None:
        """动效帧：只重画**动的那一格**，收工就停表。

        停表这件事是要紧的：定时器一直跑着，即使什么都不动，整机也一直在耗电
        （而这一轮的目标恰恰是「别白画」）。
        """
        now = motion.now_ms()
        busy = False
        if self._place:
            x, y = self._place["x"], self._place["y"]
            if now - self._place["t0"] >= motion.duration("place"):
                self._place = None
            else:
                busy = True
            self.update(self._cell_rect(x, y))
        if self._ring:
            x, y, t0 = self._ring
            if now - t0 >= motion.duration("ring"):
                self._ring = None
            else:
                busy = True
            self.update(self._cell_rect(x, y))
        if self._hint_t0 is not None:
            if now - self._hint_t0 >= motion.duration("hint"):
                self._hint_t0 = None
            else:
                busy = True
            self.update()
        if not busy:
            self._frame.stop()

    # ---------------------------------------------------------------- 几何

    def layout_now(self) -> tuple[int, float, float, float]:
        """返回 (cell, origin_x, origin_y, side)：当前尺寸下的实际排布。

        棋盘始终**居中且正方**，边长取控件短边 —— 这样布局给多大就画多大，
        不需要页面去算「视口剩余高度」（网页版那段 JS 预算逻辑在原生端是多余的）。
        """
        w, h = self.width(), self.height()
        side = max(1, min(w, h))
        cell = max(12.0, math.floor(side / (self._size + 1)))
        total = cell * (self._size - 1) + cell * 2
        # total 可能略大于 side（cell 有下限），此时按 total 居中，允许轻微溢出
        ox = (w - total) / 2 + cell
        oy = (h - total) / 2 + cell
        return cell, ox, oy, total

    def board_rect(self) -> QRectF:
        """**木盘本身**的正方形区域（逻辑像素），居中在控件里。

        单独成一个方法而不是埋在 `paintEvent` 里：绘制与测试要用同一个算式，
        各算各的就会一起漂移成同一个错。控件被布局压成横长条时（复盘页左列
        实测 592x320）这块就是那 320x320 —— 四周让给面板底色，见 `paintEvent`。
        """
        _cell, _ox, _oy, total = self.layout_now()
        return QRectF((self.width() - total) / 2.0,
                      (self.height() - total) / 2.0, total, total)

    def center(self, x: int, y: int) -> QPointF:
        """交叉点的逻辑像素坐标（测试取像素、页面定位浮层都用它）。"""
        cell, ox, oy, _ = self.layout_now()
        return QPointF(ox + x * cell, oy + (self._size - 1 - y) * cell)

    def point_at(self, px: float, py: float) -> tuple[int, int] | None:
        """像素 → 交叉点。与网页版 `toBoard` 同式（含最近点取整与越界判定）。"""
        cell, ox, oy, _ = self.layout_now()
        x = round((px - ox) / cell)
        y = self._size - 1 - round((py - oy) / cell)
        if 0 <= x < self._size and 0 <= y < self._size:
            # 只在离交叉点足够近时才认，避免点在两条线正中时误触发
            c = self.center(x, y)
            if abs(px - c.x()) <= cell * 0.62 and abs(py - c.y()) <= cell * 0.62:
                return x, y
        return None

    def stone_at(self, x: int, y: int) -> int:
        if 0 <= x < self._size and 0 <= y < self._size:
            row = self._board[y]
            # 行长校验（审计 S2）：`_apply_board` 明确容忍短行（残缺 payload 补空行，
            # 也照抄服务端给的行），于是 `row[x]` 会在短行上 IndexError —— 而
            # `stone_at` 被 paintEvent 调用，pythonw 下整盘静默不画、日志里一个字都没有。
            return row[x] if 0 <= x < len(row) else EMPTY
        return EMPTY

    # ---------------------------------------------------------------- 事件

    def mouseMoveEvent(self, ev):                            # noqa: N802
        p = None if not self._interactive else self.point_at(ev.position().x(), ev.position().y())
        if p != self._hover:
            old = self._hover
            self._hover = p
            self.hoverChanged.emit(p)
            # 只重画动过的那两格（旧悬停格与新悬停格）：鼠标每划过一格都整盘重绘
            # 是「卡手感」最直接的来源 —— 悬停只影响它自己那一格。
            self._refresh_cells(old, p)

    def leaveEvent(self, ev):                                # noqa: N802
        if self._hover is not None:
            old = self._hover
            self._hover = None
            self.hoverChanged.emit(None)
            self._refresh_cells(old, None)

    def _cell_rect(self, x: int, y: int):
        """一格（含悬停圈、落定圈与抗锯齿外扩）在控件坐标里的矩形，用于最小重绘。

        半径要按**最大的那个叠层**算，不是按棋子本体：落定圈在动画起点是 1.35r
        （见 `_draw_last_move`），再加上描边的半宽。从前按 1.25r 算，cell > 23.5px
        时圈的外沿每帧都被脏矩形裁掉一圈 —— 拖影段（审计 S4，算式已核）。
        """
        cell, _ox, _oy, _ = self.layout_now()
        c = self.center(x, y)
        r = cell * STONE_R
        half = max(r * 1.35, cell * 0.5) + max(1.5, r * 0.17) + 2.0
        return QRect(int(math.floor(c.x() - half)), int(math.floor(c.y() - half)),
                     int(math.ceil(half * 2)), int(math.ceil(half * 2)))

    def _refresh_cells(self, *points) -> None:
        for pt in points:
            if pt is not None:
                self.update(self._cell_rect(*pt))

    def mousePressEvent(self, ev):                           # noqa: N802
        if not self._interactive:
            return
        p = self.point_at(ev.position().x(), ev.position().y())
        if not p:
            return
        if ev.button() == Qt.LeftButton:
            self.pointClicked.emit(*p)
        elif ev.button() == Qt.RightButton:
            self.pointRightClicked.emit(*p)

    def resizeEvent(self, ev):                               # noqa: N802
        super().resizeEvent(ev)
        # 精灵是按当前格宽渲染的：窗口一改大小，旧的那几张就再也用不上了
        self._sprites.clear()

    # ---------------------------------------------------------------- 绘制

    def paintEvent(self, ev):                                # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)

        dpr = float(self.devicePixelRatioF())
        cell, ox, oy, total = self.layout_now()
        size = self._size
        #: 这一次要画的区域。悬停/动效只脏一格时它就很小，下面的棋子循环靠它跳过。
        clip = QRectF(ev.rect())

        # 木纹底：只画**正方形的那块盘**（`board_rect()`），四周是面板底色。
        # 原先是铺满整个控件、注释写着「不留黑边」—— 那前提是控件本来就是方的。
        # 复盘页左列高度不够，控件被压成 567x250 的横长条，于是画面成了
        # 「一整条横木中间画着个小棋盘」，看着像棋盘被拉扁（截图 p4_02 抓到）。
        # 所有调用点都把棋盘放在 Panel 里，故四周取 PANEL 而不是 BG。
        plate = self.board_rect()
        p.fillRect(self.rect(), QColor(theme.PANEL))
        grad = QLinearGradient(plate.topLeft(), plate.bottomRight())
        grad.setColorAt(0.0, QColor(theme.BOARD_TOP))
        grad.setColorAt(1.0, QColor(theme.BOARD_BOTTOM))
        corner = max(2.0, cell * 0.10)
        p.setPen(self._hairline(QColor(theme.LINE), dpr))
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(plate, corner, corner)
        p.setPen(Qt.NoPen)

        # 领地热力图（黑正白负），阈值 0.08 与网页版一致。
        # 第 32 轮起画进一张缓存图：361 个格子每帧重算是纯浪费（数据换了才重建）。
        own_tex = self._own_layer()
        if own_tex is not None:
            p.drawPixmap(0, 0, own_tex)

        # 网格 + 星位 + 坐标标签：每帧都一样，缓存成一张透明底图（第 32 轮）
        p.drawPixmap(0, 0, self._grid_layer())

        # 棋子（只画曝光区里压得到的那几颗）
        r = cell * STONE_R
        alpha = 0.75 if self._dim else 1.0
        place = self._place_progress()
        # 两张精灵**先取出来**：整盘只有黑白两种，没必要每颗子都去查一次缓存
        # （180 次字典查找 + round 也是钱；第 32 轮把它挪到帧首各一次）
        sprites = {BLACK: self._stone_sprite(BLACK, r, cell),
                   WHITE: self._stone_sprite(WHITE, r, cell)}
        for x, y, v in self._stones:
            c = self.center(x, y)
            if not clip.intersects(QRectF(c.x() - r * 1.2, c.y() - r * 1.2,
                                          r * 2.4, r * 2.4)):
                continue
            if place and place[0] == x and place[1] == y:
                continue                      # 这一颗正在淡入，交给下面那段画
            self._blit_stone(p, c, sprites.get(v) or sprites[BLACK], alpha)
        if place:
            x, y, v, scale, a = place
            self._blit_stone(p, self.center(x, y), sprites.get(v) or sprites[BLACK],
                             alpha * a, scale)

        self._draw_move_numbers(p, cell, r, clip)
        self._draw_dead(p, cell, r, dpr)
        self._draw_last_move(p, r, dpr)
        self._draw_variation(p, cell, r)
        self._draw_marks(p, cell, r, dpr)
        self._draw_hints(p, cell, r)
        self._draw_hover(p, r)
        p.end()

    # ------------------------------------------------------------ 缓存层

    def _layer_pixmap(self) -> QPixmap:
        """建一张与控件同尺寸、带 DPR 的透明底图（下面两张缓存图共用这一套算术）。"""
        dpr = float(self.devicePixelRatioF())
        pm = QPixmap(max(1, int(math.ceil(self.width() * dpr))),
                     max(1, int(math.ceil(self.height() * dpr))))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        return pm

    def _grid_layer(self) -> QPixmap:
        """网格 + 星位 + 坐标标签的缓存图（第 32 轮）。

        这三样只跟**几何**有关（路数、控件尺寸、DPR），与棋局无关 —— 实测它们
        占了空盘重绘 4.20 ms 里的大头（76 次 `drawText`），而每帧的结果完全一样。
        键里带 dpr：窗口被拖到另一块缩放不同的屏上时（本机 1.5 → 1.0）必须重建，
        否则细线会糊成两像素。
        """
        dpr = float(self.devicePixelRatioF())
        key = (self._size, self.width(), self.height(), round(dpr, 3))
        if self._grid is not None and self._grid_key == key:
            return self._grid
        cell, ox, oy, _ = self.layout_now()
        size = self._size
        pm = self._layer_pixmap()
        q = QPainter(pm)
        q.setRenderHint(QPainter.Antialiasing, True)
        q.setRenderHint(QPainter.TextAntialiasing, True)

        # 网格：cosmetic 画笔（宽度 0 = 恒 1 设备像素）+ 半设备像素对齐，修掉缺陷 ③
        q.setPen(self._hairline(QColor(theme.GRID), dpr))
        for i in range(size):
            a = self.center(0, i)
            b = self.center(size - 1, i)
            y = self._snap(a.y(), dpr)
            q.drawLine(QPointF(self._snap(a.x(), dpr), y), QPointF(self._snap(b.x(), dpr), y))
            c = self.center(i, 0)
            d = self.center(i, size - 1)
            x = self._snap(c.x(), dpr)
            q.drawLine(QPointF(x, self._snap(c.y(), dpr)), QPointF(x, self._snap(d.y(), dpr)))

        # 星位：先画星位、后画棋子，且棋子不描边 —— 修掉缺陷 ④ 的双圈
        q.setPen(Qt.NoPen)
        q.setBrush(QColor(theme.STAR))
        r_star = max(2.0, cell * 0.09)
        for sx, sy in star_points(size):
            c = self.center(sx, sy)
            q.drawEllipse(c, r_star, r_star)

        # 坐标标签（缺陷 ①：只占 pad 带，字号随 cell 缩放）
        self._draw_labels(q, cell, ox, oy, size)
        q.end()
        self._grid, self._grid_key = pm, key
        return pm

    def _own_layer(self) -> QPixmap | None:
        """领地热力图的缓存图；没开开关或数据不够长时 None。

        键里带 `_own_seq`（换了一份数据才 +1）：页面每次都把同一个 list 对象送进来，
        按对象身份失效就够了，不必每帧重算 361 格。
        """
        own = self._ownership_layer()
        if own is None:
            return None
        dpr = float(self.devicePixelRatioF())
        key = (self._own_seq, self._size, self.width(), self.height(), round(dpr, 3))
        if self._own_tex is not None and self._own_key == key:
            return self._own_tex
        cell, _ox, _oy, _ = self.layout_now()
        size = self._size
        pm = self._layer_pixmap()
        q = QPainter(pm)
        for y in range(size):
            for x in range(size):
                v = own[y * size + x]
                if not v or abs(v) < 0.08:
                    continue
                c = self.center(x, y)
                a = min(0.45, abs(v) * 0.5) if v > 0 else min(0.6, abs(v) * 0.65)
                col = QColor(20, 20, 20, int(a * 255)) if v > 0 \
                    else QColor(255, 255, 255, int(a * 255))
                q.fillRect(QRectF(c.x() - cell / 2, c.y() - cell / 2, cell, cell), col)
        q.end()
        self._own_tex, self._own_key = pm, key
        return pm

    # ------------------------------------------------------------ 绘制细节

    @staticmethod
    def _hairline(color: QColor, dpr: float) -> QPen:
        pen = QPen(color, 0)              # 宽度 0 = cosmetic，任何缩放下都是 1 设备像素
        pen.setCosmetic(True)
        return pen

    @staticmethod
    def _snap(v: float, dpr: float) -> float:
        """把逻辑坐标对齐到设备像素的中央，1 设备像素的线才会锐利不糊。"""
        return (math.floor(v * dpr) + 0.5) / dpr

    def label_band(self, cell: float, ox: float, oy: float) -> tuple[int, float, float, float, float]:
        """`(字号, 上, 下, 左, 右)`：四条坐标标签带的中心线（逻辑像素）。

        单独成一个方法而不是埋在 `_draw_labels` 里：测试要拿它判两件事
        —— 带子在控件内、且不与边行棋子的半径重叠 —— 而不把公式算第二遍
        （算第二遍就会与实现一起漂移成同一个错，那比没测还坏）。
        """
        fs = max(9, int(cell * 0.34))
        # 小 cell 时字号有 9px 下限，这时「不被控件边缘裁掉」比「不碰棋子」更要紧
        out = max(cell * LABEL_BAND, fs / 2 + 2)
        size = self._size
        return (fs,
                oy - cell + out,                                  # 上：网格外沿往里
                oy + (size - 1) * cell + cell - out,              # 下
                ox - cell + out,                                  # 左
                ox + (size - 1) * cell + cell - out)              # 右

    def _draw_labels(self, p: QPainter, cell: float, ox: float, oy: float, size: int) -> None:
        """四条边的坐标标签带。

        带中心的位置以**网格外沿**为基准（而不是控件边缘）：棋盘按短边居中，控件比
        正方形高时 `oy > cell`，按控件边缘画会让标签飘到木盘顶上、与棋盘脱节。

        几何全部出自 `label_band()`，理由写在那里。
        """
        p.setPen(QColor(theme.LABEL))
        p.setBrush(Qt.NoBrush)
        font = QFont(theme.FONT_FAMILY)
        fs, top, bottom, left, right = self.label_band(cell, ox, oy)
        font.setPixelSize(fs)
        p.setFont(font)
        box = cell * 0.9
        for i in range(size):
            cx = ox + i * cell
            p.drawText(QRectF(cx - box / 2, top - cell * 0.35, box, cell * 0.7),
                       Qt.AlignCenter, LETTERS[i])
            p.drawText(QRectF(cx - box / 2, bottom - cell * 0.35, box, cell * 0.7),
                       Qt.AlignCenter, LETTERS[i])
            cy = oy + (size - 1 - i) * cell
            p.drawText(QRectF(left - box / 2, cy - cell * 0.35, box, cell * 0.7),
                       Qt.AlignCenter, str(i + 1))
            p.drawText(QRectF(right - box / 2, cy - cell * 0.35, box, cell * 0.7),
                       Qt.AlignCenter, str(i + 1))

    def _stone_sprite(self, color: int, r: float, cell: float) -> QPixmap:
        """一颗子的「阴影 + 高光渐变」预渲染图（第 32 轮）。

        实测：每颗子每帧现建一个 `QRadialGradient` 再画两次椭圆要 **100 µs**
        （180 子 = 18 ms/帧），而这张图跟棋局无关、只跟「颜色 + 半径 + 格宽 + DPR」
        有关 —— 全盘 361 颗子一共只有两种。缓存之后每颗子是一次 `drawPixmap`。

        图里留了 `pad`：子的投影是「向下偏移 cell*0.05 的同尺寸椭圆」，
        不留边就会被裁掉一道，看着像子被切了底。
        """
        dpr = float(self.devicePixelRatioF())
        key = (color, round(r, 2), round(cell, 2), round(dpr, 3))
        hit = self._sprites.get(key)
        if hit is not None:
            return hit
        pad = r * 0.18 + 2.0
        side = (r + pad) * 2.0
        pm = QPixmap(max(2, int(math.ceil(side * dpr))), max(2, int(math.ceil(side * dpr))))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        q = QPainter(pm)
        q.setRenderHint(QPainter.Antialiasing, True)
        c = QPointF(side / 2.0, side / 2.0)
        # 投影：网页版用 shadowBlur=cell*0.12 / offsetY=cell*0.05；QPainter 没有阴影，
        # 用「先画一层半透明黑椭圆再画子」等价表达，偏移与模糊半径按同比例取
        q.setPen(Qt.NoPen)
        q.setBrush(QColor(0, 0, 0, 70))
        q.drawEllipse(QPointF(c.x(), c.y() + cell * 0.05), r * 0.98, r * 0.98)
        # 高光渐变。注意参数顺序与 canvas **不一样**：
        # QRadialGradient(cx, cy, radius, fx, fy, focalRadius) 是中心在前，
        # 而 ctx.createRadialGradient(fx, fy, focalRadius, cx, cy, radius) 是内圆在前。
        # 直接照抄 canvas 的写法会传入 focalRadius > radius 的退化渐变，
        # Qt 不报错也不抛异常，**整块子画成全透明**（底下只有阴影那层半透明黑）。
        grad = QRadialGradient(c.x(), c.y(), r,
                               c.x() - r * 0.3, c.y() - r * 0.35, r * 0.15)
        if color == BLACK:
            grad.setColorAt(0.0, QColor(theme.STONE_BLACK_IN))
            grad.setColorAt(1.0, QColor(theme.STONE_BLACK_OUT))
        else:
            grad.setColorAt(0.0, QColor(theme.STONE_WHITE_IN))
            grad.setColorAt(1.0, QColor(theme.STONE_WHITE_OUT))
        q.setBrush(QBrush(grad))
        q.drawEllipse(c, r, r)
        q.end()
        self._sprites[key] = pm
        return pm

    def _draw_stone(self, p: QPainter, c: QPointF, r: float, color: int,
                    alpha: float, cell: float, scale: float = 1.0) -> None:
        """画一颗子（自己取精灵）。批量画时用 `_blit_stone` + 帧首取好的两张精灵。"""
        self._blit_stone(p, c, self._stone_sprite(color, r, cell), alpha, scale)

    def _blit_stone(self, p: QPainter, c: QPointF, pm: QPixmap,
                    alpha: float, scale: float = 1.0) -> None:
        """把一张精灵贴到 `c`。`alpha` 是「回看时整盘变暗」那层，`scale` 只给落子动效用。"""
        if alpha <= 0.0:
            return
        side = pm.width() / pm.devicePixelRatio()
        if alpha == 1.0 and scale == 1.0:
            p.drawPixmap(QPointF(c.x() - side / 2.0, c.y() - side / 2.0), pm)
            return
        p.save()                              # 只有真需要时才 save/restore（整盘变暗/动效）
        p.setOpacity(alpha)
        if scale != 1.0:
            p.translate(c)
            p.scale(scale, scale)
            p.translate(-c.x(), -c.y())
        p.drawPixmap(QPointF(c.x() - side / 2.0, c.y() - side / 2.0), pm)
        p.restore()

    def _draw_move_numbers(self, p: QPainter, cell: float, r: float, clip=None) -> None:
        if not self._show_move_numbers or not self._moves:
            return
        font = QFont(theme.FONT_FAMILY)
        font.setPixelSize(max(8, int(cell * 0.34)))
        p.setFont(font)
        seen: dict[tuple[int, int], int] = {}
        for idx, m in enumerate(self._moves):
            if m.get("x") is None or m.get("y") is None:
                continue
            seen[(int(m["x"]), int(m["y"]))] = idx + 1
        for (x, y), num in seen.items():
            if self.stone_at(x, y) == EMPTY:
                continue
            c = self.center(x, y)
            if clip is not None and not clip.intersects(
                    QRectF(c.x() - r, c.y() - r, r * 2, r * 2)):
                continue
            p.setPen(QColor("#f1f3f5") if self.stone_at(x, y) == BLACK else QColor(theme.INK))
            p.drawText(QRectF(c.x() - r, c.y() - r, r * 2, r * 2), Qt.AlignCenter, str(num))

    def _draw_dead(self, p: QPainter, cell: float, r: float, dpr: float) -> None:
        if not self._dead:
            return
        pen = QPen(QColor(theme.DEAD_MARK), max(1.5, cell * 0.07))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        s = r * 0.55
        for x, y in self._dead:
            if not (0 <= x < self._size and 0 <= y < self._size):
                continue
            c = self.center(x, y)
            p.drawLine(QPointF(c.x() - s, c.y() - s), QPointF(c.x() + s, c.y() + s))
            p.drawLine(QPointF(c.x() + s, c.y() - s), QPointF(c.x() - s, c.y() + s))

    def _draw_last_move(self, p: QPainter, r: float, dpr: float) -> None:
        if not self._last_move:
            return
        x, y = self._last_move
        if not (0 <= x < self._size and 0 <= y < self._size):
            return
        on_black = self.stone_at(x, y) == BLACK
        color = QColor(theme.LAST_MOVE_ON_BLACK if on_black else theme.LAST_MOVE_ON_WHITE)
        # 落定动效：圈从 1.35r 收到 0.45r、同时从半透明变实（第 32 轮）。
        # 只在**这一手是新落的**时才有进度；拖动回看/重连补局面时直接给终态。
        prog = self._ring_progress()
        if prog is None:
            radius, alpha = r * 0.45, 1.0
        else:
            radius, alpha = r * (0.45 + 0.90 * (1.0 - prog)), 0.25 + 0.75 * prog
        pen = QPen(color, max(1.5, r * 0.17))
        p.save()
        p.setOpacity(alpha)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(self.center(x, y), radius, radius)
        p.restore()

    def _draw_variation(self, p: QPainter, cell: float, r: float) -> None:
        for i, v in enumerate(self._variation):
            if v.get("x") is None or v.get("y") is None:
                continue
            x, y = int(v["x"]), int(v["y"])
            if not (0 <= x < self._size and 0 <= y < self._size):
                continue
            c = self.center(x, y)
            is_black = v.get("color") == BLACK
            p.save()
            p.setOpacity(0.55)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#1b1b1b") if is_black else QColor("#f8f9fa"))
            p.drawEllipse(c, r * 0.9, r * 0.9)
            p.setOpacity(1.0)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("#f8f9fa") if is_black else QColor("#1b1b1b"), 1.2))
            p.drawEllipse(c, r * 0.9, r * 0.9)
            font = QFont(theme.FONT_FAMILY)
            font.setPixelSize(max(9, int(cell * 0.36)))
            p.setFont(font)
            p.setPen(QColor("#ffffff") if is_black else QColor("#111111"))
            p.drawText(QRectF(c.x() - r, c.y() - r, r * 2, r * 2), Qt.AlignCenter,
                       str(v.get("label") or i + 1))
            p.restore()

    def _draw_marks(self, p: QPainter, cell: float, r: float, dpr: float) -> None:
        for m in self._marks:
            x, y = int(m.get("x", -1)), int(m.get("y", -1))
            if not (0 <= x < self._size and 0 <= y < self._size):
                continue
            c = self.center(x, y)
            col = QColor(theme.MARK_COLOR.get(m.get("kind", ""), theme.DEAD_MARK))
            p.save()
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(col, max(2.0, cell * 0.1)))
            p.drawEllipse(c, r * 1.05, r * 1.05)
            if m.get("label"):
                font = QFont(theme.FONT_FAMILY)
                font.setPixelSize(max(9, int(cell * 0.36)))
                font.setBold(True)
                p.setFont(font)
                p.setPen(col)
                # 字心落在**圈的右上方外沿**。网页版是 `fillText(label, px + r*1.1, py - r*1.1)`，
                # 这里取 1.35r 而不是 1.1r —— 原生端这一处**主动偏离**，因为 1.1r 不够：
                # 字宽 0.36cell、圈外径 (1.05r + 半笔宽) = 0.5435cell，
                # 1.1r 时字的内角只离圆心 (0.517-0.18)·√2 = 0.476cell，还在圈上；
                # 1.35r 时是 (0.6345-0.18)·√2 = 0.643cell > 0.5435cell，字与圈分开。
                # 截图里那个「正」被圈压过一笔，看着像缺了笔画的坏字形 —— 圈与字
                # 都是评价记号，叠在一起就两个都读不出来。
                lx, ly = c.x() + r * 1.35, c.y() - r * 1.35
                p.drawText(QRectF(lx - cell / 2, ly - cell / 2, cell, cell),
                           Qt.AlignCenter, str(m["label"]))
            p.restore()

    def _draw_hints(self, p: QPainter, cell: float, r: float) -> None:
        if not self._show_hints or not self._hints:
            return
        # 提示点是「推荐」不是「已下」：淡入一下就够了，不做缩放（缩放会让人以为是子）
        fade = self._hint_alpha()
        for i, c in enumerate(self._hints[:3]):
            if c.get("x") is None or c.get("y") is None:
                continue
            x, y = int(c["x"]), int(c["y"])
            if not (0 <= x < self._size and 0 <= y < self._size):
                continue
            pt = self.center(x, y)
            p.save()
            p.setOpacity(max(0.1, 0.42 - i * 0.09) * fade)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.HINT_FIRST if i == 0 else theme.HINT_REST))
            p.drawEllipse(pt, r * (1 - i * 0.12), r * (1 - i * 0.12))
            p.setOpacity(1.0)
            font = QFont(theme.FONT_FAMILY)
            font.setPixelSize(max(9, int(cell * 0.36)))
            font.setBold(True)
            p.setFont(font)
            p.setPen(QColor("#ffffff"))
            p.drawText(QRectF(pt.x() - r, pt.y() - r, r * 2, r * 2), Qt.AlignCenter, str(i + 1))
            p.restore()

    def _draw_hover(self, p: QPainter, r: float) -> None:
        if not self._hover or not self._interactive or self._dim:
            return
        x, y = self._hover
        # 范围校验（审计 S5）：叠层里唯一缺这道守卫的一个。换路数（19→9）时
        # 旧悬停坐标会落在新盘外，`stone_at` 现在回 EMPTY，不挡就会画到盘外。
        if not (0 <= x < self._size and 0 <= y < self._size):
            return
        if self.stone_at(x, y) != EMPTY:
            return
        p.save()
        p.setOpacity(0.4)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#111111"))
        p.drawEllipse(self.center(x, y), r, r)
        p.restore()
