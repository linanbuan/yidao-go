"""死活题的存取：从摆子建棋盘、内置题库落库（幂等 upsert）、生成结果缓存。"""
from __future__ import annotations

import hashlib
import importlib
import json
import logging
import marshal
import time
from pathlib import Path
from typing import Optional

from sqlalchemy import select

from ..database import SessionLocal
from ..game.rules import BLACK, Board, WHITE
from ..models import TsumegoProblem
from .library import KIND_LIFE, TIERS, Spec, build_library, tier_of

logger = logging.getLogger("go.tsumego")

# 生成结果的磁盘缓存。题目仍然由搜索现场推导（不手抄答案），只是把推导结果存下来：
# 全量生成要两分多钟（吃子手筋那套枚举最贵），放在每次启动的路径上不可接受。
# 指纹 = 判定与生成三个模块的源码哈希 + 版本号，逻辑一改缓存就自动失效重算，
# 所以不会出现「库里存着旧答案」。
#
# 缓存文件跟代码放在一起（而不是 backend/data），但**不入版本库**（.gitignore 排除）：
# 它是构建产物而不是用户数据。新克隆/新部署的第一次启动会先现场推导一遍（约两分钟），
# 之后写回缓存就是秒级。
#
# 递增时机：题目的**结构或字段含义**变了（不只是代码改了）：
#   1 → 2：加了 tier（难度五档标签）；
#   2 → 3：正解线里多存了 verdict（attempt 判定答对时直接取它，不再重搜一次）。
# 旧缓存缺新字段不会炸（读取处都用 .get() 回退），但会让优化静默失效，所以要递增。
CACHE_VERSION = 3
# store.py 自己也算指纹源：标题去重、落库字段都在它里，改了它们缓存里的旧结果就作废了。
_CACHE_SOURCES = ("solve.py", "library.py", "puzzles.py", "store.py")
_CACHE_FILE = Path(__file__).resolve().parent / "library_cache.json"


def _source_bytes(name: str) -> bytes:
    """指纹源文件的内容；**打包成 exe 后没有 .py 源码**，退回编译后的字节码。

    为什么要退这一条：`_fingerprint()` 是 `library_problems()` 的第一行，一个
    FileNotFoundError 会让出题接口直接挂掉，桌面端的症状是「死活练习永远转圈」。
    字节码同样在“改了逻辑”时变化，只是粒度粗（重新编译就会变，哪怕一行没改），
    代价是多算一次两分钟的题，而不是启动就坏。

    开发期走不到 except 分支，指纹与以前逐字节相同（已入库的缓存不会作废），
    这一点由 `tests/test_tsumego.py` 里算手工哈希的那条钉着。
    """
    path = Path(__file__).resolve().parent / name
    try:
        return path.read_bytes()
    except OSError:
        pass
    try:
        mod = importlib.import_module(f"{__package__}.{Path(name).stem}")
        return marshal.dumps(mod.__loader__.get_code(mod.__name__))
    except Exception:                               # noqa: BLE001  兜底：宁稳不炸
        return f"no-source:{name}".encode("utf-8")


def _fingerprint() -> str:
    digest = hashlib.sha256(f"v{CACHE_VERSION}".encode())
    for name in _CACHE_SOURCES:
        digest.update(_source_bytes(name))
    return digest.hexdigest()[:16]


def _load_cache(fingerprint: str) -> Optional[list[dict]]:
    try:
        raw = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None                       # 没有缓存 / 缓存坏了 → 重新生成
    if raw.get("fingerprint") != fingerprint:
        return None
    problems = raw.get("problems")
    return problems if isinstance(problems, list) else None


def _save_cache(fingerprint: str, problems: list[dict]) -> None:
    try:
        _CACHE_FILE.write_text(
            json.dumps({"fingerprint": fingerprint, "problems": problems},
                       ensure_ascii=False),
            encoding="utf-8")
    except OSError as exc:                # 缓存写不进去不影响出题，只是下次还慢
        logger.warning("题库缓存写入失败（%s），本次结果不落盘", exc)


def library_problems(force: bool = False) -> list[dict]:
    """内置题库（带缓存）。force=True 跳过缓存重算。"""
    fingerprint = _fingerprint()
    if not force:
        cached = _load_cache(fingerprint)
        if cached is not None:
            logger.debug("题库命中缓存 %s：%d 题", fingerprint, len(cached))
            return cached
    started = time.perf_counter()
    # 先把话说在前面：重算要两分多钟，不提前告知就会被当成启动卡死
    logger.warning("题库缓存失效（源码指纹 %s），正在重新生成，约需两分钟……", fingerprint)
    problems = build_library()
    _dedupe_titles(problems)
    logger.info("题库重新生成：%d 题，耗时 %.1fs（指纹 %s）",
                len(problems), time.perf_counter() - started, fingerprint)
    _log_distribution(problems)
    _save_cache(fingerprint, problems)
    return problems


