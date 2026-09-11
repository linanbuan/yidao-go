"""P3 验收：死活练习页与段位页真走一遍。

计划口径：「抽 3 题（含劫争）点到正解与 1 道错着；截图核对正解线箭头与 verdict；
断言答对/答错音效触发次数」。三条纪律：

  · **题号、题名、坐标一个都不写死**。内置题库每次启动按源码指纹重新推导，写死题号
    等于把测试钉在某一天题库上（同一类纪律见日志 §5-59）。选题一律按数据形状筛：
    「kind=ko 且正解线带 pv」这种条件，而不是「第 3 题」。
  · 落子一律 `QTest.mouseClick` 打在交叉点上，选下一题一律真点题库行。
  · 允许用 `GET /api/tsumego/{id}/solution` 抄答案 —— 单手题的「点到正解」不抄答案
    根本没法自动化。但它会在服务端记一次 attempts 并置 seenAnswer，所以**凡与次数
    有关的断言都比前后差值，不比绝对数**。

关键帧落 artifacts/，我逐张读图。读完图补上的五个缺陷（标记字压圈 / 侧栏滚走 /
出处撑爆卡片 / 筛选条放错卡 / 「一次做对率」名不副实）各有一条测试钉住，
都在文末「界面完整性」一节。
"""
from __future__ import annotations

import json
import math
import uuid

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QListWidgetItem

from core import api as A
from core import backend_host as bh
from core.settings import Prefs
from tests import harness as H
from ui import shell as shell_mod
from ui.pages import ranks as ranks_page
from ui.pages import tsumego as tsumego_page

PASSWORD = "mimashou123"
SOLVED, FAILED = "solved", "failed"


@pytest.fixture(scope="module")
def host():
    h = bh.BackendHost()
    h.start(timeout=90.0)
    yield h
    h.stop()


@pytest.fixture
def shell(qapp, host, tmp_path):
    prefs = Prefs(str(tmp_path / "client.ini"))
    sh = shell_mod.Shell(host, prefs)
    sh.resize(1280, 800)
    sh.show()
    qapp.processEvents()
    yield sh
    sh.close()
    sh.deleteLater()


# ---------------------------------------------------------------- 通用夹具操作

def _login(shell, qapp, prefix: str = "p3") -> str:
    username = prefix + uuid.uuid4().hex[:8]
    QTest.keyClicks(shell._login.user, username)
    QTest.keyClicks(shell._login.pw, PASSWORD)
    QTest.mouseClick(shell._login.btnRegister, Qt.LeftButton)
    assert H.wait(qapp, lambda: shell._stack.currentIndex() == 1), \
        f"注册没进主界面：{shell._login.error.text()!r}"
    return username


def _get(host, shell, path, query=None):
    url = A.build_url(host.base_url, path, query)
    return A.http_json("GET", url, None, shell._prefs.token, timeout=90.0)


def _enter_tsumego(shell, qapp) -> tsumego_page.TsumegoPage:
    """切到死活页并等到「题已经开在手上、列表与统计都回来了」。

    顺手把「答对后自动下一题」**关掉**：它默认是开的，而这里绝大多数用例要在
    答对之后继续检查这一题的判定/正解圈/列表回填 —— 不关的话 1.6 秒后题就被换走了，
    那些断言会变成随机红。专门验自动换题的用例自己再打开（见文件末尾两条）。
    """
    shell.go("tsumego")
    page = shell.current_page()
    assert isinstance(page, tsumego_page.TsumegoPage), type(page).__name__
    page.chkAutoNext.setChecked(False)
    assert H.wait(qapp, lambda: page.current is not None and bool(page.items),
                  timeout=60.0), \
        f"进页没开出题：error={page.errorBar.label.text()!r}"
    assert H.wait(qapp, lambda: page.summary is not None, timeout=30.0), "统计没回来"
    return page


def _set_combo(combo, value) -> None:
    """按 userData 选一项 —— 这会真的发出 currentIndexChanged，走的就是用户那条路。"""
    idx = next((i for i in range(combo.count()) if combo.itemData(i) == value), None)
    assert idx is not None, f"下拉里没有这一项 {value!r}：{[combo.itemData(i) for i in range(combo.count())]}"
    combo.setCurrentIndex(idx)


def _filter_kind(page, qapp, kind: str) -> list[dict]:
    """切题型筛选，等到列表真的只剩这个题型。"""
    _set_combo(page.cbKind, kind)
    ok = H.wait(qapp, lambda: page.items and all(it.get("kind") == kind for it in page.items),
                timeout=60.0)
    assert ok, f"按 kind={kind} 筛完列表不对：{[it.get('kind') for it in page.items][:8]}"
    return page.items


def _row_of(page, pid: str) -> QListWidgetItem | None:
    for i in range(page.problemList.count()):
        item = page.problemList.item(i)
        if item.data(Qt.UserRole) == pid:
            return item
    return None


def _click_row(page, qapp, pid: str) -> None:
    """真点题库里的一行（表头行没有 UserRole，压根点不到）。"""
    item = _row_of(page, pid)
    assert item is not None, f"题库列表里没有 {pid} 这一行"
    rect = page.problemList.visualItemRect(item)
    # `visualItemRect` 给的是 QRect，`center()` 已经是 QPoint（再 .toPoint() 会
    # AttributeError）；而 `boardView.center()` 给的是 QPointF，那边才需要转。
    QTest.mouseClick(page.problemList.viewport(), Qt.LeftButton, Qt.NoModifier,
                     rect.center())
    assert H.wait(qapp, lambda: (page.current or {}).get("id") == pid, timeout=30.0), \
        "点题库行没换题"


def _click_point(page, qapp, x: int, y: int) -> None:
    pos = page.boardView.center(x, y).toPoint()
    QTest.mouseClick(page.boardView, Qt.LeftButton, Qt.NoModifier, pos)


def _wait_result(page, qapp, pid: str | None = None, timeout: float = 90.0) -> dict:
    ok = H.wait(qapp, lambda: bool(page.result) and (pid is None
                 or (page.current or {}).get("id") == pid), timeout=timeout)
    assert ok, f"没等到判定结果：error={page.errorBar.label.text()!r}"
    return page.result


def _lines_of(host, shell, pid: str) -> list[dict]:
    """抄答案。会在服务端记一次 attempts + seenAnswer，用法见模块 docstring。"""
    sol = _get(host, shell, f"/api/tsumego/{pid}/solution") or {}
    return sol.get("lines") or []


def _correct_move(host, shell, pid: str) -> tuple[int, int]:
    correct = [ln for ln in _lines_of(host, shell, pid) if ln.get("result") == "correct"]
    assert correct and correct[0].get("moves"), f"{pid} 没有正解线可抄"
    return int(correct[0]["moves"][0][0]), int(correct[0]["moves"][0][1])


