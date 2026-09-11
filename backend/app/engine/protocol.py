"""KataGo analysis 协议封装：查询构造与结果解析。

视角约定（已用极端贴目 + 摆子实测校准，**不要凭文档猜**）：

  KataGo 侧（实测）：
  * winrate   —— 按「轮到走子的一方」报告（reportAnalysisWinratesAs = SIDETOMOVE）
  * scoreLead —— **同样按走子方视角**，正数 = 走子方领先。
                同一个黑必胜局面（komi=-30）：轮黑时 +25.0、轮白时 -24.8，
                符号跟着走子方翻而不是跟着黑方。
  * ownership —— 按走子方为正，且下标行0 = GTP 第9行（顶部）。

  本项目内部（两个引擎必须一致）：
  * winrate   —— 走子方视角（与 KataGo 同口径，不换算）
  * score_lead—— 黑方视角，正数 = 黑领先
  * ownership —— 白方为正，下标 = y * size + x，y=0 在 GTP 第1行（底部）

  换算全部集中在 parse_response / _convert_ownership 这一个边界，上层
  （analyzer 的损失目数、胜率曲线、终局死子判定、前端热力图）只认内部口径，
  避开视角混淆导致的曲线上下颠倒。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

from ..game.rules import BLACK, WHITE, Point, from_gtp


def _opt_float(v) -> Optional[float]:
    """KataGo 对不适用的字段会直接不给（比如没开 humanSLProfile 时没有 humanPrior）。"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _move_pair(m) -> list:
    """内部 move 转成 KataGo 要的 ["B"|"W", 顶点] 配对。

    宽容两种写法：字典 {"player":.., "move":..} 与已经是配对的序列，
    因为 rules.Game 与部分调用方用的不是同一种形式。
    """
    if isinstance(m, dict):
        color = str(m.get("player") or m.get("color") or "B")
        vertex = m.get("move") or m.get("gtp") or "pass"
    else:
        seq = list(m)
        color, vertex = (seq + ["B", "pass"])[:2]
        color = str(color)
    return [color.upper()[:1], str(vertex)]


@dataclass
class AnalysisQuery:
    size: int
    moves: list = field(default_factory=list)          # [{"player":"B","move":"Q16"}] 或 [["B","Q16"]]
    komi: float = 7.5
    rules: str = "chinese"
    initial_stones: list[list] = field(default_factory=list)
    initial_player: str = "B"
    max_visits: int = 64
    include_ownership: bool = True
    include_policy: bool = True
    analyze_turns: Optional[list[int]] = None           # 复盘：一次查询分析多手
    human_sl_profile: str = ""
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_json(self) -> dict:
        # 只放 analysis 引擎认识的**查询级**字段。多一个不认识的键，KataGo 会
        # 先用同一个 id 回一条 {"warning":"Unexpected or unused field"}，
        # 很容易被当成结果，把后续所有响应错位。具体踩过两个：
        #   reportAnalysisWinratesAs —— 配置级键（已写进 analysis.cfg），查询里不认；
        #   humanSLProfile          —— 必须放进 overrideSettings，放顶层会直接
        #                              FATAL ERROR 杀掉整个引擎进程；
        #   maxMoves                —— v1.17.1 既不是查询字段也不是配置键（两边都查过），
        #                              返回多少候选完全由 maxVisits 决定，所以已删除。
        q: dict = {
            "id": self.request_id,
            # moves 必须是**配对数组** [["B","Q16"], ["W","D4"]]，发成对象数组会直接报
            #   {"error":"Must be an array of pairs of the form: [\"b\" or \"w\", GTP board vertex]"}
            # 坑在于空数组两种写法都合法，所以只有真的下了子才会暴露
            "moves": [_move_pair(m) for m in self.moves],
            "rules": self.rules,
            "komi": self.komi,
            "boardXSize": self.size,
            "boardYSize": self.size,
            "maxVisits": self.max_visits,
            "includeOwnership": self.include_ownership,
            "includePolicy": self.include_policy,
        }
        if self.initial_stones:
            q["initialStones"] = [_move_pair(s) for s in self.initial_stones]
            q["initialPlayer"] = self.initial_player
        if self.analyze_turns:
            q["analyzeTurns"] = self.analyze_turns
        if self.human_sl_profile:
            q["overrideSettings"] = {"humanSLProfile": self.human_sl_profile}
        return q


