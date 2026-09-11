"""胜率曲线 + 目差 + 逐手问题手散点（QtCharts）。

对应 `frontend/src/components/WinrateChart.tsx`（ECharts 双轴联动）。对局页与复盘页
共用这一个控件：网页版也是同一份组件挂两个页面（`chartSlot` 与复盘左下那张卡）。

四条口径是从网页版逐条抄来的，改动前先看清理由：
  · **玩家视角**：执白时把黑胜率翻转、目差取反，曲线永远"越高越好"。
    拿原始 `winrateBlack` 画给执白的学员，他会看见自己赢棋时曲线一路往下。
  · **缺分析的点直接跳过**（不是补 0、也不是断线）：ECharts 用 null 断线，
    QtCharts 的 QXYSeries 没有 null，只能不喂这个点。
  · **散点按 ply 对齐手数**，纵坐标取那一手的胜率（没有就取 50）：
    问题手要钉在曲线上"掉下去的那个位置"，钉在横轴上就看不出它是从哪儿掉下来的。
  · **低于 20% 涂一层淡红**：那是"这盘已经在输"的区间，网页版是 markArea，
    原生端自己画（下面 `drawBackground` 里），不用 QAreaSeries。

QtCharts 也没有 tooltip 与 markLine：读数改成一点击回填（`readout` 进 toolTip，
`pointClicked` 把 ply 交给页面去选那手棋），当前手那条竖虚线在 `drawForeground`
里自己画。

淡红带为什么不用现成的 `QAreaSeries`：它的"接管 lower/upper 两条 series 所有权"
只在 C++ 构造期间做，Shiboken 收不到通知，于是 Python 侧那两个局部包装一回收，
C++ 对象就被删掉，图表下一次布局时解空指针 —— **整个进程 access violation，
连一条 Python 栈都不留**（实测：`_probe_gc.py` 三个模式，只保住 band 也崩，
lo/hi/band 三个都保住才不崩）。这个控件每次 `set_data` 都要重建 series，
没有"长期保住局部对象"的干净写法，所以那条带与曲线下方的渐变面积一起，
拿到 `drawForeground` 里自己画（`drawBackground` 里画了看不见 —— QChart 会
自己刷一层不透明底，实测那层东西会把背景层盖得一个像素都不剩）。
"""
from __future__ import annotations

import math

from PySide6.QtCharts import (
    QChart, QChartView, QLineSeries, QScatterSeries, QValueAxis,
)
from PySide6.QtCore import QMargins, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter,
                           QPainterPath, QPen)

from ui import theme

BLACK, WHITE = 1, 2

#: 曲线主色 / 目差虚线色（WinrateChart.tsx 的 #1971c2 与 #2f9e44）
WR_COLOR = "#1971c2"
SCORE_COLOR = "#2f9e44"
#: 问题手散点：kind → (颜色, 点径)。颜色沿用棋盘标记那一套，两处指同一件事。
MARK_STYLE = {"blunder": (theme.MARK_COLOR["blunder"], 12),
              "bad": (theme.MARK_COLOR["bad"], 10),
              "slow": (theme.MARK_COLOR["slow"], 8)}