def _wrong_move(host, shell, pid: str) -> tuple[int, int]:
    wrong = [ln for ln in _lines_of(host, shell, pid) if ln.get("result") == "wrong"]
    assert wrong and wrong[0].get("moves"), f"{pid} 没有失败线可抄"
    return int(wrong[0]["moves"][0][0]), int(wrong[0]["moves"][0][1])


def _progress_rows(page) -> dict[str, str]:
    """把「练习进度」那张卡的网格读成 {标题: 值}。"""
    out: dict[str, str] = {}
    grid = page.progressGrid
    for row in range(grid.rowCount()):
        k, v = grid.itemAtPosition(row, 0), grid.itemAtPosition(row, 1)
        if k is None or v is None:
            continue
        kw, vw = k.widget(), v.widget()
        if isinstance(kw, QLabel) and isinstance(vw, QLabel):
            out[kw.text()] = vw.text()
    return out


# ---------------------------------------------------------------- 进页与筛选

def test_first_run_opens_a_problem_and_prunes_dead_goals(shell, qapp, host):
    """进页即有题；目标下拉把**零题的目标**剪掉（劫活/连络/切断 都不该出现）。

    「劫活」是静态表里唯一会骗人的选项：本形状的家族里根本没有劫活形（守先都是净活，
    只有攻先才成劫），留着它就是一筛就空。
    """
    _login(shell, qapp)
    page = _enter_tsumego(shell, qapp)
    cur = page.current
    assert cur.get("id") and cur.get("title")
    assert cur.get("kind") in ("life", "ko", "race", "capture"), cur.get("kind")
    assert page.problemTitle.text() == cur["title"]
    assert page.kindBadge.text() == cur["kindText"]
    assert cur["tier"] in page.tierBadge.text() and f"难度 {cur['difficulty']}" in page.tierBadge.text()
    assert cur["toMoveText"] in page.goalBadge.text() and cur["goalText"] in page.goalBadge.text()
    assert page.taskLine.text() and "点击棋盘落子" in page.taskLine.text()
    assert not page.solvedBadge.isVisible(), "新账号一题都没解出，徽章不该先亮"
    assert page.solutionPanel.isHidden(), "答案卡默认必须收起"

    goals = {page.cbGoal.itemData(i) for i in range(page.cbGoal.count())}
    by_goal = page.summary["byGoal"]
    expect = {""} | {g for g, _lab in tsumego_page.GOALS_BY_KIND[""]
                     if g and int((by_goal.get(g) or {}).get("total") or 0) > 0}
    assert goals == expect, f"目标下拉没按题库实际数剪枝：{goals} vs {expect}"
    # 剪枝真的在起作用：零题目标（静态表里列着或没列着都一样）绝不允许出现在下拉里。
    # （旧版另有一句「静态表与题库不能完全重合」——连络/切断 L3 上线后静态表已全覆盖，
    # 那句的前提没了；零题目标不出现才是这条断言真正要守的不变式。）
    zero = {g for g in goals if g and not int((by_goal.get(g) or {}).get("total") or 0)}
    assert not zero, f"零题目标还在下拉里：{zero}"
    summary = page.summary
    assert summary["total"] == len(_get(host, shell, "/api/tsumego/problems")["items"])
    assert summary["solved"] == 0 and summary["attempts"] == 0
    rows = _progress_rows(page)
    assert f"0 / {summary['total']} 题" == rows["已解出"], rows

    # 题库分组：劫争题存在，就必须有一个「劫争」组头，且组头不可点
    heads = [page.problemList.item(i).text() for i in range(page.problemList.count())
             if not page.problemList.item(i).data(Qt.UserRole)]
    assert any("劫争" in h for h in heads), f"题库没有劫争组：{heads}"
    assert page.problemList.count() == len(page.items) + len(heads)
    H.snap(shell, "p3_01_tsumego_first_run")


def test_goal_options_follow_summary_and_survive_kind_switch(shell, qapp, host):
    """换题型时目标下拉重建；已选中的目标在新题型下没题就自动重置并重拉列表。"""
    _login(shell, qapp)
    page = _enter_tsumego(shell, qapp)
    _set_combo(page.cbKind, "ko")
    assert H.wait(qapp, lambda: page.items and all(it["kind"] == "ko" for it in page.items),
                  timeout=60.0)
    ko_goals = {page.cbGoal.itemData(i) for i in range(page.cbGoal.count())}
    by_goal = page.summary["byGoal"]
    expect = {""} | {g for g, _lab in tsumego_page.GOALS_BY_KIND["ko"]
                     if g and int((by_goal.get(g) or {}).get("total") or 0) > 0}
    assert ko_goals == expect, f"劫争下的目标项与题库实际数不符：{ko_goals} vs {expect}"
    assert ko_goals != {""}, "劫争下连一个具体目标都没得选，这一支测不到东西"
    # 停在「劫杀」上再切回死活：劫杀在 life 里没题，选中值必须被重置为「全部」
    _set_combo(page.cbGoal, "ko_kill")
    _set_combo(page.cbKind, "life")
    assert H.wait(qapp, lambda: page.cbGoal.currentData() == ""
                   and all(it["kind"] == "life" for it in page.items), timeout=60.0), \
        f"切回死活后目标没重置：{page.cbGoal.currentData()!r}"
    assert page.items, "重置成「全部」后列表却空了"


def test_tier_and_unsolved_filters_match_server_counts(shell, qapp, host):
    """按难度档筛出来的题数 == summary.byTier 的数；「只做没解出的」跟着进度变。"""
    _login(shell, qapp)
    page = _enter_tsumego(shell, qapp)
    summary = page.summary
    tier = next(iter(summary["byTier"]))
    _set_combo(page.cbTier, tier)
    assert H.wait(qapp, lambda: len(page.items) == summary["byTier"][tier]["total"],
                  timeout=60.0), \
        f"{tier} 筛出 {len(page.items)} 题，服务端说 {summary['byTier'][tier]['total']}"
    assert all(it["tier"] == tier for it in page.items)
    assert page.countBadge.text() == f"{len(page.items)} 题"

    # 解出一道题，再勾「只做没解出的」：那道题必须从列表里消失
    pid = page.items[0]["id"]
    _click_row(page, qapp, pid)
    x, y = _correct_move(host, shell, pid)
    _click_point(page, qapp, x, y)
    res = _wait_result(page, qapp, pid)
    assert res["status"] == SOLVED, res
    assert H.wait(qapp, lambda: len(page.items) == summary["byTier"][tier]["total"],
                  timeout=60.0)
    page.chkUnsolved.setChecked(True)
    expect = summary["byTier"][tier]["total"] - 1
    assert H.wait(qapp, lambda: len(page.items) == expect and pid not in
                  [it["id"] for it in page.items], timeout=60.0), \
        f"勾了只做没解出，题数 {len(page.items)}（应为 {expect}）或那题还在"
    # 被筛掉的那题还在手上，进度就得靠判定回包回灌 —— 徽章必须已经亮了
    assert page.current["id"] == pid
    assert page.solvedBadge.isVisible(), "被筛掉的已解题，横幅徽章没跟上"
    page.chkUnsolved.setChecked(False)


