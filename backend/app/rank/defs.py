"""等级体系定义：18级 → 九段，共 27 级。

rank_id 约定：1 = 18级 ... 18 = 1级，19 = 业余1段 ... 27 = 九段。

每一级都定义了：
  * 显示名与 AI 对手人设名（增强陪伴感）
  * 晋升条件（累计胜场 + 晋升战连胜数；高段另需吻合度达标）
  * AI 棋力配置（KataGo maxVisits、采样温度、是否开启 ponder、human SL 档位）

弱档位（级位～1段）**不靠砍 visit 削弱** —— 实测那样反而最强：visits=2 时 KataGo
只报 1.1 个候选，AI 没机会选错，每手损失仅 0.82 目（职业量级）。真正的旋钮是：
  * local_noise —— 以该概率改下「紧贴已有棋子的空点」，模拟新手「只看局部、
    没有全局观」。级位档的主力：实测 p=0→0.8 时每手损失 1.73→3.94 目。
  * sample_tolerance —— 按「相对最优点亏多少目」做 softmax（weight = exp(-loss/T)），
    单位就是复盘报告展示给学员的「每手损失目数」，因此可标定、可解释。
  * blunder_rate —— 从候选尾部挑，模拟看漏了的手滑。
  * human_sl_profile —— 让 KataGo 的搜索先验按该档人类条件化，影响的是**棋风**。

为何不用 humanPrior / visits 做采样基准（两个都是实测踩过的坑）：
  * humanPrior 的 argmax 与引擎最优点**不是同一个点**，段位档没配 human 档位却
    仍按它采样，结果七段每手亏 1.68 目、五段只亏 0.39 目 —— 高段反而更弱，
    且 tolerance=0 走 argmax(score) 分支，在 0 附近形成断崖。
  * visits 在高搜索量下极度尖峰（如 900/200/60/40），温度幂运算几乎不改变分布，
    于是三/五段都只有 0.4 目（职业量级），段位档整体削不动。
max_visits 买的是「候选池宽度」与「给学员的分析精度」，弱档位同样需要 32 以上。

实测标定结果（`python scripts/calibrate_strength.py`：KataGo v1.17.1 +
b18c384nbt-humanv0，9 路 12 个真实局面 × 每档 20 次采样，参考分析 400 visits；
落在参考候选之外的噪声手用跨节点差值算真实损失，不做 clamp）：

    档位     18级  15级  10级   5级   1级   1段   3段   5段  六段  七段  八段  九段
    每手亏损  4.95  4.48  3.88  3.00  2.36  1.84  1.30  1.00  0.58  0.48  0.18  0.02
    命中最优   5%   10%   11%   18%   22%   25%   32%   41%   51%   63%   73%   75%

对比参考量级（职业 0.3~0.8 目、业余中段 1.5~3 目、级位新手 5 目以上）是对得上的。
修改任何旋钮后应当重跑标定并对照这两行，尤其要盯住**单调性**：曾经出现过
七段（1.68 目）比五段（0.39 目）还弱的倒挂，而它只有实测才能发现 ——
单元测试与冒烟都是绿的。该脚本会自查倒挂（容差 0.6 目）并非 0 退出。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, replace
from typing import Optional


@dataclass(frozen=True)
class EngineProfile:
    """某一等级对应的 AI 棋力配置。"""

    max_visits: int                 # KataGo 搜索 visit 上限（决定候选池宽度，不是削弱手段）
    # 每手可容忍的损失目数：按 exp(-loss/容忍度) 对候选做 softmax。
    # 0 = 只下最优点（九段）；1.5 ≈ 「这一档平均愿意亏 1 目上下」。
    # 用「目」做单位是因为它就是复盘报告展示给学员的那个指标，两边口径一致。
    sample_tolerance: float
    sample_top_n: int               # 采样时只在前 N 个候选点中选
    blunder_rate: float = 0.0       # 额外失误概率（0~1），模拟"手滑"
    # 以该概率放弃引擎候选，改下「紧贴已有棋子的空点」。级位档的主要削弱手段：
    # 实测 p=0→0.8 时每手损失 1.73→3.94 目，单调可控；而只在 KataGo 报出的候选里
    # 采样，每手损失封顶在 ~3 目（它报出来的全是它认可的好点）。
    local_noise: float = 0.0
    ponder: bool = False            # 是否在对手思考时后台搜索
    human_sl_profile: str = ""      # 可选：KataGo human SL 模型档位（如 preaz_9k）
    use_human_model: bool = False   # 是否优先使用 human SL 网络
    # AI 认输的落后目数阈值：None = 用全局 settings.resign_score_threshold，0 = 永不认输。
    # 级位档给的是**大而非零**的阈值：早先试过 0（永不认输），后果是玩家领先几十目
    # 时 AI 只会在原地无限虚手，对局永远收不了尾 —— 比「认输太早」糟糕得多。
    # 防「认输太早」靠另外两道闸：manager 里的最小手数（开局摆动不算）与
    # 「无路可走才认输」（引擎首推 pass 且落后超阈值时立即认输）。
    resign_score: Optional[float] = None


@dataclass(frozen=True)
class RankInfo:
    rank_id: int
    name: str               # 例如 "18级"、"业余3段"、"九段"
    short: str              # 徽章短名，例如 "18K"、"3D"、"9D"
    ai_name: str            # 该级 AI 对手人设名
    ai_title: str           # 一句话人设描述
    wins_required: int      # 触发晋升战所需累计胜场
    promo_streak: int       # 晋升战需连胜场数
    max_avg_loss_points: Optional[float] = None  # 本级晋升战期间「平均每手损失目数」上限，None=不校验
    elo: int = 0            # 标定等级分（用于匹配与曲线展示，标定脚本会覆写）
    engine: Optional[EngineProfile] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _e(visits: int, tol: float, top_n: int, blunder: float = 0.0, noise: float = 0.0,
       ponder: bool = False, human: bool = False, profile: str = "",
       resign: Optional[float] = None) -> EngineProfile:
    return EngineProfile(max_visits=visits, sample_tolerance=tol, sample_top_n=top_n,
                         blunder_rate=blunder, local_noise=noise, ponder=ponder,
                         use_human_model=human, human_sl_profile=profile,
                         resign_score=resign)


# ---------------------------------------------------------------------------
# 27 级主表
# ---------------------------------------------------------------------------
_KYU_NAMES = ["入门学徒", "初学稚子", "落子生涩", "识路少年", "小有所成", "棋童", "算路初开",
              "边角猎手", "布局新手", "定式学徒", "攻守初识", "棋形有感", "中盘行者",
              "官子入门", "轻重可辨", "厚薄分明", "级位精英", "冲段少年"]

_DAN_DATA = [
    # (name, short, ai_name, ai_title, wins, streak, acc, elo, engine)
    # acc = 本级晋升战期间允许的「平均每手损失目数」上限，防止纯磨盘升段；
    # 5段→6段 起开始校验吻合度（六段～九段为荣誉段位）。
    #
    # 段位档不配 human_sl_profile：实测 humanPrior 的集中度与棋力**不单调**
    # （空盘 9 路上 preaz_5d 的有效档位数 2.16、preaz_9d 反而 5.01），当不了削弱旋钮；
    # 不传时查询继承 analysis.cfg 里的 preaz_9d。段位档靠 tolerance + local_noise 削弱。
    #
    # tolerance 直接当「目标每手损失目数」来标定（参考量级：职业 0.3~0.8 目、
    # 业余中段 1.5~3 目），因为它就是 softmax 的分母，与测量口径同一个单位。
    ("业余1段", "1D", "初段新锐", "刚入段位，计算已具雏形", 4, 3, None, 2100,
     _e(160, 1.80, 8, 0.05, noise=0.15)),
    ("业余2段", "2D", "二段锐士", "攻杀敏锐，善用先手", 4, 3, None, 2250,
     _e(200, 1.60, 7, 0.04, noise=0.12)),
    ("业余3段", "3D", "三段棋匠", "棋形扎实，全局均衡", 4, 3, None, 2400,
     _e(280, 1.40, 7, 0.04, noise=0.10)),
    ("业余4段", "4D", "四段强豪", "算深远，官子精准", 5, 3, None, 2550,
     _e(400, 1.20, 6, 0.03, noise=0.08)),
    ("业余5段", "5D", "五段名手", "地方强豪水平，几无破绽", 5, 5, 1.6, 2700,
     _e(560, 1.00, 6, 0.03, noise=0.06)),
    ("六段", "6D", "六段国手", "全国大赛级实力", 5, 4, 1.3, 2850,
     _e(800, 0.80, 5, 0.02, noise=0.04, ponder=True)),
    ("七段", "7D", "七段宗师", "职业级大局观", 5, 4, 1.0, 3000,
     _e(1200, 0.60, 4, 0.02, noise=0.025, ponder=True)),
    ("八段", "8D", "八段棋圣", "顶尖职业水准", 5, 5, 0.8, 3150,
     _e(2000, 0.35, 3, 0.01, noise=0.01, ponder=True)),
    ("九段", "9D", "九段天元", "满血 KataGo，人类极限之上", 5, 5, None, 3300,
     _e(3200, 0.0, 1, 0.0, ponder=True)),
]

# 级位引擎：local_noise（0.75 → 0.19）与 tolerance（5.0 → 2.1 目）双旋钮递减。
#
# visits 反而是**递增**的（48 → 120），这跟直觉相反，是实测结论：
#   visits=2 → KataGo 只报 1.1 个候选 → AI 被迫下最优点 → 每手损失 0.82 目（职业量级）；
#   visits 拉到 600 + 宽采样 → 候选 20.5 个 → 每手损失 3.02 目。
# visits 买的是「候选池宽度」和「给学员看的分析精度」，两样都是弱档位更需要的。
#
# human 档位（preaz_18k～preaz_1k，29 个已逐个验证合法）决定棋风像不像那个级别，
# 但**不决定棋力**（搜索会盖过先验），所以它必须和 local_noise / tolerance 一起用。
#
# resign 给大阈值而非 0：级位档局面摆动大，但「永不认输」会让领先几十目的玩家
# 面对一个只会在原地虚手的对手，对局收不了尾（理由见 EngineProfile.resign_score）。
_KYU_ENGINE = [
    _e(48, 5.00, 16, 0.15, noise=0.75, human=True, profile="preaz_18k", resign=45.0),   # 18级
    _e(50, 4.80, 16, 0.15, noise=0.72, human=True, profile="preaz_17k", resign=45.0),
    _e(52, 4.60, 15, 0.14, noise=0.69, human=True, profile="preaz_16k", resign=45.0),
    _e(54, 4.40, 15, 0.14, noise=0.66, human=True, profile="preaz_15k", resign=45.0),
    _e(56, 4.20, 14, 0.13, noise=0.63, human=True, profile="preaz_14k", resign=45.0),
    _e(58, 4.00, 14, 0.13, noise=0.60, human=True, profile="preaz_13k", resign=45.0),
    _e(60, 3.80, 13, 0.12, noise=0.57, human=True, profile="preaz_12k", resign=40.0),
    _e(62, 3.60, 13, 0.12, noise=0.54, human=True, profile="preaz_11k", resign=40.0),
    _e(64, 3.40, 12, 0.11, noise=0.51, human=True, profile="preaz_10k", resign=40.0),
    _e(68, 3.20, 12, 0.11, noise=0.48, human=True, profile="preaz_9k", resign=40.0),      # 9级
    _e(72, 3.00, 11, 0.10, noise=0.45, human=True, profile="preaz_8k", resign=40.0),
    _e(76, 2.90, 11, 0.10, noise=0.42, human=True, profile="preaz_7k", resign=40.0),
    _e(80, 2.80, 10, 0.09, noise=0.39, human=True, profile="preaz_6k", resign=35.0),
    _e(86, 2.70, 10, 0.09, noise=0.35, human=True, profile="preaz_5k", resign=35.0),
    _e(92, 2.60, 9, 0.08, noise=0.31, human=True, profile="preaz_4k", resign=35.0),
    _e(100, 2.50, 9, 0.08, noise=0.27, human=True, profile="preaz_3k", resign=35.0),
    _e(110, 2.30, 8, 0.07, noise=0.23, human=True, profile="preaz_2k", resign=35.0),
    _e(120, 2.10, 8, 0.07, noise=0.19, human=True, profile="preaz_1k", resign=35.0),     # 1级
]


def _build_ranks() -> list[RankInfo]:
    ranks: list[RankInfo] = []
    # 级位：18级 → 1级，均为「3 胜 + 2 连胜」晋升战
    for i in range(18):
        kyu = 18 - i                      # 18,17,...,1
        ranks.append(RankInfo(
            rank_id=i + 1,
            name=f"{kyu}级",
            short=f"{kyu}K",
            ai_name=_KYU_NAMES[i],
            ai_title="陪你从入门走到冲段" if i < 6 else "级位好手，稳步提升中",
            wins_required=3,
            promo_streak=2,
            max_avg_loss_points=None,
            elo=600 + i * 80,             # 18级≈600，1级≈1960（标定脚本会修正）
            engine=_KYU_ENGINE[i],
        ))
    # 段位：1段 → 九段
    for j, (name, short, ai_name, ai_title, wins, streak, acc, elo, eng) in enumerate(_DAN_DATA):
        ranks.append(RankInfo(
            rank_id=19 + j, name=name, short=short, ai_name=ai_name, ai_title=ai_title,
            wins_required=wins, promo_streak=streak, max_avg_loss_points=acc, elo=elo, engine=eng,
        ))
    return ranks


RANKS: list[RankInfo] = _build_ranks()
RANK_BY_ID: dict[int, RankInfo] = {r.rank_id: r for r in RANKS}

MIN_RANK_ID = 1          # 18级
MAX_RANK_ID = len(RANKS)  # 27 = 九段
DEFAULT_RANK_ID = 1       # 新用户从 18级 起步


def get_rank(rank_id: int) -> RankInfo:
    return RANK_BY_ID[max(MIN_RANK_ID, min(MAX_RANK_ID, int(rank_id)))]


def get_engine_profile(rank_id: int) -> EngineProfile:
    r = get_rank(rank_id)
    assert r.engine is not None
    return r.engine


def rank_name(rank_id: int) -> str:
    return get_rank(rank_id).name


def ranks_payload() -> list[dict]:
    """给前端段位徽章墙用的完整表。"""
    return [r.to_dict() for r in RANKS]


def _load_calibrated_elo() -> None:
    """若存在标定文件（scripts/calibrate.py --apply 生成），用实测 Elo 覆盖默认值。"""
    global RANKS, RANK_BY_ID
    try:
        from ..config import DATA_DIR
        path = DATA_DIR / "elo_calibration.json"
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        elo_map = {int(k): float(v) for k, v in (data.get("elo") or {}).items()}
        if not elo_map:
            return
        RANKS = [replace(r, elo=int(round(elo_map[r.rank_id]))) if r.rank_id in elo_map else r
                 for r in RANKS]
        RANK_BY_ID = {r.rank_id: r for r in RANKS}
    except (OSError, ValueError, KeyError):
        # 标定文件损坏时不影响启动，继续使用默认 Elo
        return


_load_calibrated_elo()
