"""棋盘控件的几何与像素验收。

每一条测试都对应一个**真实存在过**的问题，不是凑数：
  · y 轴方向：本项目历史上踩过「引擎行序与屏幕行序」的坑，画反了整盘上下翻转；
  · 线宽糊：spike 实测 DPR=1.5 时 1px 逻辑线宽会变成深浅不一的 1~2 设备像素；
  · 星位双圈：spike 截图里星位点与棋子描边叠成了两个圈；
  · 坐标被遮：spike 截图里底部坐标 N~T 被浮层压住。
"""
from __future__ import annotations

import math

from PySide6.QtGui import QColor

from tests import harness as H
from ui import theme
from ui.widgets import board as board_mod


def _in_cell(b, x: int, y: int) -> tuple[float, float]:
    """取交叉点所在格子的**象限中心**（右下偏 cell/4），不是交叉点正中。

    这是本文件所有像素取样的唯一入口。原因很实在：交叉点正中被网格线穿过，
    星位上还有一个点，有子/有提示时正中又是白色序号 —— 在正中取样，
    测到的是「我取样的位置恰好有别的笔画」，不是「这块画得对不对」（首跑就三连红在这里）。
    象限中心离四条边线都是 cell/4，又仍在子/提示/热力图色块的覆盖范围内。
    """
    c = b.center(x, y)
    cell = b.layout_now()[0]
    return c.x() + cell / 4.0, c.y() - cell / 4.0


def _q(b, x: int, y: int):
    """按格内象限中心取样一个点的颜色。"""
    lx, ly = _in_cell(b, x, y)
    return H.sample(b, lx, ly)


# ---------------------------------------------------------------- 几何

def test_cell_and_pad_follow_web_spec(board_widget):
    """cell = max(12, floor(side/(size+1)))、pad = cell —— 与 GoBoard.tsx 同式。"""
    for size, side, expect_cell in ((19, 640, 32), (13, 560, 40), (9, 400, 40)):
        b = board_widget(size=size)
        b.resize(side, side)
        cell, ox, oy, total = b.layout_now()
        assert cell == expect_cell, f"size={size} side={side} 得到 cell={cell}"
        assert total == expect_cell * (size - 1) + expect_cell * 2
        # 第一列/第一行的交叉点离边缘正好一个 cell（pad），标签就住在那条带里
        assert abs(ox - expect_cell) < 1e-6
        assert abs(oy - expect_cell) < 1e-6


def test_y_axis_points_up(board_widget):
    """y=0 在**底边**：center(0,0) 的像素 y 必须大于 center(0,size-1)。"""
    b = board_widget(size=19)
    b.resize(640, 640)
    bottom = b.center(0, 0)
    top = b.center(0, 18)
    assert bottom.y() > top.y()
    left = b.center(0, 0)
    right = b.center(18, 0)
    assert right.x() > left.x()


def test_the_wood_plate_stays_square_when_the_widget_is_not(board_widget):
    """控件被布局压成横长条时，木盘必须仍是**正方形**（复盘页左列 592x320 抓到）。

    老写法是「木纹铺满控件、网格取短边居中」，控件一扁就成了一条横木中间画着
    个小棋盘，看着像棋盘被拉扁。除了量几何，还得取像素：左边留白处要是木色，
    就说明「铺满控件」只是从注释里消失了、画面上还在。
    """
    b = board_widget(size=9)
    b.resize(592, 320)
    plate = b.board_rect()
    assert abs(plate.width() - plate.height()) < 1e-6, plate
    assert plate.width() <= 320 + 1e-6, "盘比短边还长，会被控件裁掉"
    assert plate.x() >= 0 and plate.y() >= 0
    assert plate.x() + plate.width() <= b.width() + 1e-6
    assert plate.x() + plate.width() / 2.0 == b.width() / 2.0, "没居中"

    edge = H.sample(b, 4, b.height() / 2.0)      # 控件最左边、盘的留白带
    assert not H.is_wood(edge), f"留白带还是木纹，等于没改：{edge.name()}"
    assert H.color_distance(edge, QColor(theme.PANEL)) < 12, edge.name()
    assert H.is_wood(_q(b, 4, 4)), "盘面本身不该跟着变白"