# ---------------------------------------------------------------- 抽三题点到正解

def _pick_one(host, shell, kind: str, need_pv: bool, need_targets: bool,
              taken: set[str]) -> dict:
    """按**数据形状**挑一道题（不写死题号）。"""
    items = _get(host, shell, "/api/tsumego/problems", {"kind": kind})["items"]
    for it in items:
        if it["id"] in taken:
            continue
        lines = [ln for ln in _lines_of(host, shell, it["id"])
                 if ln.get("result") == "correct"]
        pv = lines[0].get("pv") if lines else None
        has_targets = bool(it.get("targets"))
        if need_pv != bool(pv) or need_targets != has_targets:
            continue
        taken.add(it["id"])
        return it
    raise AssertionError(f"题库里挑不出 kind={kind} pv={need_pv} targets={need_targets} 的题")


def test_three_problems_solved_to_the_end_including_ko(shell, qapp, host):
    """抽 3 题（劫争 / 吃子 / 死活）点到正解：判定、徽章、列表 ✓、统计、音效、正解线。"""
    _login(shell, qapp)
    page = _enter_tsumego(shell, qapp)
    sound = shell._sound
    taken: set[str] = set()
    picks = [
        # 劫争：正解必带 pv（劫的变化线就是那几个提劫点），且劫题没有目标子
        ("ko", _pick_one(host, shell, "ko", True, False, taken), "正解线"),
        ("capture", _pick_one(host, shell, "capture", True, True, taken), "蓝圈"),
        ("life", _pick_one(host, shell, "life", False, False, taken), "无幽灵子"),
    ]
    solved_before = page.summary["solved"]

    for index, (_kind, item, tag) in enumerate(picks, start=1):
        pid = item["id"]
        _filter_kind(page, qapp, item["kind"])
        _click_row(page, qapp, pid)
        assert page.current["id"] == pid
        assert not page.result and not page.moves, "换题没清干净：上一题的判定或手顺还在"
        assert page.solutionPanel.isHidden(), "换题没收起答案卡"

        x, y = _correct_move(host, shell, pid)
        assert page.board[y][x] == 0, f"正解点 {pid}({x},{y}) 在初始盘上不空，抄的答案有问题"
        sound.forget_played()
        _click_point(page, qapp, x, y)
        res = _wait_result(page, qapp, pid)

        assert res["status"] == SOLVED, f"{tag}：判定不是正解 {res}"
        assert res["moveText"], "GTP 坐标文案是空的"
        assert res["verdictText"], "本形结论是空的（verdict 没落进界面）"
        assert page.resultBar.label.text().startswith("✓ 正解！"), page.resultBar.label.text()
        assert res["comment"] in page.resultBar.label.text()
        assert page.verdictLine.text() == res["verdictText"]
        assert page.verdictLine.isVisible()
        assert sound.count_of("correct") == 1, f"{tag}：答对音效没触发：{sound.played}"
        assert sound.count_of("wrong") == 0 and sound.count_of("stone") == 0

        kinds = {(m["x"], m["y"], m["kind"]) for m in page._marks()}
        assert (x, y, "good") in kinds and (x, y, "blunder") not in kinds, f"{tag}：评价圈画错 {kinds}"
        assert next(m for m in page._marks() if m["kind"] == "good")["label"] == "正"
        pv = res["pv"] or []
        variation = page._variation()
        assert len(variation) == len(pv), f"{tag}：正解线手数与 pv 不符"
        first = item["toMove"]
        # 颜色交替从「对方应」开始：不能接着玩家的颜色画（那等于让玩家连下两手）。
        # pv 为空是合法的：题库里有 35 条正解线没有后续（_pick_one 故意挑了一道这种）。
        assert [v["color"] for v in variation] == [
            (3 - first) if i % 2 == 0 else first for i in range(len(variation))], \
            f"{tag}：正解线颜色不对：{variation}"
        marks = page._marks()
        if item["targets"]:
            assert any(m["kind"] == "target" for m in marks), "吃子题没画目标子蓝圈"
            assert item["goalText"] in page.legendLine.text() or "蓝圈" in page.legendLine.text()
        else:
            assert not any(m["kind"] == "target" for m in marks), "无目标子的题画了蓝圈"
            assert not page.legendLine.isVisible()
        assert page.done and not page.boardView.interactive, "解出后棋盘该收着"
        assert page.btnRedo.isEnabled(), "解出后重做该可用"
        assert page.btnAnswer.isEnabled(), "本页还没通过界面看过答案，按钮该亮着"

        assert H.wait(qapp, lambda: page.current.get("solved") is True, timeout=60.0), \
            f"{tag}：解出后横幅徽章没跟上（网页版就是这里过期）"
        assert page.solvedBadge.isVisible()
        assert "看过答案" in page.problemRows["attempts"].text()     # 抄答案的副作用
        row = _row_of(page, pid)
        assert row.text().startswith("✓ "), f"{tag}：列表里没打勾：{row.text()!r}"

        if index == len(picks):
            assert H.wait(qapp, lambda: page.summary["solved"] == solved_before + 3,
                          timeout=60.0), f"统计没跟上：{page.summary}"
            rows = _progress_rows(page)
            assert rows["已解出"].startswith(f"{solved_before + 3} / ")
        H.snap(shell, f"p3_0{index + 1}_solved_{item['kind']}")


def test_solved_ko_line_is_painted_on_the_board(shell, qapp, host):
    """正解线的幽灵子必须真的**画在棋盘上**（像素级），不是只存在列表里。

    劫争题的 pv 就是那几个提劫点：没画出来，学员看不出「打劫」这个结论从哪来。
    """
    _login(shell, qapp)
    page = _enter_tsumego(shell, qapp)
    taken: set[str] = set()
    item = _pick_one(host, shell, "ko", True, False, taken)
    _filter_kind(page, qapp, "ko")
    _click_row(page, qapp, item["id"])
    x, y = _correct_move(host, shell, item["id"])
    _click_point(page, qapp, x, y)
    res = _wait_result(page, qapp, item["id"])
    assert res["status"] == SOLVED
    pv = res["pv"] or []
    assert pv, "这道劫题没有 pv，正解线无从核对（选题条件失效）"

    cell = page.boardView.layout_now()[0]

    def at(px: int, py: int):
        # 取交叉点所在格的**象限中心**而不是正中：正中要么压着网格线，要么是幽灵子序号
        c = page.boardView.center(px, py)
        return H.sample(page.boardView, c.x() + cell / 4.0, c.y() - cell / 4.0)

    pts = [(int(p[0]), int(p[1])) for p in pv]
    # 玩家那一记正解是实子，用「不是木色」就能判
    assert not H.is_wood(at(x, y)), "玩家那记正解没画在盘上"
    # 正解线的幽灵子不能靠「不是木色」判：它们是 55% 不透明画的，半透明的白子
    # 叠在木色上仍然是暖色（实测这么判会漏一个点）。只能拿「没画正解线」的
    # 同一局当对照组，比像素变了没有 —— 画了就一定变。
    ghosts = page._variation()
    marks = page._marks()
    assert [g["x"] for g in ghosts] == [px for px, _py in pts], "幽灵子点位与 pv 不符"
    page.boardView.set_props(variation=[], marks=[])
    before = [at(px, py) for px, py in pts]
    page.boardView.set_props(variation=ghosts, marks=marks)
    after = [at(px, py) for px, py in pts]
    still = [(pt, b.name(), a.name()) for pt, a, b in zip(pts, after, before)
             if H.color_distance(a, b) <= 12]
    assert not still, f"这些正解线上的点没画出子：{still}"
    assert "劫" in page.kindBadge.text() or "劫" in page.goalBadge.text(), \
        f"劫争题的横幅没提劫：{page.kindBadge.text()} / {page.goalBadge.text()}"
    H.snap(shell, "p3_05_ko_variation")


