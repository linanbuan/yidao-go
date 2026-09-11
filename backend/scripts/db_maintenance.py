"""生产库维护：清理探针/测试残留、收编僵尸局、回收空间、可选瘦身。

审计 1.22 / §3 的落地工具。**默认只读（dry-run）**，看清清单后加 `--apply` 才动手；
任何写操作之前先做一份 SQLite 在线备份到 `data/backups/`。

    # 只看现状
    python scripts/db_maintenance.py --list

    # 预演清理（不改任何数据）
    python scripts/db_maintenance.py --purge-test-accounts --close-stale-games 6 --vacuum

    # 真的执行
    python scripts/db_maintenance.py --purge-test-accounts --close-stale-games 6 --vacuum --apply

    # 可选：把 N 天前的对局瘦身成「摘要」（保留手顺/SGF/结果，剥掉 analyses 与 review_json）
    # 每用户最近 20 局无论多老都保留全量（对应应用启动期的 GO_RETENTION_* 策略）
    python scripts/db_maintenance.py --retention-days 90 --keep-recent 20 --apply

为什么值得有这件工具：`backend/data/` 里既有真实用户数据，也混着历轮自检脚本
写下的 smoke/probe 账号与测试对局（审计实查 13 个探针账号），它们会出现在大厅的
「进行中对局」与用户列表里，把排障现场搅浑；同时 `auto_vacuum=0` 意味着删完不
VACUUM 文件不会变小。
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

#: 探针/自检账号的用户名特征。历轮脚本用过 `smoke<epoch>`、`probe<epoch>`、
#: `l18probe` / `l18p2`（L18 那轮的临时账号）、`wr2`（胜率口径）、
#: `probe_对局口径(human)`（带中文后缀的口径探针）——统一在下面这条正则里。
TEST_ACCOUNT_RE = re.compile(r"^(smoke|probe|wr\d*|l\d+p)", re.IGNORECASE)

#: 绝不删的账号（真实用户）。部署相关的白名单不写死在代码里：
#: 设环境变量 `GO_KEEP_ACCOUNTS`（逗号分隔）列出要保留的用户名，未设则不保留任何账号。
KEEP_ACCOUNTS: set[str] = {
    name.strip() for name in
    os.environ.get("GO_KEEP_ACCOUNTS", "").split(",") if name.strip()
}


def _default_db() -> Path:
    # 不 import app.config：那会在 import 期就把数据目录建出来、甚至写 jwt_secret.txt，
    # 而本工具应当能被安全地对着任意库跑。这里只按同一口径认 GO_DATA_DIR。
    base = Path(os.environ.get("GO_DATA_DIR") or (BACKEND / "data"))
    return base / "go_teach.db"


def connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
        return sqlite3.connect(uri, uri=True)
    return sqlite3.connect(str(path))


def backup(path: Path) -> Path:
    """写操作前先备份。

    放进 `backups/manual/` 子目录而不是与 `_auto_backup()` 的每日份并列：
    后者的保留策略是 `glob("go_teach-*.db")` 只留最近 7 份，手工备份若与它同层
    且同名模式，就会被自动清理连带删掉。
    """
    target = path.parent / "backups" / "manual" / (
        f"go_teach-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db")
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(path))
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def report(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    print("=" * 68)
    print("库现状")
    print("=" * 68)
    for t in ("users", "games", "rank_events", "tsumego_problems", "tsumego_progress"):
        n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:20s} {n}")

    print("\n  对局状态：")
    for status, n in cur.execute(
            "SELECT status, COUNT(*) FROM games GROUP BY status ORDER BY 2 DESC"):
        print(f"    {status:12s} {n}")

    print("\n  探针账号（将被 --purge-test-accounts 清理）：")
    rows = find_test_accounts(conn)
    for uid, name in rows:
        g = cur.execute("SELECT COUNT(*) FROM games WHERE user_id=?", (uid,)).fetchone()[0]
        e = cur.execute("SELECT COUNT(*) FROM rank_events WHERE user_id=?", (uid,)).fetchone()[0]
        p = cur.execute("SELECT COUNT(*) FROM tsumego_progress WHERE user_id=?", (uid,)).fetchone()[0]
        print(f"    {name!r:28s} games={g} rank_events={e} tsumego={p}")
    print(f"    小计 {len(rows)} 个")

    print("\n  真实账号（保留）：")
    for uid, name in cur.execute("SELECT id, username FROM users"):
        if not TEST_ACCOUNT_RE.match(name):
            g = cur.execute("SELECT COUNT(*) FROM games WHERE user_id=?", (uid,)).fetchone()[0]
            print(f"    {name!r:28s} games={g}")

    ps = cur.execute("PRAGMA page_size").fetchone()[0]
    fc = cur.execute("PRAGMA freelist_count").fetchone()[0]
    pc = cur.execute("PRAGMA page_count").fetchone()[0]
    print(f"\n  文件 ≈ {pc * ps / 1024 / 1024:.2f} MiB，其中可回收 {fc * ps / 1024 / 1024:.2f} MiB")


def find_test_accounts(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute("SELECT id, username FROM users").fetchall()
    return [(uid, name) for uid, name in rows
            if TEST_ACCOUNT_RE.match(name) and name not in KEEP_ACCOUNTS]


def find_stale_games(conn: sqlite3.Connection, hours: float) -> list[tuple]:
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    stamp = cutoff.strftime("%Y-%m-%d %H:%M:%S.%f")
    return conn.execute(
        "SELECT id, user_id, status, updated_at FROM games"
        " WHERE status IN ('playing','scoring') AND updated_at < ?"
        " ORDER BY updated_at", (stamp,)).fetchall()


# ---------------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------------

def purge_test_accounts(conn: sqlite3.Connection) -> int:
    rows = find_test_accounts(conn)
    if not rows:
        print("  没有探针账号需要清理")
        return 0
    ids = [uid for uid, _ in rows]
    marks = ",".join("?" * len(ids))
    cur = conn.cursor()
    n_games = cur.execute(f"DELETE FROM games WHERE user_id IN ({marks})", ids).rowcount
    n_ev = cur.execute(f"DELETE FROM rank_events WHERE user_id IN ({marks})", ids).rowcount
    n_tp = cur.execute(f"DELETE FROM tsumego_progress WHERE user_id IN ({marks})", ids).rowcount
    n_users = cur.execute(f"DELETE FROM users WHERE id IN ({marks})", ids).rowcount
    conn.commit()
    print(f"  删除账号 {n_users} 个；连带 games {n_games}、rank_events {n_ev}、"
          f"tsumego_progress {n_tp}")
    return n_users


def close_stale_games(conn: sqlite3.Connection, hours: float) -> int:
    rows = find_stale_games(conn, hours)
    if not rows:
        print(f"  没有超过 {hours} 小时仍挂着「进行中」的对局")
        return 0
    for gid, _uid, status, updated in rows:
        print(f"    {gid}  status={status}  最后活动 {updated}")
    ids = [r[0] for r in rows]
    marks = ",".join("?" * len(ids))
    cur = conn.cursor()
    n = cur.execute(
        f"UPDATE games SET status='finished', finished=1,"
        f" finish_reason='abandoned', result_text='未续下（维护清理）',"
        f" winner=0, player_won=0 WHERE id IN ({marks})", ids).rowcount
    conn.commit()
    print(f"  标记 {n} 局为「未续下」（只改状态，不记等级、不动手顺）")
    return n


def prune_old_games(conn: sqlite3.Connection, days: float, keep_recent: int = 0) -> int:
    """把 N 天前的对局瘦身：保留手顺/SGF/结果/复盘状态，剥掉两份重量级 JSON。

    analyses（每手含 361 个 ownership 浮点）与 review_json 占了库里 ~79% 的体积；
    历史局几乎不会再翻看逐手曲线，留着只是让库无界增长。

    `keep_recent > 0` 时，每个用户**最近 keep_recent 局无论多老都保留全量** ——
    这正是应用启动期同名策略（`app/database.py::_prune_history`，
    受 `GO_RETENTION_DAYS` / `GO_RETENTION_KEEP_RECENT` 控制）的口径，两处请同步改。
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    stamp = cutoff.strftime("%Y-%m-%d %H:%M:%S.%f")
    keep = max(0, int(keep_recent or 0))

    victims: list[tuple[str, int]] = []
    users = [r[0] for r in conn.execute("SELECT DISTINCT user_id FROM games")]
    for uid in users:
        rows = conn.execute(
            "SELECT id, LENGTH(COALESCE(analyses,'')) + LENGTH(COALESCE(review_json,''))"
            " FROM games WHERE user_id=? AND updated_at < ? AND ("
            "  LENGTH(COALESCE(analyses,'')) > 2 OR LENGTH(COALESCE(review_json,'')) > 2)"
            " ORDER BY created_at DESC", (uid, stamp)).fetchall()
        victims.extend((r[0], r[1] or 0) for r in rows[keep:])
    if not victims:
        print(f"  没有 {days} 天前还需要瘦身的对局（每用户保留最近 {keep} 局全量）")
        return 0
    freed = sum(v[1] for v in victims)
    print(f"  将瘦身 {len(victims)} 局，释放约 {freed / 1024 / 1024:.2f} MiB 文本"
          f"（每用户保留最近 {keep} 局全量）")
    ids = [v[0] for v in victims]
    marks = ",".join("?" * len(ids))
    conn.execute(f"UPDATE games SET analyses='[]', review_json='{{}}' WHERE id IN ({marks})",
                 ids)
    conn.commit()
    return len(victims)


