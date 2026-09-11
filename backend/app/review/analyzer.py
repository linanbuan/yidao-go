"""复盘数据分析：把逐手引擎分析转成"问题手报告 + 吻合度 + 阶段统计"。

数据流：analyses[i] = 第 i 手之后的局面（i=0 为开局）。
对第 j 手（1-based）而言：
    走子前局面 = analyses[j-1]，走子后局面 = analyses[j]

损失目数的两种口径（优先用前者）：
  1. **同节点**：走子前那一手分析里，引擎首选与实际落点的目差。
     这就是 KataGo 官方的 points-lost 定义（rootInfo 与 moveInfo 同属一个节点），
     也是唯一对任何估值函数都成立的口径：两个值子数相同、先后手相同。
  2. **跳节点**：走子前目差 - 走子后目差。依赖引擎值函数能正确体现
     "多下一子不是白得的"（KataGo 能，启发式引擎不能），因此仅作兜底。
     它的额外好处是会把对方应手的质量也算进来，更贴近实际得失。
"""
from __future__ import annotations

from typing import Optional

from ..config import settings
from ..game.rules import BLACK, WHITE

FLAG_GOOD = "good"
FLAG_SLOW = "slow"
FLAG_BAD = "bad"
FLAG_BLUNDER = "blunder"
FLAG_PASS = "pass"

FLAG_LABEL = {
    FLAG_GOOD: "好手",
    FLAG_SLOW: "缓手",
    FLAG_BAD: "恶手",
    FLAG_BLUNDER: "大恶手",
    FLAG_PASS: "虚手",
}


def _wr(a: Optional[dict], color: int) -> Optional[float]:
    if not a or a.get("missing"):
        return None
    return a.get("winrateBlack") if color == BLACK else a.get("winrateWhite")


def _sc(a: Optional[dict], color: int) -> Optional[float]:
    if not a or a.get("missing"):
        return None
    sl = a.get("scoreLead")
    if sl is None:
        return None
    return float(sl) if color == BLACK else -float(sl)


def classify(loss: float) -> str:
    if loss >= settings.mistake_blunder:
        return FLAG_BLUNDER
    if loss >= settings.mistake_bad:
        return FLAG_BAD
    if loss >= settings.mistake_slow:
        return FLAG_SLOW
    return FLAG_GOOD


def _find_candidate(before: Optional[dict], mv: dict) -> Optional[dict]:
    if not before or before.get("missing") or mv.get("x") is None:
        return None
    for c in before.get("candidates") or []:
        if c.get("x") == mv.get("x") and c.get("y") == mv.get("y"):
            return c
    return None


def node_loss(before: Optional[dict], mv: dict, color: int) -> Optional[float]:
    """同节点损失目数：引擎首选 vs 实际落点（走子方视角，正数 = 损失）。

    实际落点不在候选列表里时返回 None（说明这手比所有候选都差，
    但差多少无法从现有数据得知，由调用方决定退化策略）。
    """
    if not before or before.get("missing") or mv.get("x") is None:
        return None
    cands = [c for c in (before.get("candidates") or [])
             if c.get("scoreLead") is not None]
    if not cands:
        return None
    sign = 1.0 if color == BLACK else -1.0
    played = _find_candidate(before, mv)
    if played is None or played.get("scoreLead") is None:
        return None
    best = max(sign * float(c["scoreLead"]) for c in cands)
    return max(0.0, best - sign * float(played["scoreLead"]))


def node_loss_winrate(before: Optional[dict], mv: dict) -> Optional[float]:
    """同节点胜率损失：候选的 winrate 已是走子方视角，无需再换算。"""
    if not before or before.get("missing") or mv.get("x") is None:
        return None
    cands = [c for c in (before.get("candidates") or []) if c.get("winrate") is not None]
    played = _find_candidate(before, mv)
    if not cands or played is None or played.get("winrate") is None:
        return None
    return max(0.0, max(float(c["winrate"]) for c in cands) - float(played["winrate"]))


def build_move_reports(moves: list[dict], analyses: list[dict], size: int,
                       player_color: int) -> list[dict]:
    reports: list[dict] = []
    for idx, mv in enumerate(moves):
        j = idx + 1                       # 1-based 手数
        color = int(mv.get("color", BLACK))
        before = analyses[idx] if idx < len(analyses) else None
        after = analyses[idx + 1] if idx + 1 < len(analyses) else None

        wb, wa = _wr(before, color), _wr(after, color)
        sb, sa = _sc(before, color), _sc(after, color)
        is_pass = mv.get("x") is None

        loss_points: Optional[float] = None
        loss_winrate: Optional[float] = None
        within = node_loss(before, mv, color)
        if within is not None:
            loss_points = round(within, 2)
        elif sb is not None and sa is not None:
            loss_points = round(max(0.0, sb - sa), 2)
        within_wr = node_loss_winrate(before, mv)
        if within_wr is not None:
            loss_winrate = round(within_wr, 4)
        elif wb is not None and wa is not None:
            loss_winrate = round(max(0.0, wb - wa), 4)

        if is_pass:
            flag = FLAG_PASS
        elif loss_points is None:
            flag = FLAG_GOOD       # 缺少分析数据时不诬陷玩家
        else:
            flag = classify(loss_points)

        # 引擎在该手之前的首选与玩家实际落点的排名
        best = None
        player_rank = None
        if before and before.get("candidates"):
            cands = before["candidates"]
            best = cands[0]
            for rank, c in enumerate(cands):
                if c.get("x") == mv.get("x") and c.get("y") == mv.get("y"):
                    player_rank = rank
                    break

        reports.append({
            "moveNum": j,
            "ply": idx + 1,
            "color": color,
            "isPlayer": color == player_color,
            "x": mv.get("x"),
            "y": mv.get("y"),
            "gtp": mv.get("gtp") or ("pass" if is_pass else ""),
            "captures": len(mv.get("captures") or []),
            "winrateBefore": round(wb, 4) if wb is not None else None,
            "winrateAfter": round(wa, 4) if wa is not None else None,
            "scoreBefore": round(sb, 2) if sb is not None else None,
            "scoreAfter": round(sa, 2) if sa is not None else None,
            "lossPoints": loss_points,
            "lossWinrate": loss_winrate,
            "flag": flag,
            "flagLabel": FLAG_LABEL[flag],
            "bestMove": best,
            "playerRank": player_rank,
            "variation": (best or {}).get("pvPoints") or [],
            "variationGtp": (best or {}).get("pv") or [],
        })
    return reports