def test_connect_variation_skips_tenuki_sentinel(shell, qapp, host):
    """连络题（L3）的幽灵子：脱先哨兵 [-1,-1] 不画子、著法权照常翻转。

    连络正解线是「占缺口 → 对方无处可下脱先 → 连上第二头」，pv 里夹着
    [-1,-1]。_variation 必须：① 不产生坐标为 -1 的幽灵子；② 幽灵子数与
    非哨兵项一致；③ 每个幽灵子的颜色按「跳过哨兵但翻色」的规则与 pv 对齐
    （哨兵前后没有颜色错位）。
    """
    _login(shell, qapp)
    page = _enter_tsumego(shell, qapp)
    taken: set[str] = set()
    item = _pick_one(host, shell, "connect", True, True, taken)
    _filter_kind(page, qapp, "connect")
    _click_row(page, qapp, item["id"])
    x, y = _correct_move(host, shell, item["id"])
    _click_point(page, qapp, x, y)
    res = _wait_result(page, qapp, item["id"])
    assert res["status"] == SOLVED, res
    pv = res["pv"] or []
    assert any(p[0] == -1 for p in pv), "这道连络题的 pv 没有脱先哨兵（选题条件失效）"

    ghosts = page._variation()
    assert all(g["x"] >= 0 and g["y"] >= 0 for g in ghosts), "脱先哨兵被画成幽灵子了"
    pts = [(int(p[0]), int(p[1])) for p in pv if p[0] != -1]
    assert [(g["x"], g["y"]) for g in ghosts] == pts, "幽灵子点位与 pv（去哨兵）不符"
    # 颜色按「每项都翻、哨兵项不画」模拟，必须与 _variation 一致
    first = int(item["toMove"] or 1)                    # 1=黑 2=白
    sim = []
    color = 2 if first == 1 else 1                      # pv 第一手是对方的
    for p in pv:
        if p[0] == -1:
            color = 3 - color
            continue
        sim.append(color)
        color = 3 - color
    assert [g["color"] for g in ghosts] == sim, f"脱先哨兵把颜色带偏了：{ghosts}"


def test_wrong_move_marks_blunder_and_answer(shell, qapp, host):
    """错着：✗ 不成立 + 红「错」圈 + 对方最佳应手的「应」圈 + wrong 音效 + 棋盘锁住。"""
    _login(shell, qapp)
    page = _enter_tsumego(shell, qapp)
    sound = shell._sound
    _filter_kind(page, qapp, "life")
    pid = page.items[0]["id"]
    _click_row(page, qapp, pid)
    bx, by = _wrong_move(host, shell, pid)
    assert page.board[by][bx] == 0, "失败线首手在初始盘上不空，抄的答案有问题"

    sound.forget_played()
    _click_point(page, qapp, bx, by)
    res = _wait_result(page, qapp, pid)
    assert res["status"] == FAILED, res
    assert sound.count_of("wrong") == 1, f"答错音效没触发：{sound.played}"
    assert sound.count_of("correct") == 0
    assert page.resultBar.label.text().startswith("✗ 不成立"), page.resultBar.label.text()
    assert page.resultBar.kind == "err"
    assert page.verdictLine.text() == (res.get("verdictText") or "")
    marks = {(m["x"], m["y"], m["kind"]): m for m in page._marks()}
    assert (bx, by, "blunder") in marks and marks[(bx, by, "blunder")]["label"] == "错"
    assert not any(k == "good" for _x, _y, k in marks), "答错却画了正解圈"
    ref = res.get("refutation") or []
    if ref:
        key = (int(ref[0][0]), int(ref[0][1]), "bad")
        assert key in marks and marks[key]["label"] == "应", f"没标出对方那记好手：{marks}"
    assert page.done and not page.boardView.interactive
    assert page.btnRedo.isEnabled(), "有手顺时重做该可用"
    before = [list(m) for m in page.moves]
    empty = next((px, py) for py in range(len(page.board)) for px in range(len(page.board))
                 if page.board[py][px] == 0)
    _click_point(page, qapp, *empty)
    qapp.processEvents()
    assert [list(m) for m in page.moves] == before and page.result["status"] == FAILED, \
        "已经判错的题还在接受落子"
    H.snap(shell, "p3_06_wrong_move")


def test_clicking_occupied_point_is_refused_without_sound(shell, qapp, host):
    """点已有子的点：报错、不提交、不出声 —— 手滑不该被算成一次做题。"""
    _login(shell, qapp)
    page = _enter_tsumego(shell, qapp)
    cur = page.current
    occupied = next((x, y) for y in range(len(page.board)) for x in range(len(page.board))
                    if page.board[y][x])
    attempts_before = _get(host, shell, f"/api/tsumego/{cur['id']}")["problem"]["attempts"]
    shell._sound.forget_played()
    _click_point(page, qapp, *occupied)
    assert H.wait(qapp, lambda: page.errorBar.isVisible()
                  and "已有棋子" in page.errorBar.label.text(), timeout=20.0), \
        f"点已有子没报错：{page.errorBar.label.text()!r}"
    assert not page.moves and page.result is None
    assert shell._sound.count_of("wrong") == 0 and shell._sound.count_of("stone") == 0
    assert _get(host, shell, f"/api/tsumego/{cur['id']}")["problem"]["attempts"] == attempts_before


