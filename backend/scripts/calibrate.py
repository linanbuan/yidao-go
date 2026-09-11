"""段位棋力标定脚本：用自对弈拟合各档位的 Elo，校准"18级 → 九段"的棋力梯度。

原理：
  让相邻档位的 AI 互相对弈若干局（交替先后手），由胜率反推等级分差：
      ΔElo = -400 · log10(1/score - 1)
  再以最高段位为锚点向下累加，得到全 27 级的 Elo 曲线，写入
  `backend/data/elo_calibration.json`；rank/defs.py 启动时会自动读取覆盖默认值。

用法（在 backend 目录）：
    python scripts/calibrate.py --games 40 --size 9            # 相邻档位各 40 局（9 路更快）
    python scripts/calibrate.py --ranks 1,5,10,19,23,27        # 只标定指定档位
    python scripts/calibrate.py --games 20 --apply             # 标定并写入覆盖文件
    python scripts/calibrate.py --report                       # 只打印当前 Elo 表

建议：装好 KataGo 后再跑（CPU 也可，只是慢）；未安装时用内置引擎只能得到粗略梯度。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DATA_DIR, settings          # noqa: E402
from app.engine.pool import EnginePool              # noqa: E402
from app.engine.protocol import AnalysisQuery       # noqa: E402
from app.game.rules import BLACK, WHITE, Game, IllegalMove  # noqa: E402
from app.rank.defs import MAX_RANK_ID, RANKS, get_engine_profile, get_rank  # noqa: E402

CALIBRATION_FILE = DATA_DIR / "elo_calibration.json"


def build_query(game: Game, visits: int) -> AnalysisQuery:
    return AnalysisQuery(
        size=game.size, moves=game.gtp_moves(), komi=game.komi,
        rules="chinese" if game.score_method == "area" else "japanese",
        initial_stones=game.initial_stones_gtp(),
        initial_player="W" if game.handicap >= 2 else "B",
        max_visits=visits, include_ownership=False, include_policy=False,
    )


async def play_one(pool: EnginePool, size: int, komi: float, rank_a: int, rank_b: int,
                   a_is_black: bool, max_plies: int, seed: int) -> int:
    """跑一局自对弈，返回胜方颜色（BLACK/WHITE），无法判定则返回 0。"""
    game = Game(size=size, komi=komi)
    pa = get_engine_profile(rank_a)
    pb = get_engine_profile(rank_b)
    profile_of = {BLACK: (pa if a_is_black else pb), WHITE: (pb if a_is_black else pa)}
    color_of = {BLACK: (rank_a if a_is_black else rank_b),
                WHITE: (rank_b if a_is_black else rank_a)}
    plies = 0
    while not game.finished and plies < max_plies:
        color = game.next_color
        profile = profile_of[color]
        result = await pool.analyze(build_query(game, profile.max_visits),
                                    side_to_move=color, turn=len(game.moves), profile=profile)
        point = pool.choose_move(game.board, color, result, profile)
        try:
            game.play(color, point)
        except IllegalMove:
            game.play(color, None)
        plies += 1
    if not game.finished:
        game.finish("pass-pass")
    winner = (game.result or {}).get("winner") or 0
    if winner not in (BLACK, WHITE):
        return 0
    return int(color_of[winner])


def elo_from_score(score: float) -> float:
    """score = A 对 B 的得分率（0~1）。返回 A 相对 B 的等级分差。"""
    score = min(0.995, max(0.005, score))
    return -400.0 * math.log10(1.0 / score - 1.0)


async def calibrate(pairs: list[tuple[int, int]], games: int, size: int, komi: float,
                    max_plies: int, seed: int) -> dict[str, float]:
    pool = EnginePool(seed=seed)
    await pool.startup()
    print(f"引擎：{pool.active_engine}；棋盘 {size} 路；每对档位 {games} 局（交替先后手）\n")
    diffs: dict[str, float] = {}
    try:
        for higher, lower in pairs:
            wins_higher = 0
            decided = 0
            for i in range(games):
                a_is_black = (i % 2 == 0)
                # higher 档位执 A，lower 档位执 B
                winner_rank = await play_one(pool, size, komi, higher, lower,
                                             a_is_black, max_plies, seed + i)
                if winner_rank == 0:
                    continue
                decided += 1
                if winner_rank == higher:
                    wins_higher += 1
            if decided == 0:
                print(f"  {get_rank(higher).name} vs {get_rank(lower).name}：无有效结果，跳过")
                continue
            score = wins_higher / decided
            diff = elo_from_score(score)
            diffs[f"{higher}-{lower}"] = diff
            print(f"  {get_rank(higher).name:>6} vs {get_rank(lower).name:<6}"
                  f"  高段胜率 {score * 100:5.1f}%  →  ΔElo {diff:+7.1f}  ({decided} 局)")
    finally:
        await pool.shutdown()
    return diffs


def fit_elo(diffs: dict[str, float]) -> dict[int, float]:
    """以最高段位为锚点，按相邻档位差值向下累加。"""
    anchor_rank = MAX_RANK_ID
    anchor_elo = float(get_rank(anchor_rank).elo)
    elo: dict[int, float] = {anchor_rank: anchor_elo}
    for r in range(anchor_rank - 1, 0, -1):
        key = f"{r + 1}-{r}"
        delta = diffs.get(key)
        if delta is None:
            # 缺测时用段位表默认差值兜底
            delta = float(get_rank(r + 1).elo - get_rank(r).elo)
        elo[r] = elo[r + 1] - delta
    return elo


def report(elo: dict[int, float] | None = None) -> None:
    print(f"\n{'段位':<10}{'默认Elo':>10}{'标定Elo':>10}{'与下一级差':>12}")
    for r in RANKS:
        calibrated = (elo or {}).get(r.rank_id)
        gap = None
        if calibrated is not None and r.rank_id > 1:
            gap = calibrated - (elo or {}).get(r.rank_id - 1, calibrated)
        print(f"{r.name:<10}{r.elo:>10}"
              f"{(f'{calibrated:.0f}' if calibrated is not None else '—'):>10}"
              f"{(f'{gap:+.0f}' if gap is not None else '—'):>12}")


async def run(args: argparse.Namespace) -> int:
    if args.report:
        elo = load_calibration()
        report(elo)
        if not elo:
            print("\n（尚无标定文件，显示的是段位表默认值）")
        return 0

    ranks = args.ranks
    if ranks:
        chosen = sorted({int(x) for x in ranks.split(",") if x.strip()})
        pairs = [(chosen[i + 1], chosen[i]) for i in range(len(chosen) - 1)]
    else:
        pairs = [(r + 1, r) for r in range(1, MAX_RANK_ID)]

    diffs = await calibrate(pairs, args.games, args.size, args.komi,
                            args.max_plies, args.seed)
    if not diffs:
        print("没有可用对局结果，未生成标定数据")
        return 1
    elo = fit_elo(diffs)
    report(elo)

    if args.apply:
        payload = {
            "engine": settings.katago_enabled and Path(settings.katago_bin).exists(),
            "size": args.size,
            "gamesPerPair": args.games,
            "pairDiffs": diffs,
            "elo": {str(k): round(v, 1) for k, v in elo.items()},
        }
        CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
        CALIBRATION_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
        print(f"\n已写入标定文件：{CALIBRATION_FILE}")
        print("重启后端后，段位表与大厅显示的 Elo 将使用标定值。")
    else:
        print("\n（未写入文件；加 --apply 可持久化标定结果）")
    return 0


def load_calibration() -> dict[int, float] | None:
    if not CALIBRATION_FILE.exists():
        return None
    try:
        data = json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
        return {int(k): float(v) for k, v in (data.get("elo") or {}).items()}
    except (json.JSONDecodeError, ValueError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="段位棋力 Elo 标定")
    ap.add_argument("--games", type=int, default=20, help="每对档位的对局数")
    ap.add_argument("--size", type=int, default=9, choices=[9, 13, 19], help="棋盘路数")
    ap.add_argument("--komi", type=float, default=7.5)
    ap.add_argument("--max-plies", type=int, default=160, help="单局最大手数")
    ap.add_argument("--seed", type=int, default=20260904)
    ap.add_argument("--ranks", default="", help="只标定指定档位，如 1,5,10,19,23,27")
    ap.add_argument("--apply", action="store_true", help="写入标定文件")
    ap.add_argument("--report", action="store_true", help="只打印当前 Elo 表")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
