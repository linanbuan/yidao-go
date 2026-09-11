"""全局配置：所有可通过环境变量覆盖的设置集中在这里。"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent          # backend/
PROJECT_ROOT = BASE_DIR.parent                              # d:/AI/围棋
KATAGO_DIR = BASE_DIR / "katago"                            # 引擎二进制与网络权重

# 数据目录可以整体外置。桌面客户端将来打包成 exe 时，`__file__` 会落在只读的
# 临时解包目录里，那时必须由 GO_DATA_DIR 把数据指到可写位置（%LOCALAPPDATA% 或程序旁 data/）。
# 不设这个环境变量时行为与以前完全一致（仍是 backend/data）；所有 import DATA_DIR 的地方
# （引擎 stderr、等级分标定、SQLite、标定脚本）跟着一起搬，不需要逐个改。
DATA_DIR = Path(os.environ.get("GO_DATA_DIR") or (BASE_DIR / "data"))   # SQLite、SGF、报告
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 引擎可执行文件名随平台而变（Docker/Linux 下为 katago）
_KATAGO_EXE = "katago.exe" if os.name == "nt" else "katago"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(BASE_DIR / ".env"), env_prefix="GO_", extra="ignore")

    # ---- 服务 ----
    # 默认只听回环：这个平台存的是本地 SQLite（含 JWT 密钥写在源码里），
    # `0.0.0.0` 会把 /api 与 /docs 暴露给同网段的任何人，而本机没有任何场景需要
    # 监听全网 —— 网页版前端由 vite proxy / 同源静态托管走 127.0.0.1，桌面端
    # 内嵌时 host 直接传 127.0.0.1（core/backend_host.py），都不读这个默认值。
    # 容器里要对外仍由显式参数决定：Dockerfile 的 `--host 0.0.0.0`、
    # docker-compose 的 `GO_HOST: 0.0.0.0`，都不依赖此处默认值。
    host: str = "127.0.0.1"
    port: int = 8000
    # 交互式接口文档（/docs、/redoc、/openapi.json）。绑回环时开着方便本机调试；
    # 容器里 `--host 0.0.0.0` 会把它们连同完整接口定义暴露给同网段任何人，
    # 所以 Dockerfile 默认关掉（GO_DOCS_ENABLED=false）。审计 1.22。
    docs_enabled: bool = True
    # JWT 密钥**不再有源码内置默认值**：空表示「未配置」，启动时会自动生成一枚
    # 并持久化到数据目录（见下方 `_resolve_jwt_secret`）。此前源码里写死的
    # `dev-secret-change-me-…` 是公开信息，任何人拿到仓库就能离线伪造任意用户令牌
    # （审计 1.3 已用 HMAC 复算证明线上确实在用这个默认值签发）。
    # 生产/多机部署请用 GO_JWT_SECRET 显式指定。
    jwt_secret: str = ""
    # 有效期从 30 天降到 3 天：令牌泄露（它会出现在 URL、浏览器历史、代理日志里）
    # 的可利用窗口越小越好；桌面端令牌存在本地 ini 里，过期只需重新登录一次。
    jwt_expire_hours: int = 24 * 3

    # ---- 数据库 ----
    database_url: str = f"sqlite:///{(DATA_DIR / 'go_teach.db').as_posix()}"

    # ---- KataGo 引擎 ----
    # 为空时自动降级到内置随机引擎（保证平台在没装 KataGo 时也能跑通全流程）
    katago_bin: str = str(KATAGO_DIR / _KATAGO_EXE)
    katago_model: str = str(KATAGO_DIR / "models" / "kata_b18c384nbt.bin.gz")
    katago_human_model: str = str(KATAGO_DIR / "models" / "human5k.bin.gz")
    katago_config: str = str(KATAGO_DIR / "analysis.cfg")
    katago_enabled: bool = True
    katago_timeout: float = 60.0          # 单次查询超时（秒）
    katago_max_processes: int = 1         # analysis 进程池大小

    # ---- 对局与 AI 认输 ----
    resign_consecutive_moves: int = 10    # 连续多少手处于绝望局面
    resign_score_threshold: float = 25.0  # 落后目数阈值
    resign_winrate_threshold: float = 0.03  # 胜率阈值
    # 每手限时秒数默认值（0=不限时）。没有限时时一盘拖住的对局永远收不了尾，
    # 所以默认给一个宽松的值，大厅里可以改或关掉。
    move_seconds_default: int = 60

    # ---- 复盘 ----
    # 复盘每手分析 visit 数。KataGo 报出的候选数随 visits 涨（实测 9 路：
    # 32 visits 只探 ~7 个点、200 visits ~17 个），太少时玩家的实际落点根本不在
    # 候选里，同节点损失目数就算不出来，只能退回跳节点估算。
    review_visits: int = 96
    mistake_slow: float = 2.0             # 缓手：损失目数
    mistake_bad: float = 5.0              # 恶手
    mistake_blunder: float = 10.0         # 大恶手

    # ---- 大模型（OpenAI 兼容接口）----
    llm_enabled: bool = True
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout: float = 120.0
    llm_max_batch: int = 8                # 每批喂给 LLM 的关键手数

    # ---- 等级体系 ----
    demotion_enabled: bool = False        # 教学模式默认关闭降级
    demotion_losing_streak: int = 4

    # ---- 数据保留（审计 §3 / §5 P2）----
    # 「每用户最近 N 局保留全量，更早且超过 retention_days 的对局只留摘要」：
    # 剥掉 analyses 与 review_json 两份重量级 JSON（占库体积约 79%），保留手顺 /
    # SGF / 结果 / 复盘状态。历史局几乎不会再翻看逐手曲线，留着只是让库无界增长
    # （实测 34 局 15.7MB，线性外推 1000 局 ≈ 465MB）。
    # **默认关闭**：这是数据销毁性质的操作，是否启用与阈值多少由部署方决定。
    retention_days: int = 0               # >0 才启用；0=永不自动瘦身
    retention_keep_recent: int = 20       # 每用户最近 N 局无论如何保留全量


settings = Settings()

#: 曾经的源码默认密钥。老部署可能把它写进了 .env，这里当「未配置」处理。
_LEGACY_DEFAULT_SECRET = "dev-secret-change-me-in-production-0123456789"


def _resolve_jwt_secret(s: Settings) -> None:
    """确定 JWT 密钥：显式配置 > 数据目录里的持久化密钥 > 现场生成并落盘。

    现场生成还落盘是有意的：只生成不保存的话，每次重启都会换一枚密钥、已登录的
    桌面端全部被踢回登录页。落在数据目录（已被 .gitignore）而不是源码里，
    既保证「不是公开信息」，又保证「重启不失效」。
    """
    if s.jwt_secret and s.jwt_secret != _LEGACY_DEFAULT_SECRET:
        return
    path = DATA_DIR / "jwt_secret.txt"
    secret = ""
    try:
        if path.exists():
            secret = path.read_text(encoding="utf-8").strip()
    except OSError:
        secret = ""
    if not secret:
        secret = secrets.token_urlsafe(48)
        try:
            path.write_text(secret, encoding="utf-8")
        except OSError as exc:      # 只读目录：退化为「本次运行有效」
            logging.getLogger("go.config").warning(
                "无法持久化 JWT 密钥到 %s（%s），本次运行每次重启都会让已登录会话失效", path, exc)
    s.jwt_secret = secret
    logging.getLogger("go.config").info(
        "JWT 密钥：未显式配置，已使用数据目录中的自动生成密钥（%s）", path)


_resolve_jwt_secret(settings)

