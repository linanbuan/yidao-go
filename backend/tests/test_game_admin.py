"""对局管理：强制结束、删除记录（保留战绩）、日历统计。

这三件事共用一条底线：**胜负场数与总胜率只由正常终局写入，任何清理动作都不能改它**。
所以每个用例都会顺带断言 User 上的统计字段没被动过 —— 那是最容易在重构里被顺手
"修正"掉的地方，而一旦被动，学员的晋升进度就会凭空回退。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.game.rules import Game
from app.main import app
from app.models import GameRecord


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth(client):
    username = f"admin{int(time.time() * 1000) % 100000}"
    r = client.post("/api/auth/register", json={
        "username": username, "password": "secret123", "displayName": "管理员测试"})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['token']}"}, data["user"]


def _register(client, tag: str) -> dict:
    """另开一个账号，用来验证越权访问被挡住。"""
    r = client.post("/api/auth/register", json={
        "username": f"{tag}{int(time.time() * 1000) % 100000}", "password": "secret123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _new_game(client, headers, size: int = 9) -> str:
    r = client.post("/api/games", json={"size": size, "komi": 5.5, "hintMode": False},
                    headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["game"]["id"]


def _progress(client, headers) -> dict:
    return client.get("/api/auth/me", headers=headers).json()["user"]["progress"]


# ---------------------------------------------------------------- 强制结束


def test_finish_without_winner_does_not_invent_one():
    """winner=0 必须原样落到 result 里。

    旧逻辑只有 pass-pass 与「认输」两条分支，后者会从 result_text 里猜颜色
    （没有"黑"字就判白胜）。强制结束的文案里没有颜色字，于是 result_json 会
    凭空记一个白胜 —— 日历统计正是按 winner 判断有无胜负的，这一条假胜负会
    把作废局算成一场败局。
    """
    g = Game(size=9, komi=5.5)
    res = g.finish("force-end", winner=0, result_text="强制结束（不计入胜负）")
    assert res["winner"] == 0
    assert g.finished is True and g.finish_reason == "force-end"


def test_force_end_releases_the_active_slot(client, auth):
    """强制结束的首要用途：把卡住的对局名额放出来。"""
    headers, _ = auth
    gid = _new_game(client, headers)
    # 有进行中的对局时开新局会被拒
    assert client.post("/api/games", json={"size": 9}, headers=headers).status_code == 409

    r = client.post(f"/api/games/{gid}/force-end", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    # 必须是一条完整的 gameEnd：前端 applyEvent 按 type 分发，缺了就静默丢弃，
    # 表现为「点了强制结束但页面没反应」（WS 断开时尤其明显）
    assert body["event"]["type"] == "gameEnd"
    assert body["event"]["reason"] == "force-end"
    assert body["event"]["playerWon"] is False
    assert body["event"]["rank"] is None, "不计胜负就不该有等级变动"
    assert body["event"]["countsForRank"] is False
    assert body["event"]["resultText"]
    assert body["game"]["finishReason"] == "force-end"

    assert client.post("/api/games", json={"size": 9}, headers=headers).status_code == 200


def test_force_end_leaves_rank_stats_untouched(client, auth):
    headers, _ = auth
    before = _progress(client, headers)
    gid = _new_game(client, headers)
    assert client.post(f"/api/games/{gid}/force-end", headers=headers).status_code == 200

    after = _progress(client, headers)
    for key in ("totalGames", "totalWins", "rankWins", "rankLosses", "winStreak"):
        assert after[key] == before[key], f"强制结束不该改动 {key}"

    g = client.get(f"/api/games/{gid}", headers=headers).json()["game"]
    assert g["status"] == "abandoned" and g["finished"] is True
    assert g["winner"] == 0 and g["playerWon"] is False
    # 作废局不进复盘队列的判定依据是 winner==0，日历据此把它排除在胜率分母之外
    assert g["result"]["winner"] == 0


def test_force_end_refuses_an_already_finished_game(client, auth):
    """已终局的棋不允许再强制结束。

    否则一条真实败局会被改写成「无胜负」：败局早已计入 total_losses，而日历是
    按 winner 算胜率的，改写后它就掉出了分母 —— 总战绩说输过、日历胜率却没输过，
    两处数字对不上。要作废一盘下完的棋请用删除记录。
    """
    headers, _ = auth
    gid = _new_game(client, headers)
    assert client.post(f"/api/games/{gid}/resign", headers=headers).status_code == 200

    r = client.post(f"/api/games/{gid}/force-end", headers=headers)
    assert r.status_code == 400
    g = client.get(f"/api/games/{gid}", headers=headers).json()["game"]
    assert g["finishReason"] == "player-resign" and g["winner"] == 2


def test_force_end_works_after_a_restart(client, auth):
    """服务重启后内存里没这局了，也得能强制结束（它同样占着名额）。

    这条路径会先走 restore() 把局面从数据库重建出来再作废。重点盯两件事：
    记录真的变成了 abandoned，以及作废后不会再被 restore 当成活对局拉回来。
    """
    from app.database import SessionLocal
    from app.game.manager import get_hub

    headers, _ = auth
    gid = _new_game(client, headers)
    get_hub().remove(gid)                     # 模拟重启：内存里已经没有这局了
    with SessionLocal() as db:
        assert db.get(GameRecord, gid).status == "playing"

    assert client.post(f"/api/games/{gid}/force-end", headers=headers).status_code == 200
    with SessionLocal() as db:
        rec = db.get(GameRecord, gid)
        assert rec.status == "abandoned" and rec.finish_reason == "force-end"
    # 作废后 restore() 不会再把它当成活对局拉回来
    assert get_hub().get(gid) is None


def test_force_end_without_a_live_game_still_returns_a_full_event(client, auth):
    """直接对 hub 调 force_end(rec, live=None)，事件仍必须是完整的 gameEnd。

    REST 层正常情况下总能 restore 出活对局，所以这条分支走不到；但事件组装
    写在 if live 分支里的话，任何不经 restore 的调用方（运维脚本、以后的管理
    接口）都会拿到一条缺 type 的半成品，而前端按 type 分发 —— 缺了就静默丢弃。
    """
    from app.database import SessionLocal
    from app.game.manager import get_hub

    headers, _ = auth
    gid = _new_game(client, headers)
    with SessionLocal() as db:
        rec = db.get(GameRecord, gid)
        event = get_hub().force_end(rec, None)      # 明确不给 live
        db.commit()
    get_hub().remove(gid)

    for key in ("type", "reason", "winner", "resultText", "playerWon",
                "avgLossPoints", "sgf", "rank", "aiWords"):
        assert key in event, f"DB-only 路径的 gameEnd 事件缺字段 {key}"
    assert event["type"] == "gameEnd" and event["rank"] is None
    assert event["resultText"] == "强制结束（不计入胜负）"


# ---------------------------------------------------------------- 删除记录


def test_delete_record_keeps_win_loss_stats(client, auth):
    """删记录只删明细：胜负场数与总胜率必须原封不动。

    本级胜场驱动晋升进度，删一盘棋就把进度回退是不可接受的；代价是
    「总战绩 50 胜」与「列表只剩 10 条」会共存，那是预期行为。
    """
    headers, _ = auth
    gid = _new_game(client, headers)
    client.post(f"/api/games/{gid}/resign", headers=headers)
    assert _progress(client, headers)["totalGames"] == 1

    r = client.delete(f"/api/games/{gid}", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] == 1 and body["remaining"] == 0
    assert body["stats"] == {"totalGames": 1, "totalWins": 0, "totalLosses": 1,
                             "winrate": 0.0}

    assert _progress(client, headers)["totalGames"] == 1
    assert client.get(f"/api/games/{gid}", headers=headers).status_code == 404
    assert client.get("/api/games", headers=headers).json()["total"] == 0


def test_delete_unlinks_timeline_entry(client, auth):
    """时间线的「复盘」按钮按 gameId 渲染，记录删了却留着 id 就是 404。

    RankEvent.game_id 没有外键约束，不会跟着级联清掉，必须显式断开。
    """
    from app.database import SessionLocal
    from app.models import RankEvent

    headers, user = auth
    gid = _new_game(client, headers)
    client.post(f"/api/games/{gid}/resign", headers=headers)
    with SessionLocal() as db:
        db.add(RankEvent(user_id=user["id"], kind="promote", from_rank=1, to_rank=2,
                         game_id=gid, detail="测试用晋升事件"))
        db.commit()
    assert any(it["gameId"] == gid
               for it in client.get("/api/auth/me/timeline", headers=headers).json()["items"])

    assert client.delete(f"/api/games/{gid}", headers=headers).status_code == 200
    items = client.get("/api/auth/me/timeline", headers=headers).json()["items"]
    assert not any(it["gameId"] == gid for it in items)


def test_clear_history_skips_ongoing_games(client, auth):
    headers, _ = auth
    lost = _new_game(client, headers)
    client.post(f"/api/games/{lost}/resign", headers=headers)
    void = _new_game(client, headers)
    client.post(f"/api/games/{void}/force-end", headers=headers)
    ongoing = _new_game(client, headers)              # 进行中，不该被批量删掉

    r = client.post("/api/games/clear-history", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] == 2 and body["skipped"] == 1
    assert body["remaining"] == 1
    assert body["stats"]["totalGames"] == 1 and body["stats"]["totalWins"] == 0

    left = client.get("/api/games", headers=headers).json()
    assert [it["id"] for it in left["items"]] == [ongoing]


# ---------------------------------------------------------------- 越权


def test_admin_endpoints_are_owner_only(client, auth):
    headers, _ = auth
    gid = _new_game(client, headers)
    stranger = _register(client, "stranger")

    assert client.post(f"/api/games/{gid}/force-end", headers=stranger).status_code == 403
    assert client.delete(f"/api/games/{gid}", headers=stranger).status_code == 403
    # 陌生人的清空操作只动自己的库，一条也删不到别人头上
    assert client.post("/api/games/clear-history", headers=stranger).json()["deleted"] == 0
    assert client.get("/api/games", headers=headers).json()["total"] == 1
    assert client.get(f"/api/games/{gid}", headers=headers).status_code == 200


# ---------------------------------------------------------------- 日历


def test_calendar_buckets_by_local_day_not_utc():
    """created_at 存的是 naive UTC，日历必须按本地日期分桶。

    直接对 naive 值调 astimezone() 会把它当成本地时间，于是东八区凌晨 0~8 点的
    对局全部被记到前一天。下面两个断言分别盯住「本地凌晨不错位」与「naive 值
    确实被当成 UTC 解释」，两者都与运行环境的时区无关。
    """
    from app.api.stats import _local_tz, _to_local

    tz = _local_tz()
    probe = datetime.now(tz).replace(hour=0, minute=30, second=0, microsecond=0)
    naive_utc = probe.astimezone(timezone.utc).replace(tzinfo=None)
    got = _to_local(naive_utc)
    assert got.date() == probe.date(), "本地凌晨的对局被记到了前一天"
    assert (got.hour, got.minute) == (0, 30)

    # naive 值必须按 UTC 解释：等于先补上 UTC 再转本地，而不是当成已经是本地时间
    naive = datetime(2026, 1, 1, 12, 0)
    assert _to_local(naive) == datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).astimezone(tz)


def test_calendar_counts_games_and_excludes_void_from_winrate(client, auth):
    headers, _ = auth
    lost = _new_game(client, headers)
    client.post(f"/api/games/{lost}/resign", headers=headers)
    void = _new_game(client, headers)
    client.post(f"/api/games/{void}/force-end", headers=headers)

    cal = client.get("/api/stats/calendar", headers=headers).json()
    # 不假定是哪一天：测试正好跨过本地午夜时 today 会算错，直接取唯一的那天
    assert len(cal["days"]) == 1, cal["days"]
    day = next(iter(cal["days"].values()))
    assert day["games"] == 2
    assert day["losses"] == 1 and day["wins"] == 0
    assert day["noResult"] == 1
    # 作废局进了局数但不进胜率分母，否则强制结束就成了抬高胜率的按钮
    assert day["winrate"] == 0.0

    assert cal["totals"]["games"] == 2 and cal["totals"]["winrate"] == 0.0
    assert cal["lifetime"]["totalGames"] == 1, "强制结束不计入生涯战绩"
    assert cal["lifetime"]["winrate"] == 0.0
    assert "UTC" in cal["timezone"]


def test_calendar_reflects_deleted_records_but_not_lifetime(client, auth):
    """删掉记录后日历上那天会消失，生涯胜负仍然在。"""
    headers, _ = auth
    gid = _new_game(client, headers)
    client.post(f"/api/games/{gid}/resign", headers=headers)
    assert client.get("/api/stats/calendar", headers=headers).json()["totals"]["games"] == 1

    client.delete(f"/api/games/{gid}", headers=headers)
    cal = client.get("/api/stats/calendar", headers=headers).json()
    assert cal["totals"]["games"] == 0 and cal["days"] == {}
    assert cal["lifetime"]["totalGames"] == 1 and cal["lifetime"]["totalWins"] == 0


def test_calendar_accepts_explicit_month_and_rejects_others(client, auth):
    headers, _ = auth
    empty = client.get("/api/stats/calendar", params={"year": 2001, "month": 3},
                       headers=headers)
    assert empty.status_code == 200
    assert empty.json()["days"] == {} and empty.json()["year"] == 2001
    # month=13 会被 Query 的取值范围挡掉
    assert client.get("/api/stats/calendar", params={"month": 13},
                      headers=headers).status_code == 422
