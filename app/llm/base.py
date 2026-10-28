"""LLM 抽象层：所有节点只依赖这里的接口，不感知具体厂商。

调用约定：
- node 字段标识调用方（clarifier/sql_agent/insight/qa），便于记账与 mock 分发；
- user 字段统一放 JSON 序列化的上下文 payload，真实模型可读，mock 可解析；
- json_mode=True 时要求返回合法 JSON 文本，由调用方用 pydantic 校验并进入修复循环。
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field


class LLMError(RuntimeError):
    """LLM 调用在重试耗尽后仍失败（网络/限流/超时/坏响应）。

    节点层捕获它做各自语义的降级（澄清放行/生成拒答/叙述留空/质检走确定性结论），
    保证 LLM 服务不可用时系统仍给出诚实答案而不是 500。
    """


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ChatRequest(BaseModel):
    node: str
    system: str
    user: str
    json_mode: bool = False
    temperature: float = 0.1


class LLMResponse(BaseModel):
    content: str
    usage: TokenUsage = Field(default_factory=TokenUsage)
    provider: str = "mock"
    model: str = "mock"


class LLMClient(Protocol):
    async def chat(self, req: ChatRequest) -> LLMResponse: ...
