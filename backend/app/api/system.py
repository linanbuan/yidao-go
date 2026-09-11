"""系统信息 API：引擎状态、段位表、全局配置概览。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from ..config import settings
from ..engine.pool import get_pool
from ..llm.client import LLMClient
from ..models import User
from ..rank.defs import ranks_payload
from .deps import get_current_user, get_optional_user

router = APIRouter(prefix="/api", tags=["system"])
logger = logging.getLogger("go.api.system")


@router.get("/health")
def health():
    """就绪探针：保持**快速应答**（不阻塞等引擎预热，见 pool.startup 的说明）。

    但要带上引擎态，容器编排/Docker 的健康检查才能区分「服务活着但引擎没起来」。
    """
    return {"ok": True, "service": "yidao-backend",
            "engine": get_pool().active_engine}


@router.get("/system/status")
def system_status(user: User = Depends(get_current_user)):
    pool = get_pool()
    llm = LLMClient.resolve(dict(user.llm_config or {}))
    return {
        "engine": pool.status(),
        "llm": llm.info(),
        "resign": {
            "consecutiveMoves": settings.resign_consecutive_moves,
            "scoreThreshold": settings.resign_score_threshold,
            "winrateThreshold": settings.resign_winrate_threshold,
        },
        "review": {
            "visits": settings.review_visits,
            "thresholds": {
                "slow": settings.mistake_slow,
                "bad": settings.mistake_bad,
                "blunder": settings.mistake_blunder,
            },
        },
        "demotion": {
            "globalEnabled": settings.demotion_enabled,
            "userEnabled": user.demotion_enabled,
            "losingStreak": settings.demotion_losing_streak,
        },
    }


@router.post("/system/llm-test")
async def llm_test(user: User = Depends(get_current_user)):
    """发一条极短的测试请求，验证用户的大模型配置可用。"""
    llm = LLMClient.resolve(dict(user.llm_config or {}))
    if not llm.configured:
        return {"ok": False, "message": "配置不完整：需要 baseUrl、模型名与 API Key"}
    try:
        reply = await llm.chat([
            {"role": "system", "content": "你是围棋教练，回复简短。"},
            {"role": "user", "content": "请用一句中文围棋术语问候，不超过 20 字。"},
        ], temperature=0.3, max_tokens=60)
        return {"ok": True, "model": llm.model,
                "message": f"连接成功（{llm.model}），模型回复：{reply[:60]}"}
    except Exception as exc:   # noqa: BLE001
        # 不回传上游原文（可能是几百字的响应体/内部地址）；详情进服务端日志
        logger.warning("大模型连接测试失败: %s", exc)
        return {"ok": False, "model": llm.model, "message": "连接失败，请检查 Base URL / 模型名 / API Key"}


@router.get("/ranks")
def ranks(user=Depends(get_optional_user)):
    """27 级段位表（含晋升条件与 AI 棋力配置），供徽章墙与说明页使用。"""
    items = ranks_payload()
    for it in items:
        eng = it.get("engine") or {}
        it["engine"] = {
            "maxVisits": eng.get("max_visits"),
            # tolerance 单位是「目」：这一档平均每手愿意亏多少。直接对应
            # 复盘报告里的「每手损失目数」，比旧的温度值对学员可解释得多
            "tolerance": eng.get("sample_tolerance"),
            "topN": eng.get("sample_top_n"),
            "blunderRate": eng.get("blunder_rate"),
            "localNoise": eng.get("local_noise"),
            "ponder": eng.get("ponder"),
            "humanModel": bool(eng.get("use_human_model")),
        }
    current = user.rank_id if isinstance(user, User) else None
    return {"items": items, "currentRankId": current}
