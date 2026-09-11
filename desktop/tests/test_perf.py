"""性能与动效的验收（第 32 轮）。

**为什么把性能写成测试**：这一轮的全部改动都是「让每帧更便宜」，而性能恰恰是会
悄悄退回去的东西 —— 下一个人往 `paintEvent` 里加一句「现建一个 QRadialGradient」
或「现算 76 次坐标文字」，当场谁都不会发现，直到用户再说一次「卡」。
阈值取得比实测宽（实测整盘 2.0 ms，这里卡 8 ms）：它要抓的是**退回老写法**（22 ms），
不是测试机今天慢了半拍。

**动效为什么默认关**：见 `conftest.py` 里 `YIDAO_ANIM=0` 那段 —— 像素断言要终态。
要验动效本身的用例自己 `motion.set_enabled(True)`（`anim` 夹具，退出时还原）。
"""
from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QPixmap
from PySide6.QtWidgets import QApplication

from tests import harness as H
from ui.widgets import motion
from ui.widgets.board import GoBoard


# ---------------------------------------------------------------- 夹具与工具

@pytest.fixture
def anim():
    """打开动效跑这一条，退出时还原（还原成「看环境变量」，也就是测试里的关）。"""
    motion.set_enabled(True)
    yield
    motion.set_enabled(None)


