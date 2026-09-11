"""复盘页的验收：喂一份**合成**的复盘报告进界面，逐条钉住会算错的口径。

为什么不打后端：这一页真正容易错的是**派生与渲染**（ply 对齐、走子方视角、
只标我方还在盘上的问题手、Markdown 洗没洗干净），这些用一份手写的报告能钉得
比真棋更死 —— 我可以故意摆一颗「第 3 手落下、第 8 手被提掉」的子，而真实对局里
想碰上这个形状全凭运气。真数据那一头由 `test_game_flow_e2e.py` 的复盘段覆盖。

计划对 P4 的验收口径：曲线点数、降级文本不含 `**`、「离你这手」不许指到 AI 手、
我读 2 张截图。前几条在这里与 e2e 各钉一遍，截图落在 artifacts/p4_0*_*.png。
"""
from __future__ import annotations

import math

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel

from core.api import ApiError, Reply
from ui.pages import review as rv
from tests import harness as H

BLACK, WHITE = 1, 2
SIZE = 9
GID = "revtest01"

# 手顺表：(color, x, y, gtp, flag, loss, wr_before, wr_after, score_before, score_after)
#
# 形状是**故意**造出来的，不是为了像真棋：
#   · 黑 (2,2) 第 3 手落下、第 8 手被白提掉 → 逼出「已被提掉的子不再画圈」那条规则；
#   · 第 8 手是 **AI** 的大恶手 → 逼出「圈只标我方」那条规则；
#   · 第 5 手之后那份分析标 missing → 逼出「这一手没有引擎分析数据」的降级文案。
PLAN = [
    (BLACK, 4, 4, "e5", "good", 0.1, 0.52, 0.55, 1.2, 1.6),
    (WHITE, 1, 2, "b7", "good", 0.3, 0.50, 0.47, -0.8, -1.1),
    (BLACK, 2, 2, "c7", "slow", 2.4, 0.4855, 0.44, -1.2, -3.6),
    (WHITE, 3, 2, "d7", "good", 0.2, 0.51, 0.50, 0.5, 0.3),
    (BLACK, 6, 6, "g3", "good", 0.4, 0.53, 0.51, 1.0, 0.6),
    (WHITE, 2, 3, "c6", "good", 0.1, 0.49, 0.50, -0.4, -0.2),
    (BLACK, 5, 5, "f4", "bad", 4.6, 0.50, 0.39, 0.7, -3.9),
    (WHITE, 2, 1, "c8", "blunder", 6.2, 0.45, 0.31, 5.5, 11.7),
    (BLACK, 0, 0, "a9", "good", 0.2, 0.55, 0.56, -1.1, -0.9),
    (WHITE, 8, 8, "j1", "good", 0.3, 0.47, 0.45, 2.2, 1.9),
    (BLACK, 0, 8, "a1", "slow", 2.9, 0.52, 0.48, -1.5, -4.4),
    (WHITE, 8, 0, "j9", "good", 0.5, 0.44, 0.46, 3.1, 3.6),
]
FLAG_LABEL = {"good": "好手", "slow": "缓手", "bad": "恶手", "blunder": "大恶手"}
#: 带讲解的手。后端只给 slow/bad/blunder 生成 comment（worker.py:335），
#: 好手没有 —— 于是卡片有两条分支，两条都得有数据走到。
COMMENTED = {
    3: {
        "reason": "**方向**跑偏了：左上那颗白子还没安定，你先在右边动手。"
                  "具体数据：损失 2.4 目；你的落点在引擎候选里排第 3，前排还有多处更优。",
        "advice": "先在`左上`逼住，边攻边围；离你这手 3 路（往左上方）。",
        "maxim": "入界宜缓",
        "source": "llm",
    },
    7: {
        "reason": "（中盘阶段）这一手是恶手，断送了先手。",
        "advice": "引擎首选 e3，离你这手 2 路（往右上方）。攻要连续。",
        "maxim": "攻其所必救",
        "source": "template",
    },
    8: {   # AI 的手：人称必须是「白方」（commentary.py:365 那个 who）
        "reason": "（中盘阶段）白方这一手是大恶手，撞上去反而把自己收紧了。",
        "advice": "引擎首选 e5，离白方这手 4 路（往左下方），该点胜率 61.3%；"
                  "它的后续大致是 e5 d5。想想这手想成什么事——"
                  "它要成的那个，正是你该提前破坏的地方。",
        "maxim": "敌之要点即我之要点",
        "source": "template",
    },
    11: {  # 正文里带 `<`：不转义就被当成标签起始，整句后半截静默消失
        "reason": "官子算小了：先手 1 目 < 后手 3 目，这里应当先手收官。",
        "advice": "从二路扳，能便宜 2 目。",
        "maxim": "官子无大小，先手为大",
        "source": "llm",
    },
}


def _moves():
    out = []
    for i, (color, x, y, gtp, *_rest) in enumerate(PLAN, start=1):
        item = {"num": i, "color": color, "x": x, "y": y, "gtp": gtp,
                "passTurn": False, "captures": [], "timeUsed": 4200}
        if i == 8:
            item["captures"] = [[2, 2]]       # 提掉黑第 3 手那颗
        out.append(item)
    return out


def _analyses():
    """analyses[i] = 第 i 手**之后**的局面，长度 = 手数 + 1。"""
    out = [{"ply": 0, "winrateBlack": 0.52, "winrateWhite": 0.48, "scoreLead": 1.2,
            "visits": 1200, "candidates": [], "topMove": "e5"}]
    for i, (_c, _x, _y, _g, _f, _l, _wb, wa, _sb, sa) in enumerate(PLAN, start=1):
        if i == 5:
            out.append({"ply": i, "missing": True})   # 第 5 手之后没取到分析
            continue
        out.append({"ply": i, "winrateBlack": wa, "winrateWhite": round(1 - wa, 4),
                    "scoreLead": sa, "visits": 1200, "candidates": [], "topMove": "e3"})
    return out


def _report_moves():
    out = []
    for i, (color, x, y, gtp, flag, loss, wb, wa, sb, sa) in enumerate(PLAN, start=1):
        item = {
            "moveNum": i, "ply": i, "color": color, "isPlayer": color == BLACK,
            "x": x, "y": y, "gtp": gtp, "captures": 1 if i == 8 else 0,
            "winrateBefore": wb, "winrateAfter": wa,
            "scoreBefore": sb, "scoreAfter": sa,
            "lossPoints": loss, "lossWinrate": round(max(0.0, wb - wa), 4),
            "flag": flag, "flagLabel": FLAG_LABEL[flag],
            "bestMove": {"gtp": "e3", "x": 4, "y": 3, "winrate": 0.613,
                         "pv": ["e3", "d5", "c4"], "pvPoints": [[4, 3], [3, 4], [2, 4]]},
            "playerRank": 2 if color == BLACK else None,
            "variation": [[4, 3], [3, 4], [2, 4]],
            "variationGtp": ["e3", "d5", "c4"],
        }
        if i in COMMENTED:
            item["comment"] = dict(COMMENTED[i])
        out.append(item)
    return out


def _curve():
    """复盘时按 review_visits 重算的一版曲线：ply 0..手数，**黑视角**。"""
    out = []
    for i in range(len(PLAN) + 1):
        wr = round(0.40 + 0.03 * math.sin(i / 2.0), 4)
        out.append({"ply": i, "winrateBlack": wr, "winrateWhite": round(1 - wr, 4),
                    "scoreLead": round(3.0 - 0.4 * i, 2), "visits": 1400})
    return out


