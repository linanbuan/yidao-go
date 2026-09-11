"""P4 之一：胜率曲线控件的验收（不连后端，喂合成数据）。

为什么单独一支而不是等复盘页一起测：这个控件要同时服务对局页与复盘页，
它的四条口径（玩家视角翻转、缺点跳过、散点钉在曲线上、点击跳手）
在页面里测会被网络与引擎行为糊住 —— 单独喂数据才能把「画错」和「数据没到」分开。

其中点击那条是**双向**验的：先正着算 `viewport_point_for(ply)`，
再反着喂给 `ply_at_viewport(pos)`，最后真的 `QTest.mouseClick` 一次。
坐标系这件事（场景 / 视口 / 设备像素三层）光看代码推是靠不住，
本项目已经在棋盘上错过一次上下翻转了。
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest

from tests import harness as H
from ui import theme
from ui.widgets import winrate
from ui.widgets.winrate import BLACK, WHITE, WinrateChart, nice_step, player_score

#: 41 个点（ply 0~40）：0 是开局那一帧，所以点数比手数多 1（见下面的断言注释）。
N_PLY = 41


def _curve(n: int = N_PLY):
    out = []
    for i in range(n):
        wr = 0.30 + i * 0.012            # 一路从 30% 涨到 78%
        out.append({"ply": i, "winrateBlack": wr, "winrateWhite": 1 - wr,
                    "scoreLead": i * 0.4 - 6.0, "visits": 96})
    return out


@pytest.fixture
def chart(qapp):
    w = WinrateChart()
    w.resize(620, 220)
    w.show()
    qapp.processEvents()
    yield w
    w.close()
    w.deleteLater()


def _series_names(w) -> list[str]:
    return sorted(s.name() for s in w.chart().series())


def _diff_pixels(a, b, limit: int = 24) -> int:
    """两个画面差多少个样点（每 3 像素取一个）。`limit` 是「算不算不同」的门槛。"""
    n = 0
    for y in range(0, a.height(), 3):
        for x in range(0, a.width(), 3):
            if H.color_distance(a.pixelColor(x, y), b.pixelColor(x, y)) > limit:
                n += 1
    return n


# ---------------------------------------------------------------- 玩家视角

def test_curve_is_flipped_for_the_white_player(chart):
    """同一份数据，执白看到的曲线必须与执黑上下颠倒（含目差取反）。

    这条不翻，执白的学员会看见「自己赢棋时曲线一路往下」—— 网页版
    `WinrateChart.tsx` 那句注释说的就是这件事，原生端同一条口径。
    """
    chart.set_data(_curve(), (), BLACK)
    black_side = chart.readout(20)
    chart.set_data(_curve(), (), WHITE)
    white_side = chart.readout(20)
    # 黑 54.0% ↔ 白 46.0%（0.30 + 20*0.012 = 0.54）
    assert "54.0%" in black_side, black_side
    assert "46.0%" in white_side, white_side
    # 目差同理：黑 +2.0 目 → 白 -2.0 目
    assert "2.0 目" in black_side and "-2.0 目" in white_side
    assert player_score({"scoreLead": 2.0}, WHITE) == -2.0
    assert player_score({"scoreLead": None}, BLACK) is None


# ---------------------------------------------------------------- 点与缺口

def test_missing_analysis_points_are_skipped_not_zeroed(chart):
    """没有胜率的帧**不进曲线**：补 0 会把「没分析到」画成「大劣势」。"""
    curve = _curve(21)
    curve[7]["winrateBlack"] = None
    curve[7]["winrateWhite"] = None
    curve[8]["winrateBlack"] = None
    curve[8]["winrateWhite"] = None
    chart.set_data(curve, (), BLACK)
    assert chart.plotted_points() == 19
    assert chart.readout(7) == "第 7 手后\n我方胜率：—　目差：—"
    # 缺的那一帧没数据 → 正着算点位拿不到它，反着点也只会落到邻近的实点上
    assert chart.ply_at_viewport(chart.viewport_point_for(6)) in (6, 5)


def test_empty_curve_shows_an_empty_chart_instead_of_a_lie(chart):
    """一份报告都还没有时不许画出 0% 的平线（那等于宣布「你已经输了」）。"""
    chart.set_data([], (), BLACK)
    assert chart.plotted_points() == 0
    assert chart.chart().series() == []
    assert not chart.chart().legend().isVisible()
    assert chart.viewport_point_for(0) is None
    assert chart.ply_at_viewport(chart.rect().center()) is None


# ---------------------------------------------------------------- 点击跳手

def test_clicking_the_chart_jumps_to_the_nearest_move(chart, qapp):
    """正算点位 → 反解 ply → 真点一次，三条路必须给出同一个手数。"""
    chart.set_data(_curve(), (), BLACK)
    assert chart.plotted_points() == N_PLY
    for ply in (0, 3, 20, 40):
        pos = chart.viewport_point_for(ply)
        assert pos is not None, f"第 {ply} 手算不出屏幕位置（绘图区还没布局好？）"
        assert chart.ply_at_viewport(pos) == ply, f"第 {ply} 手点出来不是自己"
    seen: list[int] = []
    chart.pointClicked.connect(seen.append)
    QTest.mouseClick(chart.viewport(), Qt.LeftButton, Qt.NoModifier,
                     chart.viewport_point_for(20))
    qapp.processEvents()
    assert seen == [20], f"点曲线没有跳到那一手：{seen}"
    assert "第 20 手后" in chart.toolTip()
    # 绘图区之外（左侧 y 轴标签上）不该被当成点击。
    # 这里不能拿 `chart.rect().topLeft()` 当「图外那一点」：它是 (0,0)，而
    # `QTest.mouseClick` 把 `QPoint()` 当**哨兵值**（「位置未指定」），会改拿当前
    # 光标位置 —— 实测那一下落在 (309, 109)，正好还是刚点过的第 20 手。
    seen.clear()
    left, top, right, bottom = chart._plot_rect()
    QTest.mouseClick(chart.viewport(), Qt.LeftButton, Qt.NoModifier,
                     QPoint(left - 20, (top + bottom) // 2))
    qapp.processEvents()
    assert seen == [], "点在轴外面也跳手，等于整张图都是按钮"


# ---------------------------------------------------------------- 散点

def test_problem_moves_are_pinned_onto_the_curve(chart):
    """问题手钉在**曲线上那一手的位置**，钉在横轴上就看不出它是从哪儿掉下来的。"""
    marks = [{"ply": 5, "kind": "blunder"}, {"ply": 12, "kind": "bad"},
             {"ply": 12, "kind": "slow"}, {"ply": 30, "kind": "good"},
             {"ply": 999, "kind": "slow"}]
    chart.set_data(_curve(), marks, BLACK)
    names = _series_names(chart)
    # 好手不上图（网页版 chartMarks 只挑 slow/bad/blunder），
    # 曲线外的 ply 也不许凭空造一个点（那个 999 会飘到图外去）。
    # 劣势带也不在 series 里：它是 drawForeground 画的（见 winrate.py 模块注释）。
    assert names == ["大恶手", "恶手", "目差", "缓手", "胜率"], names
    by_name = {s.name(): s for s in chart.chart().series()}
    assert [(p.x(), p.y()) for p in by_name["大恶手"].points()] == [(5, 36.0)]
    assert [p.y() for p in by_name["缓手"].points()] == [44.4]
    # 点径：`setMarkerSize` 只接 float，`markerSize()` 也就还给 float
    # （C++ 那边返的是 QSizeF，PySide 绑成了标量 —— 拿 `.width()` 会拿到 float 上）
    assert by_name["大恶手"].markerSize() == 12
    assert by_name["缓手"].markerSize() == 8


# ---------------------------------------------------------------- 轴与刻度

def test_axis_ticks_stay_whole_numbers(chart):
    """刻度间距取"整数好值"。等分 10 份会把标签画成 5 8 10 13 15…（重复且误导）。"""
    assert nice_step(9) == 1 and nice_step(25) == 5 and nice_step(137) == 20
    chart.set_data(_curve(138), (), BLACK)      # ply 0..137
    # `chart.axisX()` 也被标了 deprecated，走不带 series 的 `axes(orientation)`
    axis = chart.chart().axes(Qt.Horizontal)[0]
    step = (axis.max() - axis.min()) / (axis.tickCount() - 1)
    assert float(step) == int(step), f"刻度间距不是整数：{axis.min()}~{axis.max()}/{step}"
    assert axis.max() >= 137 and axis.min() == 0


def test_score_axis_is_wide_enough_to_show_the_line(chart):
    """目差轴必须自己算区间：QtCharts 不自动 fit，停在 0~0 时那条线**静默消失**。

    整盘只领先零点几目也要有区间（至少上下各 2 目），否则「目差」这条与图例
    都在，画面上却只有一条胜率曲线 —— 少了一件事而不报任何错。
    """
    flat = [{"ply": i, "winrateBlack": 0.5, "winrateWhite": 0.5,
             "scoreLead": 0.3 * i, "visits": 90} for i in range(11)]
    chart.set_data(flat, (), BLACK)
    axis = chart.chart().axes(Qt.Vertical)[-1]      # 左=胜率、右=目差
    assert axis.max() > axis.min() + 4, f"目差轴区间塌了：{axis.min()}~{axis.max()}"
    assert axis.min() <= 0.0 and axis.max() >= 3.0


def test_score_axis_ticks_land_on_round_numbers(chart):
    """目差轴的刻度要落在整档上：QtCharts 只把区间**等分**，不等分出来的标签
    要人先做一次减法。实测截图 p4_02 那一组就是 -3.8~5.0 五等分出的
    2.8 / 0.6 / -1.6 / -3.8 —— 而这条轴存在的目的正是「现在领先几目」。

    顺带钉住「刻度数不得越过 `max_ticks`」：外扩是在选档**之后**发生的，
    不重新数一遍就会拿到一个超限的刻度数（下面那个 -14~-4.4 就是旧版的 8 个，
    挤在 82px 高的标签带里直接变成一列「···」）。这条不变量对每一档上限都要成立。
    """
    assert winrate.nice_score_range([]) == (-1.0, 1.0, 3)
    assert winrate.nice_score_range([0.0]) == (-2.0, 2.0, 5)      # 全程持平也要有区间
    assert winrate.nice_score_range([12.4, -8.2]) == (-20.0, 20.0, 5)
    # 真报告那一种量级：旧版给的是 (-17.5, 0.0, 8)，越过了 6 这一档上限
    assert winrate.nice_score_range([-14.0 + 0.8 * i for i in range(13)]) == \
        (-20.0, 0.0, 5)

    lo, hi, ticks = winrate.nice_score_range([round(3.0 - 0.4 * i, 2) for i in range(13)])
    assert (lo, hi, ticks) == (-4.0, 6.0, 6), (lo, hi, ticks)

    for cap in (3, 5, 6, 10):
        for vals in ([], [0.0], [-14.0, -4.4], [12.4, -8.2], [3.0, -1.8],
                     [0.4], [-40.0, 40.0]):
            a, b, n = winrate.nice_score_range(vals, cap)
            assert n <= max(3, cap), (vals, cap, (a, b, n))
            assert b > a, (vals, cap, (a, b, n))
            if vals:
                assert a <= min(vals) and b >= max(vals), (vals, a, b)

    curve = [{"ply": i, "winrateBlack": 0.4, "winrateWhite": 0.6,
              "scoreLead": round(3.0 - 0.4 * i, 2), "visits": 90} for i in range(13)]
    chart.set_data(curve, (), BLACK)
    axis = chart.chart().axes(Qt.Vertical)[-1]
    step = (axis.max() - axis.min()) / (axis.tickCount() - 1)
    assert step in winrate.SCORE_STEPS, f"目差刻度不整档：{step}"
    assert axis.min() <= -1.8 and axis.max() >= 3.0, "对齐整档时把数据本身框丢了"
    assert "%.0f" == axis.labelFormat(), f"整档还是标出了小数：{axis.labelFormat()}"


def test_winrate_axis_has_no_decimals(chart):
    """胜率轴固定 0~100，默认格式会标成 100.0 / 75.0：百分比没有小数。

    只能断格式串：轴标签是 QtCharts 自己画的，`clipped_texts` 只认 QLabel。
    「标签到底画没画全」有另一个口径：`harness.chart_axis_ink` 数那一列的墨像素
    （见下面 `test_a_wide_score_range_still_prints_numbers`），不是 OCR，不用认字。"""
    chart.set_data(_curve(), (), BLACK)
    axis = chart.chart().axes(Qt.Vertical)[0]
    assert (axis.min(), axis.max()) == (0.0, 100.0)
    assert axis.labelFormat() == "%.0f", f"胜率轴还带着小数位：{axis.labelFormat()}"


# ---------------------------------------------------------------- 自绘的两样

def test_current_move_marker_is_a_vertical_line_with_a_label(chart, qapp):
    """当前手那条竖线：QtCharts 没有 markLine，这里自己画，所以得验它真的在画。"""
    chart.set_data(_curve(), (), BLACK)
    base = H.grab_image(chart)
    chart.set_current(20)
    qapp.processEvents()
    assert _diff_pixels(base, H.grab_image(chart)) > 40, "set_current 之后画面一个像素都没变"
    chart.set_current(None)
    qapp.processEvents()
    assert _diff_pixels(H.grab_image(chart), base) <= 40, "收起竖线后没回到原样"


def test_the_danger_band_sits_at_the_bottom_two_tenths(chart, qapp):
    """0~20% 那条带：位置要按纵轴比例算对，而且**真的画在像素上**。

    不用 `QAreaSeries` 是因为它会踩坏内存（`winrate.py` 模块注释里记了实测），
    自己画就得自己验：先验几何，再拿开关前后的像素差验「确实有东西在画」——
    只验几何的话，`drawForeground` 里少写一行 fillRect 也照样过。
    """
    chart.set_data(_curve(), (), BLACK)
    qapp.processEvents()
    pa = chart.chart().plotArea()
    band = chart.danger_rect()
    assert band is not None
    assert band.left() == pytest.approx(pa.left()) and band.right() == pytest.approx(pa.right())
    assert band.bottom() == pytest.approx(pa.bottom())
    # 纵轴 0~100，20% 就在自底向上两成高处
    assert band.top() == pytest.approx(pa.bottom() - pa.height() * 0.2)
    with_band = H.grab_image(chart)
    chart.danger_hi = 0.0
    chart.viewport().update()
    qapp.processEvents()
    assert chart.danger_rect() is None
    # 这条带与网页版对齐，只有 5% 不透明度，本身就是很淡的一个色变，
    # 所以「算不算不同」的门槛要从默认 24 降到 8
    # （而不是把颜色改得更重来讨好测试）。
    assert _diff_pixels(with_band, H.grab_image(chart), 8) > 40, \
        "那条带在图上根本看不出来"


def test_rebuilding_the_chart_a_dozen_times_survives(chart, qapp):
    """重画不能崩：这个控件在页面上会被反复喂数据，而 QtCharts 的崩溃是
    **整个进程没掉**（无 Python 栈），当场只表现为「测试莫名红了」。
    劣势带改成自绘之前，先红的就是这一类（实测 rc 3221225477）。
    """
    for i in range(12):
        chart.set_data(_curve(10 + i * 3), [{"ply": 2 + i, "kind": "blunder"}],
                       BLACK if i % 2 else WHITE)
        chart.set_current(3 + i)
        qapp.processEvents()
        assert chart.plotted_points() == 10 + i * 3
    assert chart.current == 14


# ---------------------------------------------------------------- 自绘的面积

def test_a_higher_winrate_sits_higher_on_the_screen(chart):
    """方向断言：胜率高的那一手必须画在**更靠上**的位置。

    这条看起来是废话，但它抓的是本项目已经犯过一次的那类错：纵向映射写反
    （`bottom - (100-v)/100*h`）时 x 全对、点击跳手全对、竖虚线全对，
    只有面积多边形整块跑到曲线上面去 —— 是读 `p4_01` 那张图才发现的。
    """
    chart.set_data(_curve(), (), BLACK)          # 黑视角：30% → 78% 一路上涨
    pa = chart.chart().plotArea()
    assert chart.scene_point_for(0).y() > chart.scene_point_for(40).y()
    # 30% 就在自底向上三成的位置，不是七成
    assert chart.scene_point_for(0).y() == pytest.approx(pa.bottom() - pa.height() * 0.30, abs=1)
    assert chart.scene_point_for(40).y() == pytest.approx(pa.bottom() - pa.height() * 0.78, abs=1)


def test_the_area_under_the_curve_is_painted(chart, qapp):
    """曲线下那层蓝必须真在图上，而且**形状要贴着曲线**：自己画的东西就得自己验。

    起因是读 `p4_01` 那张图：注释里写了「曲线下方那层渐淡的蓝」，图上却根本没有
    —— `QLineSeries.setBrush()` 不报错、不提示，就是不出面积。
    两个判据：同一条竖线上「曲线下方比上方偏蓝」（有没画），以及
    「左边曲线低，它上方那一大片必须是白的」（画得对不对形状 ——
    纵向映射写反时，面积整块镜像到曲线上面，第一条照样过）。
    采 ply 5 不采 ply 20：第 20 手正是胜率线与目差线交叉的地方，上方那一点
    会采到目差线的绿（绿也是 blue>red），判据就假了。
    """
    chart.set_data(_curve(), (), BLACK)
    qapp.processEvents()
    img = H.grab_image(chart)
    dpr = chart.devicePixelRatio()

    def bias(vp, dy):
        """视口坐标那一行上三个相邻像素的「偏蓝程度」均值（单像素会被
        antialias 与网格线抽到，拿均值就不靠运气）。"""
        y = int((vp.y() + dy) * dpr)
        vals = [img.pixelColor(int(vp.x() * dpr) + k, y) for k in (-3, 0, 3)]
        return sum(c.blue() - c.red() for c in vals) / len(vals)

    p5 = chart.viewport_point_for(5)
    # 实测：曲线下方 6px 是 27~29，上方 12px 是 0（纯白）
    assert bias(p5, 6) > bias(p5, -12) + 8, (
        f"曲线下没有面积填充：下方偏蓝 {bias(p5, 6)}，上方偏蓝 {bias(p5, -12)}")
    # 形状：第 0 手只有 30%，而第 40 手涨到 78% —— 左边那条曲线上方不该有蓝
    p0 = chart.viewport_point_for(0)
    p40 = chart.viewport_point_for(40)
    assert p0.y() > p40.y()
    assert bias(p0, p40.y() - p0.y() - 8) < 8, (
        f"曲线上方那块被当成面积涂了：{bias(p0, p40.y() - p0.y() - 8)}")
    # 而右边曲线下方仍然是蓝（第 40 手以下）
    assert bias(p40, 6) > 8, f"第 40 手曲线下方应该是蓝的：{bias(p40, 6)}"


# ---------------------------------------------------------------- 关键帧


def test_the_curve_is_legible_as_a_picture(chart, qapp):
    """一张要给人看的图，最后还是要看。落 artifacts/，我逐张读。

    除了看图，这里先把「该画的五种颜色到底画没画」量成数字：
    `blank_ratio` 对这张图没有意义（图表自己刷了一层白底，怎么量都是满的）。
    """
    chart.set_data(_curve(), [{"ply": 9, "kind": "slow"}, {"ply": 22, "kind": "bad"},
                              {"ply": 33, "kind": "blunder"}], WHITE)
    chart.set_current(22)
    shot = H.snap(chart, "p4_01_winrate_curve")
    assert shot.exists()
    img = H.grab_image(chart)

    def hits_around(target, cx: int, cy: int, r: int) -> int:
        """数一下 (`cx`, `cy`) 周围边长 `2r+1` 的方框里有几个像素是这个色。"""
        return sum(1 for y in range(cy - r, cy + r + 1)
                   for x in range(cx - r, cx + r + 1)
                   if 0 <= x < img.width() and 0 <= y < img.height()
                   and H.color_distance(img.pixelColor(x, y), target) < 60)

    dpr = chart.devicePixelRatio()
    # 两条线：线很长，整幅扫、步长 2 就够（命中数上百，不至于差几个抗锯齿像素定生死）
    for name, hexcolor in (("胜率线", winrate.WR_COLOR), ("目差线", winrate.SCORE_COLOR)):
        target = QColor(hexcolor)
        hits = sum(1 for y in range(0, img.height(), 2)
                   for x in range(0, img.width(), 2)
                   if H.color_distance(img.pixelColor(x, y), target) < 60)
        assert hits >= 8, f"{name}（{hexcolor}）在图上找不到：只 {hits} 个样点"
    # 散点：一枚只有 ~7px 宽。拿整幅扫 + 步长 2 当判据时，琥珀色正好卡在 6 个样点
    # —— 过与不过取决于最后一圈抗锯齿，`>= 8` 就是抛硬币（本项目跑在 offscreen
    # 平台上，DPR 实为 1.0：所有截图都是逻辑尺寸，没有 1.5 那一层放大）。
    # 改成围着它**该在的位置**逐像素数：既量出“这个色真画了”，顺手量出“它就在那一手”。
    for ply, kind in ((9, "slow"), (22, "bad"), (33, "blunder")):
        target = QColor(theme.MARK_COLOR[kind])
        vp = chart.viewport_point_for(ply)
        hits = hits_around(target, int(vp.x() * dpr), int(vp.y() * dpr), 10)
        assert hits >= 12, \
            f"{kind}（{theme.MARK_COLOR[kind]}）那枚散点在 ply {ply} 处只画了 {hits} 个像素"
    # 图例只有 胜率/目差 两项：三条散点都不得挤进来（劣势带根本不在 series 里）
    labels = [m.label() for m in chart.chart().legend().markers() if m.isVisible()]
    assert sorted(labels) == ["目差", "胜率"], labels
    assert not H.clipped_texts(chart)[0]


# ---------------------------------------------------------------- 紧凑档

@pytest.fixture
def sidebar_chart(qapp):
    """侧栏里那张的真实几何：423x150（对局页侧栏量出来的，不是拍的）。"""
    w = WinrateChart(height=150)
    w.resize(423, 150)
    w.show()
    qapp.processEvents()
    yield w
    w.close()
    w.deleteLater()


def test_a_short_chart_gives_its_height_back_to_the_curve(sidebar_chart):
    """150px 高的那张必须把绘图区让出来 —— 不是“好看一点”，是“能不能读”。

    换上真字体重读对局页中盘帧（`page_07`）才看见：侧栏那张图里 x 轴 11 个
    刻度全被省略号化成「...」、右轴（目差）也是三个点、左轴标题裁成「胜率…」，
    而曲线本体只剩 42px 高 —— 胜率从 50% 到 60% 在手上只有几个像素。
    成因是 QtCharts 与 ECharts 的一个差别：网页版 `grid: {top: 26, bottom: 34}`
    把轴名字画在边距里（150px 高时绘图区有 90px），而 QtCharts 是轴标题 /
    图例 / 标签各占一整带，且是**加在**绘图区外面的。

    这条量的是“图还有没有地方画”：绘图区高度 >= 80（实测 98）。
    拿像素而不是拿“图例没显”当判据：前面那些都是手段，只有它是目的。
    """
    sidebar_chart.set_data(_curve(10), (), BLACK)
    pa = sidebar_chart.chart().plotArea()
    assert sidebar_chart.compact, "150px 高该走紧凑档"
    assert pa.height() >= 80, f"绘图区只有 {pa.height():.0f}px，曲线会被压成一条直线"
    assert pa.width() >= 300, f"绘图区宽 {pa.width():.0f}px（轴占了太多）"
    # 轴标题与图例让位了（颜色对应关系由页面那行说明接手，见 test_pages 里那条）
    assert sidebar_chart.chart().legend().isVisible() is False
    for axis in (sidebar_chart._axis_x, sidebar_chart._axis_y, sidebar_chart._axis_score):
        assert axis.titleText() == "", axis.titleText()


def test_a_tall_chart_keeps_its_titles_and_legend(chart):
    """复盘页那张（620x220，与网页版同值）不走紧凑档：轴标题与图例都得在。

    与上一条成对：只钉“矮的时候收”会漏掉“高的时候不能一并收” —— 后者
    才是这张图信息量更足那一档，阈值写反两边都看不出来。
    """
    chart.set_data(_curve(), (), BLACK)
    assert chart.compact is False
    assert chart.chart().legend().isVisible() is True
    assert chart._axis_x.titleText() == winrate.AXIS_TITLES["x"]
    assert chart._axis_score.titleText() == winrate.AXIS_TITLES["score"]


def test_no_axis_label_gets_squeezed_into_an_ellipsis(sidebar_chart):
    """每个标签得占得下一行字，不然 QtCharts 会把它省略号化成「...」（不报错）。

    为什么不止靠上一张图看：“轴标签变成三个点”在 1080px 的整页截图里十次有
    九次会被当成“字太小看不清”，而它其实是布局量不够。这里直接算每个标签
    能分到的尺寸：一行字在 8pt 下要 ~14px 高、一个数字要 ~16px 宽。
    """
    sidebar_chart.set_data(_curve(41), (), BLACK)      # 41 手：不密就撞车的那一档
    pa = sidebar_chart.chart().plotArea()
    x_ticks = sidebar_chart._axis_x.tickCount()
    y_ticks = sidebar_chart._axis_y.tickCount()
    s_ticks = sidebar_chart._axis_score.tickCount()
    assert pa.width() / max(1, x_ticks - 1) >= 16, \
        f"x 轴 {x_ticks} 个刻度分 {pa.width():.0f}px，每个标签只 {pa.width() / (x_ticks - 1):.0f}px"
    for tag, ticks in (("胜率轴", y_ticks), ("目差轴", s_ticks)):
        assert pa.height() / max(1, ticks - 1) >= 14, \
            (f"{tag} {ticks} 个刻度分 {pa.height():.0f}px：每个标签只 "
             f"{pa.height() / (ticks - 1):.0f}px，不够一行字，会被化成「...」")
    assert x_ticks <= winrate.COMPACT_MAX_X_TICKS + 1, x_ticks
    # 几何算看过了，再拿像素确认一次「真的写出了字」：紧凑档左右两列各三个刻度，
    # 全被省略号化之后整列只剩个位数墨像素（实测见 `harness.chart_axis_ink`）
    for side in ("right", "left"):
        ink = H.chart_axis_ink(sidebar_chart, side)
        assert ink >= 20, f"{side} 轴标签带只 {ink} 个墨像素：那一列没写出字"


@pytest.fixture
def review_chart(qapp):
    """复盘页那张的真实几何：590x200（从页面上量的，不是 `height=200` 那个数）。"""
    w = WinrateChart(height=200)
    w.resize(590, 200)
    w.show()
    qapp.processEvents()
    yield w
    w.close()
    w.deleteLater()


def test_a_wide_score_range_still_prints_numbers(review_chart, qapp):
    """目差拉到十几目（真报告就这样）时，右轴那一列还得是数字而不是「···」。

    旧版在这里中过：`nice_score_range` 按外扩**之前**的跨度选档，外扩完多出的
    两格没人重数 —— -14~-4.4 那一组拿到 8 个刻度，8 行字挤在 82px 高的标签带里，
    QtCharts 不报错不警告，直接把整列省略号化（实测那一列只剩 9 个墨像素）。

    为什么数墨而不是算几何：量出来「右带 60px、最宽标签 26px」是装得下的，
    画出来却仍然可能是一列点 —— 只有像素能证明它真的被写出来了。
    后半段再钉一个「该留小数时仍留小数」：修得太狠（一律取整档）会把
    本来装得下的 ±10 目那一组也抹成粗刻度，那是另一个信息损失。
    """
    scores = [-14.0 + 0.8 * i for i in range(13)]        # -14 ~ -4.4
    review_chart.set_data([{"ply": i, "winrateBlack": 0.5, "winrateWhite": 0.5,
                            "scoreLead": scores[i]} for i in range(13)], (), BLACK)
    qapp.processEvents()
    ax = review_chart._axis_score
    assert ax.tickCount() <= winrate.MAX_Y_TICKS, \
        f"{ax.tickCount()} 个刻度挤不进 82px 高的标签带，会被化成「···」"
    ink = H.chart_axis_ink(review_chart)
    pa = review_chart.chart().plotArea()
    assert ink >= 40, (
        f"目差那一列只 {ink} 个墨像素（真报告上画全是 58，全成「···」是 9）："
        f"轴 {ax.min()}~{ax.max()} ticks={ax.tickCount()} fmt={ax.labelFormat()} "
        f"控件 {review_chart.width()}x{review_chart.height()} 绘图区 "
        f"{pa.width():.0f}x{pa.height():.0f}")
    assert not H.clipped_texts(review_chart)[0]

    # 装得下的那一组：仍然走 2.5 这一档、仍然带一位小数（不被修粗的循环误伤）
    scores2 = [-8.0, 0.5, -3.0, 0.0]
    review_chart.set_data([{"ply": i, "winrateBlack": 0.5, "winrateWhite": 0.5,
                            "scoreLead": scores2[i % len(scores2)]} for i in range(13)],
                          (), BLACK)
    qapp.processEvents()
    assert (ax.min(), ax.max()) == (-10.0, 2.5), (ax.min(), ax.max())
    assert ax.labelFormat() == "%.1f", ax.labelFormat()
    assert H.chart_axis_ink(review_chart) >= 40