#: 「在输」的那条带（网页版 markArea 的 0~20）。实例上的 `danger_hi` 可覆盖。
DANGER_HI = 20.0
DANGER_FILL = "#e03131"
DANGER_ALPHA = 13            # 0.05 的不透明度换成 0~255
#: x 轴最多画几个刻度。再多就重叠，ECharts 的 category 轴会自动跳，这里自己算。
MAX_X_TICKS = 10
#: 纵轴最多几个刻度。横轴那套是手数可以密，纵轴密了就成了刻度盘。
MAX_Y_TICKS = 6
#: **紧凑档**（侧栏那张 150px 高的）的刻度上限。不是审美选择，是量出来的：
#: QtCharts 会把标签带画在绘图区**外面**（ECharts 的 grid 是绝对值，名字画在
#: grid 预留的那 26/34px 里），于是 6 个目差刻度挤在 54px 高的一列里，
#: 每格只有 9px 而一行字要 14px —— QtCharts 就把每个标签省略号化成「...」
#: （实测：423x150 的控件里绘图区只剩 309x42，x 轴与右轴全是点）。
COMPACT_MAX_X_TICKS = 6
COMPACT_MAX_Y_TICKS = 3
#: 多高以下算紧凑。侧栏给 150（`GamePage.tsx:280` 同值），复盘页给 200
#: （`ReviewPage.tsx:282` 同值），190 恰好把两者分开。
COMPACT_BELOW = 190
#: 紧凑档还会收掉轴标题与图例（各占一整条带：实测图例 42px、标题各 12px），
#: 收完之后绘图区从 42px 高变成 98px —— 比网页版同尺寸的 90px 还宽一点。
#: 颜色对应关系改由页面里那行说明文字承担（见 `game.py` 的 `chartHint`）。
AXIS_TITLES = {"x": "手数", "y": "胜率%", "score": "目差"}
NICE_STEPS = (1, 2, 5, 10, 20, 25, 50, 100, 200, 500)
#: 目差轴的候选间距（目）。一局棋的目差量级在 ±40 内，0.5 已经比引擎
#: 自己的读数（2 位）还细，再小就是自找小数位。
#: 后面那三档（100/200/500）不是给正常棋用的，是给「不变量」用的：
#: `nice_score_range` 承诺刻度数不越过 `max_ticks`，而紧凑档的上限只有 3 ——
#: 目差算到 ±50 以外（指导局、以及引擎把劣势读得很开的那几手）时，
#: 没有更粗的档可选就只能违约（实测 [-40, 40] 在 cap=3 下拿到 5 个刻度）。
SCORE_STEPS = (0.5, 1, 2, 2.5, 5, 10, 20, 50, 100, 200, 500)


def nice_step(span: int, max_ticks: int = MAX_X_TICKS) -> int:
    """刻度间距：让刻度数不超过 `max_ticks` 的最小"整数好值"。

    不能直接 `span / max_ticks` 再交给 QValueAxis：它把区间**等分**，
    25 手等分 10 份会得到 2.5，`%d` 格式下标签就成了 5 8 10 13 15…（会重复）。

    判据是 `span <= s * (max_ticks - 1)`，不是 `* max_ticks`：刻度数 = 区间格数 + 1
    （含 0 那一格），`_apply_range` 里 `tickCount = top // step + 1`。按 `* max_ticks`
    挑档时，最后一格正好压在 `max_ticks * step` 上 → `tickCount == max_ticks + 1`
    越了上限，QtCharts 就把整列标签省略号化成「···」（审计 L8：实测 137 手 / 紧凑档
    返回 25、tickCount=7 > 6）。
    """
    cap = max(1, max_ticks - 1)
    for s in NICE_STEPS:
        if span <= s * cap:
            return s
    return NICE_STEPS[-1]