def vacuum(conn: sqlite3.Connection) -> None:
    before = Path(conn.execute("PRAGMA database_list").fetchone()[2]).stat().st_size
    conn.isolation_level = None
    conn.execute("VACUUM")
    conn.isolation_level = ""
    after = Path(conn.execute("PRAGMA database_list").fetchone()[2]).stat().st_size
    print(f"  VACUUM：{before / 1024 / 1024:.2f} MiB → {after / 1024 / 1024:.2f} MiB"
          f"（省 {(before - after) / 1024 / 1024:.2f} MiB）")


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生产库维护（默认 dry-run）")
    ap.add_argument("--db", type=Path, default=None, help="数据库路径（默认 backend/data/go_teach.db）")
    ap.add_argument("--apply", action="store_true", help="真的执行；不给则只预演")
    ap.add_argument("--list", action="store_true", help="只打印现状")
    ap.add_argument("--purge-test-accounts", action="store_true", help="删探针/自检账号及其数据")
    ap.add_argument("--close-stale-games", type=float, metavar="HOURS",
                    help="把超过 HOURS 小时仍『进行中』的局标为未续下")
    ap.add_argument("--retention-days", type=float, metavar="DAYS",
                    help="把 DAYS 天前的对局瘦身（剥 analyses / review_json）")
    ap.add_argument("--keep-recent", type=int, default=0, metavar="N",
                    help="配合 --retention-days：每用户最近 N 局保留全量（默认 0=不保留）")
    ap.add_argument("--vacuum", action="store_true", help="回收空间")
    args = ap.parse_args(argv)

    path = args.db or _default_db()
    if not path.exists():
        print(f"数据库不存在：{path}")
        return 1

    read_only = not args.apply
    conn = connect(path, readonly=read_only)
    try:
        print(f"数据库 {path}\n模式 {'只读预演（加 --apply 才会写）' if read_only else '写入'}")
        report(conn)

        wants = any([args.purge_test_accounts, args.close_stale_games is not None,
                     args.retention_days is not None, args.vacuum])
        if args.list or not wants:
            conn.close()
            return 0

        print("\n" + "=" * 68)
        print("将要执行" if read_only else "执行中")
        print("=" * 68)
        if args.apply:
            saved = backup(path)
            print(f"  已备份 → {saved}")
        if args.purge_test_accounts:
            n = len(find_test_accounts(conn))
            print(f"  · 清理探针账号 {n} 个")
            if args.apply:
                purge_test_accounts(conn)
        if args.close_stale_games is not None:
            n = len(find_stale_games(conn, args.close_stale_games))
            print(f"  · 收编僵尸局 {n} 个（阈值 {args.close_stale_games}h）")
            if args.apply:
                close_stale_games(conn, args.close_stale_games)
        if args.retention_days is not None:
            print(f"  · 瘦身 {args.retention_days} 天前的对局"
                  f"（每用户保留最近 {max(0, args.keep_recent)} 局全量）")
            if args.apply:
                prune_old_games(conn, args.retention_days, args.keep_recent)
        if args.vacuum:
            print("  · VACUUM 回收空间")
            if args.apply:
                vacuum(conn)

        if not args.apply:
            print("\n以上为预演，未改动任何数据。确认后加 --apply 执行。")
            conn.close()
            return 0

        conn.close()
        # VACUUM 会重排文件，收尾再看一眼体积
        conn = connect(path, readonly=True)
        ps = conn.execute("PRAGMA page_size").fetchone()[0]
        pc = conn.execute("PRAGMA page_count").fetchone()[0]
        fc = conn.execute("PRAGMA freelist_count").fetchone()[0]
        print(f"\n完成后：文件 ≈ {pc * ps / 1024 / 1024:.2f} MiB，待回收 {fc * ps / 1024 / 1024:.2f} MiB")
        print(f"完整性检查：{conn.execute('PRAGMA integrity_check').fetchone()[0]}")
        conn.close()
        return 0
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