def payload(**over):
    """整包 `GET /api/reviews/{id}` 的响应。`over` 改局部，免在每个测试里抄一遍。"""
    data = {
        "meta": {"gameId": GID, "size": SIZE, "komi": 7.0, "handicap": 0,
                 "playerColor": BLACK, "playerColorName": "黑", "rankName": "12级",
                 "aiName": "小棋", "aiTitle": "棋士", "resultText": "黑中盘胜",
                 "moveCount": len(PLAN), "reviewStatus": "done", "reviewProgress": 1.0,
                 "reviewStage": "done", "reviewDetail": "已完成", "reviewError": None,
                 "avgLossPoints": 0.62, "engine": "katago"},
        "report": {
            "version": 1, "engine": "katago", "lowConfidence": False, "visits": 1200,
            "playerColor": BLACK, "totalMoves": len(PLAN), "resultText": "黑中盘胜",
            "avgLossPoints": 0.836, "aiAvgLossPoints": 0.124,
            "counts": {"blunder": 0, "bad": 1, "slow": 2, "good": 9, "pass": 0},
            "phases": {}, "keyMoves": [], "moments": [],
            "curve": _curve(), "moves": _report_moves(),
            "summary": {
                "overall": "### 整局总评\n这盘**棋力**在线，中盘那几手方向偏了；"
                           "官子阶段先手意识不足。\n- 要点一：攻棋要连续",
                "opening": "布局双方平稳。", "middle": "中盘一手恶手把优势送了出去。",
                "endgame": "1. 官子再练练。",
                "training": ["多练中盘攻杀的连续性", "官子大小顺序题 20 道"],
                "maxim": "势孤取和"},
            "llm": {"used": False, "model": None, "configured": False,
                    "error": "未配置大模型 API Key（设置页可填写），本报告由引擎数据直接生成"},
        },
        "moves": _moves(),
        "analyses": _analyses(),
        "sgf": "(;SZ[9]GM[1]FF[4]B[]W[];)\n",
    }
    data.update(over)
    return data


#: 一份「像真模板产出的」总评：用户截图里那种长段 + 长句，专门用来验排版。
REAL_SUMMARY = {
    "overall": "本局结果：白胜（识路少年 投子认输）。你的吻合度为平均每手损失 6.2 目，"
               "属于「需要系统训练」；AI 为 6.9 目。全局共 18 次大恶手、14 次恶手、"
               "26 次缓手，其中恶手以上 32 手。把 58 处问题手按类型拆开看，最多的是"
               "「官子大小」（42 处）——这是本局比较固定的思维误区，比偶尔的一两手大漏"
               "更值得练。全局起伏最大的是你的第 132 手（O6）：黑方胜率当场升 55.4 "
               "个百分点，对你而言是丢分，第 132 手前后就是本局的胜负分界。",
    "opening": "布局阶段共 19 手，平均每手损失 2.6 目。攒了若干缓手，主要是大小与先手的判断。"
               "其中第 24 手损失最大（6.6 目），属于综合类问题，这一段累计损失 49.0 目。",
    "middle": "中盘阶段共 36 手，平均每手损失 6.5 目。损失偏大，建议先补基础棋形与死活。"
              "其中第 74 手损失最大（24.0 目），属于弃取（贪吃）类问题，这一段累计损失 235.9 目。",
    "endgame": "官子阶段共 22 手，平均每手损失 8.8 目。损失偏大，建议先补基础棋形与死活。"
               "其中第 146 手损失最大（19.5 目），属于官子大小类问题，这一段累计损失 193.2 目。",
    "training": [
        "练「战场方向」：落子前先花三秒问自己——对方下一手最想走哪里？本局有 6 手偏在这条判断上。",
        "练弃取：提子前先问“提完之后我得到的目，比对方得到的大吗”；"
        "配合做“吃子手筋”题型，把收气与倒扑算清。",
        "练局部手段：同一带里把扳/虎/跳/粘几种应法都算一遍再选，"
        "死活练习页的「吃子手筋」与「对杀」题型就是练这个的。",
        "练次序：先把先手交换完再补棋。悔棋一步、看看换个次序后胜率如何，"
        "是比多做两道题更快的改法。",
        "练官子：从 9 路小官子题开始，掌握先手官子与后手官子的价值排序。",
    ],
    "maxim": "官子二目半，先手抵三目",
}
REAL_PHASES = {
    "opening": {"moves": 19, "avgLoss": 2.6, "worstMoveNum": 24, "worstLoss": 6.6,
                "totalLoss": 49.0},
    "middle": {"moves": 36, "avgLoss": 6.5, "worstMoveNum": 74, "worstLoss": 24.0,
               "totalLoss": 235.9},
    "endgame": {"moves": 22, "avgLoss": 8.8, "worstMoveNum": 146, "worstLoss": 19.5,
                "totalLoss": 193.2},
}


def real_payload():
    """模板产出的满配总评（含结构化 phases）。"""
    data = payload()
    rep = data["report"]
    rep["summary"] = dict(REAL_SUMMARY)
    rep["phases"] = dict(REAL_PHASES)
    rep["avgLossPoints"] = 6.21
    rep["aiAvgLossPoints"] = 6.86
    rep["totalMoves"] = 154
    rep["counts"] = {"blunder": 18, "bad": 14, "slow": 26, "good": 19, "pass": 0}
    rep["visits"] = 96
    rep["resultText"] = "白胜（识路少年 投子认输）"
    return data


class FakeApi:
    """按 (动词, 路径) 查表的假客户端。

    回包一律延到**下一轮事件循环**（`QTimer.singleShot(0)`），与真客户端的跨线程
    投递同一时序：测试因此必须 `H.wait`，不能拿「调完就断言」的写法糊过去 ——
    那正好是真异步代码会漏掉的窗口。键里带动词，是因为重跑走的是
    `POST /api/reviews/{id}`，与 GET 同路径，只按路径查表两者会互相顶掉。
    """

    def __init__(self, replies=None):
        self.calls = []
        self.replies = dict(replies or {})

    def _reply(self, verb, path):
        self.calls.append((verb, path))
        r = Reply()
        data, error = self.replies.get((verb, path),
                                       (None, ApiError(f"没有这个桩：{verb} {path}", 404)))
        QTimer.singleShot(0, lambda: r.finished.emit(data, error))
        self.replies.setdefault(("_keep", id(r)), (r, None))   # 投递前别被析构
        return r

    def get(self, path, query=None, timeout=20.0):
        return self._reply("GET", path)

    def get_text(self, path, timeout=30.0):
        return self._reply("GET", path)

    def post(self, path, body=None, query=None, timeout=20.0):
        return self._reply("POST", path)

    def count(self, verb, path):
        return sum(1 for c in self.calls if c == (verb, path))


class FakeSound:
    """复盘完成那一声要能被数出来，而不指望测试机上真有声卡。"""

    def __init__(self):
        self.played = []

    def play(self, name):
        self.played.append(name)
        return True


@pytest.fixture
def page(qapp):
    w = rv.ReviewPage(FakeApi(), FakeSound())
    # 宽度取真实外壳的内容宽（1280 窗口 - 168 导航 - 边框），别用 1280：
    # 用宽了，被裁的文字会在截图与 `clipped_texts` 里都装得下，验收就成了假的。
    w.resize(1104, 760)
    w.show()
    qapp.processEvents()
    yield w
    w.shutdown()
    w.close()
    w.deleteLater()
    qapp.processEvents()


