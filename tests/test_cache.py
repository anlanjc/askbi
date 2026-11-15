"""查询缓存测试（E4③）：指纹稳定性 / 双闸失效 / 开关零影响 / 并发 / 空结果。

分层：单元测试自建 QueryCache（stub 版本号，不触库）；端到端走 mock 全图
（run_ask），验证"缓存不得改变答案"与默认关闭零侵入。
"""

from __future__ import annotations

import asyncio
import threading
import time

from app.agents.graph import run_ask
from app.cache import (
    QueryCache,
    align_columns,
    fingerprint,
    get_query_cache,
    reset_query_cache,
)
from app.config import get_settings


def _make_cache(**kw) -> QueryCache:
    kw.setdefault("version_provider", lambda: "v1")
    kw.setdefault("enabled", True)
    kw.setdefault("version_ttl_seconds", 0.0)  # 单测语义=立即失效；记忆化行为单独测
    return QueryCache(**kw)


# ---------------- 1. 指纹稳定性：不同写法同一查询 ----------------


def test_fingerprint_stable_across_field_order():
    """维度/过滤书写顺序、相对 vs 绝对时间、intent 差异——同一查询同 key。"""
    a = {
        "intent": "breakdown",
        "metric": "revenue",
        "dimensions": ["region", "month"],
        "filters": [
            {"field": "region", "op": "=", "value": "华南"},
            {"field": "channel", "op": "=", "value": "直播"},
        ],
        "time": {"mode": "last_n_months", "n": 3},
    }
    b = {
        "intent": "topn",  # intent 从不参与编译，不影响键
        "metric": "revenue",
        "dimensions": ["month", "region"],
        "filters": [
            {"field": "channel", "op": "=", "value": "直播"},
            {"field": "region", "op": "=", "value": "华南"},
        ],
        "time": {"mode": "between", "start": "2026-07", "end": "2026-09"},
    }
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_ignores_in_list_order_but_role_matters():
    """IN 列表是集合语义（顺序无关）；role 改变行级权限——绝不能共享缓存。"""
    a = {"metric": "revenue", "filters": [{"field": "region", "op": "in", "value": ["华南", "华北"]}]}
    b = {"metric": "revenue", "filters": [{"field": "region", "op": "in", "value": ["华北", "华南"]}]}
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(a, role="boss") != fingerprint(a, role="south")


# ---------------- 2. 双闸失效 ----------------


def test_ttl_invalidation():
    """TTL 闸：超过 ttl 后即使数据版本未变也失效，并计入 ttl_expired。"""
    now = {"t": 1000.0}
    c = _make_cache(ttl_seconds=300, clock=lambda: now["t"])
    c.set("k", {"rows": [[1]]})
    assert c.get("k") is not None
    now["t"] = 1300.0  # 恰好 300s：到期
    assert c.get("k") is None
    st = c.stats()
    assert st["ttl_expired"] == 1 and st["size"] == 0


def test_version_invalidation_purges_all_entries():
    """版本闸：数据版本变化 → 全量失效（不是单条），旧条目全部清除。"""
    ver = {"v": "v1"}
    c = _make_cache(version_provider=lambda: ver["v"])
    c.set("k1", {"rows": [[1]]})
    c.set("k2", {"rows": [[2]]})
    assert c.get("k1") is not None and c.get("k2") is not None
    ver["v"] = "v2"  # 模拟数据重生成
    assert c.get("k1") is None and c.get("k2") is None
    st = c.stats()
    assert st["version_invalidations"] >= 1 and st["size"] == 0


def test_real_data_version_changes_with_database(db_path):
    """版本号来自真实库指纹：明细行数变化必须被 data_version 捕获（插后即删，不留痕）。"""
    import duckdb

    from app.cache import data_version

    v1 = data_version(db_path)
    con = duckdb.connect(str(db_path))  # 写连接与 data_version 的只读连接必须串行
    try:
        con.execute("INSERT INTO order_items VALUES (-1, -1, 1, 1.0, 0.0)")
    finally:
        con.close()
    v2 = data_version(db_path)
    con = duckdb.connect(str(db_path))
    try:
        con.execute("DELETE FROM order_items WHERE order_id = -1")
    finally:
        con.close()
    assert v1 != v2
    assert data_version(db_path) == v1  # 清理后指纹复原


