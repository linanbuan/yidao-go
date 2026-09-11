"""等级体系测试：段位表完整性、晋升战流程、降级保护、高段吻合度门槛。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import User
from app.rank.defs import MAX_RANK_ID, RANKS, get_engine_profile, get_rank
from app.rank.logic import on_game_finished, progress_of, promotion_profile
from app.security import hash_password


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    yield s
    s.close()


def make_user(session, rank_id: int = 1, demotion: bool = False) -> User:
    u = User(username=f"u{rank_id}", password_hash=hash_password("test123456"),
             rank_id=rank_id, best_rank_id=rank_id, demotion_enabled=demotion)
    session.add(u)
    session.commit()
    return u


# ---------------------------------------------------------------------------
# 段位表
# ---------------------------------------------------------------------------
def test_rank_table_has_27_levels():
    assert len(RANKS) == MAX_RANK_ID == 27
    assert [r.rank_id for r in RANKS] == list(range(1, 28))
    assert RANKS[0].name == "18级"
    assert RANKS[17].name == "1级"
    assert RANKS[18].name == "业余1段"
    assert RANKS[-1].name == "九段"


def test_engine_profiles_are_monotonic():
    visits = [get_engine_profile(r.rank_id).max_visits for r in RANKS]
    assert visits == sorted(visits), "搜索量应随段位单调不降"
    assert get_engine_profile(MAX_RANK_ID).max_visits >= 1600, "九段接近满血"

    tols = [get_engine_profile(r.rank_id).sample_tolerance for r in RANKS]
    assert tols == sorted(tols, reverse=True), "容忍度应随段位递减（越强越不肯亏）"
    assert get_engine_profile(MAX_RANK_ID).sample_tolerance == 0.0

    # 级位使用 human SL 拟人棋风，高段开启 ponder
    assert all(get_engine_profile(i).use_human_model for i in range(1, 19))
    assert not get_engine_profile(27).use_human_model
    assert all(get_engine_profile(i).ponder for i in range(24, 28))


def test_local_noise_is_the_weakening_knob():
    """两个削弱旋钮（local_noise / sample_tolerance）必须随段位单调递减。

    **不要改回「用 max_visits 削弱」**：实测 visits=2 时 KataGo 只报 1.1 个候选，
    AI 被迫下最优点，每手损失仅 0.82 目（职业量级）—— 砍 visit 反而最强。
    visits 买的是候选池宽度与给学员的分析精度，弱档位同样需要 32 以上，
    所以这里盯的是削弱旋钮单调递减 + visits 有下限，而不是 visits 的上限。

    tolerance 递减尤其重要：旧实现拿 humanPrior 做采样基准时，七段每手亏
    1.68 目、五段只亏 0.39 目 —— 高段反而更弱，而且这种倒挂只有实测才能发现。
    """
    noise = [get_engine_profile(r.rank_id).local_noise for r in RANKS]
    assert noise == sorted(noise, reverse=True), "局部噪声应随段位递减"
    assert all(0.0 <= n <= 1.0 for n in noise)
    assert get_engine_profile(1).local_noise >= 0.5, "18级应当大量只看局部"
    assert get_engine_profile(MAX_RANK_ID).local_noise == 0.0, "九段不掺噪声"
    assert get_engine_profile(1).max_visits >= 32, "18级也要有足够宽的候选池"


def test_promotion_profile_faces_target_rank_not_a_superhuman():
    """晋升战对手必须是「目标段位的常规画像」，不能是零削弱的满血 KataGo。

    旧实现取「本级 visits×2 + 三个削弱旋钮全归零」，后果是难度断崖：
    18级 平时每手亏 ~4.3 目（新手能赢），晋升战却是每手亏 ~0 目的超人类对手，
    还要连胜 2 局 —— 玩家永远升不上去，这是「各段位难度太高」的主因。
    """
    for rid in (1, 9, 18, 19, 23):
        cur, nxt, p = (get_engine_profile(rid), get_engine_profile(rid + 1),
                       promotion_profile(rid))
        assert p.local_noise == nxt.local_noise, "应沿用目标段位的削弱档位"
        assert p.sample_tolerance == nxt.sample_tolerance
        assert p.human_sl_profile == nxt.human_sl_profile
        assert p.max_visits >= cur.max_visits, "晋升战搜索量不应低于平时"
        assert p.ponder is True
        # 不能出现断崖：低段的晋升对手依旧得是可赢的（带削弱）
        assert p.local_noise > 0.0 and p.sample_tolerance > 0.0
    # 九段已是顶格，目标就是它自己（满血，无削弱）
    top = promotion_profile(MAX_RANK_ID)
    assert top.local_noise == 0.0 and top.sample_tolerance == 0.0
    assert top.max_visits == get_engine_profile(MAX_RANK_ID).max_visits


def test_resign_is_gated_for_kyu_but_never_disabled():
    """级位档认输有三道闸：大阈值、最小手数、连续绝望手数；但绝不是「永不认输」。

    早先试过 resign=0（永不认输），后果是玩家领先几十目时 AI 只在原地无限虚手，
    对局永远收不了尾 —— 比「认输太早」糟糕得多。这里分别盯住五件事：
    开局不认、级位阈值比段位大、绝望到阈值后确实会认、
    「无路可走且大败」时首推 pass 就该认、局面接近时仍正常虚手进结算。
    """
    from dataclasses import replace

    from app.config import settings
    from app.engine.protocol import AnalysisResult, Candidate
    from app.game.manager import LiveGame
    from app.game.rules import BLACK, WHITE, Game

    def live(rank_id: int) -> LiveGame:
        g = Game(size=9, komi=5.5, player_color=BLACK)   # AI 执白
        return LiveGame(id="t", user_id="u", game=g, rank_id=rank_id,
                        profile=get_engine_profile(rank_id), ai_name="AI")

    def fill(l: LiveGame, plies: int) -> None:
        pts = [(x, y) for y in range(l.game.size) for x in range(l.game.size)]
        for i in range(plies):
            l.game.play(BLACK if i % 2 == 0 else WHITE, pts[i])

    def doomed(lead: float) -> AnalysisResult:
        # 黑（玩家）胜率 99%、领先 lead 目（score_lead 是黑方视角）
        return AnalysisResult(turn=0, side_to_move=BLACK, winrate=0.99,
                              score_lead=lead, engine="fake")

    # 1) 开局不认：手数没到最小值，落后再多也不算绝望
    kyu = live(1)
    assert kyu.resign_min_plies() == 18
    assert kyu.despair_margin(doomed(80.0)) is None
    assert not any(kyu.update_despair(doomed(80.0)) for _ in range(30))
    assert kyu.despair_plies == 0

    # 2) 级位阈值比段位大：同样落后 30 目，18级 不算绝望、九段算
    kyu = live(1)
    fill(kyu, kyu.resign_min_plies())
    dan = live(MAX_RANK_ID)
    fill(dan, dan.resign_min_plies())
    assert kyu.despair_margin(doomed(30.0)) is None, "18级 阈值应大于 30 目"
    assert dan.despair_margin(doomed(30.0)) is not None, "九段沿用全局 25 目阈值"

    # 3) 绝望到阈值后确实会认（且不满连续手数不提前认）
    big = doomed(50.0)
    hits = [kyu.update_despair(big) for _ in range(settings.resign_consecutive_moves)]
    assert hits[-1] is True, "18级 落后 50 目且过了最小手数后应认输"
    assert not any(hits[:-1])

    # 4) 无路可走且大败：首推 pass 的第一手就该投子，而不是无限虚手拖住玩家
    g = Game(size=9, komi=5.5, player_color=BLACK)
    pts = [(x, y) for y in range(9) for x in range(9)]
    g.board.place(BLACK, pts[:40])
    g.board.place(WHITE, pts[40:63])
    end = live(1)
    end.game = g
    assert len(g.board.empty_points()) <= 9 * 2
    passing = replace(doomed(20.0), candidates=[
        Candidate(point=None, gtp="pass", visits=1, score_lead=20.0, rank=0)])
    assert end.should_pass(passing)
    assert end.lost_with_nothing_to_play(passing), "落后 20 目（>9）且首推 pass 应投子"

    # 5) 局面接近时不该认：落后 5 目仍应正常虚手、交给双方虚手后的结算
    close = replace(passing, score_lead=5.0,
                    candidates=[replace(passing.candidates[0], score_lead=5.0)])
    assert end.should_pass(close)
    assert not end.lost_with_nothing_to_play(close)


# KataGo v1.17.1 实测合法的 humanSLProfile 全集（逐个发查询验过：preaz_20k～preaz_9d
# 共 29 个全部可用；rank_* / proyear_* 也合法但本项目不用）。
# 传了集外的值不会报错到界面上：analyze() 发现响应带 error 时会**静默去掉
# human_sl_profile 重试**，于是该等级悄悄改用 analysis.cfg 里的 preaz_9d 先验 ——
# 棋风与档位不符且无从察觉。换 KataGo 版本后用 scripts/check_katago.py 重验。
_LEGAL_HUMAN_PROFILES = {f"preaz_{k}k" for k in range(20, 0, -1)} | \
                        {f"preaz_{d}d" for d in range(1, 10)}


def test_human_sl_profiles_are_supported_by_katago():
    used = {p.human_sl_profile for r in RANKS if (p := r.engine) and p.human_sl_profile}
    assert used, "级位档应该都配了拟人档位"
    illegal = used - _LEGAL_HUMAN_PROFILES
    assert not illegal, f"KataGo 不认这些档位，对应等级会静默丢掉拟人棋风: {sorted(illegal)}"


def test_kyu_human_profiles_descend_with_rank():
    """级位的 human 档位要跟段位同向：18级配 18k、1级配 1k。"""
    def kyu_num(name: str) -> int:
        return int(name.replace("preaz_", "").rstrip("k"))

    nums = [kyu_num(get_engine_profile(i).human_sl_profile) for i in range(1, 19)]
    assert nums == list(range(18, 0, -1)), f"档位与级位应逐一对应，实际 {nums}"


def test_promotion_requirements_scale_with_rank():
    assert get_rank(1).wins_required == 3 and get_rank(1).promo_streak == 2
    assert get_rank(19).wins_required == 4 and get_rank(19).promo_streak == 3
    assert get_rank(23).max_avg_loss_points == 1.6, "5段→6段开始校验吻合度"
    assert get_rank(26).max_avg_loss_points == 0.8
    assert get_rank(27).max_avg_loss_points is None, "九段无晋升目标"


def test_get_rank_clamps_out_of_range():
    assert get_rank(-5).rank_id == 1
    assert get_rank(999).rank_id == MAX_RANK_ID


# ---------------------------------------------------------------------------
# 晋升流程
# ---------------------------------------------------------------------------
def test_kyu_promotion_after_three_wins_plus_two_streaks(session):
    u = make_user(session, rank_id=1)
    kinds = []
    for _ in range(5):
        res = on_game_finished(session, u, won=True, game_id="g")
        kinds.extend(e["kind"] for e in res["events"])
    assert u.rank_id == 2, "3 胜 + 晋升战 2 连胜 = 5 胜应升到 17级"
    assert "promo_start" in kinds and "promote" in kinds
    assert u.rank_wins == 0 and u.promotion_wins == 0 and not u.in_promotion
    assert u.best_rank_id == 2
    assert u.total_games == 5 and u.total_wins == 5


def test_promotion_progress_visible_in_payload(session):
    u = make_user(session, rank_id=1)
    on_game_finished(session, u, won=True, game_id="g")
    on_game_finished(session, u, won=True, game_id="g")
    p = progress_of(u)
    assert p.rank_wins == 2 and p.wins_required == 3
    assert "再赢 1 场" in p.hint

    on_game_finished(session, u, won=True, game_id="g")   # 第 3 胜 → 进入晋升战
    p = progress_of(u)
    assert p.in_promotion and p.promotion_wins == 0
    assert "晋升战" in p.hint

    on_game_finished(session, u, won=True, game_id="g")
    p = progress_of(u)
    assert p.promotion_wins == 1 and "再连胜 1 场" in p.hint


def test_promotion_reset_by_loss(session):
    u = make_user(session, rank_id=1)
    for _ in range(3):
        on_game_finished(session, u, won=True, game_id="g")
    assert u.in_promotion
    res = on_game_finished(session, u, won=False, game_id="g")
    assert not u.in_promotion
    assert u.promotion_wins == 0
    assert u.rank_wins == get_rank(1).wins_required - 1, "晋升战失败后需再赢 1 场"
    assert res["promoFailed"]
    assert any(e["kind"] == "promo_fail" for e in res["events"])
    # 再赢一场重新进入晋升战
    on_game_finished(session, u, won=True, game_id="g")
    assert u.in_promotion


def test_losing_streak_resets_on_win(session):
    u = make_user(session, rank_id=5, demotion=True)
    on_game_finished(session, u, won=False, game_id="g")
    on_game_finished(session, u, won=False, game_id="g")
    assert u.losing_streak == 2
    on_game_finished(session, u, won=True, game_id="g")
    assert u.losing_streak == 0


def test_demotion_disabled_by_default(session):
    u = make_user(session, rank_id=5, demotion=False)
    for _ in range(8):
        on_game_finished(session, u, won=False, game_id="g")
    assert u.rank_id == 5, "教学模式默认不降级"


def test_demotion_when_enabled(session):
    u = make_user(session, rank_id=5, demotion=True)
    for _ in range(4):
        res = on_game_finished(session, u, won=False, game_id="g")
    assert u.rank_id == 4
    assert any(e["kind"] == "demote" for e in res["events"])
    assert u.losing_streak == 0


def test_no_promotion_beyond_max_rank(session):
    u = make_user(session, rank_id=MAX_RANK_ID)
    for _ in range(12):
        on_game_finished(session, u, won=True, game_id="g")
    assert u.rank_id == MAX_RANK_ID
    assert progress_of(u).is_max_rank
    assert "九段" in progress_of(u).hint


def test_high_dan_accuracy_gate_blocks_promotion(session):
    """5段晋升战：吻合度平均值超过 1.6 目则不予升段。"""
    u = make_user(session, rank_id=23)
    for _ in range(5):                       # 累计 5 胜 → 进入晋升战
        on_game_finished(session, u, won=True, game_id="g")
    assert u.in_promotion
    for _ in range(4):                       # 晋升战前 4 胜，吻合度良好
        on_game_finished(session, u, won=True, game_id="g", accuracy=1.0)
    assert u.promotion_wins == 4
    res = on_game_finished(session, u, won=True, game_id="g", accuracy=5.0)
    assert res["promoFailed"], "最后一胜吻合度太差，晋升应被否决"
    assert u.rank_id == 23, "段位不变"
    assert not u.in_promotion
    assert u.promo_accuracy_n == 0, "吻合度累计应清零"


def test_high_dan_accuracy_gate_passes(session):
    u = make_user(session, rank_id=23)
    for _ in range(5):
        on_game_finished(session, u, won=True, game_id="g")
    for _ in range(5):
        on_game_finished(session, u, won=True, game_id="g", accuracy=1.2)
    assert u.rank_id == 24, "吻合度达标应升为六段"


def test_accuracy_moving_average(session):
    u = make_user(session, rank_id=10)
    on_game_finished(session, u, won=True, game_id="g", accuracy=2.0)
    on_game_finished(session, u, won=False, game_id="g", accuracy=4.0)
    assert u.accuracy_avg == pytest.approx(3.0, abs=0.01)
    on_game_finished(session, u, won=True, game_id="g", accuracy=3.0)
    assert u.accuracy_avg == pytest.approx(3.0, abs=0.01)
