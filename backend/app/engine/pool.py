"""引擎池：统一入口 + 拟人化选点。

设计要点：
  * 一次查询同时服务两件事——胜率/目差分析（喂给曲线与复盘）与 AI 选点；
    玩家落子后分析出的局面正是 AI 的行棋局面，因此无需二次查询。
  * 弱档位不靠"把 visit 砍到 1"硬削弱（实测那样反而最强：KataGo 只报一两个候选，
    AI 没机会选错），而是靠三件事：局部噪声（local_noise）、按「容忍亏多少目」
    做 softmax（sample_tolerance）、以及从候选尾部挑的 `blunder_rate`，让 AI 犯该级别
    典型的错误（只看局部、方向感偏差、贪小、忽视急所），而不是一味下"低质量但正确"的棋。
  * KataGo 不可用时自动切到内置启发式引擎，接口与返回结构完全一致。
  * 运行期断链（引擎进程意外退出）不靠人工重启：看门狗按退避自动拉起，
    期间查询照走启发式引擎，恢复后无缝切回（见 _supervise）。
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from typing import Optional

from ..game.rules import BLACK, Board, EMPTY, Point
from ..rank.defs import EngineProfile
from .fallback import HeuristicEngine
from .katago import KataGoEngine
from .protocol import AnalysisQuery, AnalysisResult

logger = logging.getLogger("go.engine")

# 断链后自动重启的节奏（秒），上限就是元组长度，到顶不再重试。
# 必须设上限：KataGo 每次启动都要加载 90MB+ 网络、占几百 MB 工作集并初始化
# GPU；若死因是稳定复现的（权重损坏、GPU 枚举失败），无限重启就是重启风暴。
RESTART_SCHEDULE = (5.0, 15.0, 30.0, 60.0, 60.0)
MAX_RESTARTS = len(RESTART_SCHEDULE)


class EnginePool:
    def __init__(self, seed: Optional[int] = None):
        self.katago = KataGoEngine()
        self.fallback = HeuristicEngine(seed=seed)
        self.rng = random.Random(seed)
        self.started = False
        self._boot_task: Optional[asyncio.Task] = None      # 进行中的启动（预热或重启）
        self._supervise_task: Optional[asyncio.Task] = None
        self._boot_lock = asyncio.Lock()  # 避免预热与看门狗同时各起一个引擎进程
        self.restart_count = 0            # 当前已连续重启几次（恢复成功则归零）
        self.last_restart_at: Optional[float] = None

    # ------------------------------------------------------------------
    async def startup(self) -> None:
        """预热 KataGo，但**不阻塞**应用启动。

        OpenCL / CUDA 后端首次运行要编译并调优 GPU 内核，实测可能耗时数分钟；
        同步等会让 /api/health 迟迟不响应，启动器的就绪探测直接超时。
        预热期间 katago.available 为 False，查询自动落到启发式引擎，
        就绪后无缝切回（active_engine 随之变化）。
        """
        if self.started:
            return
        self.started = True
        # 每次启动先换一批 asyncio 原语：本对象是进程级单例，而桌面端重启服务会拿
        # 一个新事件循环复用同一个 pool —— 不换的话看门狗等一个绑在旧循环上的
        # Event，直接 `RuntimeError: is bound to a different event loop`（只有真跑
        # KataGo 那一支才会暴露，见 `KataGoEngine.rebind_loop`）。
        self._boot_lock = asyncio.Lock()
        self.katago.rebind_loop()
        if not self.katago.preflight():
            # 根本没装（或在配置里关掉）：重启也拉不起来，就不监控，
            # 否则每台没装 KataGo 的机器都会永远挂着一个后台任务
            logger.warning("引擎：内置启发式（KataGo 不可用：%s）", self.katago.error)
            return
        self._boot_task = asyncio.create_task(self._boot_katago())
        self._supervise_task = asyncio.create_task(self._supervise())
        logger.info("引擎：内置启发式（KataGo 正在后台预热，就绪后自动切换）")

    async def _boot_katago(self) -> bool:
        """拉起 KataGo（首次预热与断链重启共用）。

        用锁串行是因为启动路径不只一处（lifespan 预热、看门狗重启、可能还有
        手工调用），两个并发的 start() 会各 spawn 一个引擎进程。
        """
        async with self._boot_lock:
            if self.katago.available:
                return True
            prev, self._boot_task = self._boot_task, asyncio.current_task()
            try:
                ok = await self.katago.start()
            except Exception as exc:   # noqa: BLE001
                ok = False
                self.katago.error = f"KataGo 启动异常: {exc!r}"
                logger.warning("KataGo 启动异常: %r", exc)
            finally:
                self._boot_task = prev
            if ok:
                logger.info("引擎已切换：KataGo（强棋力 + 精确分析）")
            else:
                logger.warning("引擎：内置启发式（KataGo 不可用：%s）", self.katago.error)
            return ok

    async def _supervise(self) -> None:
        """看门狗：KataGo 运行期断链后按退避自动重启。

        没有它的话，available 一旦被读循环置 False 就再也不会回到 True（那个
        置位只存在于启动路径上），此后所有对局与复盘都静默降级到启发式引擎，
        只能靠人工重启服务恢复——这是实测过的缺陷（旧日志里出现过 12 次）。
        """
        while self.started:
            await self.katago.dead_event.wait()
            self.katago.dead_event.clear()
            if not self.started:
                return
            for attempt, delay in enumerate(RESTART_SCHEDULE, start=1):
                # 不重复贴死因：上一条「KataGo 断链（第 N 次）：…」已带上了，
                # 而 stderr 尾部是一整块几百字的文本，贴两遍会把日志刷得很难读
                logger.warning("KataGo 断链，%.0f 秒后自动重启（第 %d/%d 次）",
                               delay, attempt, MAX_RESTARTS)
                await asyncio.sleep(delay)
                if not self.started:          # 等期间服务关了：别再拉起引擎
                    return
                self.restart_count = attempt
                self.last_restart_at = time.time()
                if await self._boot_katago():
                    self.restart_count = 0    # 恢复了，下次断链重新给满额度
                    logger.info("KataGo 已自愈（本次断链重启 %d 次），引擎切回 KataGo", attempt)
                    break        # 回外层接着守下一次，不能就此收工
                if attempt >= MAX_RESTARTS:
                    logger.error(
                        "KataGo 连续 %d 次自动重启均未恢复，停止自愈并长期走启发式引擎。"
                        "末次原因：%s；引擎 stderr：%s。修复后重启服务即可恢复（无需重装）",
                        MAX_RESTARTS, self.katago.error or self.katago.last_death_reason,
                        self.katago.stderr_log_path)
                    return

    async def wait_until_ready(self, timeout: float = 0.0) -> bool:
        """等 KataGo 预热完成。复盘报告的精度差异极大，值得等一等。

        看门狗正在重启引擎时同样等它：断链后头几秒重启窗口很短（第一档 5s），
        等一下就能拿到高精度复盘，比直接降级到启发式划算。
        """
        if self.katago.available:
            return True
        task = self._boot_task
        if task is None or task.done():
            return self.katago.available
        try:
            # shield：调用方自己超时/被取消不能把启动任务一并杀掉
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout or None)
        except asyncio.TimeoutError:
            logger.info("KataGo 预热超过 %.0fs 仍未完成，本次继续用启发式引擎", timeout)
        except asyncio.CancelledError:
            logger.info("等待 KataGo 预热被取消")
        except Exception as exc:   # noqa: BLE001
            logger.warning("等待 KataGo 预热时出错: %s", exc)
        return self.katago.available

    async def shutdown(self) -> None:
        self.started = False      # 先置位：看门狗靠它退出，避免关停后又拉起引擎
        for task in (self._supervise_task, self._boot_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:   # noqa: BLE001
                    pass
        self._supervise_task = self._boot_task = None
        await self.katago.stop()

    @property
    def active_engine(self) -> str:
        return "katago" if self.katago.available else self.fallback.name

    def status(self) -> dict:
        return {
            "active": self.active_engine,
            "warming": self.katago.warming,
            "katago": self.katago.status(),
            "fallback": self.fallback.status(),
            # 自愈进度：前端据此区分「没装」/「断链正在重启」/「重启失败」
            "recover": {
                "watching": (self._supervise_task is not None
                             and not self._supervise_task.done()),
                "attempt": self.restart_count,
                "maxAttempts": MAX_RESTARTS,
            },
        }

    # ------------------------------------------------------------------
    async def analyze(self, q: AnalysisQuery, side_to_move: int, turn: int = 0,
                      profile: Optional[EngineProfile] = None) -> AnalysisResult:
        if profile is not None:
            q.max_visits = profile.max_visits
            if profile.use_human_model and profile.human_sl_profile and self.katago.available:
                q.human_sl_profile = profile.human_sl_profile
        if self.katago.available:
            try:
                return await self.katago.analyze(q, side_to_move, turn)
            except Exception as exc:   # noqa: BLE001
                # %r 保留异常类型：TimeoutError()/CancelledError() 的 str 是空串，
                # 用 %s 会打出「失败: 」让人看不出发生了什么
                logger.warning("KataGo 查询失败，本次改用启发式引擎: %r", exc)
        noise = 0.35 if (profile is None or profile.local_noise > 0.2) else 0.1
        return self.fallback.analyze(q, side_to_move, turn, noise=noise)

    async def analyze_turns(self, q: AnalysisQuery, turns: list[int],
                            side_to_move_seq: list[int],
                            timeout: Optional[float] = None) -> list[AnalysisResult]:
        if self.katago.available:
            try:
                return await self.katago.analyze_turns(q, turns, side_to_move_seq,
                                                       timeout=timeout)
            except Exception as exc:   # noqa: BLE001
                logger.warning("KataGo 批量分析失败，改用启发式引擎: %r", exc)
        return self.fallback.analyze_turns(q, turns, side_to_move_seq)

    # ------------------------------------------------------------------
    def choose_move(self, board: Board, color: int, result: AnalysisResult,
                    profile: EngineProfile) -> Optional[Point]:
        """按等级画像从候选点中挑一手；返回 None 表示虚手（pass）。

        削弱 KataGo 的三个旋钮，按重要性排序（参数已实测标定，见 rank/defs.py）：
          1. local_noise —— 以该概率改下「紧贴已有棋子的空点」。这是唯一能把棋力
             拉到级位量级的手段：KataGo 报出的候选全是它认可的好点，在候选里
             怎么采样每手损失都封顶在 ~3 目（实测 36 种组合最高 1.66 目）。
          2. sample_tolerance —— 按「相对最优点亏多少目」做 softmax，平滑且单位可解释。
             棋风的「像人」由查询里的 human_sl_profile 负责（影响搜索先验）。
          3. blunder_rate —— 从候选池尾部挑，模拟看漏了的手滑。

        **max_visits 不是削弱手段**：visits 太低时 KataGo 只报一两个候选
        （实测 visits=2 → 平均 1.1 个），AI 被迫下最优点，反而最强。visits 在这里
        买的是「候选池宽度」，弱档位也需要 32 以上。
        """
        # 1) 级位档的主要削弱：局部合理但无全局观
        if profile.local_noise > 0 and self.rng.random() < profile.local_noise:
            local = self._local_points(board, color)
            if local:
                return self.rng.choice(local)

        cands = [c for c in result.candidates if self._is_playable(board, color, c.point)]
        if not cands:
            return None      # 无处可下 → 虚手

        # 满盘/收官末期：若引擎首推 pass 且候选极少，允许直接 pass
        if cands[0].is_pass:
            return None

        playable = [c for c in cands if not c.is_pass]
        if not playable:
            return None

        top_n = max(1, profile.sample_top_n)
        pool = playable[:top_n]

        # 3) 手滑：从更靠后的候选里随机挑一个（更像该级别的真实错误）
        if profile.blunder_rate > 0 and self.rng.random() < profile.blunder_rate \
                and len(playable) > top_n:
            tail = playable[top_n:top_n + 8]
            if tail:
                choice = self.rng.choice(tail)
                return None if choice.is_pass else choice.point

        # 满配档位（容忍度 0）直接取最佳点
        if profile.sample_tolerance <= 0 or len(pool) == 1:
            return None if pool[0].is_pass else pool[0].point

        weights = self._tolerance_weights(pool, color, profile.sample_tolerance)
        pick = self.rng.choices(pool, weights=weights, k=1)[0]
        return None if pick.is_pass else pick.point

    @staticmethod
    def _tolerance_weights(pool, color: int, tolerance: float) -> list[float]:
        """按「相对最优点亏多少目」做 softmax：weight = exp(-loss / tolerance)。

        为何不用 humanPrior 或 visits 做基准（两个都是实测踩过的坑）：
          * humanPrior 的 argmax 与引擎最优点**不是同一个点**。段位档没配 human
            档位却仍按它采样，结果七段每手亏 1.68 目、五段只亏 0.39 目 ——
            高段反而更弱；而且 tolerance=0 走的是 argmax(score) 分支，在 0 附近断崖。
          * visits 在高搜索量下极度尖峰（如 900/200/60/40），幂运算几乎不改变
            分布，于是三/五段都只剩 0.4 目（职业量级），段位档整体削不动。
        以「目」为单位则直接对应复盘报告展示给学员的指标，可标定、可解释：
        tolerance=1.5 就意味着「这一档平均愿意亏 1 目上下」。
        """
        # score_lead 两个引擎都统一为黑方视角（见 protocol 模块 docstring），
        # 这里换算成走子方视角后才能直接相减
        sign = 1.0 if color == BLACK else -1.0
        scores = [sign * float(c.score_lead) for c in pool]
        best = max(scores)
        t = max(1e-6, tolerance)
        weights = [math.exp(-max(0.0, best - s) / t) for s in scores]
        total = sum(weights)
        return [w / total for w in weights] if total > 0 else [1.0 / len(pool)] * len(pool)

    @staticmethod
    def _local_points(board: Board, color: int) -> list[Point]:
        """紧贴已有棋子（含斜邻）的合法空点 —— 级位新手最典型的选点范围。

        新手的问题不是「下在离谱的地方」，而是「只看局部、没有全局观」：
        贴着已有的子走、该拆边时去粘、该脱先时跟着应。所以噪声点要落在
        棋子附近，而不是全盘均匀随机（后者会下出一路线这类不像人棋的着手）。
        """
        size = board.size
        seen: set = set()
        out: list[Point] = []
        for y in range(size):
            row = board.grid[y]
            for x in range(size):
                if row[x] == EMPTY:
                    continue
                for dy in (-1, 0, 1):
                    ny = y + dy
                    if not 0 <= ny < size:
                        continue
                    for dx in (-1, 0, 1):
                        nx = x + dx
                        if not 0 <= nx < size:
                            continue
                        p = (nx, ny)
                        if p in seen:
                            continue
                        seen.add(p)
                        if EnginePool._is_playable(board, color, p):
                            out.append(p)
        return out

    @staticmethod
    def _is_playable(board: Board, color: int, point: Optional[Point]) -> bool:
        if point is None:
            return True
        if board.at(point) != EMPTY:
            return False
        # 不填自己的眼（所有正交邻点都是己方子）
        nbrs = board.neighbors(point)
        if nbrs and all(board.at(n) == color for n in nbrs):
            return False
        return board.is_legal(color, point)

    def estimate_thinking_delay(self, profile: EngineProfile) -> float:
        """让 AI "思考"一会儿，避免瞬间落子（体验用，可按需调整）。"""
        base = 0.15 + math.log2(max(2, profile.max_visits)) * 0.05
        return min(1.2, base)


pool = EnginePool()


def get_pool() -> EnginePool:
    return pool


def engine_status() -> dict:
    return pool.status()


__all__ = ["EnginePool", "AnalysisQuery", "AnalysisResult", "pool", "get_pool",
           "engine_status"]