# ---------------- 3. 计数器与线程安全 ----------------


def test_hit_and_miss_counters():
    c = _make_cache()
    c.get("missing")          # miss
    c.set("k", {"rows": [[1]]})
    c.get("k")
    c.get("k")                # 2 hits
    st = c.stats()
    assert st["hits"] == 2 and st["misses"] == 1 and st["stores"] == 1
    assert st["hit_rate"] == round(2 / 3, 4)


def test_concurrent_access_is_thread_safe():
    """8 线程并发读写：无异常，计数守恒（hits+misses == 恰好全部 get 次数）。"""
    c = _make_cache(max_entries=64)
    errors: list[Exception] = []
    n_threads, n_iters = 8, 200

    def worker(i: int) -> None:
        try:
            for j in range(n_iters):
                k = f"k{(i * j) % 32}"
                if j % 3 == 0:
                    c.set(k, {"rows": [[i, j]]})
                else:
                    r = c.get(k)
                    assert r is None or "rows" in r
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    st = c.stats()
    gets_per_thread = sum(1 for j in range(n_iters) if j % 3 != 0)
    assert st["hits"] + st["misses"] == n_threads * gets_per_thread
    assert st["size"] <= 64  # LRU 容量钳制生效


def test_disabled_cache_stats_stay_zero():
    """禁用态：get/set 均为 no-op，stats 如实报告 enabled=False 且全部计数为零。"""
    c = _make_cache(enabled=False)
    c.set("k", {"rows": [[1]]})
    assert c.get("k") is None
    st = c.stats()
    assert st["enabled"] is False
    assert st["size"] == 0 and st["stores"] == 0 and st["hits"] == 0 and st["misses"] == 0


def test_empty_result_is_cached_too():
    """空结果也缓存：权限范围外的问题重复问不必反复执行沙箱。"""
    c = _make_cache()
    empty = {"columns": ["value"], "rows": [], "row_count": 0, "note": "空结果"}
    c.set("k", empty)
    assert c.get("k") == empty
    assert c.stats()["hits"] == 1


# ---------------- 4. 列对齐（维度顺序归一的配套） ----------------


def test_align_columns_reorders_hit_to_requested_order():
    cached = {"columns": ["month", "region", "value"], "rows": [["2026-09", "华南", 10.0]]}
    out = align_columns(cached, ["region", "month"])
    assert out["columns"] == ["region", "month", "value"]
    assert out["rows"] == [["华南", "2026-09", 10.0]]
    assert align_columns(cached, ["month", "region"]) is cached  # 同序原样返回


# ---------------- 5. 端到端（mock 全图）：缓存不得改变答案 ----------------


async def test_end_to_end_replay_hits_cache(db_path):
    """开缓存第二次同问：命中回放，结果与首跑逐字段一致，徽章标记 from_cache。"""
    s = get_settings()
    reset_query_cache()
    s.cache_enabled = True
    try:
        q = "上个月的销售额是多少"
        s1 = await run_ask(q)
        s2 = await run_ask(q)
        assert "from_cache" not in s1["result"]
        assert s2["result"]["from_cache"] is True
        assert s2["result"]["rows"] == s1["result"]["rows"]
        assert s2["result"]["columns"] == s1["result"]["columns"]
        assert s2["answer"]["data_badge"]["from_cache"] is True
        assert any(e["type"] == "cache_hit" for e in s2["events"] if e["node"] == "executor")
        st = get_query_cache().stats()
        assert st["hits"] == 1 and st["misses"] == 1
    finally:
        s.cache_enabled = False
        reset_query_cache()


