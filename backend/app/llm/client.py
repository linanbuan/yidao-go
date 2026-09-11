"""大模型网关：OpenAI 兼容接口（DeepSeek / Qwen / Kimi / 本地 Ollama 均可）。

只做一件事——把 KataGo 产出的结构化数据"翻译成人话"。所有棋力判断都来自引擎，
LLM 不参与胜负与最佳点计算，因此即使模型能力一般，讲解的事实性也有保障。
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from ..config import settings
from ..crypto import unseal

logger = logging.getLogger("go.llm")


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120.0):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.timeout = timeout

    @classmethod
    def resolve(cls, user_config: Optional[dict] = None) -> "LLMClient":
        """全局配置 + 用户级覆盖（用户在设置页填自己的 Key）。

        用户级 apiKey 在库里是密文（见 app/crypto.py），所以在这里统一解封 ——
        这是唯一一处「读用户 Key」的地方（settings 页体检、复盘讲解都经由此），
        解密只做一次，调用方拿到的仍是明文。
        """
        cfg = user_config or {}
        return cls(
            base_url=cfg.get("baseUrl") or settings.llm_base_url,
            api_key=unseal(cfg.get("apiKey") or "") or settings.llm_api_key,
            model=cfg.get("model") or settings.llm_model,
            timeout=float(cfg.get("timeout") or settings.llm_timeout),
        )

    @property
    def configured(self) -> bool:
        return bool(settings.llm_enabled and self.api_key and self.base_url and self.model)

    async def chat(self, messages: list[dict], temperature: float = 0.4,
                   max_tokens: int = 2000) -> str:
        if not self.configured:
            raise LLMError("未配置大模型（缺少 API Key），已降级为纯数据分析")
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        last_error: Optional[Exception] = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code >= 400:
                        raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
                    data = resp.json()
                    choices = data.get("choices") or []
                    if not choices:
                        raise LLMError(f"LLM 返回为空: {str(data)[:300]}")
                    content = choices[0].get("message", {}).get("content") or ""
                    return content.strip()
            except LLMError as exc:
                last_error = exc
                logger.warning("LLM 调用失败（第 %d 次）：%s", attempt + 1, exc)
                if attempt == 0:
                    continue
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning("LLM 网络错误（第 %d 次）：%s", attempt + 1, exc)
        raise LLMError(str(last_error or "LLM 调用失败"))

    def info(self) -> dict:
        return {
            "configured": self.configured,
            "baseUrl": self.base_url,
            "model": self.model,
            "hasApiKey": bool(self.api_key),
        }
