"""性能探针：量「卡手感」到底是哪一段（第 32 轮起常驻，改绘制/列表前后各跑一次）。

用法（在 `desktop` 目录下）：

    .venv\\Scripts\\python.exe scripts\\perf_probe.py

口径：**全部走生产代码路径**（`GoBoard.paintEvent` / `TsumegoPage._paint_list` /
`ReviewPage._paint_list` / `GamePage._paint`），不复制一份算法来量 —— 复制出来的
量的是副本（§5-88 的教训）。offscreen 平台、DPR 1.0、19 路 640x640、盘上 180 子。

第 32 轮的基线（同一条探针、同一台机器；「改前」是当轮实测，括号里是同一操作的另一次采样）：

| 项 | 改前 | 改后 |
| --- | --- | --- |
| 整盘重绘（180 子） | 22.25 ms（另一次 16.16） | 1.6~1.7 ms |
| 空盘（木底 + 网格 + 标签） | 4.20 ms | 0.53 ms |
| 棋子那一段 | 18.05 ms（100 µs/子） | 1.11 ms（6 µs/子） |
| 题库列表重建（414 题） | 3.46 ms | 0.06 ms（内容没变时） |
| 复盘手顺表（300 手） | 5~12 ms（机器负载不同） | 0.05 ms（行没变时） |
| 相同 `set_props` 的 update 次数 | 4 | 0 |

数字比基线明显变差时，先看 `test_perf.py` 那 6 条守门人哪一条红了 —— 它们断的就是
下面这些量（守门人断「最快那一帧」，探针给平均与最快两档）。
"""
from __future__ import annotations

import os
import sys
import time

#: 让脚本从 `desktop/scripts/` 直接跑也能 import 到 `ui`（父目录就是 desktop 根）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GO_KATAGO_ENABLED", "false")
#: 探针量的是终态绘制：动效开着的话每帧都在动画中间态，数字就不是"一帧要多久"
os.environ.setdefault("YIDAO_ANIM", "0")

from PySide6.QtGui import QPixmap                      # noqa: E402
from PySide6.QtWidgets import QApplication             # noqa: E402

from ui import theme                                    # noqa: E402
from ui.widgets.board import GoBoard                    # noqa: E402


def midgame(size: int = 19, n: int = 180) -> list[list[int]]:
    """中盘局面：前 n 个交叉点交替放黑白。"""
    board = [[0] * size for _ in range(size)]
    put = 0
    for y in range(size):
        for x in range(size):
            if put >= n:
                return board
            board[y][x] = 1 if put % 2 == 0 else 2
            put += 1
    return board


def bench(fn, times: int = 40) -> tuple[float, float]:
    """`(平均, 最快)` 毫秒。两个都报：平均会被"机器当时有多忙"带偏，最快是代码本来的速度。"""
    fn()                                  # 预热（首帧要建字体缓存与精灵）
    samples = []
    for _ in range(times):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return sum(samples) / len(samples), min(samples)


class _Api:
    """页面构造器要一个 api。探针不发请求，调到了就是探针写错了。"""

    def get(self, *a, **k):
        raise AssertionError("探针不联网")

    post = get
    patch = get
    delete = get