async def test_empty_result_replayed_from_cache_end_to_end(db_path):
    """权限范围外空结果：第二次命中缓存，note 语义保留。"""
    s = get_settings()
    reset_query_cache()
    s.cache_enabled = True
    try:
        q = "上月东北地区销售额"
        s1 = await run_ask(q, role="south")  # south 只可见华南 → 空结果
        assert s1["result"]["row_count"] == 0
        s2 = await run_ask(q, role="south")
        assert s2["result"]["from_cache"] is True
        assert s2["result"]["rows"] == []
        assert "空结果" in s2["result"].get("note", "")
    finally:
        s.cache_enabled = False
        reset_query_cache()


async def test_disabled_cache_keeps_legacy_behavior(db_path):
    """开关关闭（默认）：两次执行都真实落沙箱，无缓存事件，stats 零计数。"""
    reset_query_cache()
    assert get_settings().cache_enabled is False  # 默认关：既有行为零影响
    q = "上个月的销售额是多少"
    s1 = await run_ask(q)
    s2 = await run_ask(q)
    for st_ in (s1, s2):
        assert [e["type"] for e in st_["events"] if e["node"] == "executor"] == ["result"]
        assert "from_cache" not in st_["result"]
        assert st_["answer"]["data_badge"]["from_cache"] is False
    st = get_query_cache().stats()
    assert st["enabled"] is False and st["hits"] == 0 and st["stores"] == 0


def test_semver_change_invalidates():
    """评审 A1 回归：指标语义版本变化 → 指纹变化。"""
    import app.cache as cache_mod
    dsl = {"metric": "gross_margin_rate", "dimensions": ["region"],
           "time": {"mode": "last_n_months", "n": 1}}
    fp1 = cache_mod.fingerprint(dsl)
    cache_mod._COMPILE_SEMVER_CACHE = "changed-semver"
    fp2 = cache_mod.fingerprint(dsl)
    assert fp1 != fp2
    cache_mod._COMPILE_SEMVER_CACHE = None   # 还原


def test_ttl_race_recheck_in_second_lock():
    """评审 A2 回归：锁外版本查询期间跨过 TTL 边界 → 必须判 miss。"""
    import app.cache as cache_mod

    holder = {"t": 1000.0}

    class Clock:
        def __call__(self):
            return holder["t"]

    cache = cache_mod.QueryCache(ttl_seconds=1.0, enabled=True, clock=Clock(),
                                 version_provider=lambda: "v1", version_ttl_seconds=0.0)
    dsl = {"metric": "gross_margin_rate", "dimensions": ["region"], "time": {"mode": "all"}}
    key = cache_mod.fingerprint(dsl)
    cache.set(key, {"columns": ["value"], "rows": [[1]], "row_count": 1})
    holder["t"] = 1000.99                    # 第一次 TTL 检查（锁内）未过期
    orig = cache._version_provider
    cache._version_provider = lambda: (holder.__setitem__("t", holder["t"] + 0.2), orig())[1]
    got = cache.get(key)                     # 锁外版本查询推进时钟跨过 TTL
    assert got is None                       # 二次拿锁重查 TTL → miss
    assert cache._counters["ttl_expired"] == 1



def test_between_empty_and_all_same_fingerprint():
    """评审 A3 回归：between 全空 ≡ all → 同指纹。"""
    import app.cache as cache_mod
    a = cache_mod.fingerprint({"metric": "gmv", "dimensions": [], "time": {"mode": "all"}})
    b = cache_mod.fingerprint({"metric": "gmv", "dimensions": [],
                               "time": {"mode": "between"}})
    assert a == b


def test_filter_value_type_normalized():
    """评审 A4 回归：value=1 与 value="1" 同指纹。"""
    import app.cache as cache_mod
    a = cache_mod.fingerprint({"metric": "gmv", "dimensions": ["region"],
                               "filters": [{"field": "region", "op": "=", "value": 1}],
                               "time": {"mode": "all"}})
    b = cache_mod.fingerprint({"metric": "gmv", "dimensions": ["region"],
                               "filters": [{"field": "region", "op": "=", "value": "1"}],
                               "time": {"mode": "all"}})
    assert a == b


