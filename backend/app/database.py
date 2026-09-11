"""数据库会话与建表。开发期使用 SQLite，生产可切 PostgreSQL（改 GO_DATABASE_URL 即可）。"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATA_DIR, settings

logger = logging.getLogger("go.db")

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)

if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        """单机 SQLite 的标准调优：WAL + synchronous=NORMAL。

        默认的 journal_mode=DELETE + synchronous=FULL 意味着**每次 commit 都要 fsync**，
        而且写会拿独占锁、把并发读一起堵住。实测 `/api/tsumego/{id}/attempt` 单次 121ms
        里约 90ms 是这类固定开销（局部穷举搜索本身只 ~30ms），而落子、复盘进度、
        练习记录等**所有写路径**同理。

        WAL 让读不阻塞写；synchronous=NORMAL 在 WAL 下仍是崩溃安全的（断电最多丢
        最后几个事务，数据库文件不会损坏）。副作用是库里多出 -wal / -shm 两个文件，属正常。

        故意**不动 foreign_keys**：SQLite 默认关闭外键约束，贸然打开会改变删除对局记录等
        既有行为（那些路径没按级联删设计），不属于性能优化的范围。
        """
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        # 写锁竞争时的等待上限。sqlite3 的 C 层默认已是 5s，但那是**库内默认**、
        # 不写在代码里就看不见；显式声明一枚，便于按部署环境调，也避免将来换驱动
        # （或换成 aiosqlite）时悄悄退回「立即 SQLITE_BUSY」。审计 §3.15。
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI 依赖：每个请求一个会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  确保模型已注册

    Base.metadata.create_all(bind=engine)
    _migrate_schema()
    _reconcile_stale_games()
    _auto_backup()          # 先备份（含全量历史），再谈瘦身 —— 备份是瘦身的兜底
    _prune_history()


# 旧库升级清单：(表, 列, DDL)。
# create_all 只会建新表，不会给已存在的表补列；而 SQLAlchemy 的 INSERT 会带上
# 全部映射列，旧库缺列时第一次开局就会炸。所以启动时按模型把缺的列补上。
# 只加列、不改不删；带默认值的 ADD COLUMN 在 SQLite 上对已有行是安全的。
_MISSING_COLUMNS: list[tuple[str, str, str]] = [
    ("games", "move_seconds", "INTEGER NOT NULL DEFAULT 0"),
    ("games", "review_progress", "FLOAT NOT NULL DEFAULT 0"),
    ("games", "review_stage", "VARCHAR(16) NOT NULL DEFAULT ''"),
    ("games", "review_detail", "VARCHAR(128) NOT NULL DEFAULT ''"),
    # 题库扩容：题型 + 目标子（旧行补上默认值，下次 seed_builtin 会按题型重写）
    ("tsumego_problems", "kind", "VARCHAR(16) NOT NULL DEFAULT 'life'"),
    ("tsumego_problems", "targets", "TEXT NOT NULL DEFAULT '[]'"),
    ("tsumego_problems", "own", "TEXT NOT NULL DEFAULT '[]'"),
    # 难度改为按正解线手数客观打分（1~9）后，多了一个五档标签列
    ("tsumego_problems", "tier", "VARCHAR(8) NOT NULL DEFAULT '入门'"),
    # L15：执子来源（猜先 vs 自选）入库；旧行无从考证，一律算自选
    ("games", "color_source", "VARCHAR(16) NOT NULL DEFAULT 'pick'"),
]


def _add_missing_columns() -> None:
    if not settings.database_url.startswith("sqlite"):
        return      # PostgreSQL 之类请用正规迁移工具，这里不猜它的方言
    with engine.connect() as conn:
        for table, column, ddl in _MISSING_COLUMNS:
            info = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            if not info:            # 表还不存在：create_all 会连列一起建好
                continue
            if column not in {row[1] for row in info}:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        conn.commit()


# 迁移只加列、从不建索引：新库由 create_all 建出 20 个索引，而升级上来的生产库
# 只有 18 个（缺 tsumego_problems.kind / _tier），筛选照样全表扫描。
# 索引缺失不改变行为，所以这条是「补上就好」。
# 第三个元素是**列表达式**（可含多列），不是单个列名 —— 复合索引用得上。
_MISSING_INDEXES: list[tuple[str, str, str]] = [
    ("tsumego_problems", "ix_tsumego_problems_kind", "kind"),
    ("tsumego_problems", "ix_tsumego_problems_tier", "tier"),
    ("rank_events", "ix_rank_events_game_id", "game_id"),
    # 对局列表是 `WHERE user_id=? ORDER BY created_at DESC LIMIT/OFFSET`：
    # 单列 user_id 索引还要再排序，复合索引 (user_id, created_at) 一步到位（§3.15）。
    ("games", "ix_games_user_created", "user_id, created_at"),
]


def _ensure_indexes() -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    with engine.connect() as conn:
        for table, name, columns in _MISSING_INDEXES:
            info = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            if not info:            # 表不存在：create_all 会连索一起建好
                continue
            conn.exec_driver_sql(
                f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})")
        conn.commit()


#: 当前 schema 版本，写进 SQLite 库头的 `PRAGMA user_version`。
#: 目的：升级上来的一次性迁移（补列/补索引）此后靠版本号判断「这个库滚到哪一轮」，
#: 不必每次靠「试着补、补不上就跳过」去猜。加新迁移时递增此值。
SCHEMA_VERSION = 1


def _migrate_schema() -> None:
    """把库滚到当前 schema 版本：补列 + 补索引 + 记录版本号。"""
    _add_missing_columns()
    _ensure_indexes()
    if not settings.database_url.startswith("sqlite"):
        return
    try:
        with engine.connect() as conn:
            cur = int(conn.exec_driver_sql("PRAGMA user_version").scalar() or 0)
            if cur == SCHEMA_VERSION:
                return
            # PRAGMA 不接受参数占位符，只能拼字面量；SCHEMA_VERSION 是模块常量 int，安全。
            conn.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION:d}")
            conn.commit()
        logger.info("数据库 schema 版本 %d → %d", cur, SCHEMA_VERSION)
    except Exception as exc:   # noqa: BLE001  记不上版本不该拦住启动
        logger.warning("schema 版本记录失败（不影响启动）：%s", exc)


#: 「僵尸局」年龄门槛（小时）。服务重启后内存 hub 里没有任何活对局，所以 DB 里
#: 仍挂着 playing/scoring 的行一定是上一个进程留下的（审计 1.22 中：生产库实查有
#: 2 局卡死 6 天，大厅的「进行中对局」一直显示着它们）。留一个年龄门槛是为了
#: 不跟"刚开完就重启"的局抢跑 —— 那种局用户马上就会重开。
STALE_GAME_HOURS = 6


def _reconcile_stale_games() -> None:
    """把重启后残留的进行中对局标记为「未续下」。

    只改状态、不改手顺与分析（数据留给用户自己看/导出），也**不触碰等级**：
    记一胜一负都要有对局结果作依据，而这里根本没有结果，宁可什么都不记。
    """
    if not settings.database_url.startswith("sqlite"):
        return      # 多副本部署时"重启"不等于"这局没人下"，别替它下结论
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        hours=STALE_GAME_HOURS)
    # 库里存的是 `YYYY-MM-DD HH:MM:SS.ffffff`（naive UTC），同一格式下字符串比较
    # 与时间比较等价 —— 用字符串能把比较交给 SQLite，不必把回环时区搬进查询。
    stamp = cutoff.strftime("%Y-%m-%d %H:%M:%S.%f")
    try:
        with engine.connect() as conn:
            res = conn.exec_driver_sql(
                "UPDATE games SET status='finished', finished=1,"
                " finish_reason='abandoned', result_text='未续下（服务重启）',"
                " winner=0, player_won=0"
                " WHERE status IN ('playing','scoring') AND updated_at < ?",
                (stamp,))
            changed = res.rowcount or 0
            conn.commit()
    except Exception as exc:   # noqa: BLE001  清理失败不该拦住启动
        logger.warning("进行中对局清理失败（不影响启动）：%s", exc)
        return
    if changed:
        logger.info("清理了 %d 局重启后残留的「进行中」对局（标记为未续下）", changed)


def _auto_backup() -> None:
    """SQLite 生产库每日自动备份一份（保留最近 7 份）。

    这是对审计 1.4 的兜底：`backend/data/` 在 .gitignore 里，一次 `git clean -xdf`
    就能不可逆地删掉全部用户/对局/复盘数据，而全仓此前**没有任何备份机制**。
    移动数据目录到工作区外是更彻底的做法，但会改变既有安装的数据位置；
    这里先给一条恢复路径。

    只备份默认生产库（`go_teach.db`）：测试库名各式各样，绝不能在这里误伤。
    """
    if not settings.database_url.startswith("sqlite"):
        return
    try:
        path = Path(settings.database_url.split("sqlite:///", 1)[1])
    except (IndexError, ValueError):
        return
    if path.name != "go_teach.db" or not path.exists():
        return
    backup_dir = DATA_DIR / "backups"
    target = backup_dir / f"{path.stem}-{datetime.now().strftime('%Y%m%d')}.db"
    if target.exists():
        return
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(str(path))
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        # 只保留最近 7 份
        old = sorted(backup_dir.glob(f"{path.stem}-*.db"))
        for stale in old[:-7]:
            try:
                stale.unlink()
            except OSError:
                pass
        logger.info("数据库已备份：%s", target)
    except Exception as exc:   # noqa: BLE001
        logger.warning("数据库备份失败（不影响启动）：%s", exc)


def _prune_history() -> None:
    """数据保留：每用户最近 N 局保留全量，更早且超龄的对局只留「摘要」。

    剥掉 `analyses` / `review_json` 两份重量级 JSON（占库体积约 79%），
    保留手顺 / SGF / 结果 / 复盘状态 —— 历史局几乎不会再翻看逐手曲线。

    默认关闭（`GO_RETENTION_DAYS=0`），因为这是**数据销毁**性质的操作：
    开不开、留多少天由部署方决定，不由代码替人拍板。开启后每次启动检查一次，
    且先跑 `_auto_backup()` 再跑本函数（备份是瘦身的兜底）。

    与 `scripts/db_maintenance.py --retention-days` 是同一策略的两处实现
    （那份刻意不 import 本模块，见其文件头注释），改动时请同步。
    """
    days = int(settings.retention_days or 0)
    if days <= 0 or not settings.database_url.startswith("sqlite"):
        return
    keep = max(0, int(settings.retention_keep_recent or 0))
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
              - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S.%f")
    try:
        with engine.connect() as conn:
            user_ids = [r[0] for r in conn.exec_driver_sql(
                "SELECT DISTINCT user_id FROM games").fetchall()]
            victims: list[str] = []
            for uid in user_ids:
                # 该用户「超出最近 N 局」且超龄、且还留着重量级 JSON 的对局
                rows = conn.exec_driver_sql(
                    "SELECT id FROM games WHERE user_id = ? AND updated_at < ?"
                    " AND (COALESCE(LENGTH(analyses), 0) > 2"
                    "      OR COALESCE(LENGTH(review_json), 0) > 2)"
                    " ORDER BY created_at DESC",
                    (uid, cutoff)).fetchall()
                victims.extend(r[0] for r in rows[keep:])
            if not victims:
                return
            marks = ",".join("?" * len(victims))
            conn.exec_driver_sql(
                f"UPDATE games SET analyses='[]', review_json='{{}}'"
                f" WHERE id IN ({marks})", tuple(victims))
            conn.commit()
        logger.info("数据保留：瘦身 %d 局（每用户保留最近 %d 局全量，阈值 %d 天）",
                    len(victims), keep, days)
    except Exception as exc:   # noqa: BLE001  瘦身失败不该拦住启动
        logger.warning("数据保留瘦身失败（不影响启动）：%s", exc)

