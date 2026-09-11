"""复盘讲解：Prompt 构造 + LLM 结果解析 + 无 LLM 时的模板降级。

讲解质量策略：
  1. 事实全部来自 KataGo（胜率、目差、首选点、变化图），LLM 只负责表达；
  2. Prompt 中注入术语表与"按等级调整语言"的要求，避免术语堆砌或过度口语化；
  3. LLM 不可用/解析失败时，回落到确定性模板，保证报告永远完整可用。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from ..config import settings
from ..game.rules import BLACK
from .analyzer import FLAG_BAD, FLAG_BLUNDER, FLAG_LABEL, FLAG_SLOW, PHASE_LABEL, phase_of

logger = logging.getLogger("go.review")

GO_TERMS = """【围棋术语表（讲解时可自然使用，需符合语境）】
布局/中盘/官子、大场/急所、先手/后手、厚薄、轻重、子效、方向、
挂角/守角/夹击/拆边/逼住、飞/跳/尖/长/立/扳/粘/断/连/虎/双/扑/挤/点、
气/眼/真眼假眼/死活、劫/寻劫/消劫、征子/枷/倒扑/接不归、
模样/实地/中腹、目/子、腾挪/弃子/转换/攻逼/围空/破空
"""

SYSTEM_PROMPT = f"""你是一位有 20 年经验的围棋教练，正在为学生复盘。
你只会拿到围棋引擎（KataGo）已经算好的数据：每手的胜率、目数得失、引擎首选点与后续变化。
你的任务是把这些数据讲成学生听得懂、记得住的话。

要求：
1. 严格依据给定数据，绝不编造胜率数字或"引擎认为"的结论；
2. 语言深度必须匹配学生等级：级位学生用通俗说法（如"这一步让自己的棋变薄了"），
   段位学生可用专业术语（如"该点为双方模样消长的天王山"）；
3. 每条讲解聚焦"为什么不好"与"该怎么想"，不复述坐标即可懂的废话；
4. 只输出 JSON，不要额外文字、不要 Markdown 代码块。
{GO_TERMS}"""

_MOVE_JSON_SPEC = """输出格式（JSON 对象，键为手数字符串）：
{
  "<手数>": {
    "reason": "为什么这手有问题（棋形/方向/时机/大小，40~90字）",
    "advice": "正确的思路与引擎首选点的意图（30~70字）",
    "maxim": "一句可记住的棋理格言（≤20字）"
  }
}"""

_SUMMARY_JSON_SPEC = """输出格式（JSON 对象）：
{
  "opening": "布局阶段评语（60~120字）",
  "middle": "中盘阶段评语（60~120字）",
  "endgame": "官子阶段评语（60~120字）",
  "overall": "整局总评：胜负关键与棋力定位（80~150字）",
  "training": ["针对性训练建议1", "针对性训练建议2", "针对性训练建议3"],
  "maxim": "本局最值得记住的一句棋理（≤24字）"
}"""


# ---------------------------------------------------------------------------
def describe_move(report: dict, total_moves: int, size: int, player_rank: str) -> str:
    """把一手的关键数据压成一行结构化描述喂给 LLM。"""
    ph = PHASE_LABEL[phase_of(report["moveNum"], total_moves, size)]
    color = "黑" if report["color"] == 1 else "白"
    parts = [
        f"第{report['moveNum']}手（{ph}）",
        f"{color}方（玩家）下在 {report['gtp']}",
    ]
    if report.get("winrateBefore") is not None and report.get("winrateAfter") is not None:
        parts.append(f"胜率 {report['winrateBefore'] * 100:.1f}% → {report['winrateAfter'] * 100:.1f}%")
    if report.get("scoreBefore") is not None and report.get("scoreAfter") is not None:
        parts.append(f"目差 {report['scoreBefore']:+.1f} → {report['scoreAfter']:+.1f}（正数=玩家领先）")
    if report.get("lossPoints") is not None:
        parts.append(f"损失 {report['lossPoints']:.1f} 目，判定：{FLAG_LABEL[report['flag']]}")
    if report.get("captures"):
        parts.append(f"提子 {report['captures']} 枚")
    best = report.get("bestMove")
    if best:
        parts.append(f"引擎首选 {best.get('gtp')}（该点胜率 {float(best.get('winrate', 0)) * 100:.1f}%）")
        if report.get("variationGtp"):
            parts.append("首选后续变化：" + " ".join(report["variationGtp"][:5]))
    if report.get("playerRank") is not None:
        parts.append(f"玩家落点在引擎候选中排名第 {report['playerRank'] + 1}")
    return "；".join(parts)


def build_move_messages(batch: list[dict], ctx: dict) -> list[dict]:
    total = ctx.get("totalMoves", 0)
    size = ctx.get("size", 19)
    lines = "\n".join(f"- {describe_move(r, total, size, ctx.get('rankName', ''))}" for r in batch)
    user = f"""学生信息：当前等级 {ctx.get('rankName', '')}，本局执{ctx.get('playerColorName', '黑')}，