def _log_distribution(problems: list[dict]) -> None:
    """把题型与难度档位的分布打进日志。

    难度改成自动打分之后，「分布长什么样」再也看不见了（以前手填时心里有数）。
    某个档位空了、或者全部挤在入门，都是生成器退化的信号，得在生成当时就看到。
    """
    kinds: dict[str, int] = {}
    tiers: dict[str, int] = {}
    for item in problems:
        kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
        tiers[item["tier"]] = tiers.get(item["tier"], 0) + 1
    order = [name for _, name in TIERS]
    logger.info("题型分布：%s", {k: kinds[k] for k in sorted(kinds)})
    logger.info("档位分布：%s", {t: tiers.get(t, 0) for t in order})


def _dedupe_titles(problems: list[dict]) -> None:
    """同名题加序号后缀。

    枚举出来的题很容易撞名（同一块白棋、留不同的气），列表里出现两条一模一样的
    标题会让人以为是重复数据。pid 本来就是唯一的，这里只管显示。
    """
    counts: dict[str, int] = {}
    for item in problems:
        counts[item["title"]] = counts.get(item["title"], 0) + 1
    seen: dict[str, int] = {}
    for item in problems:
        title = item["title"]
        if counts[title] < 2:
            continue
        seen[title] = seen.get(title, 0) + 1
        item["title"] = f"{title}（{seen[title]}）"


def problem_board(setup: list, size: int) -> Board:
    """由 [[x, y, color], ...] 摆子建棋盘（死活题没有手顺，直接摆）。"""
    board = Board(size)
    black = [(int(x), int(y)) for x, y, c in setup if int(c) == BLACK]
    white = [(int(x), int(y)) for x, y, c in setup if int(c) == WHITE]
    if black:
        board.place(BLACK, black)
    if white:
        board.place(WHITE, white)
    return board


def row_spec(rec: TsumegoProblem) -> Spec:
    """把数据库里的一行还原成搜索用的规格。

    玩家落在变化线之外时，服务端要按**出题时的同一套判据**重新算结论；
    判据的全部信息就是 kind + own + targets + 主角 + 范围，所以这四样必须入库。
    """
    return Spec(
        pid=rec.id,
        title=rec.title,
        kind=rec.kind or KIND_LIFE,
        goal=rec.goal,
        size=rec.size,
        setup=[(int(x), int(y), int(c)) for x, y, c in (rec.setup or [])],
        area=[(int(x), int(y)) for x, y in (rec.space or [])],
        player=rec.to_move,
        protagonist=rec.victim,
        difficulty=rec.difficulty,
        family=rec.family,
        hint="",
        note="",
        targets=[(int(x), int(y)) for x, y in (rec.targets or [])],
        own=[(int(x), int(y)) for x, y in (rec.own or [])],
    )


def seed_builtin(force: bool = False) -> int:
    """把内置题库写入数据库（按 id upsert，可重复调用）。

    题目由搜索现场推导，推导结果按源码指纹缓存（见 library_problems）。
    形状表或判定逻辑一改，指纹就变、缓存自动重算，旧库里的答案跟着更新。
    导入的非内置题不受影响；已不在题库里的内置题会被停用（保留练习记录的外键）。
    """
    problems = library_problems(force=force)
    alive = {data["pid"] for data in problems}
    with SessionLocal() as db:
        for data in problems:
            row = db.get(TsumegoProblem, data["pid"])
            if row is None:
                row = TsumegoProblem(id=data["pid"])
                db.add(row)
            row.title = data["title"]
            row.kind = data["kind"]
            row.family = data["family"]
            row.goal = data["goal"]
            row.difficulty = data["difficulty"]
            row.tier = data["tier"]
            row.size = data["size"]
            row.to_move = data["toMove"]
            row.victim = data["victim"]
            row.setup = data["setup"]
            row.space = data["space"]
            row.targets = data["targets"]
            row.own = data["own"]
            row.lines = data["lines"]
            row.hint = data["hint"]
            row.note = data["note"]
            row.tags = data["tags"]
            row.source = data["source"]
            row.builtin = True
            row.enabled = True
        stale = 0
        for row in db.scalars(select(TsumegoProblem)
                              .where(TsumegoProblem.builtin.is_(True))).all():
            if row.id not in alive:
                row.enabled = False       # 形状被删/被闸门拒了：不再出题，但留着记录
                # 停用题的档位也要跟着难度走：它的 difficulty 还是旧的手填值，而新增的
                # tier 列只会拿到列默认值「入门」，两者对不上（实测旧库升级后就有这么一行：
                # difficulty=6 但 tier=入门）。所有查询都过滤 enabled，它不会被下发，
                # 但留一行自相矛盾的数据迟早会误导排查。
                row.tier = tier_of(row.difficulty)
                stale += 1
        db.commit()
    logger.info("死活题库就绪：内置 %d 题%s", len(problems),
                f"（停用过期 {stale} 题）" if stale else "")
    return len(problems)


__all__ = ["library_problems", "problem_board", "row_spec", "seed_builtin"]