class CountingBoard(GoBoard):
    """把 `update()` 记下来的棋盘：重绘次数的验收要数得出来，不能靠肉眼。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.updates: list[tuple] = []

    def update(self, *a):                                     # noqa: N802
        self.updates.append(a)
        super().update(*a)


def midgame(size: int = 19, n: int = 180) -> list[list[int]]:
    """一个铺满前 n 个交叉点的局面（与探针同一个造法，量的是同一件事）。"""
    board = [[0] * size for _ in range(size)]
    put = 0
    for y in range(size):
        for x in range(size):
            if put >= n:
                return board
            board[y][x] = 1 if put % 2 == 0 else 2
            put += 1
    return board


def render_stats(board: GoBoard, times: int = 30) -> tuple[float, float]:
    """`board` 整盘重绘的 `(平均, 最快)` 毫秒数（走真的 paintEvent）。

    为什么要「最快」这一档：全套用例跑下来时机器正忙着（实测同一段代码的**平均**
    能从 2.1 ms 涨到 8.6 ms），而**最快**那一帧始终是"这段代码本来要多久"。
    验收断最快那一档，才不会把"测试机当时很忙"判成"代码退回去了"。
    """
    pm = QPixmap(board.size())
    board.render(pm)                       # 预热：首帧要建字体/精灵缓存
    samples = []
    for _ in range(times):
        t0 = time.perf_counter()
        board.render(pm)
        samples.append((time.perf_counter() - t0) * 1000.0)
    return sum(samples) / len(samples), min(samples)


def render_ms(board: GoBoard, times: int = 30) -> float:
    """平均毫秒数（保留给只关心量级的调用点）。"""
    return render_stats(board, times)[0]


def move_to(board: GoBoard, x: int, y: int) -> None:
    """在 `(x, y)` 那一格上造一次鼠标移动（走真的 `mouseMoveEvent`）。"""
    c = board.center(x, y)
    ev = QMouseEvent(QEvent.Type.MouseMove, QPointF(c.x(), c.y()),
                     QPointF(c.x(), c.y()), Qt.MouseButton.NoButton,
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    board.mouseMoveEvent(ev)


class _Api:
    """只用来喂页面构造器。这几条用例都不发请求，调到了就是用例写错了。"""

    def get(self, path, query=None, timeout=20.0):
        raise AssertionError(f"本用例不该发请求：{path}")

    post = get
    patch = get
    delete = get


# ---------------------------------------------------------------- 重绘预算

def test_full_board_repaint_stays_within_budget(qapp, board_widget):
    """19 路满盘重绘要明显低于一帧的预算（老写法实测 22.25 ms）。

    断「最快那一帧」而不是平均值：见 `render_stats` —— 全套用例跑下来机器是热的，
    平均值会被当时有多忙带偏（同一段代码实测从 2.1 ms 涨到 8.6 ms）。
    """
    w = board_widget(size=19)
    w.set_props(board=midgame())
    mean, best = render_stats(w)
    assert best < 10.0, f"整盘重绘最快 {best:.2f} ms（均值 {mean:.2f}）超过 10 ms 预算"


def test_paints_do_not_recreate_stone_sprites(qapp, board_widget):
    """重复重绘不再建精灵：「每颗子 100 µs 的径向渐变」就是被这一条消掉的。

    这一条是**确定性**的（数的是缓存里的键，不是时间），机器多忙都不影响判据。
    """
    w = board_widget(size=19)
    w.set_props(board=midgame())
    render_stats(w, times=3)
    keys = set(w._sprites)
    assert len(keys) == 2, f"一整盘只该有黑白两张精灵，实际 {len(keys)} 张"
    render_stats(w, times=5)
    assert set(w._sprites) == keys, "重复重绘又建了新精灵（缓存没生效）"


def test_per_stone_cost_is_a_blit_not_a_draw(qapp, board_widget):
    """每颗子的成本要落在「贴图」量级（老写法是现建渐变，100 µs/颗）。"""
    w = board_widget(size=19)
    w.set_props(board=midgame(n=0))
    _blank_mean, blank = render_stats(w)
    w.set_props(board=midgame())
    _full_mean, full = render_stats(w)
    per_stone_us = (full - blank) / 180 * 1000
    assert per_stone_us < 60, \
        f"每颗子 {per_stone_us:.0f} µs（空盘 {blank:.2f} ms，满盘 {full:.2f} ms）：走了老写法？"


def test_static_layers_are_reused_until_geometry_changes(qapp, board_widget):
    """网格层/领地层按几何缓存：同一尺寸下不重建，改了尺寸才重建。"""
    w = board_widget(size=19)
    w.set_props(board=midgame(), ownership=[0.5] * 361, show_ownership=True)
    grid1, own1 = w._grid_layer(), w._own_layer()
    assert w._grid_layer() is grid1 and w._own_layer() is own1
    w.resize(560, 560)
    assert w._grid_layer() is not grid1, "改了尺寸还拿旧网格层，标签与线会画在旧位置上"
    assert w._own_layer() is not own1, "改了尺寸还拿旧领地层，热力图会错格"


def test_stone_sprites_are_shared_between_stones(qapp, board_widget):
    """一整盘 361 颗子只有黑/白两种精灵：缓存按（颜色, 半径, 格宽, DPR）而不是按点。"""
    w = board_widget(size=19)
    w.show()                              # 隐藏的控件收不到 resizeEvent，先把窗口立起来
    qapp.processEvents()
    w.set_props(board=midgame())
    render_ms(w, times=3)
    assert len(w._sprites) == 2, f"精灵缓存有 {len(w._sprites)} 项，应当只有黑白两张"
    w.resize(560, 560)                    # 格宽变了 → 精灵作废重做
    qapp.processEvents()
    # 断的是「缓存里只剩新格宽那两张」而不是「缓存空了」：改尺寸会立刻触发一次重绘，
    # 新精灵就是在那一帧建的。留着旧的一项是内存泄漏，混着用才是画面错 —— 两者都挡住。
    cell = float(w.layout_now()[0])
    assert {k[2] for k in w._sprites} == {cell}, \
        f"缓存里混着旧格宽的精灵：{sorted(w._sprites)}（当前格宽 {cell}）"
    assert len(w._sprites) == 2, "换了格宽应当还是黑白两张，不是越攒越多"


# ---------------------------------------------------------------- 最小重绘

def test_stones_still_draw_at_fractional_dpr(qapp, board_widget):
    """DPI 1.5（本机真屏就是 1.5，offscreen 是 1.0）下棋子必须照旧画出来。

    精灵是按 `devicePixelRatioF()` 渲染并 `setDevicePixelRatio()` 回填的：漏掉后者，
    贴出来会缩成一小块；漏掉前者，真屏上会糊。这条用例把 DPR 钉成 1.5 渲染一帧，
    再按像素读黑子/白子的深浅 —— offscreen 那套（DPR 1.0）看不见这一类错。
    """

    class HiDpi(GoBoard):
        def devicePixelRatioF(self):                          # noqa: N802
            return 1.5

    w = HiDpi(size=19)
    w.resize(640, 640)
    board = [[0] * 19 for _ in range(19)]
    board[3][3] = 1                       # 黑
    board[15][15] = 2                     # 白
    w.set_props(board=board)
    pm = QPixmap(640, 640)
    w.render(pm)
    img = pm.toImage()

    def luma_at(x, y):
        c = w.center(x, y)
        px = img.pixelColor(int(c.x()), int(c.y()))
        return H.luma(px)

    assert luma_at(3, 3) < 120, f"DPR 1.5 下黑子没画出来（中心亮度 {luma_at(3, 3)}）"
    assert luma_at(15, 15) > 200, f"DPR 1.5 下白子没画出来（中心亮度 {luma_at(15, 15)}）"
    # 再直接钉住「回填 DPR」这一步：少了它，像素断言在某些缩放下可能仍然"看着对"，
    # 而 `devicePixelRatio()` 一定是错的（贴图尺寸会算成设备像素）
    cell, _ox, _oy, _ = w.layout_now()
    sprite = w._stone_sprite(1, cell * 0.47, cell)
    assert abs(sprite.devicePixelRatio() - 1.5) < 1e-6, \
        f"精灵没带上 DPR：{sprite.devicePixelRatio()}（贴出来会缩成一小块）"
    w.deleteLater()
    QApplication.processEvents()


def test_hover_repaints_only_two_cells(qapp, board_widget):
    """鼠标划过一格只脏两格（旧格 + 新格），不是整盘。"""
    w = board_widget(size=19)
    w.set_props(board=midgame())
    p = CountingBoard(size=19)
    p.resize(640, 640)
    p.set_props(board=midgame())
    p.updates.clear()
    move_to(p, 3, 3)
    assert len(p.updates) == 1, f"第一次悬停应当只重画一格，实际 {p.updates}"
    area = p.updates[0][0].width() * p.updates[0][0].height()
    assert area < p.width() * p.height() / 20, "悬停的重绘区还是太大（接近整盘）"
    p.updates.clear()
    move_to(p, 4, 3)
    assert len(p.updates) == 2, "换一格要重画旧格与新格两处，否则会留下残影"
    p.deleteLater()
    QApplication.processEvents()


def test_identical_props_do_not_repaint(qapp, board_widget):
    """同一份状态再送一遍不该重绘：页面把 `_paint` 挂在每个服务端事件上。"""
    w = CountingBoard(size=19)
    w.resize(640, 640)
    w.set_props(board=midgame(), hints=[{"x": 3, "y": 3}])
    w.updates.clear()
    w.set_props(board=midgame(), hints=[{"x": 3, "y": 3}])
    assert w.updates == [], f"没有变化却重绘了 {len(w.updates)} 次"
    w.set_props(hints=[{"x": 15, "y": 15}])
    assert len(w.updates) == 1, "提示换了必须重绘"
    w.deleteLater()
    QApplication.processEvents()


# ---------------------------------------------------------------- 列表不重建

def test_review_rows_are_not_rebuilt_when_unchanged(qapp):
    """复盘的 300 手表格：行没变就不重建（老写法每次 11.8 ms，逐手回看按一下重建一次）。"""
    from ui.pages.review import ReviewPage

    page = ReviewPage(_Api())
    page.resize(1104, 760)
    rows = [{"ply": i, "moveNum": i, "isPlayer": i % 2 == 0, "color": 1 if i % 2 else 2,
             "gtp": "Q16", "flagLabel": "缓手", "flag": "slow", "winrateAfter": 51.2,
             "lossPoints": 1.4} for i in range(300)]
    page.report = {"moves": rows}
    page.moves = rows
    page._paint_list()
    first = page.table.item(0, 0)
    assert first is not None and page.table.rowCount() == 300
    page._paint_list()
    assert page.table.item(0, 0) is first, "行没变却把表格重建了一遍"
    rows[0] = dict(rows[0], flag="blunder", flagLabel="大恶手")
    page._paint_list()
    # 变了这一条只断**内容**不断对象：`clearContents`/`setRowCount` 会复用同一块
    # 内存，shiboken 于是把新 item 装进同一个 Python 包装对象里 —— 拿 `is` 断言
    # 「重建过」在这里是假阴性（第一次写这条用例就踩了）。
    assert "大恶手" in page.table.item(0, 1).text(), "行变了却不重建，表里会留着旧评级"
    page.deleteLater()
    QApplication.processEvents()


def test_tsumego_rows_are_not_rebuilt_when_unchanged(qapp):
    """题库列表同理：414 题重建一次实测 3.5 ms，切回本页会重拉一份一样的。"""
    from ui.pages.tsumego import TsumegoPage

    page = TsumegoPage(_Api())
    page.resize(1280, 800)
    page.items = [{"id": f"p{i}", "title": f"第 {i} 题", "kindText": "死活",
                   "difficulty": 1, "tier": "初级", "family": "直三",
                   "solved": i % 4 == 1} for i in range(414)]
    page._paint_list()
    first = page.problemList.item(1)          # 第 0 行是题型表头，第 1 行才是第一道题
    assert first is not None and first.text() == "第 0 题"
    page._paint_list()
    assert page.problemList.item(1) is first, "题没变却把列表重建了一遍"
    page.items[0] = dict(page.items[0], solved=True)
    page._paint_list()
    # 同 `test_review_rows_...`：变了的这一条断内容，不断对象身份
    assert page.problemList.item(1).text() == "✓ 第 0 题", \
        "进度变了却不重建，✓ 标记会停在上一次"
    page.deleteLater()
    QApplication.processEvents()


# ---------------------------------------------------------------- 动效

def test_animations_are_off_in_tests(qapp):
    """测试进程里动效默认关（像素断言要终态）—— 这是 conftest 的契约，不是巧合。"""
    assert motion.enabled() is False
    assert motion.duration("place") == 0


def test_place_animation_runs_then_settles(qapp, board_widget, anim):
    """落子动效：新出现的那一颗会淡入，到点自己收干净（定时器要停）。"""
    w = board_widget(size=19)
    w.set_animations(True)
    board = [[0] * 19 for _ in range(19)]
    board[3][3] = 1
    w.set_props(board=board)
    assert w.place_animation == (3, 3), "新落的那一颗没有起动画"
    assert w._frame.isActive(), "动效帧定时器没跑起来"
    assert H.wait(qapp, lambda: w.place_animation is None, timeout=3.0), "落子动效没有收尾"
    assert not w._frame.isActive(), "动效结束后定时器还在跑（白耗电）"


def test_multiple_new_stones_skip_the_place_animation(qapp, board_widget, anim):
    """一次多出好几颗（重连补局面/复盘跳手）不做落子动效：逐颗淡入看不出是落子。"""
    w = board_widget(size=19)
    w.set_animations(True)
    board = [[0] * 19 for _ in range(19)]
    board[3][3] = board[4][4] = board[5][5] = 1
    w.set_props(board=board)
    assert w.place_animation is None


def test_turning_animations_off_settles_the_board(qapp, board_widget, anim):
    """关掉动效要把在跑的动画收成终态：半透明的子不能留在盘上。"""
    w = board_widget(size=19)
    w.set_animations(True)
    board = [[0] * 19 for _ in range(19)]
    board[3][3] = 1
    w.set_props(board=board)
    assert w.place_animation == (3, 3)
    w.set_animations(False)
    assert w.place_animation is None and not w._frame.isActive()


def test_fade_in_leaves_no_effect_behind(qapp, board_widget):
    """淡入结束后必须摘掉 `QGraphicsOpacityEffect`：留着它每一帧都走离屏合成。"""
    from ui.widgets.parts import Alert

    bar = Alert("info")
    bar.show_text("正解！")
    motion.fade_in(bar)                   # 动效关着 → 直接到位
    assert bar.graphicsEffect() is None
    motion.set_enabled(True)
    try:
        motion.fade_in(bar)
        assert bar.graphicsEffect() is not None, "开着动效却没挂上效果器"
        assert H.wait(qapp, lambda: bar.graphicsEffect() is None, timeout=3.0), \
            "淡入结束了效果器还挂着（之后每帧都离屏合成）"
    finally:
        motion.set_enabled(None)
    bar.deleteLater()
    QApplication.processEvents()