def test_point_at_roundtrip_on_every_intersection(board_widget):
    """每个交叉点的中心都要能反查回自己 —— 落子偏移一格的 bug 会在这里现形。"""
    b = board_widget(size=19)
    b.resize(640, 640)
    bad = []
    for y in range(19):
        for x in range(19):
            c = b.center(x, y)
            if b.point_at(c.x(), c.y()) != (x, y):
                bad.append((x, y, b.point_at(c.x(), c.y())))
    assert not bad, f"这些交叉点反查错了：{bad[:6]}"


def test_point_at_rejects_outside(board_widget):
    b = board_widget(size=19)
    b.resize(640, 640)
    assert b.point_at(2, 2) is None                 # 左上角外侧的空白
    assert b.point_at(10_000, 10_000) is None


# ---------------------------------------------------------------- 像素

def test_stone_colors_and_empty_point(board_widget):
    """黑子体暗、白子体亮、空点是木色，而且子体必须是**中性灰**。"""
    b = board_widget(size=19)
    b.resize(640, 640)
    grid = [[0] * 19 for _ in range(19)]
    grid[3][3] = 1            # board[y][x]：黑子在 (3,3)
    grid[15][15] = 2          # 白子在 (15,15)
    b.set_board(grid)

    black, white, empty = _q(b, 3, 3), _q(b, 15, 15), _q(b, 9, 3)
    assert H.luma(black) < 90, f"黑子取样点太亮：{black.name()}"
    assert H.luma(white) > 190, f"白子取样点太暗：{white.name()}"
    assert H.is_wood(empty), f"空点不是木色：{empty.name()}"
    # 这一条才是「子根本没画出来」的照妖镜：只有阴影时，那里是「木色压暗」= 暖色，
    # 真画了子则是中性灰。首跑就是在这里发现 QRadialGradient 参数顺序写反、整块子全透明。
    assert max(black.red(), black.green(), black.blue()) - min(
        black.red(), black.green(), black.blue()) < 26, f"黑子体不是中性灰：{black.name()}"
    assert max(white.red(), white.green(), white.blue()) - min(
        white.red(), white.green(), white.blue()) < 26, f"白子体不是中性灰：{white.name()}"


def test_grid_line_is_exactly_one_device_pixel(board_widget):
    """缺陷③回归：横线在 DPR=1.5 下必须只占 1 个设备像素，不能糊成 2~3 行。"""
    b = board_widget(size=19)
    b.resize(640, 640)
    img = H.grab_image(b)
    dpr = float(b.devicePixelRatioF())
    c = b.center(9, 9)
    x_dev = int(round((c.x() + b.layout_now()[0] * 0.5) * dpr))   # 取两列之间的线段中点
    y_dev = int(round(c.y() * dpr))
    dark_rows = []
    for dy in range(-4, 5):
        col = img.pixelColor(x_dev, min(img.height() - 1, max(0, y_dev + dy)))
        if H.luma(col) < 140:
            dark_rows.append(dy)
    assert len(dark_rows) <= 2, f"横线糊成了 {len(dark_rows)} 行设备像素：{dark_rows}"
    assert dark_rows, "这里本该有一条网格线，却什么都没画"