def test_wrong_review_checkbox_filters_list_and_clears_after_solving(shell, qapp, host):
    """「错题重练」勾上后列表只留「做过且没解出」的题；做对后从错题本消失。

    与「只做没解出的」的区别：没做过的题不进错题本——它是真正意义上的错题本。
    """
    _login(shell, qapp)
    page = _enter_tsumego(shell, qapp)
    _filter_kind(page, qapp, "life")
    pid = page.items[0]["id"]
    _click_row(page, qapp, pid)
    bx, by = _wrong_move(host, shell, pid)
    _click_point(page, qapp, bx, by)
    res = _wait_result(page, qapp, pid)
    assert res["status"] == FAILED, res

    page.chkWrong.setChecked(True)
    assert H.wait(qapp, lambda: page.items and all(
        it.get("attempts", 0) > 0 and not it.get("solved") for it in page.items), timeout=60.0), \
        f"错题重练列表里有没做过的题：{[it['id'] for it in page.items]}"
    assert any(it["id"] == pid for it in page.items), "做错的那道不在错题本里"

    # 重做并做对 → 从错题本消失（重新触发筛选以刷新列表）
    page.btnRedo.click()
    qapp.processEvents()
    good = _correct_move(host, shell, pid)
    _click_point(page, qapp, *good)
    res2 = _wait_result(page, qapp, pid)
    assert res2["status"] == SOLVED, res2
    page.chkWrong.setChecked(False)
    page.chkWrong.setChecked(True)
    assert H.wait(qapp, lambda: all(it["id"] != pid for it in page.items), timeout=60.0), \
        "做对后没从错题本消失"


def test_answer_button_opens_solution_panel_with_both_lines(shell, qapp, host):
    """看答案：答案卡展开，正解与失败图都列出来，含手顺文本；记一次 attempts。"""
    _login(shell, qapp)
    page = _enter_tsumego(shell, qapp)
    _filter_kind(page, qapp, "life")
    pid = page.current["id"]
    before = _get(host, shell, f"/api/tsumego/{pid}")["problem"]
    assert before["seenAnswer"] is False

    shell._sound.forget_played()
    QTest.mouseClick(page.btnAnswer, Qt.LeftButton)
    assert H.wait(qapp, lambda: page.solution is not None, timeout=60.0), \
        f"没等到答案：{page.errorBar.label.text()!r}"
    sol = page.solution
    assert page.solutionPanel.isVisible()
    assert shell._sound.count_of("click") == 1, shell._sound.played
    assert not page.btnAnswer.isEnabled(), "答案都看了，按钮还亮着可以再看一遍"
    results = [ln["result"] for ln in sol["lines"]]
    assert "correct" in results
    assert sol["kindText"] in page.solutionBadge.text()
    bars = [w for w in page.solutionPanel.findChildren(QLabel)
            if w.text().startswith(("正解", "失败图")) and w.isVisible()]
    texts = "｜".join(b.text() for b in bars)
    assert "正解" in texts and "失败图" in texts, f"答案卡里两类线不齐：{texts}"
    # 手顺文本是 GTP，必须出现在卡上（截图时要能对得上棋盘）
    first = sol["lines"][0]["movesText"]
    assert first and any(first in b.text() for b in bars), f"{first!r} 没画进答案卡"
    assert page.show_hint is True and "提示" in page.btnHint.text()
    after = _get(host, shell, f"/api/tsumego/{pid}")["problem"]
    assert after["attempts"] == before["attempts"] + 1, "看答案该记一次做题（服务端口径）"
    assert after["seenAnswer"] is True
    assert H.wait(qapp, lambda: "看过答案" in page.problemRows["attempts"].text(),
                  timeout=60.0), page.problemRows["attempts"].text()
    H.snap(shell, "p3_07_answer_panel")


def test_next_and_redo_buttons_do_what_they_say(shell, qapp, host):
    """下一题换题并清干净；重做回到初始局面但题不变。"""
    _login(shell, qapp)
    page = _enter_tsumego(shell, qapp)
    _filter_kind(page, qapp, "life")
    first_pid = page.current["id"]
    x, y = _correct_move(host, shell, first_pid)
    _click_point(page, qapp, x, y)
    _wait_result(page, qapp, first_pid)
    assert page.moves and page.last_move == (x, y)

    QTest.mouseClick(page.btnRedo, Qt.LeftButton)
    assert H.wait(qapp, lambda: not page.moves and page.result is None, timeout=20.0)
    assert page.current["id"] == first_pid, "重做换题了"
    assert page.board[y][x] == 0, "重做没把子收回去"
    assert page.boardView.interactive

    QTest.mouseClick(page.btnNext, Qt.LeftButton)
    assert H.wait(qapp, lambda: (page.current or {}).get("id") != first_pid
                  and not page.moves, timeout=60.0), "下一题没换新题"
    assert not page.errorBar.isVisible()


# ---------------------------------------------------------------- 段位页

def test_ranks_table_has_27_rungs_and_highlights_current(shell, qapp, host):
    """27 档、首尾是 18级/九段、当前段位行整行高亮、无门槛写「—」。"""
    _login(shell, qapp, prefix="rk")
    shell.go("ranks")
    page = shell.current_page()
    assert isinstance(page, ranks_page.RanksPage), type(page).__name__
    assert H.wait(qapp, lambda: page.table.rowCount() == 27, timeout=30.0), \
        f"段位表 {page.table.rowCount()} 行：{page.errorBar.label.text()!r}"
    t = page.table
    assert t.columnCount() == 7
    assert t.item(0, 0).text().startswith("18级"), t.item(0, 0).text()
    assert t.item(26, 0).text().startswith("九段"), t.item(26, 0).text()
    assert "18K" in t.item(0, 0).text() and "9D" in t.item(26, 0).text()

    current = _get(host, shell, "/api/ranks")["currentRankId"]
    rows = [(r, t.item(r, 0).text()) for r in range(27)]
    hit = [r for r, text in rows if "当前" in text]
    assert hit == [current - 1], f"当前段位行标记错位：{hit} vs currentRankId={current}"
    assert not any("已通过" in text for _r, text in rows), "新账号没有已通过的档位"
    bg = t.item(current - 1, 3).background().color()
    assert bg.name() == "#e7f5ff", f"当前行底色不是网页版那颗蓝：{bg.name()}"
    assert t.item(current - 1, 0).font().bold()
    if current < 27:
        assert not t.item(current, 0).font().bold(), "只有当前那一行该加粗"

    # 吻合度那一列逐行对服务端：null 写「—」（不能写 0，那是「一手都不能亏」），
    # 有值就带单位。两个分支都必须真的被扫到，否则整列写成同一个字也是绿。
    items = _get(host, shell, "/api/ranks")["items"]
    assert len(items) == 27
    for r, it in enumerate(items):
        acc = it.get("max_avg_loss_points")
        want = "—" if acc is None else f"\u2264 {acc:g} \u76ee/\u624b"
        assert t.item(r, 4).text() == want, \
            f"第 {r} 行吻合度写错：{t.item(r, 4).text()!r}，应为 {want!r}"
    assert any(it.get("max_avg_loss_points") is None for it in items)
    assert any(it.get("max_avg_loss_points") for it in items)
    assert t.item(0, 2).text().endswith("胜") and t.item(0, 3).text().endswith("连胜")
    assert "推演" in t.item(0, 6).text()
    assert "拟人" in t.item(0, 6).text(), "级位档该说清用的是拟人棋风"
    assert int(t.item(0, 5).text()) < int(t.item(26, 5).text()), "棋力参考没随段位递增"
    # 行高必须被内容撑开：开了 TextWordWrap 的格子中文字不折行就等于截断
    assert all(t.rowHeight(r) > 24 for r in range(27)), [t.rowHeight(r) for r in range(27)]
    H.snap(shell, "p3_08_ranks")