def load(qapp, pg, data):
    pg.game_id = GID
    pg._on_payload(data, None)
    qapp.processEvents()


def labels(root):
    """可见且有字的标签。**用 `labels` 而不是 `findChildren` 的原始结果**：
    隐藏的面板里的标签文字也在，断言「界面上不该出现 X」时被隐藏的东西不算数。"""
    return [w.text() for w in root.findChildren(QLabel) if w.isVisible() and w.text().strip()]


def card_text(page):
    return " ".join(labels(page.cardPanel))


def goto(qapp, pg, ply):
    """切一手，然后**转一轮事件循环**。

    不转就会读到「界面是空的」这个假阴性：复盘页的卡片与总评是每次重画时
    **新建**控件填进布局的，而 `qapp` 夹具装了全应用 QSS —— 新控件要等一次
    样式/布局激活才算 `isVisible()`。拿裸 QApplication 不装 QSS 复现不出来，
    所以这个坑只在测试里踩得到（踩过一次：七条卡片断言一起红）。"""
    pg.set_ply(ply)
    qapp.processEvents()


def one_label(root, needle):
    hits = [t for t in labels(root) if needle in t]
    assert hits, (f"界面里找不到含 {needle!r} 的标签（容器可见={root.isVisible()}，"
                  f"共 {len(root.findChildren(QLabel))} 个标签）：{labels(root)}")
    return hits[0]


def badge_style_of(root, text):
    for w in root.findChildren(QLabel):
        if w.text() == text and w.isVisible():
            return w.styleSheet()
    raise AssertionError(f"找不到文字正好是 {text!r} 的可见标签")


# ------------------------------------------------------------------ 纯函数

def test_pct_matches_javascript_tofixed_not_pythons_round():
    """0.4855 → 「48.6%」。Python 的 round 是 half-even，而 48.55 在二进制里偏小，
    直接 round 会给 48.5%，与网页版 toFixed（half-up）差一格 —— 同一份数据两个界面
    报两个数，学员会以为程序在猜。"""
    assert rv.pct(0.4855) == "48.6%"
    assert rv.pct(0.5) == "50.0%"
    assert rv.pct(0.0455) == "4.6%"
    assert rv.pct(0.44, 0) == "44%"          # 逐手列表那一列不留小数
    assert rv.pct(None) == "—"


def test_num_and_loss_color_are_bounded():
    assert rv.num(None) == "—"
    assert rv.num(0.836, 2) == "0.84"
    assert rv.num(-3.9, 1) == "-3.9"
    assert rv.loss_color(6.2) == "#c92a2a"
    assert rv.loss_color(2.4) == "#e8590c"
    assert rv.loss_color(0.1) == "#495057"


def test_err_text_prefers_the_message_and_survives_odd_objects():
    assert rv.err_text(ApiError("复盘不存在", 404)) == "复盘不存在"
    assert rv.err_text(None) == ""
    assert rv.err_text(ValueError("别的异常也要能印出来")) == "别的异常也要能印出来"


def test_prose_washes_line_heads_but_not_mid_line_marks():
    """`ui/text.py` 把「行首记号留给复盘页」，这条是那笔债的收据。"""
    assert rv.prose("### 整局总评") == "整局总评"
    assert rv.prose("- 要点一") == "要点一"
    assert rv.prose("1. 官子再练练。") == "官子再练练。"
    assert rv.prose("> 引用一句") == "引用一句"
    assert rv.prose("这盘**很稳**") == "这盘很稳"
    assert rv.prose("先手 1 目 < 后手 3 目") == "先手 1 目 < 后手 3 目"
    assert rv.prose("损失 2*3 目") == "损失 2*3 目"      # 行中间的星号碰不得
    assert rv.prose("") == ""


def test_drop_lead_eats_only_a_duplicated_heading():
    """面板标题已经写着「整局总评」，正文再跟一句就重复了。"""
    assert rv.drop_lead("整局总评：这盘很稳", "整局总评") == "这盘很稳"
    assert rv.drop_lead("整局总评\n这盘很稳", "整局总评") == "这盘很稳"
    assert rv.drop_lead("这盘很稳", "整局总评") == "这盘很稳"
    assert rv.drop_lead("整局总评很重要", "整局总评") == "整局总评很重要"
    assert rv.drop_lead("整局总评", "整局总评") == "整局总评"   # 剥完就空了，不如不剥
    assert rv.drop_lead("", "整局总评") == ""


def test_split_sentences_keeps_the_punctuation_and_drops_blanks():
    """一句话一行是这次排版的地基：切错了就是屏幕上多一个断句。"""
    assert rv.split_sentences("甲。乙；丙") == ["甲。", "乙；", "丙"]
    assert rv.split_sentences("甲。\n\n乙。") == ["甲。", "乙。"]
    assert rv.split_sentences("") == []
    assert rv.split_sentences("   ") == []
    assert rv.split_sentences("6.2 目不是句号") == ["6.2 目不是句号"]   # 小数点不断句


def test_split_training_only_splits_a_short_lead():
    assert rv.split_training("练弃取：提子前先问") == ("练弃取", "提子前先问")
    assert rv.split_training("没有冒号") == ("", "没有冒号")
    # 冒号出现得太晚（像正文里的冒号）→ 不拆，整句当正文
    late = "这一手真正的问题其实并不在次序上而是在方向：先该压住左上"
    assert rv.split_training(late) == ("", late)


def test_phase_tone_strips_only_the_template_head():
    """只有认得出模板句式才剥数字流水账；认错就会把评语吃掉。"""
    t = "布局阶段共 19 手，平均每手损失 2.6 目。整体平稳，个别地方还可以再紧凑。"
    assert rv.phase_tone(t) == "整体平稳，个别地方还可以再紧凑。"
    prose_only = "这盘棋布局阶段双方都很平稳，没什么好说的。"
    assert rv.phase_tone(prose_only) == prose_only
    assert rv.phase_tone("") == ""


def test_first_clause_stops_at_the_first_punctuation():
    assert rv.first_clause("整体平稳，个别地方还可以再紧凑。") == "整体平稳"
    assert rv.first_clause("损失偏大。") == "损失偏大"
    assert rv.first_clause("没有标点") == "没有标点"


def test_pivot_sentence_moves_the_deciding_sentence_out():
    sents = ["甲。", "第 132 手前后就是本局的胜负分界。", "乙。"]
    pivot, rest = rv.pivot_sentence(sents)
    assert "胜负分界" in pivot and rest == ["甲。", "乙。"]
    assert rv.pivot_sentence(["甲。"]) == ("", ["甲。"])


def test_clean_washes_only_prose_fields():
    data = {"summary": {"overall": "**总评**", "moves": 12, "training": ["- a", "**b**"]},
            "moves": [{"gtp": "**e5**", "reason": "*为什么*"}]}
    rv.clean(data)
    assert data["summary"]["overall"] == "总评"
    assert data["summary"]["training"] == ["a", "b"]
    assert data["summary"]["moves"] == 12          # 数字不该被碰
    # 嵌套里的 gtp 不是散文字段：洗了就把坐标洗坏了
    assert data["moves"][0]["gtp"] == "**e5**"


def test_flag_badge_style_falls_back_to_grey_not_the_accent_blue():
    """认不出的评级落到「虚手」那套灰：主色蓝在本应用里是「可点」的信号。"""
    assert "background: #fff9db" in rv.theme.flag_badge_style("slow")
    assert rv.theme.flag_badge_style("没见过的评级") == rv.theme.flag_badge_style("pass")
    assert "#4263eb" not in rv.theme.flag_badge_style("没见过的评级")


