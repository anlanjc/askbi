"""故障注入下的降级语义：LLM 不可用时系统给出诚实答案而不是 500。

注入方式：monkeypatch nodes.get_llm 返回受控的假客户端（工厂每次新建实例，
因此用单例保证故障状态跨调用持续）。mock provider 的行为基准不受影响。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents import nodes
from app.agents.graph import run_ask
from app.llm.base import ChatRequest, LLMError
from app.llm.mock import MockLLM


class _AlwaysFailLLM:
    def __init__(self):
        self.calls = 0

    async def chat(self, req):
        self.calls += 1
        raise LLMError("注入：LLM 服务完全不可用")


class _NodeFailLLM:
    """只对指定 node 抛 LLMError，其余转发给确定性 mock——模拟单节点服务挂。"""

    def __init__(self, fail_node: str):
        self.fail_node = fail_node
        self.mock = MockLLM()
        self.failed = 0

    async def chat(self, req):
        if req.node == self.fail_node:
            self.failed += 1
            raise LLMError(f"注入：{req.node} 服务不可用")
        return await self.mock.chat(req)


class _FlakyLLM:
    """前 fail_times 次抛错，之后恢复正常——模拟瞬时故障被客户端重试救回。"""

    def __init__(self, fail_times: int):
        self.left = fail_times
        self.mock = MockLLM()

    async def chat(self, req):
        if self.left > 0:
            self.left -= 1
            raise LLMError("注入：瞬时故障")
        return await self.mock.chat(req)


def _install(monkeypatch, llm):
    monkeypatch.setattr(nodes, "get_llm", lambda: llm)


async def test_all_llm_down_yields_refusal_not_crash(monkeypatch):
    llm = _AlwaysFailLLM()
    _install(monkeypatch, llm)
    state = await run_ask("上月销售额是多少")
    # route(纯代码) → clarifier 降级放行 → sql_agent 降级拒答
    assert (state.get("answer") or {}).get("kind") == "refusal"
    assert llm.calls >= 2  # clarifier 与 sql_agent 都尝试过


async def test_transient_failure_recovers(monkeypatch):
    # 单次瞬时故障（打在 clarifier 上）被节点降级吸收，主链路继续出答案；
    # sql_agent 的瞬时恢复由客户端重试负责（见下方 client 重试用例）
    _install(monkeypatch, _FlakyLLM(fail_times=1))
    state = await run_ask("上月销售额是多少")
    answer = state.get("answer") or {}
    assert answer.get("kind") == "answer"
    assert answer.get("table", {}).get("rows")


class _RetryableError(RuntimeError):
    pass


def _fake_ok_response(content: str = "{}"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


class _FlakyCompletions:
    def __init__(self, fail_times: int):
        self.left = fail_times
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.left > 0:
            self.left -= 1
            raise _RetryableError("注入：瞬时 5xx")
        return _fake_ok_response()


def _bare_client(completions):
    """跳过 __init__（不建真连接），直接注入假 completions。"""
    from app.llm.deepseek import OpenAICompatibleClient

    c = OpenAICompatibleClient.__new__(OpenAICompatibleClient)
    c._model = "test"
    c._provider = "test"
    c._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return c


async def test_client_retry_recovers_from_transient(monkeypatch):
    from app.llm import deepseek

    monkeypatch.setattr(
        deepseek.OpenAICompatibleClient, "_retryable_errors",
        staticmethod(lambda: (_RetryableError,)),
    )
    monkeypatch.setattr(deepseek.asyncio, "sleep", _async_noop)
    comp = _FlakyCompletions(fail_times=2)
    client = _bare_client(comp)
    resp = await client.chat(ChatRequest(node="qa", system="s", user="u"))
    assert comp.calls == 3
    assert resp.content == "{}"


async def test_client_gives_up_after_retries(monkeypatch):
    from app.llm import deepseek

    monkeypatch.setattr(
        deepseek.OpenAICompatibleClient, "_retryable_errors",
        staticmethod(lambda: (_RetryableError,)),
    )
    monkeypatch.setattr(deepseek.asyncio, "sleep", _async_noop)
    comp = _FlakyCompletions(fail_times=99)
    client = _bare_client(comp)
    with pytest.raises(LLMError):
        await client.chat(ChatRequest(node="qa", system="s", user="u"))
    assert comp.calls == 3  # 重试 3 次后放弃


async def test_client_no_retry_on_non_retryable(monkeypatch):
    from app.llm import deepseek

    comp = _FlakyCompletions(fail_times=1)
    # 不改 _retryable_errors：RuntimeError 不在默认可重试清单 → 立刻失败
    client = _bare_client(comp)
    with pytest.raises(LLMError):
        await client.chat(ChatRequest(node="qa", system="s", user="u"))
    assert comp.calls == 1


async def _async_noop(*args, **kwargs):
    return None


async def test_clarifier_down_main_chain_still_works(monkeypatch):
    llm = _NodeFailLLM("clarifier")
    _install(monkeypatch, llm)
    state = await run_ask("上月销售额是多少")
    assert (state.get("answer") or {}).get("kind") == "answer"
    assert llm.failed == 1


async def test_insight_down_keeps_table_drops_narrative(monkeypatch):
    llm = _NodeFailLLM("insight")
    _install(monkeypatch, llm)
    state = await run_ask("各品类销售额")  # heavy（"各"）→ 走 insight
    answer = state.get("answer") or {}
    assert answer.get("kind") == "answer"
    assert answer.get("narrative") == ""  # 诚实降级：不编叙述
    assert answer.get("table", {}).get("rows")
    assert any(e["type"] == "llm_unavailable" for e in state.get("events", []))


async def test_qa_down_falls_back_to_deterministic(monkeypatch):
    llm = _NodeFailLLM("qa")
    _install(monkeypatch, llm)
    state = await run_ask("为什么上月美妆品类毛利率下滑了")
    answer = state.get("answer") or {}
    qa = answer.get("qa") or {}
    assert answer.get("kind") == "answer"
    assert qa.get("passed") is True
    assert qa.get("llm_issues") == []