def test_version_probe_memoized_within_window():
    """压测回归：data_version 是 4 聚合扫描，版本探测必须带记忆化窗口（否则缓存负收益）。"""
    from app.cache import QueryCache

    calls = {"n": 0}

    def provider() -> str:
        calls["n"] += 1
        return "v1"

    now = {"t": 1000.0}

    def fake_monotonic() -> float:
        return now["t"]

    c = QueryCache(
        ttl_seconds=300, version_provider=provider, enabled=True,
        clock=lambda: 0.0, version_clock=fake_monotonic, version_ttl_seconds=30.0,
    )
    c.set("k", {"columns": ["value"], "rows": [[1]], "row_count": 1})
    first = calls["n"]
    assert first >= 1
    for _ in range(5):
        assert c.get("k") is not None
    assert calls["n"] == first  # 窗口内不再探测
    now["t"] += 31.0            # 窗口外 → 重新探测一次
    assert c.get("k") is not None
    assert calls["n"] == first + 1


# ---------------- 6. singleflight（并发击穿防护：同 key 在飞只放一个执行） ----------------

_SF_RESULT = {"columns": ["value"], "rows": [[42]], "row_count": 1, "truncated": False, "elapsed_ms": 1.0}


def _poll(pred, timeout: float = 5.0) -> bool:
    """轮询等待条件成立（线程测试的确定性护栏）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


async def _apoll(pred, timeout: float = 5.0) -> bool:
    """异步轮询等待条件成立（事件循环测试用）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