# ------------------------------------------------------------------ 渲染

def test_a_report_lands_on_every_panel(page, qapp):
    load(qapp, page, payload())
    assert page.chartPanel.isVisible()
    assert page.summaryPanel.isVisible()
    assert page.listPanel.isVisible()
    # 报告一到手就停在我方损失最大的那一手（第 7 手，-4.6 目），不是第 0 手的空盘
    assert page.plyBadge.text() == f"第 7 / {len(PLAN)} 手"
    assert page.cardPanel.isVisible()
    assert "12级" in page.headTitle.text(), page.headTitle.text()
    assert "我执黑" in page.headSub.text()
    assert one_label(page.cardPanel, "第 7 手 · ")
    assert one_label(page.summaryPanel, "大恶手 0")
    assert one_label(page.summaryPanel, "结果 黑中盘胜")
    assert one_label(page.summaryPanel, "0.84")     # 吻合度只留 2 位（后端给 3 位）
    assert one_label(page.summaryPanel, "数据来源：KataGo（每手 1200 次推演）")
    assert one_label(page.summaryPanel, "讲解：模板生成（未配置大模型 API Key")
    # 面板标题已经写着「整局总评」，正文不该再来一句（合成数据里那个 `### 整局总评`
    # 是照大模型真会写的开头摆的：提示词里那个键的说明就是「整局总评：…」）
    heads = [t for t in labels(page.summaryPanel) if t.startswith("整局总评")]
    assert len(heads) == 1, f"「整局总评」在屏上出现了 {len(heads)} 次：{labels(page.summaryPanel)}"
    assert any(t.startswith("这盘棋力在线") for t in labels(page.summaryPanel)), \
        "剥引导词剥过头了：正文本身也不见了"


def test_a_missing_rank_leaves_the_title_alone(page, qapp):
    """没段位（教学局、或 meta 里那个字段缺失）时标题不该写成「AI 复盘 · —」：
    那个破折号看着像数据丢了，而标题本身并没有少什么。"""
    page.game_id = GID
    page.meta = {"gameId": GID, "reviewStatus": "done", "size": SIZE, "aiName": "小棋"}
    page._paint_head()
    assert page.headTitle.text() == "AI 复盘"
    assert "对阵 小棋" in page.headSub.text()


def test_the_first_frame_is_the_move_you_most_need_to_see(page, qapp):
    """偏离④：报告到手不停在一张空盘上，停在我方损失最大的那一手。
    网页版 `ply` 初值是 0，学员进来第一眼什么都不是，还得自己拖过去。"""
    load(qapp, page, payload())
    assert page.ply == 7, f"最坏的那一手是第 7 手（-4.6 目），实际停在 {page.ply}"
    assert one_label(page.cardPanel, "第 7 手 · ")
    goto(qapp, page, 2)
    load(qapp, page, payload())            # 轮询完成后重取一次
    assert page.ply == 2, "重取报告把用户从他正在看的那一手拽走了"


def test_a_clean_game_lands_on_the_final_position(page, qapp):
    """一局没有缓手以上的问题：没有「最该看的那一手」，就看他下的最后一手。"""
    data = payload()
    data["report"]["moves"] = [dict(m, flag="good", flagLabel="好手")
                               for m in data["report"]["moves"]]
    load(qapp, page, data)
    assert page.ply == len(PLAN)
    assert page.plyBadge.text() == f"第 {len(PLAN)} / {len(PLAN)} 手"


def test_curve_has_one_point_per_move_plus_the_empty_board(page, qapp):
    """计划写的是「曲线点数 == 手数」。按事实断成 **手数 + 1**：第 0 个点是
    「还没落子时引擎怎么看这盘」，删了它才是错。这一条 +1 已记进项目日志。"""
    load(qapp, page, payload())
    assert page.chart.plotted_points() == len(PLAN) + 1


def test_curve_falls_back_to_live_analyses_when_the_report_has_none(page, qapp):
    """老报告里没有 `curve`（P0 之前生成的），要能用对局期那份 analyses 拼出来。
    第 5 手的分析是 missing（没有 winrateBlack），所以点数 = 13 - 1。"""
    data = payload()
    data["report"]["curve"] = []
    load(qapp, page, data)
    assert page.chart.plotted_points() == len(data["analyses"]) - 1


def test_no_markdown_reaches_the_screen(page, qapp):
    """计划口径：降级文本不许带 `**`。这里扫**整页可见标签**而不是只扫那一个字段 ——
    漏洗一处就是屏幕上多两颗星号，而漏洗的位置没人预得准。"""
    load(qapp, page, payload())
    for ply in (3, 7, 8, 11):
        goto(qapp, page, ply)                        # 不转就是只扫到旧卡片（假绿）
        bad = [t for t in labels(page) if "*" in t or "`" in t or t.lstrip().startswith("#")]
        assert not bad, f"第 {ply} 手那一屏还有 Markdown 记号：{bad}"
    assert "官子算小了" in card_text(page), \
        "第 11 手的讲解根本没进到界面 —— 那上面「没星号」就是假的（空界面永远不会红）"


def test_card_is_exactly_the_move_being_viewed(page, qapp):
    """ply 对齐是这一页最容易差一位的地方：曲线点、棋盘手数、卡片必须同一个编号。"""
    load(qapp, page, payload())
    for ply in (0, 3, 8, 12):
        goto(qapp, page, ply)
        assert page.plyBadge.text() == f"第 {ply} / {len(PLAN)} 手"
        assert page.chart.current == ply
        if ply == 0:
            assert not page.cardPanel.isVisible(), "开局没有「这一手」，不该出卡片"
            continue
        assert one_label(page.cardPanel, f"第 {ply} 手 · ").startswith(f"第 {ply} 手 · ")


def test_card_says_whose_perspective_the_numbers_are(page, qapp):
    """偏离①：winrateBefore/After 与 scoreBefore/After 是**走子方**视角
    （analyzer._wr 传的是 color）。网页版当「我方胜率」显示，AI 的手上就是反的。"""
    load(qapp, page, payload())
    goto(qapp, page, 8)                              # AI（白）的大恶手
    text = card_text(page)
    assert "胜率（该行棋方）" in text
    assert "45.0% → 31.0%" in text                  # 白方视角：白从 45% 掉到 31%
    assert "目差（该行棋方）" in text
    assert "5.5 → 11.7" in text
    assert "6.2 目（胜率 -14.0%）" in text
    assert "引擎首选 e3" in text
    assert "你的落点" not in text, "AI 的手上不该说玩家的落点排名"
    goto(qapp, page, 3)                              # 我方那一手：数字与排名都该在
    text = card_text(page)
    assert "48.6% → 44.0%" in text and "-1.2 → -3.6" in text
    assert "你的落点排在候选第 3 位" in text
    assert "参考变化" in text and "e3 d5 c4" in text


def test_ai_explanations_never_address_the_reader_as_the_author(page, qapp):
    """计划口径：「离你这手」不许指到 AI 的手。这里断的是**渲染之后**的界面文本 ——
    人称是后端话术里带的、客户端只透传，所以真正的证据在真报告上（e2e 那一支）；
    这条守的是「渲染层别自己拼一句错人称的话、也别把讲解整段丢掉」。"""
    load(qapp, page, payload())
    goto(qapp, page, 8)
    text = card_text(page)
    assert "离白方这手 4 路" in text, text
    assert "离你这手" not in text
    assert "正是你该提前破坏的地方" in text          # 面向学生的提醒要留着
    assert "讲解来源：模板" in text
    goto(qapp, page, 3)                              # 我方那一手，人称必须还是「你」
    assert "离你这手 3 路" in card_text(page)
    assert "讲解来源：大模型" in card_text(page)


