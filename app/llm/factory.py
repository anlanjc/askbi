"""LLM 工厂：按配置装配 provider，节点代码永远只拿 LLMClient。

ASKBI-2（评审 4.0）：OpenAI 兼容客户端曾每次 get_llm() 新建（带独立 httpx 连接池），
连接从不复用、创建/关闭不配对靠 GC 兜底——现按（provider, base_url, api_key, model）
配置键缓存进程级单例，连接池跨调用复用；关停时 close_all_llm_clients 统一 aclose
（main.py lifespan 接线）。配置变化 → 新键新实例，旧实例仍可被 close_all 释放。
"""

from __future__ import annotations

from ..config import get_settings
from .base import LLMClient

# OpenAI 兼容客户端注册表：配置键 → 客户端（mock 无状态不入表，维持逐次新建零漂移）
_CLIENTS: dict[tuple[str, str, str, str], LLMClient] = {}


def get_llm() -> LLMClient:
    s = get_settings()
    if s.llm_provider == "mock":
        from .mock import MockLLM

        return MockLLM()
    key: tuple[str, str, str, str] | None = None
    if s.llm_provider == "deepseek":
        key = ("deepseek", s.deepseek_base_url, s.deepseek_api_key, s.deepseek_model)
    elif s.llm_provider == "openai_compatible":
        key = ("openai_compatible", s.openai_base_url, s.openai_api_key, s.openai_model)
    else:
        raise ValueError(f"未知 LLM_PROVIDER: {s.llm_provider}")
    client = _CLIENTS.get(key)
    if client is None:
        from .deepseek import OpenAICompatibleClient

        client = OpenAICompatibleClient(
            base_url=key[1], api_key=key[2], model=key[3], provider=key[0],
        )
        _CLIENTS[key] = client
    return client


async def close_all_llm_clients() -> None:
    """进程关停：释放全部缓存的 OpenAI 兼容客户端连接池并清注册表（幂等）。"""
    while _CLIENTS:
        _, client = _CLIENTS.popitem()
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()