def test_ranks_engine_text_is_the_knobs_a_student_can_read():
    """引擎配置那一列：单位用「目」，为 0 的旋钮不写，零容差说成「只下最优点」。

    visits / human SL / ponder 是引擎术语（第 33 轮换成学员听得懂的说法）。
    """
    assert ranks_page.engine_text({"maxVisits": 48, "tolerance": 0,
                                   "localNoise": 0.8, "blunderRate": 0,
                                   "humanModel": True, "ponder": False}) \
        == "每手 48 次推演　·　只下最优点　·　只看局部 80%　·　拟人棋风"
    assert "失误率" not in ranks_page.engine_text({"maxVisits": 1, "blunderRate": 0})
    assert "pond" not in ranks_page.engine_text({"maxVisits": 1, "ponder": True})
    assert ranks_page.engine_text(None) == "每手 — 次推演　·　只下最优点"
    assert ranks_page.acc_text(0.8) == "≤ 0.8 目/手" and ranks_page.acc_text(None) == "—"


# ---------------------------------------------------------------- 界面完整性

def test_both_pages_have_no_clipped_text_and_no_page_scrollbar(shell, qapp, host):
    """两页各自把数据装满之后，再量一次「没被裁字 / 整页不用滚」。

    `test_pages.py` 那条只量了空数据态：题目标、统计、27 行段位表都没进来。
    """
    _login(shell, qapp)
    assert H.wait(qapp, lambda: shell.content_size().width() > 200, timeout=20.0)
    viewport = shell.content_size()

    page = _enter_tsumego(shell, qapp)
    _filter_kind(page, qapp, "capture")        # 目标子 + 最长题名都在这一类里
    assert H.wait(qapp, lambda: page.problemList.count() > 1, timeout=60.0)
    offenders, scanned = H.clipped_texts(page)
    assert scanned > 20, f"只扫到 {scanned} 个控件，等于没扫"
    assert not offenders, "死活页有文字被裁：" + "；".join(offenders)
    hint = page.sizeHint()
    assert hint.width() <= viewport.width() and hint.height() <= viewport.height(), \
        f"死活页装满数据后要滚：{hint.width()}x{hint.height()} vs 视口 " \
        f"{viewport.width()}x{viewport.height()}"
    # 侧栏自己滚是设计意图（四张卡 + 一百多题的列表，1280x800 装不下），
    # 真正不能妥协的是棋盘：它必须先拿够高度，否则侧栏一长棋盘就被挤成一条缝。
    board_min = page.boardView.minimumSizeHint().height()
    assert page.boardView.height() >= board_min, \
        f"棋盘被侧栏挤到 {page.boardView.height()}px，最低要 {board_min}px"
    assert page.boardView.width() >= board_min, "棋盘被挤得不是正方形了"

    shell.go("ranks")
    ranks = shell.current_page()
    assert H.wait(qapp, lambda: ranks.table.rowCount() == 27, timeout=30.0)
    offenders, scanned = H.clipped_texts(ranks)
    assert not offenders, "段位页有文字被裁：" + "；".join(offenders)
    hint = ranks.sizeHint()
    assert hint.width() <= viewport.width() and hint.height() <= viewport.height(), \
        f"段位页要滚：{hint.width()}x{hint.height()} vs {viewport.width()}x{viewport.height()}"
    # 表格自己滚是设计意图（27 行放不下），但横向一根都不许滚
    assert ranks.table.horizontalScrollBar().maximum() == 0, "段位表横向溢出了"


def test_mark_label_is_painted_clear_of_its_ring(shell, qapp, host):
    """判定圈的「正 / 错 / 应」必须画在圈外 —— 叠在圈上就成了缺笔画的坏字。

    看图抓出来的第 ① 个缺陷。判据不抄坐标公式（算第二遍就会与实现一起漂成同一个错）：
    圈的外沿从**只有圈那一张图**上量出来（绿心像素离圆心的最远距离），
    两张图的像素差就是字形占的地方，它们一个个都得在量出来的圈外。
    """
    _login(shell, qapp)
    page = _enter_tsumego(shell, qapp)
    board = page.boardView
    assert H.wait(qapp, lambda: board.width() > 100, timeout=20.0), "棋盘还没被摆进位置"
    # 挑离盘面中心最近的那个空点：靠边的点会把标签顶出控件，那是在测裁剪不是测间距
    mid = (len(page.board) - 1) / 2.0
    x, y = min(((px, py) for py in range(len(page.board)) for px in range(len(page.board))
                if page.board[py][px] == 0),
               key=lambda p: (p[0] - mid) ** 2 + (p[1] - mid) ** 2)
    cell = board.layout_now()[0]
    dpr = float(board.devicePixelRatioF())
    c = board.center(x, y)

    def dist(px, py):
        return math.hypot(px / dpr - c.x(), py / dpr - c.y())

    def box(img):
        lo = int(max(0, (c.x() - 2.6 * cell) * dpr))
        hi = int(min(img.width(), (c.x() + 2.6 * cell) * dpr))
        t0 = int(max(0, (c.y() - 2.6 * cell) * dpr))
        t1 = int(min(img.height(), (c.y() + 2.6 * cell) * dpr))
        return lo, hi, t0, t1

    def scan(img):
        """返回 `(圈外径, 字形像素的最内沿, 实墨像素的最内沿)`，都按逻辑像素算。

        两个量必须分开取图：圈外径只从**没有字的那一张**上量 —— 字与圈同一个绿色，
        在两张图上都按「绿心压倒木色」扫会把字的外角量成「圈外径」（首跑就红在这里：
        量出 73px，而圈只有 36px）。字形则拿与对照图的差判，圈在两张图里一模一样，
        差出来的只可能是字。

        圈外径按绿心压倒木色判（g 同时大于 r 与 b）而不按色距：抗锯齿的半透边缘
        颜色差很大，拿色距卡会只量到笔画中段、把圈量小。
        """
        lo, hi, t0, t1 = box(img)
        ring_out, glyph_in, ink_in = 0.0, 1e9, 1e9
        for py in range(t0, t1):
            for px in range(lo, hi):
                pa, pb = base.pixelColor(px, py), img.pixelColor(px, py)
                if pa.green() > pa.red() and pa.green() > pa.blue():
                    ring_out = max(ring_out, dist(px, py))      # 对照图上只有圈
                d = H.color_distance(pa, pb)
                if d > 24:
                    glyph_in = min(glyph_in, dist(px, py))
                if d > 120:                       # 看得清的墨，不含抗锯齿那层淡边
                    ink_in = min(ink_in, dist(px, py))
        return ring_out, glyph_in, ink_in

    board.set_props(marks=[{"x": x, "y": y, "kind": "good"}])
    qapp.processEvents()
    base = H.grab_image(board)                            # 只有圈，没有字
    board.set_props(marks=[{"x": x, "y": y, "kind": "good", "label": "正"}])
    qapp.processEvents()
    ring_out, glyph_in, ink_in = scan(H.grab_image(board))   # 圈 + 字
    board.set_props(marks=[])
    assert ring_out > 0, "对照图上没量到判定圈（这张图无法说明任何问题）"
    assert ink_in < 1e9, f"标了「正」却没画出任何像素：({x}, {y}) 处两图完全一样"
    # 只断实墨：抗锯齿那层淡边伸到哪都不影响阅读，拿它卡几何会把测试跑成神经病
    assert ink_in > ring_out, \
        f"字形最内沿 {ink_in:.1f}px 压在圈上（圈外径 {ring_out:.1f}px），两个记号都读不出来"
    assert glyph_in > ring_out - 2.0, \
        f"字形的淡边也越过了圈 {ring_out - glyph_in:.1f}px，位置得再往外推"


