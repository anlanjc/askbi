"""OpenAI 兼容实现：DeepSeek / GLM / Qwen / OpenAI 共用一套代码。"""

from __future__ import annotations

import asyncio
import json
import random

from .base import ChatRequest, LLMClient, LLMError, LLMResponse, TokenUsage

_RETRY_ATTEMPTS = 3
_RETRY_BASE_SECONDS = 0.5
# ASKBI-2（评审 4.0）：SDK 默认 timeout=600s 对本链路过长（SSE 挂死无兜底），
# 显式收紧；SDK 内层重试归零（曾默认 2 次，叠加应用层 3 次 → 单次 chat 最多 9 次 HTTP），
# 重试完全自管（退避+抖动+尊重 Retry-After，见 _retry_delay_seconds）。
_REQUEST_TIMEOUT_SECONDS = 120.0
_CONNECT_TIMEOUT_SECONDS = 10.0
_MAX_RETRY_AFTER_SECONDS = 30.0


class OpenAICompatibleClient(LLMClient):
    def __init__(self, base_url: str, api_key: str, model: str, provider: str):
        import httpx
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(
            base_url=base_url, api_key=api_key,
            max_retries=0,   # ASKBI-2：SDK 重试归零，上限语义收敛到应用层 _RETRY_ATTEMPTS
            timeout=httpx.Timeout(_REQUEST_TIMEOUT_SECONDS, connect=_CONNECT_TIMEOUT_SECONDS),
        )
        self._model = model
        self._provider = provider
        self._closed = False

    async def chat(self, req: ChatRequest) -> LLMResponse:
        messages = [
            {"role": "system", "content": req.system},
            {"role": "user", "content": req.user},
        ]
        kwargs: dict = {
            "model": self._model,
            "messages": messages,
            "temperature": req.temperature,
        }
        if req.json_mode:
            # DeepSeek 开启 json_object 时要求 prompt 中出现 "json" 字样，这里天然满足
            kwargs["response_format"] = {"type": "json_object"}

        last_err: Exception | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                resp = await self._client.chat.completions.create(**kwargs)
                break
            except self._retryable_errors() as e:
                # 瞬时故障（连接/超时/限流/服务端错误）：自管退避重试；4xx 语义错误不重试
                last_err = e
                if attempt < _RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(_retry_delay_seconds(e, attempt))
            except Exception as e:
                raise LLMError(f"LLM 调用失败（不重试）: {e}") from e
        else:
            raise LLMError(f"LLM 调用重试 {_RETRY_ATTEMPTS} 次仍失败: {last_err}") from last_err

        choice = resp.choices[0]
        usage = TokenUsage(
            prompt_tokens=getattr(resp.usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(resp.usage, "completion_tokens", 0) or 0,
        )
        return LLMResponse(
            content=choice.message.content or "",
            usage=usage,
            provider=self._provider,
            model=self._model,
        )

    @staticmethod
    def _retryable_errors() -> tuple[type[Exception], ...]:
        import openai

        return (
            openai.APIConnectionError,
            openai.APITimeoutError,
            openai.RateLimitError,
            openai.InternalServerError,
        )

    async def aclose(self) -> None:
        """关停时释放连接池（ASKBI-2：连接曾从不显式关闭，创建/关闭不配对靠 GC 兜底）。"""
        if not self._closed:
            self._closed = True
            await self._client.aclose()


def _retry_delay_seconds(err: Exception, attempt: int) -> float:
    """自管重试退避（ASKBI-2）：指数退避 + 随机抖动（防惊群）；
    限流响应携带 Retry-After 时尊重之（钳上界防恶意大值挂死重试预算）。"""
    headers = getattr(getattr(err, "response", None), "headers", None) or {}
    raw = headers.get("retry-after") if hasattr(headers, "get") else None
    if raw is not None:
        try:
            return min(float(raw), _MAX_RETRY_AFTER_SECONDS)
        except (TypeError, ValueError):
            pass
    return _RETRY_BASE_SECONDS * (2**attempt) + random.uniform(0, _RETRY_BASE_SECONDS)


def _fallback_usage(req: ChatRequest, content: str) -> TokenUsage:
    """OpenAI 兼容服务偶尔不回 usage，用字符数粗估，保证记账不中断。"""
    return TokenUsage(
        prompt_tokens=len(req.system) + len(req.user) // 4 + 1,
        completion_tokens=len(content) // 4 + 1,
    )


def load_json_loose(text: str) -> dict:
    """容错解析：剥掉 ```json 围栏后解析。"""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1].rsplit("```", 1)[0]
    return json.loads(t)