def test_a_good_move_says_so_instead_of_giving_a_lecture(page, qapp):
    """后端不给好手生成 comment。这时卡片要说「这一手没有明显问题」，
    而不是留一个空的讲解框 —— 空白框看着像加载失败。"""
    load(qapp, page, payload())
    goto(qapp, page, 1)
    assert "这一手没有明显问题，引擎判定为好手。" in card_text(page)


def test_angle_brackets_survive_the_rich_text_path(page, qapp):
    """正文里的 `<` 不转义就会被当成标签起始，**整句后半截静默消失**。"""
    load(qapp, page, payload())
    goto(qapp, page, 11)
    line = one_label(page.cardPanel, "官子算小了")
    assert "&lt;" in line, line                      # 拼进 HTML 前必须已转义
    assert "后手 3 目" in line                        # 转义了才留得下后半句


def test_card_badge_colour_follows_the_flag(page, qapp):
    load(qapp, page, payload())
    goto(qapp, page, 3)
    assert "background: #fff9db" in badge_style_of(page.cardPanel, "缓手")
    goto(qapp, page, 8)
    assert "background: #ffe3e3" in badge_style_of(page.cardPanel, "大恶手")


def test_rings_only_mark_the_players_own_still_on_board(page, qapp):
    """棋盘上的圈有三条排除：AI 的手、好手、以及**已经被提掉**的落点。
    少了第三条，第 8 手之后会在一个空点上画圈，看着像程序画错了。
    断的是**控件那份**（画出来的），不是页面算出来的那份。"""
    load(qapp, page, payload())
    page.set_ply(2)
    assert page.boardView.shown_marks == []          # 第 1 手是好手，不该有圈
    page.set_ply(6)
    assert [(m["x"], m["y"], m["label"]) for m in page.boardView.shown_marks] == [(2, 2, "3")]
    page.set_ply(7)
    assert [(m["x"], m["y"]) for m in page.boardView.shown_marks] == [(2, 2), (5, 5)]
    page.set_ply(8)                                  # 黑 (2,2) 已被提掉
    assert [(m["x"], m["y"], m["label"]) for m in page.boardView.shown_marks] == [(5, 5, "7")]
    page.set_ply(12)
    rings = {(m["x"], m["y"]): m["kind"] for m in page.boardView.shown_marks}
    assert rings == {(5, 5): "bad", (0, 8): "slow"}, rings
    assert (2, 1) not in rings, "AI 的大恶手被画进我方的问题手里了"


def test_variation_alternates_from_the_move_color_and_is_numbered(page, qapp):
    """`variation` 是 `bestMove.pvPoints`，第 0 个就是引擎推荐的那一手本身
    （`commentary.py` 与 `engine/protocol.py:137` 同口径：`pv[0]` = 首选）。

    所以幽灵子必须从**这一手自己的颜色**起画，此后交替。从前从对手色起画，
    整条线黑白全反：卡片写着「引擎首选 e3」，盘上却用白子标「1」（审计 1.19）。
    这一手是黑（`PLAN[2]` 的 c7），于是 4-3 = 黑、3-4 = 白、2-4 = 黑。
    """
    load(qapp, page, payload())
    page.set_ply(3)
    assert page.boardView.shown_variation == []
    page.chkVariation.setChecked(True)
    assert [(v["x"], v["y"], v["color"], v["label"]) for v in page.boardView.shown_variation] \
        == [(4, 3, BLACK, "1"), (3, 4, WHITE, "2"), (2, 4, BLACK, "3")]


def test_ownership_switch_does_not_appear_without_data(page, qapp):
    """偏离②：复盘 worker 用**不含 ownership** 的分析覆盖了 `rec.analyses`
    （worker.py:387 + 198），照抄网页版就留一个点了没反应的死开关。"""
    load(qapp, page, payload())
    assert page.chkOwnership.isVisible() is False
    data = payload()
    data["analyses"][7]["ownership"] = [0.0] * (SIZE * SIZE)   # 后端给的是平铺一维数组
    load(qapp, page, data)
    assert page.chkOwnership.isVisible() is True
    page.set_ply(7)
    assert page.boardView.shown_ownership is None, "没勾上就把领地层叠上去了"
    page.chkOwnership.setChecked(True)
    assert len(page.boardView.shown_ownership) == SIZE * SIZE


def test_the_judge_line_reads_the_position_being_viewed(page, qapp):
    load(qapp, page, payload())
    page.set_ply(0)
    # 第 0 手没有「这一手」，说「开局」；与棋盘上那个空盘是同一件事
    assert page.judgeLine.text() == \
        "形势判断（开局）：我方胜率 52.0% · 目差 +1.2（领先） · 1200 次推演"
    page.set_ply(5)                                  # 那一份分析是 missing
    assert page.judgeLine.text() == \
        "形势判断（第 5 手后）：这一手没有引擎分析数据（复盘时未取到），只看局面。"
    page.set_ply(3)
    assert "目差 -3.6（落后）" in page.judgeLine.text(), page.judgeLine.text()


def test_a_review_with_no_analysis_at_all_says_so(page, qapp):
    """两种「没数据」不是一回事。整局一份分析都没有（复盘还没跑完 / 跑失败了），
    而对用户说「这一手没有引擎分析数据（复盘时未取到）」：既把锅扣在不存在的
    那一手上，也把用户引向「重跑这一手」而不是「等报告」。"""
    page.game_id = GID
    page.meta = {"gameId": GID, "reviewStatus": "pending", "size": SIZE}
    page.analyses = []
    page._paint_all()
    assert page.judgeLine.text() == \
        "形势判断（开局）：这一局还没有引擎分析数据（复盘未完成或未取到），只看局面。"
    data = payload()
    load(qapp, page, data)
    page.analyses = [{"ply": i, "missing": True} for i in range(len(PLAN) + 1)]
    page.set_ply(1)                                  # 有列表但一份都没内容：同上
    assert "这一局还没有引擎分析数据" in page.judgeLine.text(), page.judgeLine.text()


# ------------------------------------------------------------------ 状态机

def test_six_stage_progress_names_the_step_out_of_six(page, qapp):
    """阶段名用接口下发的 stageText，「第几步/6」由本地表算：后端加阶段最多是
    这个数字偏小，绝不会编出一句不存在的文案。"""
    page.game_id = GID
    page.meta = {"gameId": GID, "reviewStatus": "pending", "size": SIZE}
    page.prog = {"status": "pending", "stage": "analyze", "stageText": "逐手重新分析",
                 "progress": 0.42, "detail": "已完成 5/12 手"}
    page._paint_all()
    assert page.genPanel.isVisible()
    assert page.genBar.value() == 42
    assert page.genStage.text().startswith("第 3/6 步 · 逐手重新分析 · 42%")
    assert "已完成 5/12 手" in page.genStage.text()
    assert not page.noneBar.isVisible(), "生成中不该同时说「暂无报告」"


def test_progress_from_the_payload_alone_is_enough(page, qapp):
    """刚进页面时 `/status` 还没回，`meta` 里那份持久进度要立刻画出来。"""
    page.game_id = GID
    page.meta = {"gameId": GID, "reviewStatus": "pending", "size": SIZE,
                 "reviewProgress": 0.1, "reviewStage": "engine", "reviewDetail": "等引擎就绪"}
    page.prog = None
    page._paint_all()
    assert page.genPanel.isVisible()
    assert page.genStage.text().startswith("第 2/6 步 · 等引擎就绪 · 10%")