def _finite(value):
    """把 `inf` / `nan` / 非数统一成 None。

    `scoreLead` 是引擎给的浮点，KataGo 在极端局面下会给出 `inf`（`json.loads`
    默认接受 `NaN` / `Infinity`，后端 `or 0.0` 兜不住 —— NaN 是 truthy）。
    这类值一旦流进 `math.floor/ceil` 就抛 `OverflowError / ValueError`，
    整条曲线连带坐标轴一起消失（审计 1.20）。解析入口一律先过这一层。
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def nice_score_range(vals, max_ticks: int = MAX_Y_TICKS) -> tuple[float, float, int]:
    """把目差轴对齐到整齐刻度，返回 `(lo, hi, tick_count)`。

    QtCharts 只会把区间**等分**，不会像 ECharts 那样自动取整档：-3.8~5.0 五等分
    得出一组 2.8 / 0.6 / -1.6 / -3.8 的标签（实测截图 p4_02），读一眼得先心算
    一次减法 —— 而这条轴存在的目的正是「现在领先几目」。留边距那条口径不变，
    变的只是留完之后往整档外扩。

    为什么外扩之后还要**重新数一遍刻度**：以前那个档是按外扩**之前**的跨度挑的，
    而外扩最多会多出两格。实测 -14~-4.4 这一组（真报告里的目差就是这个量级）：
    2.5 那一档按跨度 13.6 中选，外扩完变成 -17.5~0.0 → 8 个刻度，而 `max_ticks`
    是 6 —— 8 行字挤在 82px 高的标签带里（每格 10px 而一行字要 14px），
    QtCharts 不报错也不警告，直接把整列标签省略号化成「···」（实测那一列只剩
    9 个墨像素）。所以现在是：逐档试，取第一个「外扩完仍然装得下」的档。
    """
    vals = [_finite(v) for v in (vals or [])]
    vals = [v for v in vals if v is not None]
    if not vals:
        return -1.0, 1.0, 3
    lo, hi = min(vals), max(vals)
    pad = max(2.0, (hi - lo) * 0.15)      # 至少留 2 目：全程只领先 0.4 目也得画得出线
    lo, hi = lo - pad, hi + pad
    want = max(3, max_ticks)              # 少于 3 个刻度就不是坐标轴，是一条线
    for step in SCORE_STEPS:
        a = math.floor(lo / step) * step
        b = math.ceil(hi / step) * step
        if b <= a:                        # 区间正好落在整档上：外扩不能把它压成零宽
            b = a + step
        n = int(round((b - a) / step)) + 1
        if n <= want:
            return round(a, 4), round(b, 4), n
    # 一档都装不下（数据跨度超过 50 * max_ticks）：退回最粗那档。
    # 宁密不空：刻度多了只是挤，区间没了那条目差线就根本不画。
    step = SCORE_STEPS[-1]
    a = math.floor(lo / step) * step
    b = math.ceil(hi / step) * step
    return round(a, 4), round(b, 4), int(round((b - a) / step)) + 1


def player_winrate(point: dict, color: int):
    """这一点上的**玩家视角**胜率（0~1）。没有分析就返回 None。"""
    key = "winrateBlack" if color == BLACK else "winrateWhite"
    return _finite(point.get(key))


def player_score(point: dict, color: int):
    """玩家视角目差（正数 = 领先）。`scoreLead` 是黑视角，执白取反。"""
    v = _finite(point.get("scoreLead"))
    if v is None:
        return None
    return v if color == BLACK else -v


class WinrateChart(QChartView):
    """可点击的胜率曲线。`pointClicked(ply)` 与网页版 `onSelect` 同义。"""

    pointClicked = Signal(int)

    def __init__(self, height: int = 220, parent=None):
        chart = QChart()
        chart.setMargins(QMargins(2, 2, 2, 2))
        chart.setBackgroundRoundness(6)     # 不叫 backgroundRoundRadius（PySide 会提示）
        super().__init__(chart, parent)
        self.setRenderHint(QPainter.Antialiasing)
        self.setMinimumHeight(height)
        self._chart = chart
        self._height = height
        self._plies: list[int] = []          # 画进曲线的点（跳过缺分析的）
        self._wr: dict[int, float] = {}      # ply → 玩家视角胜率 %
        self._score: dict[int, float] = {}   # ply → 玩家视角目差
        self._axis_x = QValueAxis()
        self._axis_y = QValueAxis()
        self._axis_score = QValueAxis()
        self._current: int | None = None
        self.danger_hi = DANGER_HI           # 0 关掉淡红带
        self._compact = False                # 由 `_apply_compact` 按实际高度算
        self._has_score = False              # 有没有目差那条线（图例只在它存在时有用）
        chart.legend().setVisible(False)     # set_data 里按需再开（见 _show_marker）
        # `setAxisX(axis, Qt.AlignBottom)` 是错的：PySide 绑出来的第二个参数是
        # **series** 而不是对齐方向（C++ 那个「设默认轴」的重载没绑出来），
        # 传 AlignmentFlag 只会拿到一个 TypeError。挂轴一律走 addAxis。
        chart.addAxis(self._axis_x, Qt.AlignBottom)
        chart.addAxis(self._axis_y, Qt.AlignLeft)
        chart.addAxis(self._axis_score, Qt.AlignRight)
        self._axis_x.setLabelsVisible(True)
        self._axis_x.setLabelFormat("%d")
        self._axis_x.setTitleText(AXIS_TITLES["x"])
        self._axis_y.setRange(0, 100)
        # 胜率轴固定 0~100，默认格式会标成 100.0 / 75.0 / 50.0：百分比没有小数
        self._axis_y.setLabelFormat("%.0f")
        self._axis_y.setTitleText(AXIS_TITLES["y"])
        self._axis_score.setTitleText(AXIS_TITLES["score"])
        self._style_axes()

    # ---------------------------------------------------------------- 紧凑档

    def _apply_compact(self) -> None:
        """按**当前实际高度**决定要不要让出轴标题与图例。它是图例可见性的
        唯一主人（`set_data` 只记下「有没有目差那条线」再调它），因为两件事
        都能单独改变结论：高度会变（拖动窗口），数据也会变（换一局）。

        为什么不是「构造时一次性定」：`height=150` 只是 `minimumHeight`，
        侧栏给它 423x150、拖动窗口时还会变；而定下轴标题/图例那一瞬间
        控件往往还是默认尺寸。为什么轴标题与图例一起收：它们三个说的
        是同一件事（图例「胜率/目差」就是两条纵轴的名字），网页版靠
        `grid: {top: 26, bottom: 34}` 把名字画进边距里，而 QtCharts 是每条各占
        一整带 —— 150px 高的控件里养不起三份重复信息（实测绘图区只剩 42px，
        而轴标签被省略号化成一片「...」）。颜色对应关系改由页面那行说明承担。
        """
        want = 0 < self.height() < COMPACT_BELOW
        self._compact = want
        for axis, key in ((self._axis_x, "x"), (self._axis_y, "y"),
                          (self._axis_score, "score")):
            axis.setTitleText("" if want else AXIS_TITLES[key])
        # 图例那句**不能**只在档位变化时做：`set_data` 会按「有没有目差那条线」
        # 重新把它打开，而那时档位没变 —— 早退就会留下一个本不该显示的图例
        # （实测：紧凑档下 `legend().isVisible()` 仍为 True，绘图区又被抢回 42px）。
        self._chart.legend().setVisible(self._has_score and not want)
        # 刻度上限变了就得重算区间（`_apply_range` 里才是真正设 tickCount 的地方）
        self._apply_range()
        if self._plies:
            self._apply_score_range()
        self.viewport().update()

    @property
    def compact(self) -> bool:
        return self._compact

    def resizeEvent(self, ev):                                  # noqa: N802
        super().resizeEvent(ev)
        # `__init__` 里那句 `super().__init__(chart, parent)` 就能触发一次 resize，
        # 那时下面这些字段还不存在（PySide 不会替虚拟重写护短）。
        if hasattr(self, "_compact"):
            self._apply_compact()

    # ---------------------------------------------------------------- 样式
    # QtCharts 默认画的是 Windows 灰底 + 白网格，与这套浅色卡片完全不搭；
    # 网页版那侧 ECharts 的轴文字 #868e96、网格 #f1f3f5，这里逐条对上。

    def _style_axes(self) -> None:
        mut = QColor(theme.MUTED)
        grid = QColor("#f1f3f5")
        line = QColor("#ced4da")
        for axis in (self._axis_x, self._axis_y, self._axis_score):
            axis.setLabelsColor(mut)
            axis.setTitleBrush(QBrush(QColor(theme.MUTED)))
            axis.setGridLineColor(grid)
            axis.setLinePenColor(line)        # 不叫 setLineLineColor
            axis.setLabelsFont(QFont(theme.FONT_FAMILY, 8))
        # 只有胜率轴画横线网格；目差轴再画一层就成两张网（网页版 splitLine: false）
        # 颜色参数不认 Qt.transparent 枚举（QColor 那个构造是 C++ 隐式的，
        # Shiboken 不会替它转），要自己先把 GlobalColor 包成一个 QColor
        self._axis_score.setGridLineColor(QColor(Qt.transparent))

    # ---------------------------------------------------------------- 数据

    def set_data(self, curve, marks=(), player_color: int = BLACK) -> None:
        """重画整条曲线。`curve` 是 `[{ply, winrateBlack, winrateWhite, scoreLead}]`。

        每次全量重建（removeAllSeries）而不是增量改点：一盘棋最多两三百个点，
        重建的开销是毫秒级，而增量改要自己维护"上一次的 series 里有哪些点"，
        那份状态一旦与 curve 不同步就是画错图 —— 复盘页每 1.2 秒轮一次进度、
        完成后一次性灌报告，本来也没有增量可言。
        """
        # **先解析完，再拆旧 series**（审计 1.20）：从前是 `removeAllSeries()` 打头，
        # 解析途中一旦抛异常（`inf` 进 math.floor），旧曲线已经没了、新 series 又没建，
        # 图表就停在「白板 + 空轴」上；更糟的是 `_chart_sig` 已在调用方记下，
        # 同一份数据之后被指纹短路 —— 本局内再也不会重画。
        plies: list[int] = []
        wr_map: dict[int, float] = {}
        score_map: dict[int, float] = {}
        pts_wr, pts_score = [], []
        for point in curve or []:
            if not isinstance(point, dict):
                continue
            try:
                ply = int(point.get("ply"))
            except (TypeError, ValueError):
                continue                    # 没有 ply 的点画不出来，也不能顶掉后面的
            wr = player_winrate(point, player_color)
            if wr is None:
                continue                    # 见模块 docstring：缺分析就跳过
            plies.append(ply)
            wr_map[ply] = round(wr * 100, 1)
            sc = player_score(point, player_color)
            if sc is not None:
                score_map[ply] = round(sc, 1)
            pts_wr.append((ply, wr_map[ply]))
            if sc is not None:
                pts_score.append((ply, score_map[ply]))

        self._chart.removeAllSeries()
        self._plies, self._wr, self._score = plies, wr_map, score_map
        self._apply_range()
        if not self._plies:
            self._has_score = False
            self.viewport().update()
            self._chart.legend().setVisible(False)
            return
        self._apply_score_range()

        wr_series = self._add_line("胜率", pts_wr, WR_COLOR, 2.0, Qt.SolidLine,
                                   self._axis_y)
        self._add_line("目差", pts_score, SCORE_COLOR, 1.4, Qt.DashLine,
                       self._axis_score)
        self._add_marks(marks, wr_series)
        self._has_score = bool(pts_score)
        self._apply_compact()          # 图例可不可见统一由它定（紧凑档会收掉）
        self.viewport().update()             # 淡红带与竖线都在 drawForeground 里

    def _apply_range(self) -> None:
        """手数轴：区间往整档外扩，刻度数按高度限（见 `COMPACT_MAX_X_TICKS`）。"""
        cap = COMPACT_MAX_X_TICKS if self._compact else MAX_X_TICKS
        if not self._plies:
            self._axis_x.setRange(0, 1)
            self._axis_x.setTickCount(2)
            return
        last = self._plies[-1]
        step = nice_step(max(1, last), cap)
        top = max(step, ((last + step - 1) // step) * step)
        self._axis_x.setRange(0, top)
        self._axis_x.setTickCount(top // step + 1)

    def _apply_score_range(self) -> None:
        """目差轴自己算区间（QtCharts 不会像 ECharts 那样自动 fit），并对齐整档。

        不区间会停在 0~0：轴宽为零，目差那条线直接看不见（不报错、不提示）。
        档位与留边一起交给 `nice_score_range`，那里有为什么不能等分的账。
        """
        cap = COMPACT_MAX_Y_TICKS if self._compact else MAX_Y_TICKS
        lo, hi, ticks = nice_score_range(self._score.values(), cap)
        self._axis_score.setRange(lo, hi)
        self._axis_score.setTickCount(ticks)
        step = (hi - lo) / max(1, ticks - 1)
        self._axis_score.setLabelFormat("%.0f" if step.is_integer() else "%.1f")

    def _add_line(self, name, pts, color, width, style, axis_y):
        series = QLineSeries()
        series.setName(name)
        for x, y in pts:
            series.append(x, y)
        pen = QPen(QColor(color))
        pen.setWidthF(width)
        pen.setStyle(style)
        series.setPen(pen)
        series.setPointsVisible(False)
        self._chart.addSeries(series)
        # **两条轴都要挂**。只挂 y 轴不会报错，会在下一次算几何布局时
        # 解空指针（实测：整个 pytest 进程 access violation 直接死，
        # 连不上一条 Python 栈）。双轴图里第二条 y 轴也要带 x 轴。
        series.attachAxis(self._axis_x)
        series.attachAxis(axis_y)
        return series

    def danger_rect(self):
        """那条淡红带在**场景坐标**里的矩形；没有数据或关了带就返回 None。

        纵轴固定 0~100（`set_data` 从不改它），所以「胜率 20% 在哪」直接按比例算，
        不必绕 `mapToPosition(series)` —— 也不必修那条会崩的 QAreaSeries。
        """
        if self.danger_hi <= 0 or not self._plies:
            return None
        pa = self._chart.plotArea()
        if pa.width() <= 1 or pa.height() <= 1:
            return None
        lo, hi = self._axis_y.min(), self._axis_y.max()
        if hi <= lo:
            return None
        top = pa.bottom() - (self.danger_hi - lo) / (hi - lo) * pa.height()
        top = max(pa.top(), min(pa.bottom(), top))
        return QRectF(pa.left(), top, pa.width(), pa.bottom() - top)

    def _area_points(self):
        """曲线下方面积的多边形顶点（场景坐标，含闭合到底边的两个角）。

        网页版是 `areaStyle` 的 LinearGradient(0.28 → 0.02)，那一层蓝是在告诉
        学员「曲线以下是我方的地盘」；没有它，胜率轴与目差轴两条线浮在白底上，
        一眼分不清哪条才是主角。自己拼：按 ply 顺序取点，再沿底边绕回去。

        为什么不用 `QLineSeries.setBrush()`：本机 QtCharts 6.11.2 上它不产生面积
        填充（不报错，就是看不见），而正规那条路 `QAreaSeries` 会踩坏内存。
        画在前景层的话它会薄薄盖住曲线自己的下沿：同一色系的 0.28 以下，
        看上去只是线稍微润了一点，不值得为它把 `chart` 的底改透明。
        """
        pa = self._scene_rect()
        if pa is None or len(self._plies) < 2:
            return None
        pts = [self.scene_point_for(p) for p in self._plies]
        pts = [p for p in pts if p is not None]
        if len(pts) < 2:
            return None
        first, last = pts[0], pts[-1]
        return pts + [QPointF(last.x(), pa.bottom()), QPointF(first.x(), pa.bottom())]

    def _add_marks(self, marks, wr_series) -> None:
        """问题手散点。三种 kind 各一条 series —— QtCharts 的样式挂在 series 上，
        不能像 ECharts 那样一个 markPoint 数组里各点各的颜色。"""
        buckets: dict[str, list[tuple[int, float]]] = {}
        for m in marks or []:
            kind = m.get("kind") or ""
            if kind not in MARK_STYLE:
                continue
            try:
                ply = int(m.get("ply"))
            except (TypeError, ValueError):
                continue
            if ply not in self._wr:
                continue                    # 曲线没这一点，钉不住就飘在 50% 上，是假信号
            buckets.setdefault(kind, []).append((ply, self._wr[ply]))
        for kind, pts in buckets.items():
            color, size = MARK_STYLE[kind]
            series = QScatterSeries()
            series.setName({"blunder": "大恶手", "bad": "恶手",
                            "slow": "缓手"}[kind])
            series.setMarkerSize(float(size))   # 不接 QSizeF（PySide 只绑了 float 重载）
            series.setBrush(QColor(color))
            series.setPen(QPen(QColor(color).darker(120), 1))
            for x, y in pts:
                series.append(x, y)
            self._chart.addSeries(series)
            series.attachAxis(self._axis_x)
            series.attachAxis(self._axis_y)
            # 散点会自动进图例（QtCharts 每条 series 都进），但网页版图例只有
            # 胜率/目差 两项 —— 散点的颜色与棋盘上的标记、逐手卡片上的徽章是
            # 一套，再在图例里重复一遍只是偷走绘图区的高度。一律关掉。
            self._show_marker(series, False)

    def _show_marker(self, series, shown: bool) -> None:
        """单独开关某条 series 的图例项。QtCharts 不给 series 一个 hideLegend 标志，
        只能拿到它的 marker 再 setVisible —— 拿不到（还没进图例）就当没这回事。"""
        for marker in self._chart.legend().markers(series) or []:
            marker.setVisible(shown)

    # ---------------------------------------------------------------- 当前手

    def set_current(self, ply) -> None:
        """把"正在看第几手"画成一条竖虚线。None 收起。"""
        try:
            value = int(ply)
        except (TypeError, ValueError):
            value = None
        if value == self._current:
            return
        self._current = value
        self.viewport().update()

    @property
    def current(self):
        return self._current

    def plotted_points(self) -> int:
        """曲线上的点数。验收要拿它跟手数对齐（见 tests/test_review_flow.py）。"""
        return len(self._plies)

    def readout(self, ply: int) -> str:
        """一个点的读数，口径与网页版 tooltip 一致：`第 N 手后 / 胜率 / 目差`。"""
        wr = self._wr.get(ply)
        sc = self._score.get(ply)
        return (f"第 {ply} 手后\n我方胜率：{'—' if wr is None else f'{wr}%'}"
                f"　目差：{'—' if sc is None else f'{sc} 目'}")

    # ---------------------------------------------------------------- 坐标换算
    # 三层坐标得分清：`plotArea()` 与 drawForeground 的 painter 在**场景**坐标，
    # 鼠标事件与 `QTest.mouseClick` 在**视口**坐标，`grab()` 出来是**设备像素**。
    # 所以内部一律算场景坐标，只在对外两个入口上做一次 mapFromScene。

    def _scene_rect(self):
        """绘图区的场景矩形；图表还没被摆进布局时是空的。"""
        pa = self._chart.plotArea()
        if pa.width() <= 1 or pa.height() <= 1:
            return None
        return pa

    def _plot_rect(self):
        """绘图区的**视口**矩形（鼠标点位与点击判定都用它）。"""
        pa = self._scene_rect()
        if pa is None:
            return None
        origin = self.mapFromScene(pa.topLeft())
        corner = self.mapFromScene(pa.bottomRight())
        return origin.x(), origin.y(), corner.x(), corner.y()

    def scene_point_for(self, ply: int):
        """ply → 曲线上那一点（**场景**坐标）。算不出就 None。

        纵向映射：胜率越高越靠上 —— 场景 y 向下长，所以是
        `bottom - 值占比 * 高度`。写成 `bottom - (100 - 值) / 100 * 高度` 会
        得到一个沿中线上下翻转的点：x 方向完全正确，所以点击跳手、竖虚线
        都看不出错，只有靠这个 y 的东西（面积多边形）会整块跑错地方。
        本项目已经在棋盘上错过一次上下翻转，同一个形状这里钉一条方向断言。
        """
        pa = self._scene_rect()
        if pa is None or not self._plies:
            return None
        lo, hi = self._axis_x.min(), self._axis_x.max()
        if hi == lo:
            return None
        x = pa.left() + (ply - lo) / (hi - lo) * pa.width()
        y = self._wr.get(ply)
        if y is None:
            y = 50.0
        axis_lo, axis_hi = self._axis_y.min(), self._axis_y.max()
        frac = (y - axis_lo) / (axis_hi - axis_lo) if axis_hi > axis_lo else 0.5
        return QPointF(x, pa.bottom() - frac * pa.height())

    def viewport_point_for(self, ply: int):
        """ply → 视口坐标。测试点曲线就用它，不写死像素。"""
        point = self.scene_point_for(ply)
        if point is None:
            return None
        return self.mapFromScene(point)

    def ply_at_viewport(self, pos) -> int | None:
        """视口坐标 → 最近的一个有数据的 ply；点在绘图区外返回 None。"""
        rect = self._plot_rect()
        if rect is None or not self._plies:
            return None
        left, top, right, bottom = rect
        if not (left - 4 <= pos.x() <= right + 4 and top - 4 <= pos.y() <= bottom + 4):
            return None
        lo, hi = self._axis_x.min(), self._axis_x.max()
        if hi == lo:
            return self._plies[0]
        value = lo + (pos.x() - left) / (right - left) * (hi - lo)
        return min(self._plies, key=lambda p: (abs(p - value), p))

    def mousePressEvent(self, ev):                            # noqa: N802
        """点曲线 = 跳到那一手。只吃左键，右键留给以后的"标记此手"。"""
        if ev.button() == Qt.LeftButton and self._plies:
            # `pos()` 已被标 deprecated（带 DeprecationWarning），`position()` 是 Qt6 的
            # 那个：同一个坐标，只是改成 QPointF。
            ply = self.ply_at_viewport(ev.position().toPoint())
            if ply is not None:
                self.setToolTip(self.readout(ply))
                self.pointClicked.emit(ply)
                ev.accept()
                return
        super().mousePressEvent(ev)

    # ---------------------------------------------------------------- 绘制

    def drawForeground(self, painter, rect):                  # noqa: N802
        """淡红劣势带 + 曲线下方的渐变面积 + 当前手那条竖虚线与标签。

        QtCharts 没有 markLine / markArea，而这几样都必须跟着图表一起重绘
        （缩放、换数据都要），所以画在这里：painter 已在场景坐标下，
        与 `plotArea()` 同一套坐标。

        为什么不用 `drawBackground`（听起来层次更对）：那里画的东西**一个像素都
        看不见** —— QChart 自己会在场景里刷一层不透明圆角底，把视口背景盖掉
        （实测：关掉劣势带前后整张图 0 个样点不同）。要么把 `chart` 的
        `backgroundBrush` 改成透明并自己补一层卡片底，要么就在前景画；
        这里选前景，代价是面积会薄薄地盖住曲线自己的下沿（同色系，看不出来）。
        """
        super().drawForeground(painter, rect)
        band = self.danger_rect()
        if band is not None:
            fill = QColor(DANGER_FILL)
            fill.setAlpha(DANGER_ALPHA)
            painter.fillRect(band, fill)
        pts = self._area_points()
        if pts is not None:
            pa = self._scene_rect()
            # 渐变是竖向的：`QLinearGradient(x1, y1, x2, y2)` 或两个 QPointF，
            # 没有「两个 float = 一条竖线」那个写法
            grad = QLinearGradient(QPointF(pa.left(), pa.top()),
                                   QPointF(pa.left(), pa.bottom()))
            top = QColor(WR_COLOR)
            top.setAlpha(71)                     # 网页版的 0.28
            bottom = QColor(WR_COLOR)
            bottom.setAlpha(5)                   # 网页版的 0.02
            grad.setColorAt(0.0, top)
            grad.setColorAt(1.0, bottom)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(grad))
            path = QPainterPath()
            path.moveTo(pts[0])
            for p in pts[1:]:
                path.lineTo(p)
            path.closeSubpath()
            painter.drawPath(path)
        if self._current is None:
            return
        point = self.scene_point_for(self._current)
        pa = self._scene_rect()
        if point is None or pa is None:
            return
        painter.save()
        pen = QPen(QColor(theme.INK))
        pen.setStyle(Qt.DashLine)
        pen.setWidthF(1.2)
        painter.setPen(pen)
        painter.drawLine(QPointF(point.x(), pa.top()), QPointF(point.x(), pa.bottom()))
        painter.setFont(QFont(theme.FONT_FAMILY, 8))
        label = f"第 {self._current} 手"
        # 标签贴着线画，靠右边就放左边：贴着绘图区右边缘还往右写就会被裁掉
        box_x = point.x() + 4 if point.x() + 60 < pa.right() else point.x() - 64
        painter.setPen(QPen(QColor(theme.INK)))
        painter.drawText(QPointF(box_x, pa.top() + 12), label)
        painter.restore()