def test_star_point_under_a_stone_leaves_no_ring(board_widget):
    """缺陷④回归：星位上有子时，子外不该出现描边/双圈（网页版的子没有描边）。

    先断言子真的画上了：不然这条会因为在「透明子」上取不到东西而假绿。
    """
    b = board_widget(size=19)
    b.resize(640, 640)
    grid = [[0] * 19 for _ in range(19)]
    grid[9][3] = 1                     # (3,9) 是 19 路的星位，压一颗黑子
    b.set_board(grid)
    assert H.luma(_q(b, 3, 9)) < 90, "子没画出来，后面的「无双圈」断言没有意义"
    c = b.center(3, 9)
    cell = b.layout_now()[0]
    r = cell * 0.47
    # 沿 45° 方向取子外一点：那里没有网格线经过，若出现非木色就是多画了圈
    dx = dy = r * 1.06 / math.sqrt(2)
    col = H.sample(b, c.x() + dx, c.y() - dy)
    assert H.is_wood(col), f"星位/子外圈被画了东西：{col.name()}"


def _label_hits(img, dpr, x0, x1, y0, y1) -> int:
    """在一条**逻辑坐标**描述的四边形带里数标签色的像素。

    带的边界刻意避开网格线：网格色 #5a4226 与标签色 #6b4f2a 只差 34，
    把线扫进带来就会得到假的命中数。
    """
    label = QColor(theme.LABEL)
    hits = 0
    for x in range(max(0, int(x0 * dpr)), min(img.width(), int(x1 * dpr))):
        for y in range(max(0, int(y0 * dpr)), min(img.height(), int(y1 * dpr))):
            if H.color_distance(img.pixelColor(x, y), label) < 90:
                hits += 1
    return hits


def test_all_four_label_bands_are_drawn(board_widget):
    """缺陷①②回归：四条边的坐标都要完整落在控件内并真的画出字。

    左边那条是后补的：只查下边时左侧行号被裁了一半而测试全绿 ——
    断言没盖到的地方等于没有断言（那个裁切是看截图才发现的）。
    """
    b = board_widget(size=19)
    b.resize(640, 640)
    cell, ox, oy, total = b.layout_now()
    right = ox + 18 * cell
    bottom = oy + 18 * cell
    fs, top_c, bottom_c, left_c, right_c = b.label_band(cell, ox, oy)
    reach = fs / 2 + 1                            # 半个字高（字宽同量级）
    for name, edge in (("上", top_c), ("下", bottom_c), ("左", left_c), ("右", right_c)):
        assert edge + reach <= 640 and edge - reach >= 0, \
            f"{name}标签带中心 {edge:.1f} 连同半个字超出控件"

    img = H.grab_image(b)
    dpr = float(b.devicePixelRatioF())
    span_lo, span_hi = ox - cell * 0.5, right + cell * 0.5
    rows_lo, rows_hi = oy - cell * 0.5, bottom + cell * 0.5
    bands = {
        "上": _label_hits(img, dpr, span_lo, span_hi, 0, oy - 2),
        "下": _label_hits(img, dpr, span_lo, span_hi, bottom + 2, 640),
        "左": _label_hits(img, dpr, 0, ox - 2, rows_lo, rows_hi),
        "右": _label_hits(img, dpr, right + 2, 640, rows_lo, rows_hi),
    }
    for name, hits in bands.items():
        assert hits > 150, f"{name}边坐标几乎没画出来（命中 {hits} 个像素）：{bands}"


def _label_pad_hits(b, size):
    """四条 pad 带（网格线到控件边）里的标签色像素数。

    **先 grab 再量几何**：`grab_image()` 会 `show()`，而 show 可能改控件尺寸
    （本机 offscreen 下顶层控件请求 1000 高、实际只给 940）。先量后抓会拿到两套
    坐标，三条带直接数到 0 —— 首跑就是这么红的，不是标签没画。

    靠网格线那一侧留 2px：网格色 #5a4226 与标签色 #6b4f2a 只差 34，
    把线扫进带来就会得到假的命中数（ `_label_hits` 的注释里也记着这一条）。
    """
    img = H.grab_image(b)
    cell, ox, oy, _total = b.layout_now()
    dpr = float(b.devicePixelRatioF())
    bottom = oy + (size - 1) * cell
    right = ox + (size - 1) * cell
    return {
        "上": _label_hits(img, dpr, ox, right, oy - cell, oy - 2),
        "下": _label_hits(img, dpr, ox, right, bottom + 2, bottom + cell),
        "左": _label_hits(img, dpr, ox - cell, ox - 2, oy, bottom),
        "右": _label_hits(img, dpr, right + 2, right + cell, oy, bottom),
    }