对手 {ctx.get('aiName', '')}，棋盘 {size} 路，贴目 {ctx.get('komi', 7.5)}，共 {total} 手，
结果：{ctx.get('resultText', '')}。

请逐条讲解下列关键手（这是引擎认定的问题手）：
{lines}

{_MOVE_JSON_SPEC}"""
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]


def build_summary_messages(ctx: dict, phases: dict, counts: dict,
                            moments: list[dict], accuracy: Optional[float],
                            ai_accuracy: Optional[float]) -> list[dict]:
    size = ctx.get("size", 19)
    total = ctx.get("totalMoves", 0)
    precision_note = "" if ctx.get("engine") == "katago" else (
        "\n注意：本机未安装 KataGo，数据来自内置启发式引擎，精度有限；"
        "请在评语中提醒学生安装 KataGo 后重新生成报告，不要把当前吻合度当作真实棋力。")
    phase_lines = []
    for key, label in PHASE_LABEL.items():
        p = phases.get(key) or {}
        if not p:
            continue
        avg = p.get("avgLoss")
        worst = f"，最大失误在第 {p['worstMoveNum']} 手（损失 {p['worstLoss']} 目）" \
            if p.get("worstMoveNum") else ""
        phase_lines.append(f"- {label}：{p.get('moves', 0)} 手，平均每手损失 "
                           f"{avg if avg is not None else '—'} 目{worst}")
    moment_lines = []
    for m in moments:
        who = "玩家" if m.get("isPlayer") else "AI"
        color = "黑" if m.get("color") == 1 else "白"
        direction = "上升" if m["swing"] > 0 else "下降"
        moment_lines.append(f"- 第{m['moveNum']}手（{who}执{color} {m.get('gtp')}）："
                            f"黑方胜率{direction} {abs(m['swing']) * 100:.1f} 个百分点")
    user = f"""本局数据摘要：
学生等级 {ctx.get('rankName', '')}，执{ctx.get('playerColorName', '黑')}，对手 {ctx.get('aiName', '')}；
棋盘 {size} 路，贴目 {ctx.get('komi', 7.5)}，共 {total} 手；结果 {ctx.get('resultText', '')}。
吻合度（平均每手损失目数）：学生 {accuracy if accuracy is not None else '—'} 目，
AI {ai_accuracy if ai_accuracy is not None else '—'} 目（数值越小越好，职业棋手通常在 0.5 目以内）。
问题手统计：大恶手 {counts.get('blunder', 0)} 次、恶手 {counts.get('bad', 0)} 次、
缓手 {counts.get('slow', 0)} 次、好手 {counts.get('good', 0)} 次。

分阶段表现：
{chr(10).join(phase_lines) if phase_lines else '（无足够数据）'}

全局转折点：
{chr(10).join(moment_lines) if moment_lines else '（无明显转折）'}

