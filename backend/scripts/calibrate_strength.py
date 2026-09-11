"""档位棋力标定：量出 27 档 AI 的「平均每手损失目数」，验证削弱旋钮真的调对了。

为什么需要这个脚本（单元测试与冒烟都发现不了这类问题）：
  棋力档位的错误是**静默的数值错误**。曾经出现过两个只有实测才能发现的缺陷：
    1. 18级 用 visits=2 削弱，结果每手只亏 0.82 目（职业量级）—— 因为 KataGo
       在低 visits 下只报 1.1 个候选，AI 被迫下最优点，砍 visit 反而最强；
    2. 段位档按 humanPrior 采样，结果七段每手亏 1.68 目、五段只亏 0.39 目 ——
       高段反而更弱。两种情况下测试全绿、冒烟全过。
  所以每次改 rank/defs.py 的旋钮、换 KataGo 版本或换网络权重，都该重跑一次。

方法：
  1. 用 preaz_5k 自对弈生成一批真实的 9 路局面（缓存复用，见 --regen）；
  2. 每个局面跑一次高 visits 的参考分析，得到全部候选的 scoreLead；
  3. 按某档位的 EngineProfile 走 pool.choose_move 选点（与实际对局同一条路径）；
  4. 损失 = 最佳候选 scoreLead − AI 实际选的那手 scoreLead（同节点口径）。
  落在参考候选之外的点（local_noise 触发时几乎必然如此）改用**跨节点差值**：
  在该点落子后再查一次参考分析。KataGo 的值函数可信，跨节点差值成立；
  绝不能像早期版本那样硬记成 6 目 —— 那会把级位档压成一条 3~5 目的平线
  （实测：18级 超参率 49%，clamp 后均值 4.27 目，真实值 5.04 目）。

样本量：每个局面重复采样 --samples 次。引擎查询结果与采样无关，所以同一
（局面, 档位）只查一次、重复调 choose_move 即可，成本几乎为零。必须这么做：
local_noise 是伯努利抽样，12 个样本 × 1 次时标准误约 0.7 目，足以把整条级位
梯度洗成平线，根本分不出「参数无效」和「只是噪声」。

用法（在 backend 目录，需要已装好 KataGo）：
    python scripts/calibrate_strength.py                    # 全 27 档
    python scripts/calibrate_strength.py --ranks 1,9,18,19,23,27
    python scripts/calibrate_strength.py --regen            # 重新生成局面（调参时不要动，
                                                            # 否则分不清"参数改了"和"局面换了"）
    python scripts/calibrate_strength.py --games 5 --pos-per-game 6   # 加大局面样本

参考量级：职业每手损失 0.3~0.8 目，业余中段 1.5~3 目，级位新手 5 目以上。
退出码非 0 表示出现了**倒挂**（弱档反而更强）或 KataGo 不可用。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DATA_DIR                              # noqa: E402
from app.engine.pool import EnginePool                       # noqa: E402
from app.engine.protocol import AnalysisQuery                # noqa: E402
from app.game.rules import BLACK, WHITE, Game, from_gtp      # noqa: E402
from app.rank.defs import MAX_RANK_ID, get_engine_profile, get_rank  # noqa: E402

SIZE = 9
KOMI = 5.5
REF_VISITS = 400          # 参考分析的 visits（越高越接近满血）
REF_PROFILE = "preaz_9d"  # 主网络是 human SL 版，只能在这个口径下取参考
GEN_PROFILE = "preaz_5k"  # 用它生成"业余中段"的真实局面
GEN_VISITS = 32
POS_CACHE = DATA_DIR / "strength_positions.json"
# 相邻档位允许的倒挂幅度（目）。采样噪声不可避免，只在超过这个量时才判失败
INVERSION_TOLERANCE = 0.6


def ref_query(game: Game) -> AnalysisQuery:
    # 注意没有 maxMoves：v1.17.1 不认这个字段，返回多少候选完全由 maxVisits 决定
    return AnalysisQuery(size=SIZE, moves=game.gtp_moves(), komi=KOMI,
                         max_visits=REF_VISITS,
                         include_ownership=False, include_policy=False,
                         human_sl_profile=REF_PROFILE)


def rank_query(game: Game, visits: int, profile: str) -> AnalysisQuery:
    return AnalysisQuery(size=SIZE, moves=game.gtp_moves(), komi=KOMI,
                         max_visits=visits,
                         include_ownership=False, include_policy=False,
                         human_sl_profile=profile)


def build_game(moves: list[list[str]]) -> Game:
    game = Game(size=SIZE, komi=KOMI)
    for color_s, vertex in moves:
        color = BLACK if str(color_s).upper().startswith("B") else WHITE
        pt = None if str(vertex).lower() == "pass" else from_gtp(str(vertex), SIZE)
        game.play(color, pt)
    return game


async def gen_positions(pool: EnginePool, seed: int, games: int, per_game: int,
                        regen: bool) -> list[list[list[str]]]:
    """用 GEN_PROFILE 自对弈，每隔几手存一个局面（存配对数组，便于重建）。

    落子时把 local_noise 强制归零：生成器只需要「像业余中段」的局面，
    若带着噪声跑，双方会下出大量无理手，后续每一档的「最佳点」都变成惩罚手，
    损失普遍被抬高 —— 那就量不出档位之间的差异了。
    """
    if POS_CACHE.exists() and not regen:
        cached = json.loads(POS_CACHE.read_text(encoding="utf-8"))
        print(f"复用缓存局面 {len(cached)} 个（{POS_CACHE}）；--regen 可重新生成")
        return cached
    out: list[list[list[str]]] = []
    for g in range(games):
        game = Game(size=SIZE, komi=KOMI)
        pool.rng.seed(seed + g)
        saved = 0
        for ply in range(60):
            if game.finished or saved >= per_game:
                break
            color = game.next_color
            res = await pool.katago.analyze(
                rank_query(game, GEN_VISITS, GEN_PROFILE), side_to_move=color, turn=ply)
            prof = replace(get_engine_profile(14), local_noise=0.0)
            pt = pool.choose_move(game.board, color, res, prof)
            try:
                game.play(color, pt)
            except Exception:                        # noqa: BLE001
                game.play(color, None)
            # 布局走完（≥6 手）之后开始取样，避开空盘这种没有区分度的局面
            if len(game.moves) >= 6 and saved < per_game and len(game.moves) % 5 == 0:
                # gtp_moves() 回的是 [{"player":..,"move":..}]，不能直接 list(m) ——
                # 那只会拿到键名 ["player","move"]，重建时 from_gtp("move") 直接炸
                out.append([[m["player"], m["move"]] for m in game.gtp_moves()])
                saved += 1
    POS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    POS_CACHE.write_text(json.dumps(out), encoding="utf-8")
    return out


async def after_move_score(pool: EnginePool, game: Game, stm: int, point,
                           sign: float) -> float:
    """在 point 落子后，返回**原走子方视角**的局面估值（目）。

    跨节点差值只对可信的值函数成立（KataGo 成立，启发式引擎不成立），
    所以这里直接走 pool.katago 而不走 pool.analyze。
    返回的 r.score_lead 是黑方视角，乘 sign 换回原走子方视角。
    """
    g2 = Game(size=SIZE, komi=KOMI)
    for m in game.moves:
        g2.play(m.color, m.point)
    g2.play(stm, point)
    r = await pool.katago.analyze(ref_query(g2),
                                  side_to_move=WHITE if stm == BLACK else BLACK,
                                  turn=len(g2.moves))
    return sign * r.score_lead


async def measure(pool: EnginePool, refs: list, ranks: list[int], samples: int) -> dict:
    """返回 {rank_id: {"loss":…, "inRef":…, "outRate":…, "hit":…}}。"""
    # 跨节点估值缓存：按 (局面下标, 落点) 去重，所有档位共用。
    # 不去重的话 18级 光一个档就要多查上百次（超参率 49% × 240 样本）
    after_cache: dict[tuple[int, object], float] = {}
    out: dict[int, dict] = {}

    for rid in ranks:
        prof = get_engine_profile(rid)
        losses, in_ref, best_hits, out_of_ref, total = [], [], 0, 0, 0
        for pi, (game, stm, scored) in enumerate(refs):
            if not scored:
                continue
            sign = 1.0 if stm == BLACK else -1.0
            # 引擎查询与采样无关：同一局面只查一次，重复调 choose_move 采样
            ai_res = await pool.analyze(
                rank_query(game, prof.max_visits, prof.human_sl_profile),
                side_to_move=stm, turn=len(game.moves), profile=prof)
            if ai_res.engine != "katago":
                print(f"  !! {get_rank(rid).name} 的查询没走 KataGo（引擎={ai_res.engine}）"
                      f" —— human 档位可能非法，或引擎中途挂了")
            best_score, best_pt = scored[0]
            for _ in range(samples):
                chosen = pool.choose_move(game.board, stm, ai_res, prof)
                got = next((s for s, p in scored if p == chosen), None)
                if got is None:
                    key = (pi, chosen)
                    if key not in after_cache:
                        after_cache[key] = await after_move_score(pool, game, stm,
                                                                  chosen, sign)
                    losses.append(max(0.0, best_score - after_cache[key]))
                    out_of_ref += 1
                else:
                    lo = max(0.0, best_score - got)
                    losses.append(lo)
                    in_ref.append(lo)
                total += 1
                if chosen == best_pt:
                    best_hits += 1
        n = total or 1
        out[rid] = {
            "loss": sum(losses) / n,
            "inRef": sum(in_ref) / len(in_ref) if in_ref else float("nan"),
            "outRate": out_of_ref / n,
            "hit": best_hits / n,
        }
    print(f"  （超参落点共做了 {len(after_cache)} 次跨节点估值，已去重）")
    return out


def report(rows: dict[int, dict], ranks: list[int]) -> int:
    print("\n===== 各档位平均每手损失（目，越大越弱）=====")
    print(f"  {'档位':<8}{'visits':>7}{'噪声':>6}{'容差':>6}{'topN':>5}{'手滑':>6}"
          f"{'human档':>12}{'每手损失':>9}{'仅参考内':>9}{'超参率':>8}{'命中最优':>9}")
    for rid in ranks:
        prof, m = get_engine_profile(rid), rows[rid]
        print(f"  {get_rank(rid).name:<8}{prof.max_visits:>7}{prof.local_noise:>6.2f}"
              f"{prof.sample_tolerance:>6.2f}{prof.sample_top_n:>5}{prof.blunder_rate:>6.2f}"
              f"{(prof.human_sl_profile or '(继承9d)'):>12}"
              f"{m['loss']:>9.2f}{m['inRef']:>9.2f}"
              f"{m['outRate'] * 100:>7.0f}%{m['hit'] * 100:>8.0f}%")

    # 倒挂检查：这是本脚本存在的核心理由 —— 七段比五段弱这种缺陷，
    # 单元测试和冒烟都是绿的，只有把实测数值排一排才看得见
    inversions = []
    for a, b in zip(ranks, ranks[1:]):
        if rows[a]["loss"] + INVERSION_TOLERANCE < rows[b]["loss"]:
            inversions.append(f"{get_rank(a).name}({rows[a]['loss']:.2f}) < "
                              f"{get_rank(b).name}({rows[b]['loss']:.2f})")
    print("\n  「仅参考内」只看落在参考候选里的手，与「每手损失」对比能看出噪声手的杀伤力")
    print("  参考量级：职业 0.3~0.8 目，业余中段 1.5~3 目，级位新手 5 目以上")
    if inversions:
        print(f"\n  !! 棋力倒挂 {len(inversions)} 处（弱档反而更强，容差 "
              f"{INVERSION_TOLERANCE} 目）：")
        for it in inversions:
            print(f"     {it}")
        return 1
    print("\n  单调性检查通过：无倒挂")
    return 0


async def run(args: argparse.Namespace) -> int:
    pool = EnginePool(seed=args.seed)
    await pool.startup()
    if not await pool.wait_until_ready(timeout=args.wait):
        print("KataGo 未就绪，无法标定（启发式引擎的跨节点差值不可信）：",
              pool.katago.error)
        return 2
    print(f"引擎 {pool.active_engine}")
    positions = await gen_positions(pool, args.seed, args.games, args.pos_per_game,
                                    args.regen)
    if not positions:
        print("没生成出任何局面")
        return 2
    print(f"用 {len(positions)} 个局面 × 每档 {args.samples} 次采样评测")

    # 参考分析与档位无关，每个局面只算一次（放内层循环会白算 27 遍）
    print(f"跑参考分析（{REF_VISITS} visits / {REF_PROFILE}）…")
    refs = []
    for moves in positions:
        game = build_game(moves)
        stm = game.next_color
        ref = await pool.katago.analyze(ref_query(game), side_to_move=stm,
                                        turn=len(game.moves))
        sign = 1.0 if stm == BLACK else -1.0
        scored = sorted(((sign * c.score_lead, c.point) for c in ref.candidates),
                        key=lambda t: -t[0])
        refs.append((game, stm, scored))

    ranks = ([int(x) for x in args.ranks.split(",") if x.strip()] if args.ranks
             else list(range(1, MAX_RANK_ID + 1)))
    ranks = sorted({r for r in ranks if 1 <= r <= MAX_RANK_ID})
    rows = await measure(pool, refs, ranks, args.samples)
    await pool.shutdown()
    return report(rows, ranks)


def main() -> int:
    ap = argparse.ArgumentParser(description="标定 27 档 AI 的真实棋力（每手损失目数）")
    ap.add_argument("--ranks", default="", help="只标定这些档位，如 1,9,18,19,23,27")
    ap.add_argument("--samples", type=int, default=20, help="每个局面重复采样次数（默认 20）")
    ap.add_argument("--games", type=int, default=3, help="生成局面用的自对弈局数（默认 3）")
    ap.add_argument("--pos-per-game", type=int, default=4, help="每局取几个局面（默认 4）")
    ap.add_argument("--regen", action="store_true", help="重新生成局面（调参对比时别用）")
    ap.add_argument("--wait", type=float, default=300.0, help="等 KataGo 预热的秒数上限")
    ap.add_argument("--seed", type=int, default=20260904, help="随机种子")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