@dataclass
class Candidate:
    """一个候选点（moveInfos / moveAnalysis 中的一项）。"""
    point: Optional[Point]        # None = pass
    gtp: str
    visits: int = 0
    winrate: float = 0.5          # 轮到走子方视角
    score_lead: float = 0.0       # 黑方视角
    score_stdev: float = 0.0
    policy: Optional[float] = None
    # human SL 网络对「该档人类会走这手」的概率（仅在查询带 humanSLProfile 时返回）。
    # **不要拿它当选点采样基准**：它的 argmax 与引擎最优点不是同一个点，实测会让
    # 高段反而更弱（七段每手亏 1.68 目、五段只亏 0.39 目）。选点走「亏多少目」的
    # softmax（见 pool._tolerance_weights）；这个字段只做展示与引擎体检用。
    human_prior: Optional[float] = None
    play_selection: Optional[float] = None   # KataGo 自带的拟人采样值
    pv: list[str] = field(default_factory=list)   # 后续最佳应对（GTP 坐标）
    rank: int = 0

    @property
    def is_pass(self) -> bool:
        return self.point is None

    def to_dict(self, size: int) -> dict:
        return {
            "gtp": self.gtp,
            "x": None if self.point is None else self.point[0],
            "y": None if self.point is None else self.point[1],
            "visits": self.visits,
            "winrate": round(self.winrate, 4),
            "scoreLead": round(self.score_lead, 2),
            "policy": self.policy,
            "humanPrior": self.human_prior,
            "pv": self.pv[:8],
            "pvPoints": _pv_points(self.pv[:8], size),
            "rank": self.rank,
        }


def _pv_points(pv: list[str], size: int) -> list[list[Optional[int]]]:
    out = []
    for mv in pv:
        try:
            p = from_gtp(mv, size)
            out.append([p[0], p[1]])
        except ValueError:
            out.append([None, None])   # pass
    return out


@dataclass
class AnalysisResult:
    """一次分析的结果（单手）。"""
    turn: int = 0
    side_to_move: int = BLACK
    candidates: list[Candidate] = field(default_factory=list)
    winrate: float = 0.5            # 轮到走子方视角（rootInfo）
    score_lead: float = 0.0         # 黑方视角（已在 parse_response 换算过）
    visits: int = 0
    ownership: list[float] = field(default_factory=list)   # size*size，白方为正，y=0 在 GTP 第1行
    policy: list[float] = field(default_factory=list)
    notes: str = ""
    raw: dict = field(default_factory=dict)
    engine: str = "unknown"

    # ---- 视角换算 ----
    @property
    def winrate_black(self) -> float:
        return self.winrate if self.side_to_move == BLACK else 1.0 - self.winrate

    @property
    def winrate_white(self) -> float:
        return 1.0 - self.winrate_black

    @property
    def score_lead_black(self) -> float:
        return self.score_lead

    def winrate_for(self, color: int) -> float:
        return self.winrate_black if color == BLACK else self.winrate_white

    @property
    def best_move(self) -> Optional[Point]:
        for c in self.candidates:
            if not c.is_pass:
                return c.point
        return None

    def ownership_black_view(self, size: int) -> list[float]:
        """统一成「黑方视角」：正 = 黑地，负 = 白地（前端热力图直接用）。"""
        return [-v for v in self.ownership] if self.ownership else []

    def to_dict(self, size: int, top_n: int = 5, include_ownership: bool = True) -> dict:
        d = {
            "turn": self.turn,
            "sideToMove": self.side_to_move,
            "winrate": round(self.winrate, 4),
            "winrateBlack": round(self.winrate_black, 4),
            "winrateWhite": round(self.winrate_white, 4),
            "scoreLead": round(self.score_lead, 2),
            "scoreLeadWhite": round(-self.score_lead, 2),
            "visits": self.visits,
            "notes": self.notes,
            "engine": self.engine,
            "candidates": [c.to_dict(size) for c in self.candidates[:top_n]],
        }
        if include_ownership and self.ownership:
            d["ownership"] = [round(v, 3) for v in self.ownership_black_view(size)]
        return d


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def _convert_ownership(raw, size: int, side_to_move: int) -> list[float]:
    """KataGo ownership → 本项目口径（白方为正、下标 y 从 GTP 第1行开始）。

    两处不一致，都是用摆子实测钉死的，不要凭直觉改：
      * 符号：KataGo 按**走子方**为正。实测：黑占全盘且白先走 → 均值 -0.44，
        白占全盘且黑先走 → -0.44，两次走子方都是劣势方，所以都是负。
      * 行序：KataGo 下标行0 = GTP 第9行（顶部）。实测：黑摆上边、白摆下边时，
        下标行0~1 是 +0.98、行7~8 是 -0.92；而内部 y=0 = GTP 第1行（底部），
        不翻转的话热力图上下颠倒、终局死子会全部认错行。
    """
    n = size * size
    if not raw or len(raw) != n:
        return []
    sign = -1.0 if side_to_move == BLACK else 1.0    # 黑先走时「正=黑」，取负得「正=白」
    out = [0.0] * n
    for r in range(size):                            # r = KataGo 行序，0 = 顶部
        y = size - 1 - r                             # 内部 y，0 = 底部
        base, dst = r * size, y * size
        for x in range(size):
            out[dst + x] = sign * float(raw[base + x])
    return out