请据此生成整局总结报告。{precision_note}
{_SUMMARY_JSON_SPEC}"""
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]


# ---------------------------------------------------------------------------
_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


def extract_json(text: str) -> Optional[dict]:
    """从 LLM 回复里稳健地抠出 JSON（容忍 ```json 包裹与前后废话）。"""
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK.search(cleaned)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            logger.warning("LLM 返回的 JSON 无法解析，降级为模板讲解")
    return None


# ---------------------------------------------------------------------------
# 模板降级（未配置 LLM，或 LLM 返回解析失败时）
#
# 这里的文本要**比大模型更保守**：每句话都得能追到 report 里的某个字段，
# 不能为了写得详细而编引擎没说过的结论。原则按数据来源分两层：
#   * 与估值无关的事实（落点、坐标距离、提子数、候选排名、阶段划分）——
#     任何时候都能直说，未装 KataGo 时也一样成立；
#   * 依赖引擎估值的数字（胜率、损失目数、吻合度）——启发式引擎给的那些
#     不可信（见日志 §5 与 1.3），只能带限定词出现，不能写成断言。
# ---------------------------------------------------------------------------

# 问题分类：用于逐手话术选择，也用于总结里统计「这一局的思维误区在哪一层」
K_ORDER = "order"          # 次序：这个点出现在引擎首选的变化里，只是走早/走晚了
K_RISK = "risk"            # 安危：目数亏得不多，胜率掉得多 → 影响大块棋的根据
K_GREEDY = "greedy"        # 贪吃：提子了，但代价更大
K_SIZE = "size"            # 大小：官子阶段的价值排序问题
K_DIR = "direction"        # 方向：引擎在另一带行棋，属于战场判断
K_LOCAL = "local"          # 手段：方向对了，具体的形或应法欠妥
K_TIME = "time"            # 时机：形势不利时仍走不急的棋
K_GEN = "generic"

# 「目损小但胜率掉得多」的判据。围棋里 1 目大约值 3~4% 胜率，所以正常的
# 一手，胜率损 / 目损 应该落在 0.03~0.05；比值明显超出这个量级，说明掉的
# 不是大小而是后续手段（能不能被攻、能不能做活）——即「棋的安危」类问题。
RISK_RATIO = 0.08
RISK_MIN_WR = 0.06          # 低于这个胜率跌幅就不算「明显」，别把噪声当结论
BEHIND_POINTS = 3.0         # 形势领先/落后的叙述门槛（目）：以下不夸大成败


_KIND_LABEL = {
    K_ORDER: "行棋次序", K_RISK: "棋的安危", K_GREEDY: "弃取（贪吃）",
    K_SIZE: "官子大小", K_DIR: "战场方向", K_LOCAL: "局部手段",
    K_TIME: "时机", K_GEN: "综合",
}

_MAXIMS = {
    FLAG_SLOW: ["棋从宽处断，急所胜于大场", "缓手等于让先，先手就是主动权", "先看急所，再看大场"],
    FLAG_BAD: ["敌之要点即我之要点", "不入虎穴，焉得虎子——但要先算清", "凡尖无恶手，凡飞无善手"],
    FLAG_BLUNDER: ["一着不慎，满盘皆输", "落子前先问：这块棋的气够吗", "先算死活，再谈模样"],
    K_ORDER: ["宁失一子，不失一先", "次序错了，好手也成俗手"],
    K_RISK: ["金柜角里藏生死", "先看自己几口气，再看对方薄不薄"],
    K_GREEDY: ["弃子取势，弃子争先", "吃三子不如占一边"],
    K_SIZE: ["官子二目半，先手抵三目", "大小要算双方最佳应对"],
    K_DIR: ["敌之要点即我之要点", "棋盘有四个角，别只盯着一处"],
    K_LOCAL: ["入界宜缓，遇强则迂", "棋形正，着手就正"],
    K_TIME: ["落后要搅，领先要稳", "劣势下先手比实地值钱"],
    K_GEN: ["复盘一次，胜过对局十次"],
}

# 逐手话术。注意：前端是纯文本渲染（不解析 Markdown），所以强调只用「」，
# 写 **粗体** 会原样显出星号。
_KIND_REASON = {
    K_ORDER: "{you}这一手其实落在引擎首选的变化里——选点没错，是「次序」不对："
             "它属于后面才会出现的交换，现在走等于把变化提前定型。",
    K_RISK: "这手棋目数上看着亏得不多，但胜率掉得明显：这类损失一般不在「大小」上，"
            "而在「棋的安危与厚薄」上——要么把这块棋的根据送掉了，要么给对方留下了攻击目标。",
    K_GREEDY: "这手提掉了对方 {captures} 子，但提子本身不是目的："
              "引擎判定这一手的代价大于所得，属于典型的「吃小失大」。",
    K_SIZE: "官子阶段比的就是把每一手换成确定的目数。这手棋的价值排序靠后："
            "同一时间点上，盘上还有更大的官子或先手。",
    K_DIR: "这一手与引擎首选不在同一片战场：方向判断的偏差，通常来自「对方最该落子的"
           "地方」和「自己想落子的地方」不一致。",
    K_LOCAL: "方向与引擎一致（就在首选附近），问题出在「具体手段」上："
             "同一带里换一种应法（虎、跳、粘、扳的方向）损失会小很多。",
    K_TIME: "从数据看这是该局最需要抢手的时刻，而这手棋偏缓："
            "劣势或胶着时，先手的价值高于一块实地。",
    K_GEN: "这手棋被判定为{flag_label}：不是完全不能下，而是在这个时间点上不最优。",
}

# advice 的结语：按分类给一条可执行的思路（仅用于玩家自己的手）
_ADVICE_TAIL = {
    K_ORDER: "这手留着在正确的时机走更值，现在先处理更急的地方",
    K_RISK: "先检查自己块棋的气与断点，再考虑进攻",
    K_GREEDY: "提子前先算清：提完之后得到的目，比对方得到的大吗",
    K_SIZE: "官子要算双方最佳应答后的实际出入，先找先手官子",
    K_DIR: "先问自己：对方下一手最想走哪里，那通常就是这一手该去的地方",
    K_LOCAL: "同一带里换种应法试试，比较两者的气与外势",
    K_TIME: "劣势下优先找能争先手的手段，而不是补自己的空",
    K_GEN: "可对比两者的差别：一个抢到了要点，一个只是跟着应",
}


def _pt_ok(p) -> bool:
    """坐标可用吗？虚手的落点是 `None`，而候选点也可能没坐标，两者都要挡住。

    只判 `p is None` 是不够的：虚手传进来的是 `(None, None)` 这个**非空元组**，
    直接 `int(p[0])` 会抛 TypeError（实测踩到：确认终局时双方虚手会进阶段统计）。
    """
    return bool(p) and len(p) >= 2 and p[0] is not None and p[1] is not None


def _gap(a: Optional[tuple], b: Optional[tuple]) -> Optional[int]:
    """两点的切比雪夫距离（围棋里判「是不是同一片战场」的粗度量）。"""
    if not _pt_ok(a) or not _pt_ok(b):
        return None
    return max(abs(int(a[0]) - int(b[0])), abs(int(a[1]) - int(b[1])))


def _bearing(from_pt: Optional[tuple], to_pt: Optional[tuple], size: int) -> str:
    """目标点相对落点的方位词。

    坐标系是核对过渲染代码才定的：内部 x 大 = 屏幕右，内部 y 大 = 屏幕上
    （GoBoard.toPixel 用 `py = pad + (size-1-y)*cell`，与 GTP 数字大靠上一致）。
    方位说反比不说更糟，所以这里不猜，只按上面这条换算。
    """
    if not _pt_ok(from_pt) or not _pt_ok(to_pt):
        return ""
    dx, dy = int(to_pt[0]) - int(from_pt[0]), int(to_pt[1]) - int(from_pt[1])
    if max(abs(dx), abs(dy)) < 2:
        return "就在附近"
    horiz = "右" if dx > 0 else ("左" if dx < 0 else "")
    vert = "上" if dy > 0 else ("下" if dy < 0 else "")
    if horiz and vert:
        return f"往{horiz}{vert}方"
    return f"往{horiz or vert}方"


def _in_pv(report: dict) -> bool:
    """玩家落点是否出现在**引擎首选的后续变化**里（说明只是次序问题）。

    variation 是 pvPoints：首选之后的行棋序列。第 0 个就是首选本身，
    它等于玩家落点时损失会是 0、根本进不到这里，所以从第 1 个开始看。
    """
    x, y = report.get("x"), report.get("y")
    if x is None or y is None:
        return False
    for i, p in enumerate(report.get("variation") or []):
        if i == 0 or not p or len(p) < 2 or p[0] is None:
            continue
        if int(p[0]) == int(x) and int(p[1]) == int(y):
            return True
    return False


def diagnose(report: dict, size: int, low_confidence: bool) -> str:
    """把一手问题手归类。顺序按「越具体的信号越优先」，命中即止：

      次序（pv 里出现了这个点）> 安危（目损小但胜率掉得多）> 贪吃（提子）
      > 官子（阶段）> 方向 / 局部（与首选的距离）> 时机 > 综合。

    `low_confidence` 为真（未装 KataGo）时跳过「安危」这一类：它的判据是
    目损与胜率损的背离，而启发式引擎两个数都不准，凑不出这个信号。
    """
    ph = phase_of(report["moveNum"], report.get("totalMoves") or 0, size)
    loss = report.get("lossPoints") or 0.0
    wr = report.get("lossWinrate")
    best = report.get("bestMove") or {}
    if report.get("x") is None:
        # 虚手：没有落点，距离、方位、棋形全都无从谈起，不猜分类。
        # （虚手确实可能拿到 lossPoints：同节点算法探不到它，会退回跳节点差值。）
        return K_GEN
    dist = _gap((report.get("x"), report.get("y")),
                (best.get("x"), best.get("y")))
    if _in_pv(report):
        return K_ORDER
    if not low_confidence and wr is not None and wr >= RISK_MIN_WR \
            and wr > max(loss, 0.5) * RISK_RATIO:
        return K_RISK
    if (report.get("captures") or 0) >= 3:
        return K_GREEDY
    if ph == "endgame":
        return K_SIZE
    if dist is not None:
        if dist >= max(4, size // 3):
            return K_DIR
        if dist <= max(2, size // 9):
            return K_LOCAL
    if report["flag"] == FLAG_SLOW:
        before = report.get("scoreBefore")
        if before is not None and not low_confidence and before < -BEHIND_POINTS:
            return K_TIME
    return K_GEN


def template_comment(report: dict, total_moves: int, size: int,
                     low_confidence: bool = False) -> dict:
    """无大模型时的逐手讲解：把引擎给的数据组装成「为什么 + 怎么想 + 一句棋理」。

    与旧版（三种旗子各一句固定话术）的差别在于多用了五个信号：与首选的
    距离与方位、落点在候选中的排名、落点是否在首选变化里、提子数、
    这一手前后的形势。这些都不需要新数据，只是原先没用上。
    """
    flag = report["flag"]
    if flag not in (FLAG_SLOW, FLAG_BAD, FLAG_BLUNDER):
        return {}
    ph = PHASE_LABEL[phase_of(report["moveNum"], total_moves, size)]
    kind = diagnose({**report, "totalMoves": total_moves}, size, low_confidence)
    loss = report.get("lossPoints") or 0.0
    wr = report.get("lossWinrate")
    # 人称：worker 对 AI 的问题手也生成讲解（玩家点开 AI 那一手也想看为什么），
    # 而 scoreBefore/scoreAfter 是**走子方视角**，所以不能一律说「你」——
    # 实测踩过：AI 的手被写成「走这手之前你还领先 4.8 目」，张冠李戴。
    is_mine = bool(report.get("isPlayer", True))
    who = "你" if is_mine else ("黑方" if report["color"] == BLACK else "白方")

    # ---- 为什么不好 ----
    reason = _KIND_REASON[kind].format(
        captures=report.get("captures") or 0, flag_label=FLAG_LABEL[flag], you=who)
    evidence = []
    if low_confidence:
        evidence.append(f"粗估损失 {loss:.1f} 目（无 KataGo，此值仅参考）")
    else:
        evidence.append(f"损失 {loss:.1f} 目")
        if wr:
            evidence.append(f"胜率下降 {wr * 100:.1f} 个百分点")
    before, after = report.get("scoreBefore"), report.get("scoreAfter")
    if before is not None and after is not None and not low_confidence:
        if before >= BEHIND_POINTS:
            evidence.append(f"走这手之前{who}还领先 {before:.1f} 目，之后缩到 {after:.1f} 目")
        elif before <= -BEHIND_POINTS:
            evidence.append(f"当时{who}已落后 {abs(before):.1f} 目，这手没能把差距追回来"
                            f"（{after:+.1f} 目）")
    if report.get("captures"):
        evidence.append(f"提掉 {report['captures']} 子")
    rank = report.get("playerRank")
    if rank is not None:
        evidence.append(f"{who}的落点在引擎候选里排第 {rank + 1}"
                        + ("，说明它本来就在考虑范围内" if rank <= 3 else
                           "，前排还有多处更优"))
    else:
        evidence.append("这手没进入引擎考虑的前列")
    reason += "具体数据：" + "；".join(evidence) + "。"

    # ---- 怎么想 ----
    best = report.get("bestMove") or {}
    bgtp = best.get("gtp")
    if bgtp:
        bpt = (best.get("x"), best.get("y")) if best.get("x") is not None else None
        dist = _gap((report.get("x"), report.get("y")), bpt)
        advice = f"引擎首选 {bgtp}"
        if dist is not None:
            advice += f"，离{who}这手 {dist} 路（{_bearing((report.get('x'), report.get('y')), bpt, size)}）"
        bw = best.get("winrate")
        if bw is not None and not low_confidence:
            advice += f"，该点胜率 {float(bw) * 100:.1f}%"
        if report.get("variationGtp"):
            advice += f"；它的后续大致是 {' '.join(report['variationGtp'][:4])}"
        # AI 的手不讲「你该怎么想」，而是提醒学生去读对方的意图
        advice += ("。" + _ADVICE_TAIL[kind] + "。") if is_mine else \
            "。想想这手想成什么事——它要成的那个，正是你该提前破坏的地方。"
    else:
        advice = "引擎没有给出明确首选（该点分析数据不足），建议按本阶段的常规思路重想一遍。"
    maxim = _MAXIMS[kind][report["moveNum"] % len(_MAXIMS[kind])]
    return {"reason": f"（{ph}阶段）{reason}", "advice": advice, "maxim": maxim}


def kind_breakdown(reports: list[dict], size: int, low_confidence: bool) -> dict:
    """问题手按教学分类计数——总结里最有信息量的一句。

    单条讲解只能说「这一手怎么不好」，计数能回答「你是哪一类反复错」：
    方向总是跑错的人与偶尔算错气的人，训练重点完全不同。

    只统计**玩家**的手：`reports` 里 AI 的落点也在（讲解需要它们），
    不过滤就会把 AI 的失误算到学生头上。这道过滤在内层做掉，
    不依赖调用方传对。
    """
    out: dict[str, int] = {}
    mine = [r for r in (reports or []) if r.get("isPlayer")]
    for r in mine:
        if r.get("flag") not in (FLAG_SLOW, FLAG_BAD, FLAG_BLUNDER):
            continue
        kind = diagnose({**r, "totalMoves": len(mine)}, size, low_confidence)
        out[kind] = out.get(kind, 0) + 1
    return out


def _fmt(v) -> str:
    """损失目数一律留 1 位小数：吻合度算到 0.836 目是假精确，数据本身不支持。"""
    return "—" if v is None else f"{float(v):.1f}"


def _phase_bars(pro_rank: bool) -> tuple:
    """阶段评价阈值。同一个「平均每手损失」，对段位学生和级位学生意思不一样：
    高段位本该损失更小，所以给段位的评价收紧一档。"""
    return (0.6, 1.5, 3.0) if pro_rank else (1.0, 2.5, 5.0)


def template_summary(ctx: dict, phases: dict, counts: dict,
                     accuracy: Optional[float], ai_accuracy: Optional[float],
                     moments: Optional[list[dict]] = None,
                     reports: Optional[list[dict]] = None) -> dict:
    """无大模型时的全局总结。

    比旧版多用了三块已有数据：转折点（moments）、问题手分类分布（从 reports 现场算）、
    玩家等级（旧版取了 rankName 但整段没用上）。`moments` / `reports` 有默认值，
    使旧调用点不会错；但 worker 会传全。`reports` 可以传整盘（含 AI），
    误区统计只按玩家的手算（见 kind_breakdown）。
    """
    moments = moments or []
    reports = reports or []
    rank = ctx.get("rankName", "")
    pro_rank = "段" in rank
    result = ctx.get("resultText", "")
    # 未结算的局（对局中预览报告）没有结果文本，不能写成「本局结果：。」
    result_head = f"本局结果：{result}。" if result else "本局尚未结算。"
    low_confidence = ctx.get("engine") != "katago"
    bars = _phase_bars(pro_rank)
    breakdown = kind_breakdown(reports or [], ctx.get("size", 19), low_confidence)

    phase_texts = {}
    for key, label in PHASE_LABEL.items():
        p = phases.get(key) or {}
        avg = p.get("avgLoss")
        if not p or avg is None:
            phase_texts[key] = f"{label}阶段可分析的手数不足，暂无评语。"
            continue
        if low_confidence:
            # 无 KataGo 时不能拿损失目数断言绝对好坏（那个数本身就不准），
            # 但「哪一段相对损失多」仍可用——那是用同一把歪尺子量出来的比较。
            tone = "（未装 KataGo，此处只作各段横向比较，不代表绝对水平。）"
        elif avg <= bars[0]:
            tone = "几乎没有明显损失，这一段的计算是可信的。"
        elif avg <= bars[1]:
            tone = "整体平稳，个别地方还可以再紧凑。"
        elif avg <= bars[2]:
            tone = "攒了若干缓手，主要是大小与先手的判断。"
        else:
            tone = "损失偏大，建议先补基础棋形与死活。"
        detail = ""
        if p.get("worstMoveNum"):
            worst_r = next((r for r in reports
                            if r.get("moveNum") == p["worstMoveNum"]), None)
            kind_txt = ""
            if worst_r is not None:
                kind_txt = "，属于" + _KIND_LABEL[
                    diagnose({**worst_r, "totalMoves": ctx.get("totalMoves", 0)},
                             ctx.get("size", 19), low_confidence)] + "类问题"
            detail = (f"其中第 {p['worstMoveNum']} 手损失最大（{_fmt(p['worstLoss'])} 目）{kind_txt}，"
                      f"这一段累计损失 {_fmt(p.get('totalLoss'))} 目。")
        phase_texts[key] = (f"{label}阶段共 {p.get('moves', 0)} 手，平均每手损失 {_fmt(avg)} 目。"
                            f"{tone}{detail}")

    # ---- 胜负关键：把转折点说成一句话，而不是只罗列统计 ----
    pivot = ""
    # top_moments 里的 swing 是**黑方视角**的胜率变化，所以得知道玩家执什么颜色
    # 才能说清这一手对玩家是好事还是坏事。用现成的 playerColorName，不新增契约。
    player_is_black = ctx.get("playerColorName", "黑") == "黑"
    mine = [m for m in moments if m.get("isPlayer")]
    if mine:
        m = max(mine, key=lambda x: abs(x.get("swing") or 0))
        # 黑方视角的升降换算成玩家视角：执黑时同向，执白时反向
        up = (m["swing"] > 0) == player_is_black
        pivot = (f"全局起伏最大的是你的第 {m['moveNum']} 手（{m.get('gtp')}）："
                 f"黑方胜率当场{'升' if m['swing'] > 0 else '降'} "
                 f"{abs(m['swing']) * 100:.1f} 个百分点，对你而言是"
                 f"{'得分' if up else '丢分'}，第 {m['moveNum']} 手前后就是本局的胜负分界。")
    top_kind = max(breakdown, key=breakdown.get) if breakdown else None
    habit = ""
    if top_kind and breakdown[top_kind] >= 2:
        habit = (f"把 {sum(breakdown.values())} 处问题手按类型拆开看，"
                 f"最多的是「{_KIND_LABEL[top_kind]}」({breakdown[top_kind]} 处)——"
                 f"这是本局比较固定的思维误区，比偶发的一两手大漏更值得练。")

    total_bad = counts.get("bad", 0) + counts.get("blunder", 0)
    if low_confidence:
        overall = (
            f"{result_head}共 {counts.get('blunder', 0)} 次大恶手、"
            f"{counts.get('bad', 0)} 次恶手、{counts.get('slow', 0)} 次缓手。"
            f"口径提醒：本机未检测到 KataGo，下面的胜率、损失目数、吻合度都来自"
            f"内置启发式引擎，只能粗看；而坐标、与首选点的距离、提子数、候选排名"
            f"这些不依赖估值的量仍可信。装 KataGo 后重新生成报告即可得到职业级精度。"
            f"{habit}{pivot}")
    elif accuracy is None:
        overall = f"{result_head}分析数据不足，无法给出吻合度评价。{pivot}"
    else:
        level = ("接近职业水准" if accuracy <= 0.8 else
                 "高水平业余" if accuracy <= 1.5 else
                 "稳健的业余棋手" if accuracy <= 3.0 else
                 "仍处在上升期" if accuracy <= 6.0 else "需要系统训练")
        overall = (f"{result_head}你的吻合度为平均每手损失 {_fmt(accuracy)} 目，"
                   f"属于「{level}」；AI 为 {_fmt(ai_accuracy)} 目（AI 是对局时的档位棋风）。"
                   f"判定基准是**不拟人的最强引擎最优解**，与对局页的提示同口径："
                   f"照着提示下，复盘看到的就是最优。"
                   f"全局共 {counts.get('blunder', 0)} 次大恶手、{counts.get('bad', 0)} 次恶手、"
                   f"{counts.get('slow', 0)} 次缓手，其中恶手以上 {total_bad} 手。"
                   f"{habit}{pivot}")

    # ---- 训练建议：按实际误区选，并指向平台已有的东西 ----
    training = []
    if breakdown.get(K_DIR):
        training.append(
            f"练「战场方向」：落子前先花三秒问自己——对方下一手最想走哪里？"
            f"本局有 {breakdown[K_DIR]} 手偏在这条判断上。")
    if breakdown.get(K_RISK):
        training.append(
            "练死活与棋形：每天 10 道根底死活题（死活练习页「死活」题型），"
            "重点是「一眼看杀气」，避免把自己的根据送掉。")
    elif breakdown.get(K_GREEDY):
        training.append(
            "练弃取：提子前先问“提完之后我得到的目，比对方得到的大吗”；"
            "配合做“吃子手筋”题型，把收气与倒扑算清。")
    if breakdown.get(K_LOCAL):
        training.append(
            "练局部手段：同一带里把扳/虎/跳/粘几种应法都算一遍再选，"
            "死活练习页的「吃子手筋」与「对杀」题型就是练这个的。")
    if breakdown.get(K_ORDER):
        training.append(
            "练次序：先把先手交换完再补棋。悔棋一步、看看换个次序后胜率如何，"
            "是比多做两道题更快的改法。")
    if breakdown.get(K_TIME):
        training.append(
            "练形势判断：落后时要主动开劫、拆边逼住或弃子取势，"
            "而不是跟着对方应。先手在劣势下比一块实地值钱。")
    if breakdown.get(K_SIZE):
        training.append(
            "练官子：从 9 路小官子题开始，掌握先手官子与后手官子的价值排序。"
            "（平台目前无官子专项题型，可先用 9 路死活题练计算准确度。）")
    if counts.get("blunder", 0) > 0:
        training.append(f"本局有 {counts['blunder']} 手大恶手（损失≥"
                        f"{settings.mistake_blunder:g} 目），这类基本都是漏算而非不懂，"
                        "对局时每一手先检查自己块棋的气与被断点。")
    if total_bad == 0:
        training.append("本局没有出现恶手以上的问题，可以挑战高一档的对手检验棋力。")
    if not training:
        training.append("复盘时重点标注每一处损失超过 2 目的棋，找出共同的思维误区。")
    training = training[:5]
    maxim = _MAXIMS[top_kind][0] if top_kind and top_kind in _MAXIMS \
        else "复盘一次，胜过对局十次。"
    return {
        **phase_texts,
        "overall": overall,
        "training": training,
        "maxim": maxim,
    }
