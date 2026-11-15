"""LLM 客户端回归（ASKBI-2，评审 4.0）：SDK 重试归零 / 超时显式 / 客户端缓存复用 / 自管退避。

修复前（confirmed）：AsyncOpenAI 裸构造（默认 max_retries=2 + timeout=600s）且每次 get_llm()
新建带独立 httpx 连接池的实例——应用层 3 次重试叠 SDK 内层 → 单次 chat 最多 9 次 HTTP；
连接创建/关闭不配对靠 GC 兜底。全链路无网络（构造器与桩替身），不触真模型。
"""

from __future__ import annotations

import httpx
import openai
import pytest

from app.config import get_settings
from app.llm.base import ChatRequest, LLMError
from app.llm.deepseek import (
    _CONNECT_TIMEOUT_SECONDS,
    _MAX_RETRY_AFTER_SECONDS,
    _REQUEST_TIMEOUT_SECONDS,
    OpenAICompatibleClient,
    _retry_delay_seconds,
)
from app.llm.factory import _CLIENTS, close_all_llm_clients, get_llm


def _client() -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        base_url="http://localhost:9", api_key="k", model="m", provider="test",
    )


# ---------------- 1. 构造契约：SDK 内层重试归零 + 显式超时 ----------------


def test_constructor_pins_zero_sdk_retries_and_explicit_timeout():
    c = _client()
    assert c._client.max_retries == 0, "SDK 内层重试必须归零（曾默认 2 次，叠加应用层=9 次 HTTP）"
    assert c._client.timeout.connect == _CONNECT_TIMEOUT_SECONDS
    assert c._client.timeout.read == _REQUEST_TIMEOUT_SECONDS


# ---------------- 2. 工厂缓存：同配置复用同一客户端（连接池复用） ----------------


def test_factory_caches_client_per_config(monkeypatch):
    _CLIENTS.clear()   # 隔离：其他用例留下的注册表不参与断言
    s = get_settings()
    monkeypatch.setattr(s, "llm_provider", "openai_compatible")
    monkeypatch.setattr(s, "openai_base_url", "http://localhost:9")
    monkeypatch.setattr(s, "openai_api_key", "k")
    monkeypatch.setattr(s, "openai_model", "m")
    a, b = get_llm(), get_llm()
    assert a is b, "同配置的 get_llm() 必须复用同一客户端（曾每次新建连接池）"
    monkeypatch.setattr(s, "openai_api_key", "k2")
    c, c2 = get_llm(), get_llm()
    assert c is c2 and c is not a, "配置键变化 → 新实例；同键仍复用"
    assert len(_CLIENTS) == 2   # 两套配置键各自独立缓存


async def test_close_all_llm_clients_releases_and_clears_registry(monkeypatch):
    _CLIENTS.clear()   # 隔离：清掉其他用例注册的客户端，断言从零开始
    s = get_settings()
    monkeypatch.setattr(s, "llm_provider", "openai_compatible")
    monkeypatch.setattr(s, "openai_base_url", "http://localhost:9")
    monkeypatch.setattr(s, "openai_api_key", "kA")
    monkeypatch.setattr(s, "openai_model", "m")
    a = get_llm()
    monkeypatch.setattr(s, "openai_api_key", "kB")
    b = get_llm()
    assert len(_CLIENTS) == 2
    closed: list[str] = []

    async def fake_aclose():
        closed.append("x")

    a._client.aclose = fake_aclose
    b._client.aclose = fake_aclose
    await close_all_llm_clients()
    assert len(closed) == 2 and len(_CLIENTS) == 0, "关停必须释放全部连接池并清注册表"


# ---------------- 3. 自管退避：指数 + 抖动 + 尊重 Retry-After ----------------


class _FakeResp:
    def __init__(self, headers: dict):
        self.headers = headers


class _FakeErr(Exception):
    def __init__(self, headers: dict | None = None):
        self.response = _FakeResp(headers) if headers is not None else None


def test_retry_delay_respects_retry_after_with_cap():
    assert _retry_delay_seconds(_FakeErr({"retry-after": "7"}), 0) == 7.0
    assert _retry_delay_seconds(_FakeErr({"retry-after": "999"}), 0) == _MAX_RETRY_AFTER_SECONDS


def test_retry_delay_backoff_has_jitter_bounds():
    err = _FakeErr()   # 无 response → 走指数退避+抖动
    for attempt in (0, 1, 2):
        base = 0.5 * (2**attempt)
        for _ in range(20):
            d = _retry_delay_seconds(err, attempt)
            assert base <= d <= base + 0.5
    assert _retry_delay_seconds(_FakeErr({"retry-after": "not-a-number"}), 1) >= 1.0


# ---------------- 4. 重试上限：单次 chat 最多 3 次 HTTP（曾 9） ----------------


def _rate_limit_error() -> openai.RateLimitError:
    return openai.RateLimitError(
        "rate limited",
        response=httpx.Response(429, request=httpx.Request("POST", "http://localhost:9")),
        body=None,
    )


async def test_chat_retry_loop_bounded_at_three_http_calls(monkeypatch):
    c = _client()
    calls = {"n": 0}
    sleeps: list[float] = []

    async def fake_create(**_kw):
        calls["n"] += 1
        raise _rate_limit_error()

    async def fake_sleep(sec: float) -> None:
        sleeps.append(sec)

    c._client.chat.completions.create = fake_create
    monkeypatch.setattr("app.llm.deepseek.asyncio.sleep", fake_sleep)
    with pytest.raises(LLMError, match="重试 3 次仍失败"):
        await c.chat(ChatRequest(node="sql_agent", system="s", user="u"))
    assert calls["n"] == 3, "应用层 3 次尝试 + SDK 归零 = 最多 3 次 HTTP"
    assert len(sleeps) == 2


async def test_chat_non_retryable_error_fails_fast(monkeypatch):
    c = _client()
    calls = {"n": 0}

    async def fake_create(**_kw):
        calls["n"] += 1
        raise openai.BadRequestError(
            "bad request",
            response=httpx.Response(400, request=httpx.Request("POST", "http://localhost:9")),
            body=None,
        )

    c._client.chat.completions.create = fake_create
    with pytest.raises(LLMError, match="不重试"):
        await c.chat(ChatRequest(node="sql_agent", system="s", user="u"))
    assert calls["n"] == 1


# ---------------- 5. aclose：连接池显式释放（曾从不关闭靠 GC） ----------------


async def test_client_aclose_is_idempotent():
    c = _client()
    closed = {"n": 0}

    async def fake_aclose():
        closed["n"] += 1

    c._client.aclose = fake_aclose
    await c.aclose()
    await c.aclose()
    assert closed["n"] == 1