def test_singleflight_leader_claims_and_waiters_share():
    """同 key 并发：首个调用方成为 leader，其余等待者在 publish 后共享结果（不重复执行）。"""
    c = _make_cache()
    assert c.claim_flight("k") == ("leader", None)   # 主线程成为 leader

    outs: dict[int, tuple] = {}

    def waiter(i: int) -> None:
        outs[i] = c.claim_flight("k")

    threads = [threading.Thread(target=waiter, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.1)
    assert len(outs) == 0, "等待者应在 leader 完成前挂起等待而非提前返回"

    c.publish_flight("k", dict(_SF_RESULT))   # leader 完成：写缓存 + 唤醒等待者
    for t in threads:
        t.join(timeout=5)
    assert all(not t.is_alive() for t in threads)
    for i in range(3):
        src, res = outs[i]
        assert src == "flight" and res == _SF_RESULT
    st = c.stats()
    assert st["singleflight_coalesced"] == 3 and st["singleflight_timeouts"] == 0
    assert st["stores"] == 1
    assert c.get("k") == _SF_RESULT          # 后续请求走常规命中


def test_singleflight_waiter_timeout_fails_open():
    """等待超时 → fail-open 返回 ("miss", None)（调用方自行执行，与无单飞行为一致）；超时计数。"""
    c = _make_cache(flight_wait_seconds=0.05)
    assert c.claim_flight("k") == ("leader", None)

    out: dict[str, tuple] = {}

    def waiter() -> None:
        out["w"] = c.claim_flight("k")

    t = threading.Thread(target=waiter)
    t.start()
    t.join(timeout=5)
    src, res = out["w"]
    assert src == "miss" and res is None
    assert c.stats()["singleflight_timeouts"] == 1

    # fail-open 方自行执行后 publish：leader 仍在飞也不挂起；leader 事后 publish 只纯写缓存
    c.publish_flight("k", dict(_SF_RESULT))
    assert c.get("k") == _SF_RESULT
    c.publish_flight("k", dict(_SF_RESULT))
    assert c.get("k") == _SF_RESULT


def test_singleflight_leader_failure_wakes_waiters_to_relead():
    """leader 执行失败 fail_flight：等待者被唤醒重新竞选，恰一名接棒 leader，其余共享新结果。"""
    c = _make_cache()
    assert c.claim_flight("k") == ("leader", None)

    outs: dict[str, tuple] = {}

    def runner(tag: str) -> None:
        outs[tag] = c.claim_flight("k")

    threads = [threading.Thread(target=runner, args=(f"w{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.1)
    assert len(outs) == 0, "等待者应已挂起在 flight 上"

    c.fail_flight("k")          # leader 失败：拆 flight 唤醒等待者
    assert _poll(lambda: any(o[0] == "leader" for o in outs.values())), \
        "失败唤醒后必须有等待者接棒 leader"
    time.sleep(0.05)            # 给其余等待者时间挂到新 flight 上
    c.publish_flight("k", dict(_SF_RESULT))
    for t in threads:
        t.join(timeout=5)
    assert all(not t.is_alive() for t in threads)
    assert len(outs) == 3
    leaders = [k for k, o in outs.items() if o[0] == "leader"]
    assert len(leaders) == 1, f"恰一名接棒 leader，实得 {leaders}"
    flights = {k: o for k, o in outs.items() if o[0] == "flight"}
    assert len(flights) == 2
    for _src, res in flights.values():
        assert res == _SF_RESULT
    assert c.stats()["singleflight_coalesced"] == 2


def test_singleflight_fast_path_hit():
    """已有缓存条目：claim 直接命中回放，不起 flight。"""
    c = _make_cache()
    c.set("k", dict(_SF_RESULT))
    src, res = c.claim_flight("k")
    assert src == "hit" and res == _SF_RESULT
    st = c.stats()
    assert st["hits"] == 1 and st["singleflight_coalesced"] == 0 and st["singleflight_timeouts"] == 0


def test_singleflight_disabled_cache_no_flight():
    """禁用态：claim 直取执行权、不起 flight，publish 为 no-op——与无缓存行为一致、计数为零。"""
    c = _make_cache(enabled=False)
    assert c.claim_flight("k") == ("leader", None)
    c.publish_flight("k", dict(_SF_RESULT))
    st = c.stats()
    assert st["enabled"] is False and st["stores"] == 0 and st["singleflight_coalesced"] == 0
    assert c.get("k") is None


def test_flight_wait_seconds_defaults_from_settings():
    """等待上界默认来自 settings.cache_flight_wait_seconds，构造参数显式覆盖优先。"""
    import app.cache as cache_mod

    c = cache_mod.QueryCache(version_provider=lambda: "v1", enabled=True, version_ttl_seconds=0.0)
    assert c._flight_wait == get_settings().cache_flight_wait_seconds
    c2 = cache_mod.QueryCache(version_provider=lambda: "v1", enabled=True,
                              version_ttl_seconds=0.0, flight_wait_seconds=0.01)
    assert c2._flight_wait == 0.01


async def test_executor_singleflight_coalesces_concurrent_same_question(db_path):
    """端到端（mock 全图）：开缓存下两路并发同问 → 沙箱只执行一次，第二路共享结果并标记 from_cache。"""
    import asyncio

    import app.data.sandbox as sandbox_mod

    s = get_settings()
    reset_query_cache()
    s.cache_enabled = True

    gate = threading.Event()
    calls = {"n": 0}
    orig_execute = sandbox_mod.Sandbox.execute

    async def counting_execute(self, sql: str):
        calls["n"] += 1
        await asyncio.to_thread(gate.wait, 10.0)
        return await orig_execute(self, sql)

    sandbox_mod.Sandbox.execute = counting_execute
    try:
        q = "上个月的销售额是多少"
        t1 = asyncio.create_task(run_ask(q))
        assert await _apoll(lambda: calls["n"] == 1), "leader 应已进入沙箱执行"
        t2 = asyncio.create_task(run_ask(q))
        assert await _apoll(lambda: get_query_cache().stats()["misses"] >= 2), \
            "第二路应已到达 claim（miss 计数）"
        assert calls["n"] == 1, "等待者不得重复执行沙箱（击穿防护）"
        gate.set()
        s1 = await t1
        s2 = await t2
        assert s2["result"]["from_cache"] is True
        assert s2["result"]["rows"] == s1["result"]["rows"]
        st = get_query_cache().stats()
        assert st["singleflight_coalesced"] == 1 and st["singleflight_timeouts"] == 0
        hit_events = [e for e in s2["events"]
                      if e["node"] == "executor" and e["type"] == "cache_hit"]
        assert hit_events and hit_events[0]["data"].get("shared") is True
    finally:
        sandbox_mod.Sandbox.execute = orig_execute
        gate.set()
        s.cache_enabled = False
        reset_query_cache()