def test_label_bands_clear_edge_stones(board_widget):
    """几何：标签带中心到网格线的距离，扣掉半个字高后仍要在子的外沿之外。

    看终局截图才发现的问题：带中心取 `pad*0.45` 时，9 路那种 cell≈100px 的盘上
    首尾两行坐标字母被子盖掉一半（19 路 cell 只 32px，重叠只有几像素，看不出来）。
    """
    for size, side in ((19, 640), (13, 560), (9, 1000)):
        b = board_widget(size=size)
        b.resize(side, side)
        cell, ox, oy, _total = b.layout_now()
        fs, top, bottom, left, right = b.label_band(cell, ox, oy)
        reach = fs / 2.0
        grid_bottom = oy + (size - 1) * cell
        grid_right = ox + (size - 1) * cell
        for name, gap in (("上", oy - top), ("下", bottom - grid_bottom),
                          ("左", ox - left), ("右", right - grid_right)):
            assert gap - reach >= cell * board_mod.STONE_R, \
                (f"{size}路 {name}边标签带会被子压住：带中心距网格线 {gap:.1f}px、"
                 f"半个字高 {reach:.1f}px，而子的半径有 {cell * board_mod.STONE_R:.1f}px")
            assert gap + reach <= cell, f"{size}路 {name}边标签跑到控件外了"


def test_edge_stones_do_not_erase_the_labels(board_widget):
    """像素对照组：靠边两行两列填满子后，pad 带里的标签像素不能少。

    这条**不引用任何常数**，专防上面那条几何断言与实现一起算错（同一个错就会一起绿）：
    标签先画、子后画，“被盖住”会直接反映成掉像素。

    第一版把计数区间卡在子的外沿之外，于是变异（带中心改回 0.45）照样逃过这条
    —— 被盖住的那一段本来就被区间排除在外。要测“标签还在不在”，就得数整条带。
    """
    b = board_widget(size=9)
    b.resize(1000, 1000)
    empty = _label_pad_hits(b, 9)
    b.set_props(board=[[board_mod.WHITE if y in (0, 8) or x in (0, 8) else board_mod.EMPTY
                        for x in range(9)] for y in range(9)])
    kept = _label_pad_hits(b, 9)
    for name, hits in empty.items():
        assert hits > 200, f"空盘时 {name}边本来就没画出标签，两条计数都是假的：{empty}"
        assert kept[name] >= hits * 0.85, \
            (f"边行填满子后 {name}边标签像素从 {hits} 掉到 {kept[name]}："
             f"标签有一截落在边行子的半径里，被子盖掉了")


def test_ownership_heatmap_tints_both_sides(board_widget):
    """热力图：正数压暗（黑方），负数提亮（白方），阈值 0.08 以下不画。"""
    b = board_widget(size=19)
    b.resize(640, 640)
    own = [0.0] * 361
    own[5 * 19 + 5] = 0.9         # (5,5) 黑方
    own[13 * 19 + 13] = -0.9      # (13,13) 白方
    own[2 * 19 + 2] = 0.05        # 低于阈值，不该画
    b.set_props(ownership=own, show_ownership=True)
    # 参考点一律取在被测点的**紧邻一格**：底色是左上→右下的整体渐变，
    # 隔得远的两个「空点」本身就差着十几个色阶，拿远处的点当参考会误判。
    assert H.luma(_q(b, 5, 5)) < H.luma(_q(b, 5, 6)) - 20, "黑方领地没有被压暗"
    white_cell, white_ref = _q(b, 13, 13), _q(b, 13, 14)
    assert H.color_distance(white_cell, white_ref) > 12, "白方领地没有提亮"
    assert H.luma(white_cell) > H.luma(white_ref), "白方领地的变化方向反了"
    assert H.color_distance(_q(b, 2, 2), _q(b, 2, 3)) < 10, "低于阈值的格子被画了"


