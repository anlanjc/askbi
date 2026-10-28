"""OpenAI 兼容实现：DeepSeek / GLM / Qwen / OpenAI 共用一套代码。"""

from __future__ import annotations

import asyncio
import json

from .base import ChatRequest, LLMClient, LLMError, LLMResponse, TokenUsage

_RETRY_ATTEMPTS = 3
_RETRY_BASE_SECONDS = 0.5


class OpenAICompatibleClient(LLMClient):
    def __init__(self, base_url: str, api_key: str, model: str, provider: str):
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self._model = model
        self._provider = provider

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
                # 瞬时故障（连接/超时/限流/服务端错误）：指数退避重试；4xx 语义错误不重试
                last_err = e
                if attempt < _RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(_RETRY_BASE_SECONDS * (2 ** attempt))
            except Exception as e:  # noqa: BLE001  鉴权失败/参数错误等，重试无意义
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