def test_an_unknown_stage_word_is_copied_not_guessed(page, qapp):
    """后端加了本地表里没有的阶段：序号留 0，文案照抄它，不许编一个。"""
    page.game_id = GID
    page.meta = {"reviewStatus": "pending"}
    page.prog = {"status": "pending", "stage": "polish", "stageText": "润色讲解",
                 "progress": 0.9}
    page._paint_all()
    assert page.genStage.text().startswith("第 0/6 步 · 润色讲解 · 90%")


def test_none_status_offers_to_generate_instead_of_spinning(page, qapp):
    """`none` 是「从未入队」（强制结束的局）。对它轮询会永远转圈。"""
    page.game_id = GID
    page.meta = {"reviewStatus": "none", "size": SIZE, "reviewError": "这一局没有复盘数据"}
    page.report, page.prog = None, None
    page._paint_all()
    assert not page.genPanel.isVisible()
    assert page.noneBar.isVisible()
    assert "这一局没有复盘数据" in page.noneBar.label.text()
    assert page.noneBar.button.text() == "立即生成"
    assert not page._tick.isActive(), "对 none 轮询 = 永远转圈"
    assert page._polls == 0


def test_low_confidence_banner_tracks_the_report_not_the_setting(page, qapp):
    """降级提示的根据是 `report.lowConfidence`（这一份分析用没用真引擎），
    不是「后端现在有没有 KataGo」—— 旧报告是启发式算的，跟当前引擎状态无关。"""
    load(qapp, page, payload())
    assert not page.lowBar.isVisible()
    data = payload()
    data["report"]["lowConfidence"] = True
    data["report"]["engine"] = "heuristic"
    load(qapp, page, data)
    assert page.lowBar.isVisible()
    assert "KataGo" in page.lowBar.label.text()
    assert one_label(page.summaryPanel, "数据来源：内置启发式引擎")


def test_status_polling_stops_on_done_and_makes_a_sound(page, qapp):
    pg = FakeApi({
        ("GET", f"/api/reviews/{GID}/status"): (
            {"gameId": GID, "status": "done", "stage": "done", "stageText": "已完成",
             "progress": 1.0, "detail": "", "error": None, "hasReport": True}, None),
        ("GET", f"/api/reviews/{GID}"): (payload(), None),
    })
    page._api = pg
    page.game_id = GID
    page._poll_status()
    assert H.wait(qapp, lambda: page.report is not None)
    assert not page._tick.isActive(), "done 之后还在轮询 = 白要请求"
    assert "review" in page._sound.played, f"复盘完成该有一声：{page._sound.played}"
    assert pg.count("GET", f"/api/reviews/{GID}") == 1, "完成后没重取整份报告"


def test_status_for_another_game_is_ignored(page, qapp):
    """换局之后回来的旧 status 若照单全收，进度条会显示别人那一局的任务。"""
    page.game_id = GID
    page.meta = {"reviewStatus": "done"}
    page._on_status({"gameId": "bie-ren-de-ju", "status": "pending", "progress": 0.5}, None)
    assert page.prog is None
    assert not page.genPanel.isVisible()


def test_transient_status_errors_do_not_shout_at_the_user(page, qapp):
    """轮询期间的瞬时网络错：不弹红条，3 秒后再试。一次超时就把「读取失败」
    糊在用户脸上，而 1.2 秒后它其实自己好了。"""
    page.game_id = GID
    page.meta = {"reviewStatus": "pending"}
    page._paint_alerts()
    page._on_status(None, ApiError("无法连接本地服务", 0))
    assert page.error == ""
    assert not page.errorBar.isVisible()
    assert page._tick.isActive()


def test_rerun_clears_the_stale_progress_before_the_server_says_so(page, qapp):
    """POST 之后旧进度条还停在 100%：那是在告诉用户「已经好了」。"""
    pg = FakeApi({
        ("POST", f"/api/reviews/{GID}"): ({"gameId": GID, "status": "pending"}, None),
        ("GET", f"/api/reviews/{GID}"): (
            {"meta": {"gameId": GID, "reviewStatus": "pending", "size": SIZE,
                      "reviewProgress": 0.0, "reviewStage": "queued"},
             "report": None, "moves": [], "analyses": []}, None),
    })
    page._api = pg
    page.game_id = GID
    page.report = payload()["report"]
    page.prog = {"status": "done", "progress": 1.0, "stage": "done"}
    page._paint_all()
    page._rerun()
    assert pg.count("POST", f"/api/reviews/{GID}") == 1
    assert H.wait(qapp, lambda: page.busy is False and page.report is None)
    assert page.prog is None, "重新生成了还挂着上一轮的 100%"
    assert page.btnRerun.text() == "重新生成"


def test_a_packet_for_another_game_is_dropped(page, qapp):
    """换局之后才回来的旧包必须丢，不然界面上是上一局的复盘而标题写着这一局。"""
    page.game_id = GID
    stale = payload()
    stale["meta"]["gameId"] = "bubi_renzhende_yige"
    page._on_payload(stale, None)
    assert page.meta == {}
    assert page.report is None


def test_inflight_load_is_not_duplicated(page, qapp):
    """切页连点不该发出五份同样的 GET；脏标记在回包后清掉并补一次。"""
    pg = FakeApi({("GET", f"/api/reviews/{GID}"): (payload(), None)})
    page._api = pg
    page.game_id = GID
    page._load()
    page._load()
    page._load()
    assert pg.count("GET", f"/api/reviews/{GID}") == 1
    assert H.wait(qapp, lambda: page.report is not None)
    assert page._list_dirty is False
    assert pg.count("GET", f"/api/reviews/{GID}") == 2      # 补的那一次，且只补一次


def test_read_failure_is_said_in_words_the_user_can_act_on(page, qapp):
    pg = FakeApi({("GET", f"/api/reviews/{GID}"): (None, ApiError("复盘不存在", 404))})
    page._api = pg
    page.game_id = GID
    page._load()
    assert H.wait(qapp, lambda: page.error)
    assert page.error == "读取复盘失败：复盘不存在"
    assert page.errorBar.isVisible()
    assert page.errorBar.kind == "err"


# ------------------------------------------------------------------ 导航

def test_buttons_and_keys_and_rows_all_move_the_same_ply(page, qapp):
    load(qapp, page, payload())
    goto(qapp, page, 0)
    assert page.btnStart.isEnabled() is False, "第 0 手时「开局」「上一手」没有意义"
    assert page.btnNext.isEnabled()
    QTest.mouseClick(page.btnNext, Qt.LeftButton)
    assert page.ply == 1
    QTest.keyClick(page, Qt.Key_Right)               # 页面级 ← →
    assert page.ply == 2
    QTest.keyClick(page.table, Qt.Key_Right)         # 点过列表之后焦点在表格里
    assert page.ply == 3
    assert page.table.item(page.table.currentRow(), 0).data(Qt.UserRole) == 3
    QTest.keyClick(page.table, Qt.Key_Left)
    assert page.ply == 2
    QTest.mouseClick(page.btnEnd, Qt.LeftButton)
    assert page.ply == len(PLAN)
    assert page.btnNext.isEnabled() is False
    QTest.mouseClick(page.btnStart, Qt.LeftButton)
    assert page.ply == 0


