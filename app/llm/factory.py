"""LLM 工厂：按配置装配 provider，节点代码永远只拿 LLMClient。"""

from __future__ import annotations

from ..config import get_settings
from .base import LLMClient


def get_llm() -> LLMClient:
    s = get_settings()
    if s.llm_provider == "mock":
        from .mock import MockLLM

        return MockLLM()
    if s.llm_provider == "deepseek":
        from .deepseek import OpenAICompatibleClient

        return OpenAICompatibleClient(
            base_url=s.deepseek_base_url, api_key=s.deepseek_api_key,
            model=s.deepseek_model, provider="deepseek",
        )
    if s.llm_provider == "openai_compatible":
        from .deepseek import OpenAICompatibleClient

        return OpenAICompatibleClient(
            base_url=s.openai_base_url, api_key=s.openai_api_key,
            model=s.openai_model, provider="openai_compatible",
        )
    raise ValueError(f"未知 LLM_PROVIDER: {s.llm_provider}")
