"""评审 5.0 四缺陷回归（[1B6] 车道 A）：AB-19 / AB-1 / AB-18 / AB-6。

- AB-19 (P1) 缓存版本闸探测异常零兜底 → fail-open（误失效方向）+ 留痕；
- AB-1 (P1) singleflight 取消泄漏 + 等待者线程池占用反转 → 登记/等待两段拆分 +
  等待者名额上限 + leader 终态保障（try/finally）；
- AB-18 (P1) SSE 生成器未启动窗口：resume 标记永久泄漏 + 任务永驻 running →
  终态联动释放 + 超龄 TTL 收回（两道防线独立成立）；
- AB-6 (P2) 非 resume 路径无 per-thread 互斥 → acquire 泛化（仅 suspend_on 前提不变）。
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest

from app.cache import (
    QueryCache,
    fingerprint,
    get_query_cache,
    get_spec,
    reset_query_cache,
)
from app.config import get_settings

_SF_RESULT = {"columns": ["value"], "rows": [[42]], "row_count": 1, "truncated": False, "elapsed_ms": 1.0}
_DSL = {"metric": "revenue", "dimensions": ["region"], "filters": [], "time": {"mode": "all"}}
_STATE = {"dsl": dict(_DSL), "sql": "SELECT 1", "role": "boss", "question": "q", "events": [], "usage": {}}


def _make_cache(**kw) -> QueryCache:
    kw.setdefault("version_provider", lambda: "v1")
    kw.setdefault("enabled", True)
    kw.setdefault("version_ttl_seconds", 0.0)  # 单测语义=立即失效；记忆化行为单独测
    return QueryCache(**kw)


def _poll(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


# ---------------- AB-19 (P1)：缓存版本闸探测异常零兜底 ----------------


def test_ab19_version_probe_exception_fail_open():
    """AB-19：版本探测异常（缺库/文件锁 IOException）→ get 命中一律作废（误失效方向，
    条目不撕毁：探测恢复后版本闸照常）、set 跳过写缓存、stats 记 version_probe_failures，
    绝不向上抛 duckdb 原文穿透 SSE error。"""
    state = {"ok": True}

    def provider() -> str:
        if not state["ok"]:
            raise OSError("database is locked")
        return "v1"

    c = _make_cache(version_provider=provider)
    c.set("k", dict(_SF_RESULT))
    assert c.get("k") is not None                    # 健全时正常命中
    state["ok"] = False
    assert c.get("k") is None                        # 探测异常 → fail-open，不抛
    assert c.get("k") is None
    assert c.stats()["version_probe_failures"] == 2  # 每次失败留痕（失败不进记忆化窗口）
    state["ok"] = True
    assert c.get("k") is not None                    # 探测恢复 → 条目仍在，照常命中
    stores_before = c.stats()["stores"]
    state["ok"] = False
    c.set("k2", dict(_SF_RESULT))                    # 写路径同样跳过缓存，不抛
    assert c.get("k2") is None
    assert c.stats()["stores"] == stores_before      # stores 不增（跳过写入）


def test_ab19_probe_failure_publish_flight_still_wakes_waiters():
    """AB-19：探测异常时 publish_flight 不打断应答链——flight 结果分发照常（等待者
    共享 leader 结果），仅写缓存被跳过（stores 不增）。"""
    state = {"ok": False}

    def provider() -> str:
        if not state["ok"]:
            raise OSError("database is locked")
        return "v1"

    c = _make_cache(version_provider=provider, flight_wait_seconds=2.0)
    outs: dict = {}

    def leader() -> None:
        outs["l"] = c.claim_flight("k")

    def waiter() -> None:
        outs["w"] = c.claim_flight("k")

    t1 = threading.Thread(target=leader)
    t1.start()
    t1.join(timeout=2)
    assert outs["l"] == ("leader", None)
    t2 = threading.Thread(target=waiter)
    t2.start()
    assert _poll(lambda: c._flights.get("k") is not None and c._flights["k"].waiters == 1)
    published = True
    try:
        c.publish_flight("k", dict(_SF_RESULT))      # 探测异常——publish 不得抛
    except Exception:  # noqa: BLE001
        published = False
    t2.join(timeout=2)
    assert published, "publish_flight 在探测异常时必须静默跳过写缓存（不抛）"
    assert outs["w"] == ("flight", dict(_SF_RESULT))  # 等待者共享 leader 结果
    st = c.stats()
    assert st["stores"] == 0 and st["version_probe_failures"] >= 1


# ---------------- AB-1 (P1)：singleflight 取消泄漏 + 等待者线程池占用反转 ----------------


def test_ab1_waiter_cap_overflows_fail_open():
    """AB-1③：同 key 等待者名额上限——超出上限者直接 fail-open（不占等待名额，
    不进线程池），singleflight_overflow 单独留痕；flight 拆除后名额随之消亡。"""
    c = _make_cache(max_flight_waiters=2)
    assert c.register_flight("k") == ("leader", None)
    assert c.register_flight("k")[0] == "wait"       # 等待者名额已占（持有 flight 引用）
    assert c.register_flight("k")[0] == "wait"
    assert c.register_flight("k") == ("miss", None)  # 第 4 路：超上限 → 直接自行执行
    assert c.register_flight("k") == ("miss", None)
    st = c.stats()
    assert st["singleflight_overflow"] == 2 and st["singleflight_timeouts"] == 0
    c.fail_flight("k")
    assert c.register_flight("k") == ("leader", None)  # 拆除后恢复竞选
    c.fail_flight("k")


def test_ab1_leader_baseexception_cleans_flight():
    """AB-1①：leader 执行中抛 BaseException（CancelledError 同类，except Exception
    接不住）→ flight 必拆不残留（try/finally 终态保障），等待者不被永久挂死。"""

    class _Boom(BaseException):
        pass

    import app.data.sandbox as sandbox_mod
    from app.agents import nodes

    s = get_settings()
    reset_query_cache()
    s.cache_enabled = True
    cache = get_query_cache()
    orig = sandbox_mod.Sandbox.execute

    async def boom_execute(self, sql):
        raise _Boom()

    sandbox_mod.Sandbox.execute = boom_execute
    key = fingerprint(_DSL, "boss", get_spec(s.dataset).name)
    try:
        with pytest.raises(_Boom):
            import asyncio

            asyncio.run(nodes.executor_node(dict(_STATE)))
        assert key not in cache._flights, "leader 抛 BaseException 后 flight 必须已拆"
        assert cache.register_flight(key) == ("leader", None)  # 后续请求不被永久挂死
        cache.fail_flight(key)
    finally:
        sandbox_mod.Sandbox.execute = orig
        s.cache_enabled = False
        reset_query_cache()


async def test_ab1_leader_cancelled_cleans_flight(db_path):
    """AB-1①：leader 被取消（asyncio 任务 cancel → CancelledError=BaseException）
    → flight 必拆不残留，同 key 后续请求可直接再竞选。"""
    import asyncio

    import app.data.sandbox as sandbox_mod
    from app.agents import nodes

    s = get_settings()
    reset_query_cache()
    s.cache_enabled = True
    cache = get_query_cache()
    orig = sandbox_mod.Sandbox.execute

    async def slow_execute(self, sql):
        await asyncio.sleep(5.0)
        raise AssertionError("不应执行到这里（已被取消）")

    sandbox_mod.Sandbox.execute = slow_execute
    key = fingerprint(_DSL, "boss", get_spec(s.dataset).name)
    try:
        task = asyncio.create_task(nodes.executor_node(dict(_STATE)))
        await asyncio.sleep(0.05)                    # 进入 leader 执行窗口
        assert key in cache._flights, "leader 执行期间 flight 必须在册"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert key not in cache._flights, "取消后 flight 必须已拆（原缺陷：永久残留）"
        assert cache.register_flight(key) == ("leader", None)
        cache.fail_flight(key)
    finally:
        sandbox_mod.Sandbox.execute = orig
        s.cache_enabled = False
        reset_query_cache()


async def test_ab1_concurrent_storm_no_thread_pool_inversion(db_path):
    """AB-1②：40 路并发同 key 未命中——等待者在事件循环内等待（不占默认执行器线程），
    leader 的 to_thread 链路不被饿死：沙箱恰好执行 1 次、39 名等待者全数 coalesced、
    零超时零溢出。（原缺陷：等待者 claim_flight 阻塞占满默认线程池 → leader 自己的
    claim/publish 反被排队堵死 → 全数超时 fail-open，coalesced=0。）"""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    import app.data.sandbox as sandbox_mod
    from app.agents import nodes

    s = get_settings()
    reset_query_cache()
    s.cache_enabled = True
    cache = get_query_cache()
    cache._flight_wait = 1.0                          # 老代码下 40 等待者在此窗口内占死 4 线程池
    orig = sandbox_mod.Sandbox.execute
    calls = {"n": 0}

    async def slow_execute(self, sql):
        calls["n"] += 1
        await asyncio.sleep(0.3)                      # leader 执行窗口（不占线程池）
        return SimpleNamespace(columns=["value"], rows=[[42]], row_count=1,
                               truncated=False, elapsed_ms=1.0)

    sandbox_mod.Sandbox.execute = slow_execute
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=4)
    loop.set_default_executor(pool)                   # 缩小默认池，放大占用反转的可观测性
    cache._max_flight_waiters = 64                    # 风暴用例放宽名额上限（全数等待验证合并）
    try:
        results = await asyncio.gather(
            *(nodes.executor_node(dict(_STATE)) for _ in range(40))
        )
        assert calls["n"] == 1, f"沙箱必须只执行一次（leader 唯一，不反转）：{calls['n']}"
        st = cache.stats()
        assert st["singleflight_coalesced"] == 39, \
            f"39 名等待者必须共享 leader 结果（coalesced）：{st['singleflight_coalesced']}"
        assert st["singleflight_timeouts"] == 0, "等待者不得超时 fail-open"
        assert st["singleflight_overflow"] == 0, "等待者不得因名额上限被拒"
        from_cache = sum(1 for r in results if r["result"].get("from_cache"))
        assert from_cache == 39
    finally:
        sandbox_mod.Sandbox.execute = orig
        s.cache_enabled = False
        pool.shutdown(wait=False)
        reset_query_cache()


# ---------------- AB-18 (P1)：SSE 生成器未启动窗口 ----------------


def test_ab18_resume_marker_ttl_reclaim_and_stale_task_finalized(tmp_path):
    """AB-18②：resume 标记带单调时间戳——超龄占用（> sql_timeout + LLM 预算，构造
    参数可配）在下次 acquire 时 TTL 收回 + 留痕计数；收回联动把滞留 running 的绑定
    任务落 aborted 终态（不再绕过 _evict_if_over 内存上限）；新鲜占用不误伤。"""
    from app.api.task_store import TaskStore

    store = TaskStore(path=tmp_path / "tasks.jsonl", resume_ttl_seconds=0.05)
    t1 = store.create("q1", "boss")
    assert store.acquire_thread_resume("t-ab18", task_id=t1["id"]) is True
    assert store.acquire_thread_resume("t-ab18") is False          # 新鲜占用：照常拒绝
    time.sleep(0.08)
    t2 = store.create("q2", "boss")
    assert store.acquire_thread_resume("t-ab18", task_id=t2["id"]) is True   # 超龄 → 收回
    assert store.resume_ttl_reclaims == 1                          # 留痕计数
    stale = store.get(t1["id"])
    assert stale["status"] == "done" and stale["kind"] == "aborted", \
        "TTL 收回必须联动把永驻 running 的滞留任务落终态"
    assert store.acquire_thread_resume("t-ab18") is False          # 新标记（刚绑定）不误伤
    store.release_thread_resume("t-ab18")


def test_ab18_any_terminal_path_releases_marker(tmp_path):
    """AB-18①：acquire 时在任务记录绑定 resume_thread——finish / finish_if_running
    任一终态路径联动释放标记（gen finally 不再是唯一释放点）。"""
    from app.api.task_store import TaskStore

    store = TaskStore(path=tmp_path / "tasks.jsonl")
    t1 = store.create("q1", "boss")
    assert store.acquire_thread_resume("t-finish", task_id=t1["id"]) is True
    store.finish(t1["id"], {"kind": "answer"}, {}, [], 1.0)
    assert "resume_thread" not in store.get(t1["id"])              # 终态后绑定不留痕
    assert store.acquire_thread_resume("t-finish") is True         # 已联动释放 → 可再占
    store.release_thread_resume("t-finish")

    t2 = store.create("q2", "boss")
    assert store.acquire_thread_resume("t-abort", task_id=t2["id"]) is True
    assert store.finish_if_running(
        t2["id"], {"kind": "aborted", "message": "x"}, {}, [], 1.0) is True
    assert store.acquire_thread_resume("t-abort") is True
    store.release_thread_resume("t-abort")


def test_ab18_release_is_owner_scoped_after_reclaim(tmp_path):
    """AB-18 两道防线组合：TTL 收回且新 run 接管后，旧 run 迟到的释放不得偷走
    新 run 的在途标记（释放按绑定任务做属主校验）。"""
    from app.api.task_store import TaskStore

    store = TaskStore(path=tmp_path / "tasks.jsonl", resume_ttl_seconds=0.05)
    t1 = store.create("q1", "boss")
    assert store.acquire_thread_resume("t-own", task_id=t1["id"]) is True
    time.sleep(0.08)
    t2 = store.create("q2", "boss")
    assert store.acquire_thread_resume("t-own", task_id=t2["id"]) is True   # 收回 + 接管
    store.release_thread_resume("t-own", task_id=t1["id"])         # 旧 gen 迟到的 finally
    assert store.acquire_thread_resume("t-own") is False           # 新标记未被偷走
    store.release_thread_resume("t-own", task_id=t2["id"])         # 属主释放正常生效
    assert store.acquire_thread_resume("t-own") is True
    store.release_thread_resume("t-own")


# ---------------- AB-6 (P2)：非 resume 路径 per-thread 执行互斥 ----------------


async def test_ab6_non_resume_concurrent_same_thread_second_run_409(db_path, monkeypatch):
    """AB-6：suspend_on 开启时 per-thread 执行互斥泛化到非 resume——同 thread 第二个
    并发 run 被 409 拒绝（原缺陷：仅 resume acquire，非 resume 可双跑同 thread）。"""
    import asyncio

    import httpx

    from app.api.main import app as api_app

    monkeypatch.setattr(get_settings(), "clarify_suspend_enabled", True)
    tid = f"t-ab6-{uuid.uuid4().hex[:8]}"
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r1, r2 = await asyncio.gather(
            c.post("/api/ask", json={"question": "最近卖得怎么样", "thread_id": tid}),
            c.post("/api/ask", json={"question": "最近卖得怎么样", "thread_id": tid}),
        )
    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 409], f"同 thread 并发非 resume 必须 200/409 各一：got {codes}"


async def test_ab6_same_owner_self_collision_rejected_409(db_path, monkeypatch):
    """AB-6 与 owner_check 组合语义：同属主自撞（同 token 同 thread 并发第二个 run）
    同样要拒——互斥保护的是图/checkpointer 一致性，不是属主边界。"""
    import asyncio

    import httpx

    from app.api.main import app as api_app

    monkeypatch.setattr(get_settings(), "clarify_suspend_enabled", True)
    monkeypatch.setattr(get_settings(), "owner_check_enabled", True)
    tid = f"t-ab6-own-{uuid.uuid4().hex[:8]}"
    headers = {"X-Owner-Token": "self-collision-token"}
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r1, r2 = await asyncio.gather(
            c.post("/api/ask", json={"question": "最近卖得怎么样", "thread_id": tid},
                   headers=headers),
            c.post("/api/ask", json={"question": "最近卖得怎么样", "thread_id": tid},
                   headers=headers),
        )
    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 409], f"同属主自撞也必须 200/409 各一：got {codes}"


async def test_ab6_non_resume_releases_marker_after_stream(db_path, monkeypatch):
    """AB-6 配套：非 resume 的互斥标记在 gen finally 统一释放——流结束后同 thread
    可再次执行（不误拒后续正常请求）。"""
    import httpx

    from app.api.main import app as api_app

    monkeypatch.setattr(get_settings(), "clarify_suspend_enabled", True)
    tid = f"t-ab6-rel-{uuid.uuid4().hex[:8]}"
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r1 = await c.post("/api/ask",
                          json={"question": "上个月的销售额是多少", "thread_id": tid})
        assert r1.status_code == 200
        r2 = await c.post("/api/ask",
                          json={"question": "上个月的销售额是多少", "thread_id": tid})
        assert r2.status_code == 200, "流结束后标记必须已释放，同 thread 再次执行不得 409"