def test_the_table_does_not_move_its_own_cell_on_arrows(page, qapp):
    """← → 归 step()，不许被表格用成「移动单元格」：有 4 列，走到行尾会跳到
    下一行，读数就乱了。这条钉的是 `eventFilter` 真的接在线路上（拦下来之后
    当前格只跟着选行走：第 7 手在索引 6，列仍是 0）。"""
    load(qapp, page, payload())
    page.set_ply(1)
    assert (page.table.currentRow(), page.table.currentColumn()) == (0, 0)
    for _ in range(6):
        QTest.keyClick(page.table, Qt.Key_Right)
    assert page.ply == 7
    assert (page.table.currentRow(), page.table.currentColumn()) == (6, 0), \
        "表格自己动了当前格 —— 过滤器没接上"


def test_picking_a_row_jumps_to_that_move(page, qapp):
    load(qapp, page, payload())
    goto(qapp, page, 0)
    # 选**之当前没在看的**那一行：落点行已经因「第 7 手」被选中了，
    # 再 `setCurrentCell` 到同一行是一个信号都不会发（选完自己验不到东西）。
    page.table.setCurrentCell(9, 0)                  # 第 10 手（无筛选，行号 = 手数 - 1）
    assert page.ply == 10
    assert page.plyBadge.text() == "第 10 / 12 手"
    assert page.chart.current == 10
    assert [m["label"] for m in page.boardView.shown_marks] == ["7"]


def test_only_problems_filter_keeps_the_ai_out_of_it(page, qapp):
    """「只看我方问题手」必须同时滤掉 AI 的手与好手 —— 后端算精度时也是这么滤的
    （commentary.kind_breakdown：不过滤就会把 AI 的失误算到学生头上）。"""
    load(qapp, page, payload())
    assert page.table.rowCount() == len(PLAN)
    page.chkOnlyProblems.setChecked(True)
    rows = [page.table.item(i, 1).text() for i in range(page.table.rowCount())]
    assert len(rows) == 3, rows
    assert all("我（黑）" in t for t in rows), rows
    assert not any("大恶手" in t for t in rows), f"AI 的大恶手漏进了「我方问题手」：{rows}"
    assert page.table.item(0, 3).text() == "-2.4 目"     # 损失那一列
    assert page.table.item(0, 2).text() == "44%"         # 胜率那一列不留小数
    page.chkOnlyProblems.setChecked(False)
    assert page.table.rowCount() == len(PLAN)


def test_rebuilding_the_list_does_not_yank_the_selected_move(page, qapp):
    """`clearContents()` 会动当前行，那一瞬的回抛会把 ply 跳走。"""
    load(qapp, page, payload())
    page.set_ply(11)
    page.chkOnlyProblems.setChecked(True)
    assert page.ply == 11, f"重建列表把当前手改成了 {page.ply}"
    page._paint_list()
    assert page.ply == 11


def test_switching_games_unchecks_the_filters(page, qapp):
    """换局时开关必须跟着摆回去：数据换了而框还勾着，列表就少一批手而不提示。"""
    load(qapp, page, payload())
    page.chkOnlyProblems.setChecked(True)
    page.chkVariation.setChecked(True)
    assert page.only_problems and page.show_variation
    page.open_game(GID)
    assert page.chkOnlyProblems.isChecked() is False
    assert page.chkVariation.isChecked() is False
    assert page.only_problems is False and page.show_variation is False


def test_empty_list_says_why_it_is_empty(page, qapp):
    load(qapp, page, payload())
    assert page.emptyList.isVisible() is False
    data = payload()
    data["report"]["moves"] = [m for m in data["report"]["moves"] if m["flag"] == "good"]
    load(qapp, page, data)
    page.chkOnlyProblems.setChecked(True)
    assert page.emptyList.isVisible()
    assert page.emptyList.text() == "这一局我方没有缓手以上的失误"


def test_no_report_yet_hides_the_move_list(page, qapp):
    """报告还没出来时摆一张空表格 + 「没有匹配的手」，读起来像筛选坏了。"""
    page.game_id = GID
    page.meta = {"reviewStatus": "pending", "size": SIZE}
    page._paint_all()
    assert page.listPanel.isVisible() is False
    assert page.summaryPanel.isVisible() is False


def test_the_empty_right_column_says_what_will_show_up(page, qapp):
    """三个面板都收起之后，整个右半边是一片空底（截图 p4_04 看到的）。
    原生窗口不像网页有背景可看，缺一块就是缺一块 —— 得说一句等什么。"""
    page.game_id = ""
    page._paint_all()
    assert page.waitHint.isVisible() is False, "还没选一局，不该说「报告出来之后」"
    page.game_id = GID
    page.meta = {"gameId": GID, "reviewStatus": "pending", "size": SIZE}
    page._paint_all()
    qapp.processEvents()
    assert page.waitHint.isVisible()
    assert "整局总评" in page.waitHint.text()
    load(qapp, page, payload())
    assert page.waitHint.isVisible() is False, "报告到了还挂着占位句"


# ------------------------------------------------------------------ 导出

def test_export_writes_the_backends_markdown_verbatim(page, qapp, tmp_path):
    """原文由后端排（`review_to_markdown`），客户端不重排一遍：
    两个客户端各排一套，导出就会分叉。"""
    md = "# AI 复盘报告\n\n第 3 手：缓手\n"
    pg = FakeApi({("GET", f"/api/reviews/{GID}/export"): (md, None)})
    page._api = pg
    page.game_id = GID
    page.report = payload()["report"]
    out = tmp_path / "review.md"
    assert page.export_report_to(str(out))
    assert H.wait(qapp, lambda: out.exists())
    assert out.read_text("utf-8") == md
    assert page.last_export == md
    assert page.error == ""


def test_export_failure_is_not_silent(page, qapp, tmp_path):
    pg = FakeApi({("GET", f"/api/reviews/{GID}/export"): (None, ApiError("报告尚未生成", 409))})
    page._api = pg
    page.game_id = GID
    out = tmp_path / "nope.md"
    assert page.export_report_to(str(out))
    assert H.wait(qapp, lambda: page.error)
    assert page.error == "导出失败：报告尚未生成"
    assert not out.exists(), "失败了还要落一个空文件，比不落更坏"


def test_export_button_only_exists_when_there_is_something_to_export(page, qapp):
    page.game_id = GID
    page.report = None
    page._paint_buttons()
    assert page.btnExport.isEnabled() is False
    load(qapp, page, payload())
    assert page.btnExport.isEnabled()


# ------------------------------------------------------------------ 截图

def test_screenshot_of_a_finished_review(page, qapp):
    """两张关键帧之一：整页。我随后会亲眼看这张图（计划口径「我读 2 张截图」）。"""
    load(qapp, page, payload())
    goto(qapp, page, 8)
    offenders, scanned = H.clipped_texts(page)
    assert scanned > 20, f"只扫到 {scanned} 个控件，这个页面没真的摆开"
    assert not offenders, "有文字被裁：\n" + "\n".join(offenders)
    violations, bars = H.bar_texts(page)
    assert bars >= 1 and not violations, f"进度条在槽内画字：{violations}"
    shot = H.snap(page, "p4_02_review_done")
    assert H.blank_ratio(page) > 0.5, "整页几乎是空的"
    assert shot.exists()