def test_marks_and_hints_are_drawn(board_widget):
    """题目标记（冷色）与候选点提示（蓝/灰）都要出现，且颜色不同。"""
    b = board_widget(size=19)
    b.resize(640, 640)
    b.set_props(marks=[{"x": 4, "y": 4, "kind": "target", "label": "△"}],
                hints=[{"x": 10, "y": 10, "visits": 100}])
    img = H.grab_image(b)
    dpr = float(b.devicePixelRatioF())
    target = QColor("#1c7ed6")
    c = b.center(4, 4)
    r = b.layout_now()[0] * 0.47 * 1.05
    near_ring = 0
    for deg in range(0, 360, 10):
        x = int(round((c.x() + r * math.cos(math.radians(deg))) * dpr))
        y = int(round((c.y() + r * math.sin(math.radians(deg))) * dpr))
        if H.color_distance(img.pixelColor(x, y), target) < 90:
            near_ring += 1
    assert near_ring >= 20, f"目标子标记的圈几乎没画出来（{near_ring} 个点命中）"
    # 提示圈正中画着白色序号，所以往右取到圈内、字外的地方
    pt = b.center(10, 10)
    r = b.layout_now()[0] * 0.47
    hint = H.sample(b, pt.x() + r * 0.75, pt.y())
    assert not H.is_wood(hint), f"候选点提示没画：{hint.name()}"
    assert hint.blue() > hint.red(), f"提示不是冷色，画歪了？：{hint.name()}"


# ---------------------------------------------------------------- 契约与稳健性

def test_set_props_rejects_unknown_key(board_widget):
    """拼错属性名必须当场报错，不能静默不生效（网页版 props 拼错是白板的经典成因）。"""
    b = board_widget(size=19)
    import pytest
    with pytest.raises(AttributeError):
        b.set_props(showOnwership=True)


def test_short_board_payload_does_not_crash(board_widget):
    """后端给了残缺 board（行数不够）时补空行，而不是 IndexError。"""
    b = board_widget(size=19)
    b.set_board([[0] * 19])
    assert b.stone_at(0, 18) == 0


def test_screenshots_for_human_review(board_widget):
    """三张关键帧：空盘 / 中盘（含提示与热力图）/ 终局（死子 + 手数）。"""
    b = board_widget(size=19)
    b.resize(640, 640)
    p1 = H.snap(b, "board_01_empty")

    grid = [[0] * 19 for _ in range(19)]
    for x, y, c in [(3, 3, 1), (15, 3, 2), (3, 15, 1), (15, 15, 2), (9, 9, 1),
                    (9, 10, 2), (10, 9, 1), (4, 10, 2), (14, 4, 1), (10, 14, 2)]:
        grid[y][x] = c
    own = [0.0] * 361
    for y in range(19):
        for x in range(19):
            own[y * 19 + x] = 0.6 if x < 4 else (-0.5 if x > 14 else 0.0)
    b.set_props(board=grid, last_move=(10, 14),
                ownership=own, show_ownership=True,
                hints=[{"x": 7, "y": 7}, {"x": 8, "y": 6}, {"x": 6, "y": 5}])
    p2 = H.snap(b, "board_02_midgame")

    b.set_props(show_ownership=False, ownership=None, hints=[],
                dead=[(4, 10), (14, 4)],
                moves=[{"x": 3, "y": 3, "color": 1}, {"x": 15, "y": 3, "color": 2},
                       {"x": 3, "y": 15, "color": 1}],
                show_move_numbers=True, dim=True)
    p3 = H.snap(b, "board_03_scoring")

    for p in (p1, p2, p3):
        assert p.exists() and p.stat().st_size > 3000
    assert H.blank_ratio(b) > 0.5, "整块棋盘几乎是空的，截图没有验收价值"