def test_sidebar_always_ends_up_showing_the_verdict(shell, qapp, host):
    """侧栏滚到底再点题 / 再落一子，判定框都得回到可视区。

    看图抓出来的第 ② 个缺陷：题库列表在侧栏底部，点它一行会让 QScrollArea 跟着
    焦点把整条侧栏滚下去，「✓ 正解！」跑到可视区外 —— 棋盘上圈画了、声音响了，
    结论就是看不见。
    """
    _login(shell, qapp)
    page = _enter_tsumego(shell, qapp)
    sb = page.scroll.verticalScrollBar()
    vp = page.scroll.viewport()
    assert H.wait(qapp, lambda: sb.maximum() > 0, timeout=20.0), "侧栏不用滚，这条测试失去意义"

    def verdict_shown() -> bool:
        top = page.resultBar.mapTo(vp, QPoint(0, 0)).y()
        return page.resultBar.isVisible() and top >= 0 \
            and top + page.resultBar.height() <= vp.height()

    _filter_kind(page, qapp, "life")
    # 挑一道**不是当前题**的：否则 `_click_row` 里那个「current 变了没有」的等待会当场
    # 通过（它本来就是这一题），点下去没换题、侧栏也不会回顶，测试就白跑一趟
    pid = next(it["id"] for it in page.items
               if it["id"] != (page.current or {}).get("id"))
    sb.setValue(sb.maximum())                       # 用户翻到侧栏底部挑题
    _click_row(page, qapp, pid)
    assert H.wait(qapp, lambda: sb.value() == 0, timeout=10.0), \
        f"点完题侧栏没回到顶部（value={sb.value()}），题目卡被顶出去了"

    x, y = _correct_move(host, shell, pid)
    sb.setValue(sb.maximum())                       # 落子前再滚下去一次
    _click_point(page, qapp, x, y)
    res = _wait_result(page, qapp, pid)
    assert res["status"] == SOLVED, res
    assert H.wait(qapp, verdict_shown, timeout=10.0), \
        f"解出之后判定框没回到可视区：value={sb.value()}/{sb.maximum()}"


def test_source_note_stays_on_one_line(shell, qapp, host):
    """「出处」整库同文且很长：卡上只留一行，全文进 toolTip。

    看图抓出来的第 ③ 个缺陷：那段版权说明在「本题」卡里换行铺开要占 5 行，
    把判定框顶出可视区 —— 而且每题重复一遍，没有一句是题目信息。
    """
    _login(shell, qapp)
    page = _enter_tsumego(shell, qapp)
    lab = page.problemRows["source"]
    full = (page.current or {}).get("source") or ""
    assert len(full) > 40, f"这道题的出处太短，验不出截断：{full!r}"
    assert H.wait(qapp, lambda: lab.width() > 60, timeout=20.0), "出处行没被摆进位置"
    assert lab.fullText() == full, "侧栏显示的出处不是服务端给的那一段"
    assert lab.toolTip() == full, "全文没进 toolTip：截掉的那几行永久丢了"
    shown = lab.text()
    assert shown == full or shown.endswith("…"), f"既没装下也没打省略号：{shown!r}"
    fm = lab.fontMetrics()
    assert fm.horizontalAdvance(shown) <= lab.width(), \
        f"显示串 {fm.horizontalAdvance(shown)}px 仍比控件 {lab.width()}px 宽（被硬裁）"
    assert lab.height() <= fm.height() * 2, \
        f"出处占了 {lab.height()}px 高（一行 {fm.height()}px），不是单行"


def test_filters_live_in_the_card_they_act_on(shell, qapp, host):
    """筛选条住在「题库」卡里、列表上方 —— 它改的就是这个列表。

    看图抓出来的第 ⑤ 个缺陷：它原先长在「练习进度」卡底部，看上去像在筛统计，
    可统计（已解出 / 各题型 / 各档位）不受筛选影响 —— 名字与位置都在骗人。
    """
    _login(shell, qapp)
    page = _enter_tsumego(shell, qapp)
    for w in (page.cbKind, page.cbGoal, page.cbTier, page.chkUnsolved,
              page.problemList, page.countBadge):
        assert page.libraryPanel.isAncestorOf(w), \
            f"{type(w).__name__} 不在题库卡里，跟列表分了家"
    first_stat = page.progressGrid.itemAtPosition(0, 0)
    assert first_stat is not None and not page.libraryPanel.isAncestorOf(first_stat.widget()), \
        "统计数字被卷进了题库卡"


def test_no_markdown_markers_reach_the_screen(shell, qapp, host):
    """后端的讲解是按 Markdown 写的，控件是纯文本渲染：星号必须洗掉。

    看图抓出来的第 ⑥ 个缺陷：吃子题的判定框里那句「不用弃子，靠**收紧对方的气**
    吃掉它」把四颗星号原样摆在了学员面前（网页版同一屏也一样，它冻结了）。
    """
    _login(shell, qapp)
    page = _enter_tsumego(shell, qapp)
    _filter_kind(page, qapp, "capture")
    # 先拿服务端原文确认**确实**下发了带记号的文案，否则整条测试是空转
    pid = None
    for row in page.items[:10]:
        blob = json.dumps(_get(host, shell, f"/api/tsumego/{row['id']}/solution"),
                          ensure_ascii=False)
        if "**" in blob:
            pid = row["id"]
            break
    assert pid, "服务端已经不发带 Markdown 记号的讲解了，这条测试该跟着洗记号那段一起重看"
    _click_row(page, qapp, pid)
    QTest.mouseClick(page.btnAnswer, Qt.LeftButton)
    assert H.wait(qapp, lambda: page.solution is not None, timeout=60.0), "答案没打开"
    x, y = _correct_move(host, shell, pid)
    _click_point(page, qapp, x, y)
    assert _wait_result(page, qapp, pid)["status"] == SOLVED

    labs = [w for w in page.findChildren(QLabel) if w.isVisible() and w.text()]
    assert len(labs) > 12, f"只扫到 {len(labs)} 个有字的标签，等于没扫"
    leaky = [w.text()[:44] for w in labs if "**" in w.text() or "`" in w.text()]
    assert not leaky, "屏幕上有没洗掉的 Markdown 记号：" + "；".join(leaky)
    rows = [page.problemList.item(i).text() for i in range(page.problemList.count())]
    assert not any("**" in t for t in rows), "题库列表行里还带着星号"
    H.snap(shell, "p3_09_no_markdown")


