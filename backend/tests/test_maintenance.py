"""生产库维护：僵尸局收编（database._reconcile_stale_games）+ 维护脚本选择逻辑。"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.database import STALE_GAME_HOURS, SessionLocal, _reconcile_stale_games
from app.models import GameRecord, User

BACKEND = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module", autouse=True)
def _schema():
    """本模块直接用 SessionLocal，没走 TestClient 的 lifespan，得自己把表建出来。"""
    from app.database import init_db
    init_db()


def _load_db_maintenance():
    """scripts/ 不是包，按路径加载。"""
    path = BACKEND / "scripts" / "db_maintenance.py"
    spec = importlib.util.spec_from_file_location("db_maintenance", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["db_maintenance"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 僵尸局
# ---------------------------------------------------------------------------

def _make_game(db, *, status: str, age_hours: float) -> str:
    when = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=age_hours)
    user = User(username=f"stale{uuid.uuid4().hex[:12]}", password_hash="x")
    db.add(user)
    db.flush()
    game = GameRecord(user_id=user.id, status=status, finished=False)
    game.updated_at = when
    game.created_at = when
    db.add(game)
    db.commit()
    return game.id


def test_stale_playing_game_is_closed_but_rank_is_untouched():
    with SessionLocal() as db:
        gid = _make_game(db, status="playing", age_hours=STALE_GAME_HOURS + 12)

    _reconcile_stale_games()

    with SessionLocal() as db:
        game = db.get(GameRecord, gid)
        assert game.status == "finished"
        assert game.finished is True
        assert game.finish_reason == "abandoned"
        # 关键：不能因为「清理」就凭空记一胜一负
        assert game.winner == 0
        assert not game.player_won


def test_fresh_playing_game_is_left_alone():
    """年龄门槛的作用：刚开完就重启的局不该被清掉（用户马上会继续下）。"""
    with SessionLocal() as db:
        gid = _make_game(db, status="playing", age_hours=0.1)

    _reconcile_stale_games()

    with SessionLocal() as db:
        assert db.get(GameRecord, gid).status == "playing"


def test_scoring_game_is_also_closed():
    with SessionLocal() as db:
        gid = _make_game(db, status="scoring", age_hours=STALE_GAME_HOURS + 1)

    _reconcile_stale_games()

    with SessionLocal() as db:
        assert db.get(GameRecord, gid).status == "finished"


def test_finished_game_is_not_touched():
    with SessionLocal() as db:
        gid = _make_game(db, status="finished", age_hours=STALE_GAME_HOURS + 1)

    _reconcile_stale_games()

    with SessionLocal() as db:
        assert db.get(GameRecord, gid).finish_reason == ""


def test_init_db_reconciles(monkeypatch):
    """回归：这个清理函数曾经定义了却没接进 init_db（等于 1.16 的修复是死代码）。"""
    from app import database
    calls: list[int] = []
    monkeypatch.setattr(database, "_reconcile_stale_games", lambda: calls.append(1))
    database.init_db()
    assert calls == [1], "init_db 必须调用 _reconcile_stale_games()"


# ---------------------------------------------------------------------------
# 维护脚本
# ---------------------------------------------------------------------------

@pytest.fixture()
def mini_db(tmp_path):
    """一个只含维护脚本会用到的表的最小库。"""
    path = tmp_path / "mini.db"
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT);
        CREATE TABLE games (id TEXT PRIMARY KEY, user_id TEXT, status TEXT,
                            updated_at TEXT, created_at TEXT,
                            finished INTEGER DEFAULT 0,
                            finish_reason TEXT DEFAULT '', result_text TEXT DEFAULT '',
                            winner INTEGER DEFAULT 0, player_won INTEGER DEFAULT 0,
                            analyses TEXT DEFAULT '[]', review_json TEXT DEFAULT '{}');
        CREATE TABLE rank_events (id TEXT PRIMARY KEY, user_id TEXT);
        CREATE TABLE tsumego_progress (id TEXT PRIMARY KEY, user_id TEXT);
        INSERT INTO users VALUES ('u1','keeper'),('u2','smoke1788490300'),
                                 ('u3','probe_对局口径(human)'),('u4','wr2'),
                                 ('u5','l18probe'),('u6','l18p2');
        INSERT INTO games VALUES ('g1','u1','finished','2026-01-01 00:00:00.000000','2026-01-01 00:00:00.000000',1,'','',1,1,'[]','{}');
        INSERT INTO games VALUES ('g2','u2','playing','2026-01-01 00:00:00.000000','2026-01-01 00:00:00.000000',0,'','',0,0,'[]','{}');
        INSERT INTO rank_events VALUES ('e1','u1');
        INSERT INTO tsumego_progress VALUES ('t1','u4');
    """)
    conn.commit()
    conn.close()
    return path


def test_test_account_regex_hits_history_and_spares_real_users(mini_db):
    mod = _load_db_maintenance()
    conn = sqlite3.connect(str(mini_db))
    try:
        found = {name for _uid, name in mod.find_test_accounts(conn)}
    finally:
        conn.close()
    assert found == {"smoke1788490300", "probe_对局口径(human)", "wr2",
                     "l18probe", "l18p2"}
    assert "keeper" not in found


def test_backup_lands_outside_the_daily_retention_glob(mini_db):
    """手工备份若和每日备份同层同模式，会被 `_auto_backup` 的「留 7 份」连带删掉。"""
    mod = _load_db_maintenance()
    target = mod.backup(mini_db)
    assert target.exists()
    assert target.parent.name == "manual"
    # 每日备份的 glob 是 backups/go_teach-*.db（非递归），扫不到 manual/ 子目录
    assert list(target.parent.parent.glob("go_teach-*.db")) == []
    # 备份内容可用
    conn = sqlite3.connect(str(target))
    try:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 6
    finally:
        conn.close()