def _parse_candidate(item: dict, size: int, rank: int, score_sign: float = 1.0) -> Candidate:
    mv = item.get("move")
    point: Optional[Point] = None
    if mv and str(mv).lower() != "pass":
        try:
            point = from_gtp(str(mv), size)
        except ValueError:
            point = None
    return Candidate(
        point=point,
        gtp=str(mv or "pass"),
        visits=int(item.get("visits", 0) or 0),
        # winrate 已是走子方视角（与 AnalysisResult.winrate 同口径），不用再换算
        winrate=float(item.get("winrate", 0.5) or 0.5),
        score_lead=score_sign * float(item.get("scoreLead", 0.0) or 0.0),
        score_stdev=float(item.get("scoreStdev", 0.0) or 0.0),
        policy=item.get("policy"),
        human_prior=_opt_float(item.get("humanPrior")),
        play_selection=_opt_float(item.get("playSelectionValue")),
        pv=[str(x) for x in (item.get("pv") or [])],
        rank=rank,
    )


def parse_response(data: dict, size: int, side_to_move: int, turn: int = 0,
                   engine_name: str = "katago") -> AnalysisResult:
    """解析 KataGo analysis engine 的单条响应，并把视角统一到本项目口径。"""
    root = data.get("rootInfo") or {}
    items = data.get("moveInfos") or data.get("moveAnalysis") or []
    # 走子方视角 → 黑方视角：白先走时整条 score 轴取负
    score_sign = 1.0 if side_to_move == BLACK else -1.0
    candidates = [_parse_candidate(it, size, i, score_sign) for i, it in enumerate(items)]
    # 按 visits 降序（KataGo 本身有序，兜底再排一次）
    candidates.sort(key=lambda c: (-c.visits, -c.winrate))
    for i, c in enumerate(candidates):
        c.rank = i

    ownership_raw = data.get("ownership") or []
    ownership = _convert_ownership(ownership_raw, size, side_to_move)
    policy = [float(v) for v in (data.get("policy") or [])]

    winrate = float(root.get("winrate", candidates[0].winrate if candidates else 0.5))
    if "scoreLead" in root:
        score_lead = score_sign * float(root["scoreLead"] or 0.0)
    else:
        # 兜底走候选值：它们已经换算过，不能再乘一次符号
        score_lead = candidates[0].score_lead if candidates else 0.0

    return AnalysisResult(
        turn=int(data.get("turnNumber", turn)),
        side_to_move=side_to_move,
        candidates=candidates,
        winrate=winrate,
        score_lead=score_lead,
        visits=int(root.get("visits", sum(c.visits for c in candidates) or 0)),
        ownership=ownership,
        policy=policy,
        notes=str(data.get("note") or ""),
        raw=data,
        engine=engine_name,
    )


def parse_multi_response(payload, size: int, side_to_move_seq: list[int],
                         engine_name: str = "katago") -> list[AnalysisResult]:
    """解析带 analyzeTurns 的批量响应（数组）。"""
    if isinstance(payload, dict):
        payload = [payload]
    out: list[AnalysisResult] = []
    for i, item in enumerate(payload):
        stm = side_to_move_seq[i] if i < len(side_to_move_seq) else BLACK
        out.append(parse_response(item, size, stm, turn=int(item.get("turnNumber", i)),
                                  engine_name=engine_name))
    return out
