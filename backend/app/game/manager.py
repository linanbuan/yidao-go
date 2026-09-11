"""对局管理器：把「规则引擎 + 引擎池 + 等级体系 + 持久化」编排成完整对局流程。

关键流程（玩家落子一次）：
  1. 本地规则校验并落子（合法性不依赖 KataGo）
  2. 分析落子后的局面 → 得到 AI 行棋局面（一次查询同时服务分析与选点）
  3. 检查 AI 是否达到自动认输条件
  4. AI 按等级画像选点并落子，立即回传（保证手感）
  5. 后台补一次分析 → 推送胜率曲线新点与提示（不阻塞玩家）

双方 pass 后进入「终局结算」阶段：引擎 ownership 自动判定死子 → 玩家可点选修正 →
确认后数子/数目出结果，写库并触发升降级与赛后复盘任务。
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy import func, select

from ..config import settings
from ..database import SessionLocal
from ..engine.pool import get_pool
from ..engine.protocol import AnalysisQuery, AnalysisResult
from ..models import GameRecord, User
from ..rank.defs import EngineProfile, get_engine_profile, get_rank
from ..rank.logic import progress_of, promotion_profile
from .rules import BLACK, EMPTY, WHITE, Game, IllegalMove, Point, color_name, other
from .sgf import export_sgf

logger = logging.getLogger("go.game")


def _strip_ownership(a: dict) -> dict:
    """去掉一条逐手分析里的 ownership 数组。

    ownership 是 size*size 个浮点（19 路 = 361 个，单手 JSON 约 4KB），而
    **持久化与快照传输都不需要它**：复盘 worker 自己就 include_ownership=False，
    曲线只吃 winrate/scoreLead。保留它的代价是每手 save 都序列化并整体写回
    整个 analyses 列（整局 O(n²)），快照也会膨胀到 MB 级。
    当前手的 ownership 仍留在内存（live.analyses）供实时热力图使用。
    """
    if not isinstance(a, dict) or "ownership" not in a:
        return a
    b = dict(a)
    b.pop("ownership", None)
    return b


def _strip_ownership_all(items: list, keep_last: bool = False) -> list:
    """批量剥 ownership；keep_last=True 时保留最后一个有效节点的（当前手热力图）。"""
    if not items:
        return items
    last = -1
    if keep_last:
        for i in range(len(items) - 1, -1, -1):
            if isinstance(items[i], dict) and not items[i].get("missing"):
                last = i
                break
    return [_strip_ownership(a) if i != last else a for i, a in enumerate(items)]



def draw_color() -> int:
    """猜先：随机决定玩家执黑还是执白（开局面板里选「抽取」时走这里）。

    用 `secrets` 而不是 `random`：本项目的 `Game.rng` 是**接受种子**的
    （`random.Random(seed)`），用同一个模块做猜先会让人怀疑结果可复现、可预测。
    猜先的公平性是规则的一部分，直接走操作系统熵源。
    """
    return secrets.choice((BLACK, WHITE))


@dataclass
class LiveGame:
    """内存中的活对局（状态同时落库，支持断线重连）。"""
    id: str
    user_id: str
    game: Game
    rank_id: int
    profile: EngineProfile
    ai_name: str
    is_promotion: bool = False
    hint_mode: bool = True
    allow_takeback: bool = True
    analyses: list[dict] = field(default_factory=list)     # analyses[i] = 第 i 手后的局面分析
    despair_plies: int = 0                                  # AI 连续处于绝望局面的手数
    move_seconds: int = 0                                   # 每手限时秒数，0=不限时
    move_deadline: Optional[float] = None                   # 本手到期的 epoch 秒；None=不在计时
    scoring_dead: list[Point] = field(default_factory=list)  # 终局阶段判定的死子
    phase: str = "playing"                                  # playing | scoring | finished
    engine_name: str = ""
    # 已强制结束/记录已删。只靠 phase == "finished" 拦不住在飞的 AI 回合：
    # _ai_turn 在 await 引擎期间对局可能被强制结束，醒来后接着跑就会调
    # finish("ai-resign") → _apply_rank，给玩家凭空记上一胜。
    abandoned: bool = False
    # 单调递增的「局面代次」：每次悔棋/结算回退/作废都 +1。
    # 后台补分析只靠手数（ply）对齐，而悔棋后手数可能**再次相等**却局面不同 ——
    # 那时旧分析会把新曲线覆盖成旧局面的数据。代次不匹配一律作废。
    generation: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _listeners: set = field(default_factory=set)
    _bg_tasks: set = field(default_factory=set)

    # ---- 事件推送 ----
    def bind(self, notify: Optional[Callable[[dict], None]]) -> None:
        """注册事件监听（同一对局可挂多个连接：断线重连 / 多标签页）。

        旧实现是单槽的：连接 A 断开时 finally 里的 bind(None) 会把连接 B 刚注册
        的监听一并摘掉，B 从此收不到任何事件、棋盘停摆却仍显示「已连接」。
        """
        if notify is not None:
            self._listeners.add(notify)

    def unbind(self, notify: Optional[Callable[[dict], None]]) -> None:
        if notify is not None:
            self._listeners.discard(notify)

    def emit(self, event: dict) -> None:
        for cb in list(self._listeners):
            try:
                cb(event)
            except Exception:   # noqa: BLE001
                # 监听回调已不可用（连接断开等）：摘掉，避免每个事件都白打一次
                self._listeners.discard(cb)
                logger.debug("事件推送失败（连接可能已断开）: %s", event.get("type"))

    def spawn(self, coro) -> None:
        try:
            task = asyncio.create_task(coro)
        except RuntimeError:
            # 没有运行中的事件循环（同步调用方 / 单元测试直接调 hub.create）：
            # 丢弃这个后台任务。看门狗不在这里跑也没关系，
            # expire_if_due 会在玩家下一次发指令时补上超时判定。
            coro.close()
            return
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # ---- 引擎查询 ----
    def build_query(self, max_visits: Optional[int] = None,
                    include_policy: bool = True) -> AnalysisQuery:
        g = self.game
        return AnalysisQuery(
            size=g.size,
            moves=g.gtp_moves(),
            komi=g.komi,
            rules="chinese" if g.score_method == "area" else "japanese",
            initial_stones=g.initial_stones_gtp(),
            initial_player="W" if g.handicap >= 2 else "B",
            max_visits=max_visits or self.profile.max_visits,
            include_ownership=True,
            include_policy=include_policy,
            human_sl_profile=self.profile.human_sl_profile if self.profile.use_human_model else "",
        )

    def record_analysis(self, result: AnalysisResult, index: Optional[int] = None,
                        top_n: int = 8) -> dict:
        point = result.to_dict(self.game.size, top_n=top_n)
        idx = len(self.game.moves) if index is None else index
        # analyses 与手数对齐：analyses[i] = i 手之后的局面
        while len(self.analyses) < idx:
            self.analyses.append({"turn": len(self.analyses), "missing": True})
        if len(self.analyses) == idx:
            self.analyses.append(point)
        else:
            self.analyses[idx] = point
        self.engine_name = result.engine
        return point

    def winrate_for(self, color: int, idx: int) -> Optional[float]:
        if idx < 0 or idx >= len(self.analyses):
            return None
        a = self.analyses[idx]
        return a.get("winrateBlack") if color == BLACK else a.get("winrateWhite")

    def score_for(self, color: int, idx: int) -> Optional[float]:
        if idx < 0 or idx >= len(self.analyses):
            return None
        a = self.analyses[idx]
        sl = a.get("scoreLead")
        if sl is None:
            return None
        return sl if color == BLACK else -sl

    @property
    def ai_color(self) -> int:
        return self.game.ai_color()

    @property
    def player_color(self) -> int:
        return self.game.player_color

    def seconds_left(self) -> Optional[int]:
        """本手剩余秒数（向上取整）；不在计时则 None。"""
        if self.move_deadline is None or self.phase != "playing":
            return None
        return max(0, math.ceil(self.move_deadline - time.time()))

    def snapshot(self) -> dict:
        g = self.game
        d = g.to_dict()
        d.update({
            "id": self.id,
            "phase": self.phase,
            "rankId": self.rank_id,
            "rankName": get_rank(self.rank_id).name,
            "aiName": self.ai_name,
            "isPromotion": self.is_promotion,
            "hintMode": self.hint_mode,
            "allowTakeback": self.allow_takeback,
            "engine": self.engine_name,
            "profile": {
                "maxVisits": self.profile.max_visits,
                "tolerance": self.profile.sample_tolerance,
                "ponder": self.profile.ponder,
            },
            # 全量下发 analyses 会到 MB 级（每手带 361 个 ownership 浮点），
            # 只保留最后一手的 ownership 供实时热力图，其余剥掉（曲线用不到）。
            "analyses": _strip_ownership_all(self.analyses, keep_last=True),
            "despairPlies": self.despair_plies,
            "moveSeconds": self.move_seconds,
            # 剩余秒数在快照这一刻算好下发：前端从它开始本地倒数，
            # 不传 deadline 时间戳是为了避开客户端与服务器的时钟偏差
            "moveSecondsLeft": self.seconds_left(),
            "scoringDead": [[p[0], p[1]] for p in self.scoring_dead],
            "curve": self.curve_payload(),
        })
        return d

    def curve_payload(self) -> list[dict]:
        """胜率曲线：每手一个点（黑视角胜率 + 黑视角目差）。"""
        out = []
        for i, a in enumerate(self.analyses):
            if a.get("missing"):
                continue
            out.append({
                "ply": i,
                "moveNum": i,
                "winrateBlack": a.get("winrateBlack"),
                "winrateWhite": a.get("winrateWhite"),
                "scoreLead": a.get("scoreLead"),
                "visits": a.get("visits"),
                "color": (self.game.moves[i - 1].color if 0 < i <= len(self.game.moves) else None),
            })
        return out

    # ---- AI 自动认输判定 ----
    def resign_limit(self) -> float:
        limit = self.profile.resign_score
        return settings.resign_score_threshold if limit is None else limit

    def resign_min_plies(self) -> int:
        """开局摆动不算认输理由：至少下满这么多手才开始判定。

        级位档前几十手的胜负评估抖得厉害（双方都在送），不限手数的话
        「领先 40 目」可能在第 12 手就成立，新手的一盘短棋会被腰斩。
        """
        return 2 * self.game.size

    def despair_margin(self, result: AnalysisResult) -> Optional[float]:
        """AI 的落后目数（仅在胜率已崩且过了最小手数时），否则 None。"""
        limit = self.resign_limit()
        if limit <= 0:
            return None
        if len(self.game.moves) < self.resign_min_plies():
            return None
        if result.winrate_for(self.ai_color) >= settings.resign_winrate_threshold:
            return None
        score_ai = result.score_lead if self.ai_color == BLACK else -result.score_lead
        return -score_ai if score_ai < -limit else None

    def update_despair(self, result: AnalysisResult) -> bool:
        """常规认输：连续若干手处于绝望局面。"""
        if self.despair_margin(result) is None:
            self.despair_plies = 0
            return False
        self.despair_plies += 1
        return self.despair_plies >= settings.resign_consecutive_moves

    def lost_with_nothing_to_play(self, result: AnalysisResult) -> bool:
        """无路可走且大败 → 该投子，而不是在原地无限虚手拖住对手。

        这是「AI 连续虚手但玩家赢不下来」那个抱怨的正解：引擎首推 pass 说明它
        认为没有值得下的点了，此时若落后超过一个棋盘宽度（size 目），人类棋手的
        做法是投子，而不是 pass 到对方不耐烦。
        阈值比常规认输小得多且不设最小手数，是因为「首推 pass + 棋盘已填满」
        本身就把中盘局面过滤掉了，剩下的只可能是终局。
        """
        if not self.should_pass(result):
            return False
        ai = self.ai_color
        if result.winrate_for(ai) >= settings.resign_winrate_threshold:
            return False
        score_ai = result.score_lead if ai == BLACK else -result.score_lead
        return score_ai < -self.game.size

    def should_pass(self, result: AnalysisResult) -> bool:
        """AI 主动虚手：引擎首推 pass，且局面已接近收官。"""
        if not result.candidates:
            return False
        top = result.candidates[0]
        if not top.is_pass:
            return False
        empty = len(self.game.board.empty_points())
        return empty <= self.game.size * 2


class GameHub:
    """活对局的注册表 + 持久化。

    `games` 字典会被**两个线程同时碰**：REST 的同步路由跑在 AnyIO 线程池里
    （delete / clear-history / active），而对局事件循环侧在 create / restore。
    没有保护时「线程池 remove」与「事件循环 create」可以同时发生，
    `dict` 迭代中变更大小会抛 `RuntimeError: dictionary changed size during
    iteration`（偶发 500）。所以：写操作用锁，只读迭代一律快照后再遍历。
    """

    def __init__(self):
        self.games: dict[str, LiveGame] = {}
        self._lock = threading.Lock()

    def _snapshot(self) -> list[LiveGame]:
        """先复制一份 values 再遍历：迭代中别人增删不再炸。"""
        with self._lock:
            return list(self.games.values())

    # ------------------------------------------------------------------
    def _load_record(self, game_id: str) -> Optional[GameRecord]:
        with SessionLocal() as db:
            return db.get(GameRecord, game_id)

    def create(self, user: User, *, size: int = 19, komi: float = 7.5, handicap: int = 0,
               player_color: int = BLACK, score_method: str = "area",
               hint_mode: Optional[bool] = None,
               move_seconds: Optional[int] = None) -> LiveGame:
        rank = get_rank(user.rank_id)
        is_promo = bool(user.in_promotion)
        profile = promotion_profile(user.rank_id) if is_promo else get_engine_profile(user.rank_id)
        # 让子棋由玩家执黑；否则按玩家选择（传 0 = 抽取，由服务端猜先）。
        # 来源（color_source）与结果一起入库（L15）：抽的还是选的、还是被规则
        # 强制的，列表徽章与统计口径靠它分得开。
        if handicap >= 2:
            color_source = "rule"
            player_color = BLACK
        elif player_color not in (BLACK, WHITE):
            color_source = "guess"
            player_color = draw_color()
            logger.info("猜先结果：玩家执%s", color_name(player_color))
        else:
            color_source = "pick"
        ms = settings.move_seconds_default if move_seconds is None else max(0, int(move_seconds))
        game = Game(size=size, komi=komi, handicap=handicap, score_method=score_method,
                    player_color=player_color)
        rec = GameRecord(
            user_id=user.id, size=size, komi=komi, handicap=handicap,
            player_color=player_color, color_source=color_source, score_method=score_method,
            allow_takeback=not is_promo, move_seconds=ms,
            rank_id=user.rank_id, ai_name=rank.ai_name, is_promotion=is_promo,
            engine_name=get_pool().active_engine, status="playing",
        )
        with SessionLocal() as db:
            db.add(rec)
            db.commit()
            game_id = rec.id
        live = LiveGame(
            id=game_id, user_id=user.id, game=game, rank_id=user.rank_id,
            profile=profile, ai_name=rank.ai_name, is_promotion=is_promo,
            hint_mode=user.hint_mode if hint_mode is None else hint_mode,
            allow_takeback=not is_promo, move_seconds=ms,
            engine_name=get_pool().active_engine,
        )
        self.games[game_id] = live
        self.arm_player_clock(live)      # 玩家先走时第一手就开始计时
        return live

    def get(self, game_id: str) -> Optional[LiveGame]:
        return self.games.get(game_id)

    def set_hint_mode(self, user_id: str, enabled: bool) -> int:
        """把账号上的「落子推荐」开关同步进该用户进行中的对局。

        设置页改了偏好只落库不够：活对局的 `hint_mode` 是开局时快照的，
        不推过去的话「关掉落子推荐」要到下一局才生效 —— 用户报过
        「关了还一直显示、再开再关一次才消失」的现场（另一半在客户端，
        见 `ui/pages/game.py` 里 `_reset_state` 那条注释）。顺带广播一条
        轻量事件 `{"type":"hintMode"}`，让连着这条对局的客户端当场改判。
        返回受影响的局数（finished 不算：死局没有推荐可言）。
        """
        changed = 0
        for live in self._snapshot():
            if live.user_id != user_id or live.phase == "finished":
                continue
            if live.hint_mode != enabled:
                live.hint_mode = enabled
                live.emit({"type": "hintMode", "enabled": enabled})
            changed += 1
        return changed

    def get_active_for_user(self, user_id: str) -> Optional[LiveGame]:
        for live in self._snapshot():
            if live.user_id == user_id and live.phase != "finished":
                return live
        return None

    async def restore(self, game_id: str, user_hint_mode: bool = True) -> Optional[LiveGame]:
        """从数据库恢复对局（服务重启或断线重连）。"""
        if game_id in self.games:
            return self.games[game_id]
        rec = self._load_record(game_id)
        if rec is None or rec.status not in ("playing", "scoring"):
            return None
        game = Game(size=rec.size, komi=rec.komi, handicap=rec.handicap,
                    score_method=rec.score_method, ko_rule=rec.ko_rule,
                    player_color=rec.player_color)
        if game.handicap_stones:
            game.next_color = WHITE
        for m in rec.moves or []:
            point = None if m.get("x") is None else (int(m["x"]), int(m["y"]))
            try:
                game.play(int(m["color"]), point)
            except IllegalMove as exc:
                # 静默截断会让下一次 save() 用「截断版」覆盖数据库里的完整记录，
                # 数据静默丢失。至少留下一条日志。
                logger.warning("恢复对局 %s 时第 %d 手非法（%s），后续手顺被截断",
                               rec.id, len(game.moves) + 1, exc)
                break
        game.finished = False
        live = LiveGame(
            id=rec.id, user_id=rec.user_id, game=game, rank_id=rec.rank_id,
            profile=promotion_profile(rec.rank_id) if rec.is_promotion
            else get_engine_profile(rec.rank_id),
            ai_name=rec.ai_name, is_promotion=rec.is_promotion,
            hint_mode=bool(user_hint_mode), allow_takeback=bool(rec.allow_takeback),
            analyses=list(rec.analyses or []),
            phase="scoring" if rec.status == "scoring" else "playing",
            engine_name=rec.engine_name,
            move_seconds=rec.move_seconds or 0,
        )
        self.games[game_id] = live
        self.arm_player_clock(live)
        return live

    def remove(self, game_id: str) -> None:
        with self._lock:
            self.games.pop(game_id, None)

    # ------------------------------------------------------------------
    # 每手限时
    def arm_player_clock(self, live: LiveGame) -> None:
        """在「变成玩家回合」的那些点调用：开始本手计时，或在不该计时时清掉 deadline。"""
        if (live.phase != "playing" or live.move_seconds <= 0
                or live.game.next_color != live.player_color):
            live.move_deadline = None
            return
        live.move_deadline = time.time() + live.move_seconds
        live.spawn(self._clock_watchdog(live, live.move_deadline))

    async def _clock_watchdog(self, live: LiveGame, deadline: float) -> None:
        """到点玩家还没落子 → 判超时负。

        必须有服务端看门狗：玩家直接关掉标签页的话，「落子时检查超时」那条路径
        永远不会执行，对局会永远挂在「进行中」并占着新开对局的名额。
        """
        await asyncio.sleep(max(0.0, deadline - time.time()) + 0.25)
        if live.abandoned or live.phase != "playing":
            return
        if live.move_deadline != deadline:      # 悔棋 / 新的一手已重置
            return
        if live.game.next_color != live.player_color:
            return
        event = await self.finish(live, reason="timeout")
        live.emit(event)
        self.save(live)

    async def expire_if_due(self, live: LiveGame) -> Optional[dict]:
        """玩家发来指令时先查超时（以服务端时钟为准，不信客户端）。

        返回 gameEnd 事件表示这盘已经因超时结束，调用方应直接把它下发、
        不要再执行原本请求的动作。
        """
        if (live.phase == "playing" and live.move_deadline is not None
                and live.game.next_color == live.player_color
                and time.time() > live.move_deadline):
            event = await self.finish(live, reason="timeout")
            live.emit(event)
            self.save(live)
            return event
        return None

    # ------------------------------------------------------------------
    def save(self, live: LiveGame) -> None:
        if live.abandoned:
            # 作废的对局不再落库：否则在飞的 AI 回合醒来后会把 status 从
            # abandoned 改回 finished/playing，一盘作废的棋就复活了
            return
        with SessionLocal() as db:
            rec = db.get(GameRecord, live.id)
            if rec is None:
                return
            rec.moves = [m.to_dict(live.game.size) for m in live.game.moves]
            # 落库时剥掉 ownership：它对复盘无意义，却让整局写入从 KB 级涨到
            # MB 级并造成 O(n²) 写放大（每手 save 都要整体重写 analyses 列）。
            rec.analyses = _strip_ownership_all(live.analyses)
            rec.status = live.phase
            rec.engine_name = live.engine_name
            rec.updated_at = datetime.now(timezone.utc)
            if live.phase == "finished":
                rec.finished = True
                rec.finish_reason = live.game.finish_reason or ""
                rec.result_json = live.game.result or {}
                rec.winner = int((live.game.result or {}).get("winner") or 0)
                rec.result_text = (live.game.result or {}).get("result") or ""
                rec.player_won = rec.winner == live.game.player_color
                rec.sgf = export_sgf(live.game, black_name=self._black_name(live),
                                     white_name=self._white_name(live))
            db.commit()

    @staticmethod
    def _black_name(live: LiveGame) -> str:
        return "玩家" if live.game.player_color == BLACK else live.ai_name

    @staticmethod
    def _white_name(live: LiveGame) -> str:
        return live.ai_name if live.game.player_color == BLACK else "玩家"

    # ------------------------------------------------------------------
    async def player_move(self, live: LiveGame, point: Optional[Point]) -> dict:
        """玩家落子（point=None 表示虚手），返回需要下发的事件集合。

        手感关键：玩家自己的那手棋**立即**通过 live.emit 下发并落库，不等 KataGo
        分析完（分析+AI 思考要 1~3 秒，旧实现把 move 事件憋到 AI 回合结束才发出，
        玩家点完棋盘要干等几秒才能看到自己的子）。返回的 events 只装 analysis /
        aiMove / scoring 等 AI 回合的后续事件，由 WS 层补发。
        """
        g = live.game
        if live.phase != "playing":
            raise IllegalMove("当前不在对局阶段")
        if g.next_color != g.player_color:
            raise IllegalMove("还没轮到你落子")
        if point is not None and not g.board.inside(point):
            raise IllegalMove("落点超出棋盘")

        async with live.lock:
            move = g.play(g.player_color, point)
            # 立即回显 + 立即落库（服务崩溃也不丢这一手）
            live.emit({
                "type": "move",
                "move": move.to_dict(g.size),
                "moveCount": len(g.moves),
            })
            self.save(live)
            if g.finished:      # 双方虚手 → 终局结算
                return {"events": [await self.enter_scoring(live)]}

            # 「AI 正在思考」要在分析查询**之前**发出：查询是耗时大头，
            # 玩家落完子后立刻就能看到自己的子 + 思考提示，而不是白屏干等
            live.emit({"type": "thinking", "aiName": live.ai_name})

            # 分析玩家落子后的局面（= AI 行棋局面）
            query = live.build_query()
            result = await get_pool().analyze(query, side_to_move=live.ai_color,
                                              turn=len(g.moves), profile=live.profile)
            # await 引擎的这几秒里对局可能被认输/强制结束/删除：醒来后立即收手，
            # 否则 update_despair → finish → _apply_rank 会给玩家凭空记上一局
            if live.phase != "playing" or g.finished or live.abandoned:
                return {"events": []}
            apoint = live.record_analysis(result, index=len(g.moves))
            events: list[dict] = [{
                "type": "analysis", "index": len(g.moves), "analysis": apoint,
            }]

            # AI 认输判定
            if live.update_despair(result):
                events.append(await self.finish(live, reason="ai-resign"))
                self.save(live)
                return {"events": events}

            # AI 选点并落子（落子前的模拟思考期再补一次 thinking：analysis 事件
            # 会清掉前端的思考提示，不补的话提示会在 AI 落子前闪烁消失）
            await asyncio.sleep(get_pool().estimate_thinking_delay(live.profile))
            if live.phase != "playing" or g.finished or live.abandoned:
                return {"events": []}
            live.emit({"type": "thinking", "aiName": live.ai_name})
            # 无路可走且大败：投子，而不是在原地无限虚手拖住玩家
            if live.lost_with_nothing_to_play(result):
                events.append(await self.finish(live, reason="ai-resign"))
                self.save(live)
                return {"events": events}
            if live.should_pass(result):
                ai_point = None
            else:
                ai_point = get_pool().choose_move(g.board, live.ai_color, result, live.profile)
            try:
                ai_move = g.play(live.ai_color, ai_point)
            except IllegalMove:
                ai_move = g.play(live.ai_color, None)   # 兜底：虚手
            # 新一轮玩家限时从 AI 落子这一刻起算：必须先 arm 再取剩余秒数，
            # 否则下发的还是玩家上一手剩的旧倒计时（前端会先显示一个错误值）
            self.arm_player_clock(live)
            events.append({"type": "aiMove", "move": ai_move.to_dict(g.size),
                           "moveCount": len(g.moves),
                           "moveSecondsLeft": live.seconds_left()})

            if g.finished:
                events.append(await self.enter_scoring(live))
                self.save(live)
                return {"events": events}

            self.save(live)
            # 后台补分析：AI 落子后的局面 → 曲线新点 + 玩家提示
            live.spawn(self._analyze_after_ai(live, len(g.moves), live.generation))
            return {"events": events}

    async def _analyze_after_ai(self, live: LiveGame, ply: int,
                                generation: Optional[int] = None) -> None:
        try:
            query = live.build_query()
            result = await get_pool().analyze(query, side_to_move=live.player_color,
                                              turn=ply, profile=live.profile)
            if live.abandoned or live.phase == "finished":
                # 这几秒里对局被删除 / 强制结束：绝不能再往下走 ——
                # 旧实现只查 ply 与 phase，于是「AI 落后 + 玩家删了对局」会走到
                # update_despair → finish("ai-resign") → _apply_rank，给一盘已被
                # 删除的对局凭空记上一胜（save() 因 abandoned 提前返回，场上没痕迹）。
                return
            if len(live.game.moves) != ply:
                return      # 玩家已继续落子，本次分析作废
            if generation is not None and live.generation != generation:
                # 悔棋回退了手数：ply 可能又相等，但局面已经不是同一盘
                return
            apoint = live.record_analysis(result, index=ply)
            payload: dict = {"type": "analysis", "index": ply, "analysis": apoint,
                             "moveSecondsLeft": live.seconds_left()}
            if live.hint_mode:
                best = [c.to_dict(live.game.size) for c in result.candidates[:3]]
                payload["hint"] = best
            live.emit(payload)
            if live.update_despair(result):
                live.emit(await self.finish(live, reason="ai-resign"))
                self.save(live)
            else:
                self.save(live)
        except Exception as exc:   # noqa: BLE001
            logger.warning("后台分析失败: %s", exc)

    # ------------------------------------------------------------------
    async def takeback(self, live: LiveGame, plies: int = 2) -> dict:
        if live.phase != "playing":
            raise IllegalMove("当前阶段不能悔棋")
        if not live.allow_takeback:
            raise IllegalMove("晋升战不允许悔棋")
        # 悔棋必须是**正偶数**：撤 1 手等于让局面停在 AI 回合，而 AI 落子只在
        # player_move / WS 重连里触发 —— 撤完就没人走，玩家再落子只会报
        # 「还没轮到你落子」，整局永久卡死（审计 1.11 实测复现）。
        if plies <= 0 or plies % 2 != 0:
            raise IllegalMove("悔棋手数必须是正偶数（玩家 + AI 各一手）")
        # 与在飞的 AI 回合串行：player_move 持锁期间悔棋请求会等 AI 落完，
        # 避免游戏在「AI 分析到一半」时被回滚两子、醒来后接着往旧局面落子
        async with live.lock:
            undone = live.game.takeback(plies)
            if not undone:
                raise IllegalMove("还没有可悔的手")
            # 局面代次 +1：在飞的后台分析（可能 ply 又对上）一律作废
            live.generation += 1
            # 同步回滚分析与曲线
            target = len(live.game.moves)
            live.analyses = live.analyses[:target + 1]
            live.despair_plies = 0
            self.arm_player_clock(live)         # 悔完轮到玩家，重新给一手的时间
            with SessionLocal() as db:
                rec = db.get(GameRecord, live.id)
                if rec:
                    rec.takeback_count += 1
            self.save(live)
            event = {
                "type": "takeback",
                "undone": [m.to_dict(live.game.size) for m in undone],
                "moveCount": len(live.game.moves),
                "state": live.snapshot(),
            }
        # 悔完若轮到 AI（玩家执白时撤掉「AI 一手 + 玩家一手」就是这样），
        # 必须主动补一手，否则和 plies=1 一样卡住
        if live.phase == "playing" and live.game.next_color != live.player_color:
            live.spawn(self._ai_turn_after_takeback(live))
        return event

    async def _ai_turn_after_takeback(self, live: LiveGame) -> None:
        """悔棋后轮到 AI 时补一手（与 _ai_turn 同口径，但走 live.emit 广播）。"""
        async with live.lock:
            if (live.abandoned or live.phase != "playing"
                    or live.game.next_color == live.player_color):
                return
            gen = live.generation
            try:
                query = live.build_query()
                result = await get_pool().analyze(query, side_to_move=live.ai_color,
                                                  turn=len(live.game.moves),
                                                  profile=live.profile)
                if (live.abandoned or live.phase != "playing"
                        or live.generation != gen
                        or live.game.next_color == live.player_color):
                    return
                live.record_analysis(result)
                live.emit({"type": "analysis", "index": len(live.game.moves),
                           "analysis": live.analyses[-1]})
                point = get_pool().choose_move(live.game.board, live.ai_color,
                                               result, live.profile)
                move = live.game.play(live.ai_color, point)
                self.arm_player_clock(live)
                live.emit({"type": "aiMove", "move": move.to_dict(live.game.size),
                           "moveCount": len(live.game.moves),
                           "moveSecondsLeft": live.seconds_left()})
                if live.game.finished:
                    live.emit(await self.enter_scoring(live))
                self.save(live)
            except IllegalMove:
                self.save(live)
            except Exception as exc:   # noqa: BLE001
                logger.warning("悔棋后 AI 补手失败: %s", exc)

    # ------------------------------------------------------------------
    def player_resign(self, live: LiveGame) -> dict:
        winner = live.ai_color
        text = f"{color_name(winner)}胜（玩家投子认输）"
        live.game.finish("resign", winner=winner, result_text=text)
        live.phase = "finished"
        return self._finalize_sync(live, reason="player-resign", winner=winner, text=text)

    async def finish(self, live: LiveGame, reason: str) -> dict:
        ai = live.ai_color
        if reason == "ai-resign":
            text = f"{color_name(live.player_color)}胜（{live.ai_name} 投子认输）"
            winner = live.player_color
        elif reason == "timeout":
            text = f"{color_name(ai)}胜（玩家落子超时）"
            winner = ai
        else:
            text = reason
            winner = ai
        live.move_deadline = None
        live.game.finish(reason, winner=winner, result_text=text)
        live.phase = "finished"
        return self._finalize_sync(live, reason=reason, winner=winner, text=text)

    FORCE_END_TEXT = "强制结束（不计入胜负）"

    def force_end(self, rec: GameRecord, live: Optional["LiveGame"] = None) -> dict:
        """强制终止对局：不计胜负、不动晋升进度，返回一条完整的 gameEnd 事件。

        与「投子认输」的区别就在这里：认输走 _finalize_sync，会算胜负、更新连胜
        连败与本级胜场、触发晋升校验与赛后复盘。强制结束是给「卡住的、开错的、
        不想要的」对局用的出口 —— 所以不调 _apply_rank、不入复盘队列，User 上的
        total_games / total_wins / rank_wins / win_streak 一个也不碰。
        也因此不能用它来逃败：两者在记录里的 finish_reason 不同，强制结束
        在对战列表与日历里单独归为「无胜负」，不会静悄悄变成一场胜利。
        （对已经正常终局的棋，API 层会直接拒：改写一条真实败局会让总战绩
        与日历胜率对不上。）

        内存里的活对局必须一并摘掉：restore() 是按 rec.status 过滤的，但断线重连
        走的是 WS，只改 DB 不碰 hub 的话，已作废的局面会被当成活对局接着下。

        review_status 故意不改：作废的对局也可能已经下了一百多手，值得复盘；
        而且 review_status 的取值空间（none/pending/done/failed）已被前后端共同
        假设，为了一个作废标记往里塞新值不值得。

        本方法**不提交事务**：rec 属于调用方的 session，由调用方 commit。
        """
        if live is not None:
            live.abandoned = True       # 先置位，再做任何可能引发落库/记账的动作
            live.generation += 1        # 作废在飞的后台分析
            live.game.finish("force-end", winner=0, result_text=self.FORCE_END_TEXT)
            live.phase = "finished"
            if live.game.moves:
                try:
                    rec.sgf = export_sgf(live.game, black_name=self._black_name(live),
                                         white_name=self._white_name(live))
                except Exception:      # noqa: BLE001
                    logger.debug("强制结束时导出 SGF 失败", exc_info=True)
            rec.result_json = live.game.result or {}
            rec.moves = [m.to_dict(live.game.size) for m in live.game.moves]
        rec.status = "abandoned"
        rec.finished = True
        rec.finish_reason = "force-end"
        rec.winner = 0
        rec.player_won = False
        rec.result_text = self.FORCE_END_TEXT

        # 从 rec 而不是从 live 组装事件：两条路径（内存里还有/只剩数据库）回传的
        # 形状必须一模一样，否则走 REST 的客户端会收到一条缺字段的半成品。
        # 它必须带 type="gameEnd"：前端 applyEvent 是按 type 分发的，缺了就被静默丢弃。
        event = {
            "type": "gameEnd", "reason": "force-end", "winner": 0,
            "result": rec.result_json or None,
            "resultText": rec.result_text,
            "playerWon": False, "avgLossPoints": None, "sgf": rec.sgf,
            "rank": None,               # 不计胜负 → 没有等级变动
            "countsForRank": False,
            "aiWords": "这局就到这里，不算胜负。需要的话可以重新开一盘。",
        }
        if live is not None:
            live.emit(event)
            self.remove(live.id)
        return event

    def _finalize_sync(self, live: LiveGame, reason: str, winner: int, text: str,
                       counts_for_rank: bool = True) -> dict:
        live.game.finish_reason = reason
        live.phase = "finished"
        accuracy = self.quick_accuracy(live)
        # counts_for_rank=False 用于和棋（winner == 0）：平局既不是胜也不是负，
        # 旧实现把 0 传进 _apply_rank 后 `winner == player_color` 恒为 False，
        # 于是和棋被当成「负」记进战绩、断连胜、开降级时还累计连败。
        rank_result = (self._apply_rank(live, winner == live.player_color, accuracy)
                       if counts_for_rank else None)
        self.save(live)
        if accuracy is not None:
            with SessionLocal() as db:
                rec = db.get(GameRecord, live.id)
                if rec is not None:
                    rec.accuracy = accuracy
                    db.commit()
        # 触发赛后复盘（异步队列）
        from ..review.worker import enqueue_review
        enqueue_review(live.id)
        return {
            "type": "gameEnd",
            "reason": reason,
            "winner": winner,
            "result": live.game.result,
            "resultText": text,
            "playerWon": winner == live.player_color,
            "avgLossPoints": accuracy,
            "sgf": export_sgf(live.game, black_name=self._black_name(live),
                              white_name=self._white_name(live)),
            "rank": rank_result,
            "countsForRank": counts_for_rank,
            "aiWords": _farewell_words(live, winner == live.player_color),
        }

    def quick_accuracy(self, live: LiveGame) -> Optional[float]:
        """用对局中已有的逐手分析估算玩家吻合度（平均每手损失目数）。

        这个值用于即时的高段晋升校验；赛后复盘会用更高 visit 重新算一遍并回写对局。
        口径与复盘一致：优先用同节点损失，落点不在候选里时退回跳节点差值。
        """
        from ..review import analyzer
        g = live.game
        pc = g.player_color
        losses: list[float] = []
        for i, m in enumerate(g.moves):
            if m.color != pc or m.point is None:
                continue
            node = live.analyses[i] if i < len(live.analyses) else None
            loss = analyzer.node_loss(node, {"x": m.point[0], "y": m.point[1]}, pc)
            if loss is None:
                before = live.score_for(pc, i)        # 该手之前的局面
                after = live.score_for(pc, i + 1)     # 该手之后的局面
                if before is None or after is None:
                    continue
                loss = max(0.0, before - after)
            losses.append(loss)
        if not losses:
            return None
        return round(sum(losses) / len(losses), 3)

    def _apply_rank(self, live: LiveGame, player_won: bool,
                    accuracy: Optional[float] = None) -> dict:
        with SessionLocal() as db:
            user = db.get(User, live.user_id)
            if user is None:
                return {}
            from ..rank.logic import on_game_finished
            res = on_game_finished(db, user, won=player_won, game_id=live.id,
                                   accuracy=accuracy, is_promotion_game=live.is_promotion)
            db.commit()
            res["progress"] = progress_of(user).to_dict()
            return res

    def apply_review_accuracy(self, game_id: str, accuracy: Optional[float]) -> None:
        """复盘完成后回写更精确的吻合度（仅更新对局记录，不重复计入用户均值）。"""
        if accuracy is None:
            return
        with SessionLocal() as db:
            rec = db.get(GameRecord, game_id)
            if rec is None:
                return
            rec.accuracy = round(float(accuracy), 3)
            db.commit()

    # ------------------------------------------------------------------
    async def enter_scoring(self, live: LiveGame) -> dict:
        """双方虚手 → 进入终局结算阶段，先用 ownership 估死子。"""
        live.phase = "scoring"
        live.move_deadline = None           # 结算阶段不计时
        dead = await self.estimate_dead_stones(live)
        live.scoring_dead = dead
        preview = live.game.board.score(live.game.komi, live.game.score_method, dead)
        self.save(live)
        return {
            "type": "scoring",
            "deadStones": [[p[0], p[1]] for p in dead],
            "preview": preview,
            "message": "双方虚手，进入终局结算。可点击棋盘上的棋子修正死子判定，然后确认。",
        }

    async def estimate_dead_stones(self, live: LiveGame) -> list[Point]:
        """用引擎 ownership 自动判定死子；无引擎时退化为「无死子」。"""
        g = live.game
        try:
            query = live.build_query(max_visits=max(16, live.profile.max_visits))
            result = await get_pool().analyze(query, side_to_move=g.next_color,
                                              turn=len(g.moves), profile=live.profile)
        except Exception as exc:   # noqa: BLE001
            logger.warning("死子判定分析失败: %s", exc)
            return []
        own = result.ownership       # 内部口径：白方为正，下标 = y * size + x（y=0 在 GTP 第1行）
        if not own or len(own) != g.size * g.size:
            return []
        dead: list[Point] = []
        for y in range(g.size):
            for x in range(g.size):
                v = own[y * g.size + x]
                stone = g.board.grid[y][x]
                if stone == EMPTY:
                    continue
                # 黑子却判定为白地（或反之）→ 死子
                if stone == BLACK and v > 0.5:
                    dead.append((x, y))
                elif stone == WHITE and v < -0.5:
                    dead.append((x, y))
        return dead

    def _validate_dead(self, live: LiveGame, dead) -> list[Point]:
        """校验死子坐标列表，返回规范化后的点。

        终局结果直接驱动升降级与战绩，而客户端传来的坐标此前**零校验**：
          · 负坐标 → Python 负索引回绕，把对角的白子当死子抹掉（实测能把
            「白胜 2.5 目」翻成「黑胜 72.5 目」，全程无报错）；
          · 越界坐标 → IndexError（REST 500 / WS 拆连接）；
          · 非整数 → TypeError。
        非法一律拒绝，宁可不结算也不能记账。
        """
        g = live.game
        size = g.size
        out: list[Point] = []
        for p in dead:
            if isinstance(p, (str, bytes)) or not hasattr(p, "__len__") or len(p) != 2:
                raise IllegalMove("死子坐标必须是 [x, y] 形式")
            x, y = p[0], p[1]
            if isinstance(x, bool) or isinstance(y, bool) or \
                    not isinstance(x, int) or not isinstance(y, int):
                raise IllegalMove("死子坐标必须是整数")
            if not (0 <= x < size and 0 <= y < size):
                raise IllegalMove(f"死子坐标越界：({x}, {y})")
            if g.board.grid[y][x] == EMPTY:
                raise IllegalMove(f"({x}, {y}) 是空点，不能标记为死子")
            if (x, y) not in out:
                out.append((x, y))
        return out

    def confirm_score(self, live: LiveGame, dead: Optional[list[Point]] = None) -> dict:
        if dead is not None:
            live.scoring_dead = self._validate_dead(live, dead)
        g = live.game
        result = g.board.score(g.komi, g.score_method, live.scoring_dead)
        winner = int(result.get("winner") or EMPTY)
        g.finish_reason = "pass-pass"
        g.finished = True
        g.result = result
        live.phase = "finished"
        text = result.get("result") or ""
        # winner == 0 = 和棋：不计等级胜负（否则会被当成「负」记账）
        return self._finalize_sync(live, reason="pass-pass", winner=winner, text=text,
                                   counts_for_rank=winner in (BLACK, WHITE))

    def resume_from_scoring(self, live: LiveGame) -> dict:
        """结算阶段反悔：撤销最后两次虚手，继续对局。"""
        if live.phase != "scoring":
            raise IllegalMove("当前不在结算阶段")
        live.game.takeback(2)
        live.generation += 1        # 反悔改局面：作废在飞的分析
        live.analyses = live.analyses[:len(live.game.moves) + 1]
        live.phase = "playing"
        live.scoring_dead = []
        self.save(live)
        self.arm_player_clock(live)
        return {"type": "resume", "state": live.snapshot()}

    # ------------------------------------------------------------------
    def list_for_user(self, user_id: str, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        with SessionLocal() as db:
            rows = db.scalars(
                select(GameRecord).where(GameRecord.user_id == user_id)
                .order_by(GameRecord.created_at.desc()).limit(limit).offset(offset)
            ).all()
            count = db.scalar(select(func.count(GameRecord.id))
                              .where(GameRecord.user_id == user_id)) or 0
            return [record_brief(r) for r in rows], int(count)


def record_brief(r: GameRecord) -> dict:
    """对局列表用的精简载荷（不含棋谱/逐手分析，那些走 /api/games/{id}）。"""
    return {
        "id": r.id,
        "createdAt": r.created_at.isoformat() if r.created_at else "",
        "size": r.size, "komi": r.komi, "handicap": r.handicap,
        "playerColor": r.player_color, "colorSource": r.color_source,
        "rankId": r.rank_id,
        "rankName": get_rank(r.rank_id).name, "aiName": r.ai_name,
        "isPromotion": r.is_promotion,
        "status": r.status, "finished": r.finished,
        "finishReason": r.finish_reason, "winner": r.winner,
        "resultText": r.result_text, "playerWon": r.player_won,
        "moveCount": len(r.moves or []),
        "reviewStatus": r.review_status,
        # 复盘进度（列表里直接显示百分比，不用为每行再请求一次 /status）
        "reviewProgress": 1.0 if r.review_status == "done" else (r.review_progress or 0.0),
        "reviewStage": r.review_stage or "",
        "reviewDetail": r.review_detail or "",
        "avgLossPoints": r.accuracy,
        "engine": r.engine_name,
    }


_FAREWELL_WIN = [
    "这盘我认输了。你的中盘力量比我想象的更强，继续加油。",
    "好棋！这一局你抓住了我的失误，晋升路上又近一步。",
    "承让。复盘时重点看看我在哪里失去了主动权——那也是你可以学到的地方。",
]
_FAREWELL_LOSE = [
    "这盘你下得很努力，我们复盘时看看哪几手损失最大。",
    "胜负之外更重要的是找出问题手，去复盘页看看吧。",
    "别灰心，把这几处缓手改掉，下一盘就不一样了。",
]


def _farewell_words(live: LiveGame, player_won: bool) -> str:
    words = _FAREWELL_WIN if player_won else _FAREWELL_LOSE
    return random.choice(words)


hub = GameHub()


def get_hub() -> GameHub:
    return hub