def test_screenshot_of_the_summary_panel_with_real_template_text(page, qapp):
    """整局总评的排版关键帧 —— 用户点名「文字相当之挤」的就是这一屏。

    用 `real_payload()`（像真模板产出的满配文本）而不是 fixture 里那两句短话，
    否则排版问题在截图上看不出来。
    """
    load(qapp, page, real_payload())
    offenders, scanned = H.clipped_texts(page.summaryPanel)
    assert scanned >= 15, f"总评卡只扫到 {scanned} 个控件，没真摆开"
    assert not offenders, "总评里有文字被裁：\n" + "\n".join(offenders)
    shot = H.snap(page.summaryPanel, "p6_04_review_summary")
    assert shot.exists() and shot.stat().st_size > 0
    assert H.blank_ratio(page.summaryPanel) < 0.85


def test_the_summary_is_broken_into_readable_blocks(page, qapp):
    """排版口径的可断言部分：一句一行、数字卡、胜负分界 callout、训练建议拆两行。"""
    load(qapp, page, real_payload())
    texts = labels(page.summaryPanel)
    # ① 长段被切成多句，每句一个标签
    assert any(t.startswith("本局结果：白胜") for t in texts)
    assert any(t.startswith("全局起伏最大") for t in texts)          # 那句仍在屏上
    # ② 胜负分界抬成了 callout（标题 + 正文两块）
    assert "胜负分界" in texts
    # ③ 三阶段是数字卡，不是流水账
    assert "19 手" in texts and "均损 2.6 目" in texts
    assert "最大 6.6 目 · 第 24 手" in texts
    assert not any("阶段共 19 手" in t for t in texts), "模板流水账还贴在卡里"
    # ④ 训练建议：编号圆点 + 一条一行（标题与正文同一行，省 5 行高度）
    assert any(t.startswith("练「战场方向」：落子前先花三秒") for t in texts), texts
    # ⑤ 评语压成一行（每段取第一小节）
    assert any("布局 攒了若干缓手" in t for t in texts), texts


def test_the_summary_keeps_llm_prose_intact_when_there_are_no_phase_stats(page, qapp):
    """没有结构化 phases（大模型写的散文）时退回原文 —— 不许硬拆。"""
    data = payload()
    data["report"]["phases"] = {}
    data["report"]["summary"]["opening"] = "这盘开局双方都很平稳，没什么可挑的。"
    load(qapp, page, data)
    texts = labels(page.summaryPanel)
    assert any("这盘开局双方都很平稳" in t for t in texts), texts


def test_the_summary_gets_shorter_when_the_window_is_wide(page, qapp):
    """用户要「少滚动」：窗口宽了以后总评卡要明显变矮（宽 → 折行少 → 行数少）。

    这条同时是排版回归的尺子：以后往总评里塞东西，高度会顶上去，它先报红。
    """
    load(qapp, page, real_payload())
    H.settle(qapp, 0.2)
    narrow = page.summaryPanel.height()          # 量**摆完之后的实际高度**，
    page.resize(1600, 900)                       # `sizeHint()` 里折行标签用的是上一轮宽度
    H.settle(qapp, 0.3)
    wide = page.summaryPanel.height()
    assert wide < narrow, f"宽窗口没有变矮：{narrow} → {wide}"
    assert wide <= 700, f"1600 宽下总评卡还有 {wide} 高，一屏放不下"
    shot = H.snap(page.summaryPanel, "p6_05_review_summary_wide")
    assert shot.exists()
    page.resize(1104, 760)
    qapp.processEvents()


def test_the_move_list_fills_the_leftover_height(page, qapp):
    """用户报的「下面还有好大的空间，逐手分析只占一小栏」。

    表格原来被 `setMaximumHeight(320)` 卡死，右列底部的空白被 `addStretch` 吃掉。
    现在逐手列表是右列里唯一会伸展的东西：视口比内容高就吃掉剩余高度，
    视口比内容矮就退回自然高度、整列照旧能滚。
    """
    load(qapp, page, payload())
    page.resize(1104, 1400)                      # 视口比内容高
    H.settle(qapp, 0.3)
    tall = page.table.height()
    assert tall > 320, f"表格还是被 320px 卡着：{tall}"
    panel = page.listPanel
    table_bottom = page.table.mapTo(panel, page.table.rect().bottomLeft()).y()
    assert table_bottom >= panel.height() - 80, \
        f"表格底边 {table_bottom} 离卡片底 {panel.height()} 还差一大截（空白又留在底部了）"

    page.resize(1104, 760)                       # 视口比内容矮
    H.settle(qapp, 0.3)
    assert page.table.height() >= page.table.minimumHeight()
    assert page.scroll.verticalScrollBar().maximum() > 0, "内容装不下时整列必须能滚"


def test_screenshot_of_a_player_blunder_card(page, qapp):
    """两张关键帧之二：我方恶手那一手的卡片 + 变化图。"""
    load(qapp, page, payload())
    page.set_ply(7)
    page.chkVariation.setChecked(True)
    qapp.processEvents()                             # 不转就读不到刚填进去的卡片（见 `goto`）
    assert not H.clipped_texts(page.cardPanel)[0]
    shot = H.snap(page.cardPanel, "p4_03_review_card")
    assert shot.exists() and H.blank_ratio(page.cardPanel) > 0.4


def test_screenshot_while_the_review_is_still_running(page, qapp):
    """生成中那一帧也要看：进度条、六个阶段说到第几步、以及**不该出现**的东西
    （空表格、空总评卡）有没有抢先占位。"""
    page.game_id = GID
    page.meta = {"gameId": GID, "reviewStatus": "pending", "size": SIZE,
                 "reviewProgress": 0.12, "reviewStage": "engine", "reviewDetail": "预热引擎"}
    page._paint_all()
    qapp.processEvents()                             # 同上：刚改过文字，几何还是旧的
    assert page.waitHint.isVisible(), "右半边一片空底，这一帧里该能看见它说了等什么"
    assert not H.clipped_texts(page)[0]
    shot = H.snap(page, "p4_04_review_running")
    assert shot.exists() and H.blank_ratio(page) > 0.4


def test_the_page_survives_a_game_with_no_moves(page, qapp):
    """一局棋没下完（0 手）也要能进复盘页：曲线、列表、卡片都要安静地收起。"""
    data = payload()
    data["report"] = None
    data["moves"], data["analyses"] = [], []
    data["meta"]["reviewStatus"] = "none"
    load(qapp, page, data)
    assert page.report is None
    assert not page.chartPanel.isVisible()
    assert not page.cardPanel.isVisible()
    assert not page.listPanel.isVisible()
    assert page.plyBadge.text() == "第 0 / 0 手"
    assert not page.btnNext.isEnabled()


def test_the_review_board_is_not_squashed(page, qapp):
    """左列 696px 高是「棋盘 + 曲线」共用，棋盘永远是第一个被挤的那个。

    被挤不可怕（控件扁了木盘照样是正方形，见 test_board_render 里的同名口径），
    可怕的是越挤越小而没人知道。这一条钉的是**回归下限**而不是精确值：以后
    往左列再塞东西、或把列改窄，会先在这里红，而不是等我再看一遍截图。
    """
    load(qapp, page, payload())
    b = page.boardView
    plate = b.board_rect()
    assert abs(plate.width() - plate.height()) < 1e-6, plate
    assert plate.width() >= 260, (
        f"木盘只剩 {plate.width():.0f}px（控件 {b.width()}x{b.height()}），左列是不是又塞了东西")


def test_shutdown_stops_the_polling(page, qapp):
    """窗口关闭后还在轮询 = 关掉程序却看见它在发请求。"""
    page.game_id = GID
    page.meta = {"reviewStatus": "pending"}
    page._sync_polling()
    assert page._tick.isActive()
    page.shutdown()
    assert not page._tick.isActive()