def test_purge_removes_accounts_and_cascades(mini_db):
    mod = _load_db_maintenance()
    conn = sqlite3.connect(str(mini_db))
    try:
        assert mod.purge_test_accounts(conn) == 5
        left = {r[0] for r in conn.execute("SELECT username FROM users")}
        assert left == {"keeper"}
        # 关联数据一起走，不留孤儿行
        assert conn.execute("SELECT COUNT(*) FROM games WHERE user_id != 'u1'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM rank_events WHERE user_id = 'u1'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tsumego_progress").fetchone()[0] == 0
    finally:
        conn.close()


def test_close_stale_games_only_touches_old_playing_rows(mini_db):
    mod = _load_db_maintenance()
    conn = sqlite3.connect(str(mini_db))
    try:
        assert mod.close_stale_games(conn, hours=6) == 1
        rows = dict(conn.execute("SELECT id, status FROM games").fetchall())
        assert rows == {"g1": "finished", "g2": "finished"}
        assert conn.execute(
            "SELECT finish_reason FROM games WHERE id='g2'").fetchone()[0] == "abandoned"
        # 已完成的那局不该被改写
        assert conn.execute(
            "SELECT finish_reason FROM games WHERE id='g1'").fetchone()[0] == ""
    finally:
        conn.close()


def test_prune_strips_heavy_columns_but_keeps_the_game(mini_db):
    mod = _load_db_maintenance()
    conn = sqlite3.connect(str(mini_db))
    try:
        conn.execute("UPDATE games SET analyses='[{\"a\":1}]', review_json='{\"b\":2}'")
        conn.commit()
        assert mod.prune_old_games(conn, days=1) == 2
        for analyses, review in conn.execute("SELECT analyses, review_json FROM games"):
            assert analyses == "[]" and review == "{}"
        assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 2
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 数据保留策略（审计 §3 / §5 P2）
# ---------------------------------------------------------------------------

def test_prune_old_games_keeps_recent_per_user(tmp_path):
    """每用户最近 N 局无论多老都保留全量（应用启动期同策略见 _prune_history）。"""
    mod = _load_db_maintenance()
    path = tmp_path / "ret.db"
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE games (id TEXT PRIMARY KEY, user_id TEXT, status TEXT,
                            updated_at TEXT, created_at TEXT,
                            finished INTEGER DEFAULT 0, finish_reason TEXT DEFAULT '',
                            result_text TEXT DEFAULT '', winner INTEGER DEFAULT 0,
                            player_won INTEGER DEFAULT 0,
                            analyses TEXT DEFAULT '[]', review_json TEXT DEFAULT '{}');
    """)
    stamp = "2020-01-01 00:00:00.000000"
    for i in range(4):
        # 同一用户 4 局，created_at 递增；全部超龄、全部带重量级 JSON
        conn.execute(
            "INSERT INTO games (id, user_id, status, updated_at, created_at,"
            " analyses, review_json) VALUES (?,?,?,?,?,?,?)",
            (f"a{i}", "u1", "finished", stamp,
             f"2020-01-0{i + 1} 00:00:00.000000", '[{"a":1}]', '{"b":2}'))
    conn.commit()
    try:
        assert mod.prune_old_games(conn, days=1, keep_recent=2) == 2
        kept = {r[0] for r in conn.execute(
            "SELECT id FROM games WHERE analyses != '[]'")}
    finally:
        conn.close()
    assert kept == {"a2", "a3"}, "最近 2 局必须保留全量"


def test_prune_history_is_opt_in_and_keeps_recent(monkeypatch):
    """`_prune_history` 默认关闭；开启后每用户最近 N 局保留全量。"""
    from app import database

    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    with SessionLocal() as db:
        u = User(username=f"ret{uuid.uuid4().hex[:10]}", password_hash="x")
        db.add(u)
        db.flush()
        ids = []
        for i in range(4):
            g = GameRecord(user_id=u.id, status="finished", finished=True,
                           analyses=[{"a": 1}], review_json={"b": 2})
            g.created_at = old + timedelta(hours=i)
            g.updated_at = old + timedelta(hours=i)
            db.add(g)
            db.flush()
            ids.append(g.id)
        db.commit()

    # 默认关闭（retention_days=0）：一局都不碰
    monkeypatch.setattr(database.settings, "retention_days", 0)
    database._prune_history()
    with SessionLocal() as db:
        assert db.get(GameRecord, ids[0]).analyses == [{"a": 1}], "关闭时不该瘦身"

    monkeypatch.setattr(database.settings, "retention_days", 1)
    monkeypatch.setattr(database.settings, "retention_keep_recent", 2)
    database._prune_history()
    with SessionLocal() as db:
        kept = [db.get(GameRecord, gid) for gid in ids[2:]]
        pruned = [db.get(GameRecord, gid) for gid in ids[:2]]
    assert all(g.analyses == [{"a": 1}] for g in kept), "最近 2 局保留全量"
    assert all(g.analyses == [] and g.review_json == {} for g in pruned)


def test_schema_version_busy_timeout_and_composite_index():
    """§3.15：版本号记录 + busy_timeout + 对局列表的复合索引。"""
    from app.database import SCHEMA_VERSION, engine

    with engine.connect() as conn:
        assert int(conn.exec_driver_sql("PRAGMA user_version").scalar() or 0) \
            == SCHEMA_VERSION, "schema 版本没记到库头"
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
        names = {r[1] for r in conn.exec_driver_sql("PRAGMA index_list('games')")}
    assert "ix_games_user_created" in names, "对局列表的复合索引没建出来"