def test_plain_only_peels_markers_off_prose():
    """`plain()` 只拆成对记号：「2*3」这种带星号的数学不能被啃掉一半。"""
    assert tsumego_page.plain("靠**收紧对方的气**吃掉它") == "靠收紧对方的气吃掉它"
    assert tsumego_page.plain("公气是*共同命脉*") == "公气是共同命脉"
    assert tsumego_page.plain("用 `scoreBefore` 说话") == "用 scoreBefore 说话"
    assert tsumego_page.plain("黑 2*3 的矩形") == "黑 2*3 的矩形"
    assert tsumego_page.plain("") == "" and tsumego_page.plain(None) is None
    # 洗在数据入口：字段名对不上就洗不到，PROSE_KEYS 得跟着契约走
    cleaned = tsumego_page.clean({"note": "要**点**", "id": "a**b", "lines": [{"comment": "x**y**z"}]})
    assert cleaned["note"] == "要点" and cleaned["lines"][0]["comment"] == "xyz"
    assert cleaned["id"] == "a**b", "非散文字段被动过：判定与请求都会对不上后端"


def test_progress_rows_explain_the_number_they_cannot_name(shell, qapp, host):
    """「答对率」就是 `solved / attempts`：名字得与算法对得上，口径得写在 toolTip 里。

    看图抓出来的第 ④ 个缺陷：网页版把这个比值叫「一次做对率」（TsumegoPage.tsx:493），
    可它跟「一次」无关 —— 看过答案再做对也算。原生端不照抄这个名字。
    """
    _login(shell, qapp)
    page = _enter_tsumego(shell, qapp)
    assert H.wait(qapp, lambda: page.summary is not None, timeout=30.0)
    grid = page.progressGrid
    cells = {}
    for row in range(grid.rowCount()):
        k, v = grid.itemAtPosition(row, 0), grid.itemAtPosition(row, 1)
        if k is not None and v is not None:
            cells[k.widget().text()] = (k.widget(), v.widget())
    assert "一次做对率" not in cells, f"又用回了那个对不上算法的名字：{list(cells)}"
    assert "答对率" in cells, f"进度卡里没有答对率这一行：{list(cells)}"
    s = page.summary
    acc = s.get("solveRate")
    _k, v = cells["答对率"]
    if not s.get("attempts"):
        assert v.text() == "—", f"一题没做却报了百分比：{v.text()!r}"
    else:
        assert abs(acc - s["solved"] / s["attempts"]) <= 0.001, \
            f"服务端 solveRate={acc} 不是 solved/attempts"
        assert v.text().endswith("%"), f"答对率没按百分比显示：{v.text()!r}"
        assert abs(float(v.text()[:-1]) / 100 - acc) <= 0.005, \
            f"答对率 {v.text()!r} 对不上服务端的 solveRate={acc}"
    for w in cells["答对率"]:
        assert "÷" in w.toolTip() and w.toolTip() == cells["答对率"][0].toolTip(), \
            "标题与数值没共用同一句口径说明"
        assert "一次做对" in w.toolTip(), f"toolTip 没说清什么时候才是 100%：{w.toolTip()!r}"


# ---------------------------------------------------------------- 自动下一题

def _solve_current(host, shell, page, qapp) -> str:
    """把手上这道题做对，返回题号。"""
    pid = str(page.current["id"])
    x, y = _correct_move(host, shell, pid)
    _click_point(page, qapp, x, y)
    res = _wait_result(page, qapp, pid)
    assert res and res.get("status") == SOLVED, res
    return pid


def test_solving_a_problem_auto_advances_to_the_next_one(shell, qapp, host):
    """用户要的：「题目做完，自动开始下一题」。

    答对后不再需要点「下一题」—— 等 `AUTO_NEXT_MS` 自己换。等待时间要够看完
    「✓ 正解！」与棋盘上的正解圈（这也是它不能是 0 的原因）。
    """
    _login(shell, qapp, prefix="an")
    page = _enter_tsumego(shell, qapp)
    page.chkAutoNext.setChecked(True)
    pid = _solve_current(host, shell, page, qapp)
    assert page.current["id"] == pid, "前置条件：答对那一刻还停在这一题"

    ok = H.wait(qapp, lambda: page.current and str(page.current["id"]) != pid, timeout=15.0)
    assert ok, f"答对后没有自动下一题，还停在 {page.current.get('id')}"
    assert page.result is None and not page.moves, "新题带着上一题的判定/手顺（残影）"


def test_auto_next_stays_off_when_unchecked_and_can_be_switched_back(shell, qapp, host):
    """关掉之后必须**真的不动**，并且开关要落盘（下一轮进来还是关着的）。"""
    _login(shell, qapp, prefix="of")
    page = _enter_tsumego(shell, qapp)          # `_enter_tsumego` 已关掉自动换题
    assert page.chkAutoNext.isChecked() is False
    pid = _solve_current(host, shell, page, qapp)

    H.settle(qapp, tsumego_page.AUTO_NEXT_MS / 1000.0 + 1.2)   # 等过闹钟时间
    assert str(page.current["id"]) == pid, "关着还自动换题"
    assert shell._prefs.auto_next is False, "开关没落盘"
    assert Prefs(shell._prefs.path).auto_next is False, "重新读盘还是开的"


def test_redo_or_answer_cancels_the_pending_auto_next(shell, qapp, host):
    """用户一动就取消：正要重做/看答案，题被换走是最烦的那种「自作聪明」。"""
    _login(shell, qapp, prefix="cx")
    page = _enter_tsumego(shell, qapp)
    page.chkAutoNext.setChecked(True)
    pid = _solve_current(host, shell, page, qapp)

    QTest.mouseClick(page.btnRedo, Qt.LeftButton)     # 闹钟还差 1.6 秒
    H.settle(qapp, tsumego_page.AUTO_NEXT_MS / 1000.0 + 1.0)
    assert str(page.current["id"]) == pid, "点了重做还是被换走了"
    assert page.result is None, "重做没清掉判定"


def test_a_wrong_answer_does_not_auto_advance(shell, qapp, host):
    """答错不自动换：那一步还要接着想/重做，换题等于把错误答案盖过去。"""
    _login(shell, qapp, prefix="wr")
    page = _enter_tsumego(shell, qapp)
    page.chkAutoNext.setChecked(True)
    pid = str(page.current["id"])
    x, y = _wrong_move(host, shell, pid)
    _click_point(page, qapp, x, y)
    res = _wait_result(page, qapp, pid)
    assert res and res.get("status") == FAILED, res

    H.settle(qapp, tsumego_page.AUTO_NEXT_MS / 1000.0 + 1.0)
    assert str(page.current["id"]) == pid, "答错也自动换题了"