def accuracy_of(reports: list[dict], color: int) -> Optional[float]:
    losses = [r["lossPoints"] for r in reports
              if r["color"] == color and r["flag"] != FLAG_PASS and r["lossPoints"] is not None]
    if not losses:
        return None
    return round(sum(losses) / len(losses), 3)


def flag_counts(reports: list[dict], color: int) -> dict:
    counts = {FLAG_GOOD: 0, FLAG_SLOW: 0, FLAG_BAD: 0, FLAG_BLUNDER: 0, FLAG_PASS: 0}
    for r in reports:
        if r["color"] == color:
            counts[r["flag"]] = counts.get(r["flag"], 0) + 1
    return counts


def phase_of(move_num: int, total: int, size: int) -> str:
    """布局 / 中盘 / 官子 三阶段划分（按手数与棋盘规模的启发式）。"""
    opening_end = max(20, size * 2)
    endgame_start = max(opening_end + 10, int(total * 0.72))
    if move_num <= opening_end:
        return "opening"
    if move_num > endgame_start:
        return "endgame"
    return "middle"


PHASE_LABEL = {"opening": "布局", "middle": "中盘", "endgame": "官子"}


def phase_stats(reports: list[dict], color: int, total_moves: int, size: int) -> dict:
    buckets: dict[str, list[float]] = {"opening": [], "middle": [], "endgame": []}
    worst: dict[str, Optional[dict]] = {"opening": None, "middle": None, "endgame": None}
    for r in reports:
        if r["color"] != color:
            continue
        ph = phase_of(r["moveNum"], total_moves, size)
        if r["lossPoints"] is not None:
            buckets[ph].append(r["lossPoints"])
            cur = worst[ph]
            if cur is None or r["lossPoints"] > (cur.get("lossPoints") or 0):
                worst[ph] = r
    out = {}
    for ph, losses in buckets.items():
        out[ph] = {
            "label": PHASE_LABEL[ph],
            "moves": len(losses),
            "avgLoss": round(sum(losses) / len(losses), 2) if losses else None,
            "totalLoss": round(sum(losses), 1) if losses else 0.0,
            "worstMoveNum": worst[ph]["moveNum"] if worst[ph] else None,
            "worstLoss": worst[ph]["lossPoints"] if worst[ph] else None,
        }
    return out


def key_moves(reports: list[dict], color: int, limit: int = 12) -> list[dict]:
    """按损失排序取关键手（喂给 LLM 分批讲解）。"""
    flagged = [r for r in reports
               if r["color"] == color and r["flag"] in (FLAG_SLOW, FLAG_BAD, FLAG_BLUNDER)]
    flagged.sort(key=lambda r: -(r.get("lossPoints") or 0))
    picked = flagged[:limit]
    picked.sort(key=lambda r: r["moveNum"])
    return picked


def build_curve(analyses: list[dict]) -> list[dict]:
    curve = []
    for i, a in enumerate(analyses):
        if not a or a.get("missing"):
            continue
        curve.append({
            "ply": i,
            "winrateBlack": a.get("winrateBlack"),
            "winrateWhite": a.get("winrateWhite"),
            "scoreLead": a.get("scoreLead"),
            "visits": a.get("visits"),
        })
    return curve


def top_moments(analyses: list[dict], moves: list[dict], player_color: int,
                limit: int = 3) -> list[dict]:
    """全局胜率波动最大的几手（无论好坏），用于总结时的"转折点"。"""
    swings: list[dict] = []
    for idx, mv in enumerate(moves):
        before, after = analyses[idx] if idx < len(analyses) else None, \
            analyses[idx + 1] if idx + 1 < len(analyses) else None
        wb, wa = _wr(before, BLACK), _wr(after, BLACK)
        if wb is None or wa is None:
            continue
        delta = wa - wb
        swings.append({
            "moveNum": idx + 1,
            "color": mv.get("color"),
            "isPlayer": mv.get("color") == player_color,
            "gtp": mv.get("gtp"),
            "swing": round(delta, 4),          # 黑视角胜率变化
            "abs": abs(delta),
        })
    swings.sort(key=lambda s: -s["abs"])
    return swings[:limit]