def main() -> None:
    theme.configure_hi_dpi()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    theme.apply_app_font(app)
    app.setStyleSheet(theme.QSS)

    def line(name: str, both: tuple[float, float]) -> None:
        print(f"  {name:<26}: 平均 {both[0]:6.2f} ms   最快 {both[1]:6.2f} ms")

    print("=== A. 棋盘重绘（19 路，640x640）===")
    board = GoBoard(size=19)
    board.resize(640, 640)
    pm = QPixmap(640, 640)
    board.set_props(board=midgame(), last_move=(8, 8))
    line("中盘 180 子，整盘重绘", bench(lambda: board.render(pm)))
    board.set_props(board=midgame(n=0))
    blank = bench(lambda: board.render(pm))
    line("空盘（木底+网格+标签）", blank)
    board.set_props(board=midgame())
    full = bench(lambda: board.render(pm))
    per_stone = (full[0] - blank[0]) / 180 * 1000
    print(f"  {'-> 棋子那一段（180 子）':<26}: {full[0] - blank[0]:6.2f} ms "
          f"（{per_stone:.0f} µs/子）")
    board.set_props(board=midgame(), hints=[{"x": 3, "y": 3}, {"x": 15, "y": 15}])
    line("带 2 个提示点", bench(lambda: board.render(pm)))
    board.set_props(board=midgame(), ownership=[0.5] * 361, show_ownership=True)
    line("带领地热力图", bench(lambda: board.render(pm)))
    print(f"  {'-> 占 60fps 预算':<26}: {full[0] / 16.7 * 100:.0f}%（一帧 16.7 ms）")

    print()
    print("=== B. 题库列表重建（_paint_list，414 题的真实规模）===")
    from ui.pages.tsumego import TsumegoPage

    page = TsumegoPage(_Api())
    page.resize(1280, 800)
    page.items = [{"id": f"p{i}", "title": f"第 {i} 题 · 黑先活", "kindText":
                   ("死活" if i % 3 else "手筋"), "difficulty": (i % 5) + 1,
                   "tier": "初级", "family": "直三", "solved": i % 4 == 0}
                  for i in range(414)]
    # 「重建」那一档要**把签名打掉**再量：不清的话 `bench` 的预热那一次就把表建好了，
    # 后面测到的全是短路路径（第一版脚本就是这么把自己的标签写错的）
    def rebuild():
        page._list_sig = None
        page._paint_list()

    line("414 题：真重建一次", bench(rebuild, times=5))
    line("414 题：内容没变（短路）", bench(lambda: page._paint_list(), times=5))

    print()
    print("=== C. 复盘手顺表重建（_paint_list，300 手）===")
    from ui.pages.review import ReviewPage

    rp = ReviewPage(_Api())
    rp.resize(1280, 800)
    rows = [{"ply": i, "moveNum": i, "isPlayer": i % 2 == 0, "color": 1 if i % 2 else 2,
             "gtp": "Q16", "flagLabel": "缓手", "flag": "slow", "winrateAfter": 51.2,
             "lossPoints": 1.4} for i in range(300)]
    rp.report = {"moves": rows}
    rp.moves = rows
    def rebuild():
        rp._list_sig = None
        rp._paint_list()

    line("300 手：真重建一次", bench(rebuild, times=5))
    line("300 手：行没变（短路）", bench(lambda: rp._paint_list(), times=5))

    print()
    print("=== D. 每手一次的页面重画（对局页 _paint）===")
    from ui.pages.game import GamePage

    gp = GamePage(_Api())
    gp.resize(1280, 800)
    gp.game_id = "probe"
    gp.size = 19
    gp.moves = [{"x": i % 19, "y": (i * 7) % 19, "color": 1 if i % 2 == 0 else 2,
                 "moveNum": i + 1} for i in range(120)]
    gp.board = midgame()
    gp.meta = {"aiName": "AI", "hintMode": True}
    line("对局页 _paint（120 手）", bench(lambda: gp._paint(), times=20))

    print()
    print("=== E. 空转开销：同一份状态重送时的重绘次数 ===")
    calls = {"n": 0}
    original = GoBoard.update
    GoBoard.update = lambda self, *a: calls.__setitem__("n", calls["n"] + 1)
    # 先把状态摆到一个确定的起点（上一个 section 留下的 hints/ownership 都收掉），
    # 否则「第二次」其实在跟 section A 的状态比，量到的是真变化而不是空转
    board.set_props(board=midgame(), hints=[], ownership=None, show_ownership=False)
    calls["n"] = 0
    board.set_props(board=midgame(), hints=[], ownership=None, show_ownership=False)
    GoBoard.update = original
    print(f"  同一份状态重送 → update {calls['n']} 次（第 32 轮起应为 0：没变就不重绘）")


if __name__ == "__main__":
    main()
