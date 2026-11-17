"""红队 round 2（[1B4-AB] 任务三）——针对本批新增面的对抗性证据包。

与 test_redteam.py（round 1：注入/绕过/提示注入三类经典面）的分工：本套件攻击
**[1B4-AB] 批次新增与既有并发/审计/参数面**，五个攻击面：

1. singleflight 击穿对抗（并发同查询风暴 / leader 失败链 / fail-open 计费与计数 /
   跨 role·dataset 隔离 / store 失败不楔死 flight 表）；
2. 审计日志注入（X-Owner-Token 等头注入落审计字段的转义与只读性 / 损坏行容错 /
   回显端点 JSON 安全 / token 门禁 / 明文 token 不落审计面）；
3. resume 竞态新姿势（acquire_thread_resume 后异常路径释放 / **409 拒绝路径孤儿
   running 任务** / 属主注册表有界）；
4. 深度参数滥用计费逃逸（超长 id / depth 强制 full / 修复循环上界 / 拒答路径
   用量照记）；
5. prompt 注入二阶（audit 查询端点回显转义 / 任务明细回显 JSON 安全 / 管理
   配置面无自由文本注入面）。

每条 payload 断言**实际防御行为**，配套 evidence/redteam2_report.md 逐条给出
payload → 实际行为 → 判定与机制归因。红队真缺陷当日修（带编号提交）：
- RT2-1：resume 409 拒绝路径曾孤儿 running 任务（create 先于 acquire，409 后无
  兜底终态，且 running 永不被 _evict_if_over 淘汰 → 内存/任务列表泄漏）；
- RT2-2：thread_id/session_id/task_id 无长度上限（AskRequest/FeedbackRequest
  自由长度 → checkpointer 键与 JSONL 落盘面滥用）。
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time

import httpx
import pytest

from app.cache import QueryCache, fingerprint, get_query_cache, reset_query_cache
from app.config import get_settings
from app.data.sandbox import SqlRejected, guard

# ===========================================================================
# 0. 辅助
# ===========================================================================

_SF_RESULT = {"columns": ["value"], "rows": [[42]], "row_count": 1, "truncated": False, "elapsed_ms": 1.0}
_SF_RESULT_B = {"columns": ["value"], "rows": [[7]], "row_count": 1, "truncated": False, "elapsed_ms": 1.0}


def _make_cache(**kw) -> QueryCache:
    kw.setdefault("version_provider", lambda: "v1")
    kw.setdefault("enabled", True)
    kw.setdefault("version_ttl_seconds", 0.0)
    return QueryCache(**kw)


def _poll(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


async def _apoll(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.fixture
async def client():
    from app.api.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _task_id_of(text: str) -> str:
    """从 SSE final 帧取 task_id——created_at 只有秒级分辨率，list()[0] 在同秒任务上歧义。"""
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                frame = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if frame.get("type") == "final":
                return frame["task_id"]
    raise AssertionError("未收到 final 帧")


def _final(text: str) -> dict:
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                frame = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if frame.get("type") == "final":
                return frame["answer"]
    raise AssertionError(f"未收到 final 帧，流前 300 字符: {text[:300]}")


async def _ask(client, question: str, role: str = "boss", **extra) -> dict:
    async with client.stream("POST", "/api/ask", json={"question": question, "role": role, **extra}) as resp:
        assert resp.status_code == 200
        text = (await resp.aread()).decode()
    return _final(text)


# ===========================================================================
# 1. singleflight 击穿对抗（≥9 项）
# ===========================================================================


def test_rt2_storm8_slow_leader_exact_accounting():
    """8 路同 key 风暴：1 leader + 7 等待者共享，coalesced==7 / timeouts==0 / stores==1。"""
    c = _make_cache()
    assert c.claim_flight("k") == ("leader", None)
    gate = threading.Event()
    outs: dict[int, tuple] = {}

    def waiter(i: int) -> None:
        outs[i] = c.claim_flight("k")
        gate.wait(5.0)

    threads = [threading.Thread(target=waiter, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    time.sleep(0.15)
    assert len(outs) == 0, "风暴等待者必须在 leader 完成前全部挂起"
    gate.set()
    c.publish_flight("k", dict(_SF_RESULT))
    for t in threads:
        t.join(timeout=5)
    assert all(not t.is_alive() for t in threads)
    assert all(src == "flight" and res == _SF_RESULT for src, res in outs.values())
    st = c.stats()
    assert st["singleflight_coalesced"] == 8 and st["singleflight_timeouts"] == 0
    assert st["stores"] == 1


def test_rt2_storm_mixed_keys_isolated():
    """同 key 合流、异 key 各自竞选：A 风暴 3 等待者合流，B 独立 leader 不被 A 阻塞。"""
    c = _make_cache()
    assert c.claim_flight("A") == ("leader", None)
    gate_a = threading.Event()
    outs: dict[str, tuple] = {}

    def waiter(tag: str, key: str) -> None:
        outs[tag] = c.claim_flight(key)
        if key == "A":
            gate_a.wait(5.0)

    threads = [threading.Thread(target=waiter, args=(f"A{i}", "A")) for i in range(3)]
    threads += [threading.Thread(target=waiter, args=(f"B{i}", "B")) for i in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.15)
    # B 的 claim 不受 A 在飞影响：首个 B claim 立即成为 leader（第二个 B 在 B flight 上自合流）
    b_outs = [o for tag, o in outs.items() if tag.startswith("B")]
    assert len(b_outs) >= 1 and any(src == "leader" for src, _r in b_outs),         "B 的首个 claim 不应被 A 的在飞 flight 阻塞，应立即取得自己的执行权"
    gate_a.set()
    c.publish_flight("B", dict(_SF_RESULT_B))
    c.publish_flight("A", dict(_SF_RESULT))
    for t in threads:
        t.join(timeout=5)
    a_srcs = [src for tag, (src, _r) in outs.items() if tag.startswith("A")]
    assert sorted(a_srcs) == ["flight", "flight", "flight"]
    b_pairs = [(src, r) for tag, (src, r) in outs.items() if tag.startswith("B")]
    assert sorted(src for src, _r in b_pairs) == ["flight", "leader"]
    assert _SF_RESULT_B in [r for _s, r in b_pairs]
    st = c.stats()
    assert st["singleflight_coalesced"] == 4 and st["singleflight_timeouts"] == 0
    # A：主 leader 之外 3 等待者合流；B：主 leader 之外 1 等待者合流（B 首个 claim 是 leader）


def test_rt2_cross_role_fingerprint_never_coalesces():
    """对抗口径：同 DSL 不同 role 指纹必异 → 两个 key 各自 leader，绝不跨角色合流。"""
    dsl = {"metric": "revenue", "dimensions": ["channel"], "time": {"mode": "last_n_months", "n": 1}}
    kb, ks = fingerprint(dsl, "boss", "ecommerce"), fingerprint(dsl, "south", "ecommerce")
    assert kb != ks
    c = _make_cache()
    assert c.claim_flight(kb) == ("leader", None)
    assert c.claim_flight(ks) == ("leader", None), "south 同问必须独立执行（权限结果集不同）"


def test_rt2_cross_dataset_fingerprint_never_coalesces():
    """同 DSL 不同 dataset 指纹必异 → 两个 key 各自 leader，绝不跨源合流。"""
    dsl = {"metric": "revenue", "dimensions": ["channel"], "time": {"mode": "last_n_months", "n": 1}}
    ka, kh = fingerprint(dsl, "boss", "ecommerce"), fingerprint(dsl, "boss", "hotel")
    assert ka != kh
    c = _make_cache()
    assert c.claim_flight(ka) == ("leader", None)
    assert c.claim_flight(kh) == ("leader", None)


def test_rt2_leader_failure_chain_twice_never_loses_execution_duty():
    """leader 连续失败两轮：每轮恰一名接棒、其余共享，执行责任逐轮交接不丢失。"""
    c = _make_cache()
    assert c.claim_flight("k") == ("leader", None)
    outs: dict[int, tuple] = {}
    n = 7

    def waiter(i: int) -> None:
        outs[i] = c.claim_flight("k")

    threads = [threading.Thread(target=waiter, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    time.sleep(0.15)
    leaders_seen = 0

    def _leader_count() -> int:
        return sum(1 for o in outs.values() if o[0] == "leader")

    def _wait_new_leader(baseline: int) -> int:
        """等待一轮失败唤醒后出现新 leader（恰比 baseline 多一名）；返回当前 leader 数。"""
        assert _poll(lambda: _leader_count() > baseline), "失败唤醒后必须有恰一名等待者接棒 leader"
        time.sleep(0.05)
        return _leader_count()

    for _round in range(2):
        c.fail_flight("k")   # 当前 leader 失败
        leaders_seen = _wait_new_leader(leaders_seen)
        assert leaders_seen == _round + 1, f"第 {_round + 1} 轮应恰一名新 leader"
    c.publish_flight("k", dict(_SF_RESULT))
    for t in threads:
        t.join(timeout=5)
    assert all(not t.is_alive() for t in threads)
    total_leaders = sum(1 for o in outs.values() if o[0] == "leader")
    assert total_leaders == 2                       # 两轮各恰一名接棒
    flights = [o for o in outs.values() if o[0] == "flight"]
    assert len(flights) == n - 2
    assert all(res == _SF_RESULT for _s, res in flights)
    st = c.stats()
    assert st["singleflight_timeouts"] == 0 and st["singleflight_coalesced"] == n - 2


def test_rt2_fail_open_waiter_publish_interleaves_safely():
    """fail-open 等待者自行执行并 publish（原 leader 仍在飞）→ 不崩溃、写缓存生效；
    leader 事后 publish 覆盖为权威结果，两个计数器各自如实记账。"""
    c = _make_cache(flight_wait_seconds=0.02)
    assert c.claim_flight("k") == ("leader", None)
    out: dict[str, tuple] = {}

    def failopen() -> None:
        out["w"] = c.claim_flight("k")

    t = threading.Thread(target=failopen)
    t.start()
    t.join(timeout=5)
    assert out["w"] == ("miss", None)
    c.publish_flight("k", {"columns": ["value"], "rows": [[1]], "row_count": 1, "truncated": False, "elapsed_ms": 1.0})
    assert c.get("k") is not None                    # fail-open 方写入生效
    c.publish_flight("k", dict(_SF_RESULT))          # 原 leader 事后 publish（纯写缓存）
    assert c.get("k") == _SF_RESULT
    st = c.stats()
    assert st["singleflight_timeouts"] == 1


def test_rt2_publish_flight_store_failure_never_wedges_flight_map():
    """对抗 store 失败（版本探测抛错 → set 抛错）：flight 仍被拆除、等待者拿到共享结果
    不被永久挂死——缓存写失败不得把单飞变成可用性风险（try/finally 拆除语义）。"""
    calls = {"n": 0}

    def failing_provider() -> str:
        calls["n"] += 1
        # claim 的 get 未命中时不调探测（条目缺失先返回）；set 是首个调用点 → 必抛
        raise RuntimeError("version probe boom")

    c = _make_cache(version_provider=failing_provider)
    assert c.claim_flight("k") == ("leader", None)
    out: dict[str, tuple] = {}

    def waiter() -> None:
        out["w"] = c.claim_flight("k")

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.1)
    with pytest.raises(RuntimeError):
        c.publish_flight("k", dict(_SF_RESULT))      # set 抛错向上传播（leader 侧可感知）
    t.join(timeout=5)
    assert not t.is_alive(), "等待者必须被唤醒而非永久挂死"
    assert out["w"] == ("flight", _SF_RESULT)        # 结果已挂 flight 分发给等待者
    assert not c._flights, "flight 表必须被 try/finally 拆除，不留永久在飞记录"


def test_rt2_fail_flight_without_flight_is_noop():
    """无在飞记录时 fail/publish 均为安全 no-op（fail-open 方与 leader 竞态下不炸 flight 表）。"""
    c = _make_cache()
    c.fail_flight("ghost")                            # 不抛
    c.publish_flight("ghost", dict(_SF_RESULT))       # 不抛，纯写缓存
    assert not c._flights
    assert c.get("ghost") == _SF_RESULT
    # 误拆他人 flight 后原 leader 事后 publish 为纯写缓存（fail_flight docstring 承诺的竞态语义）
    assert c.claim_flight("k2") == ("leader", None)
    c.fail_flight("k2")
    assert c.claim_flight("k2") == ("leader", None)   # 拆除后可重新竞选
    c.publish_flight("k2", dict(_SF_RESULT))
    assert c.get("k2") == _SF_RESULT


async def test_rt2_executor_storm6_sandbox_executes_once(db_path):
    """e2e 风暴：6 路并发同问（mock 全图 + 开缓存 + 沙箱闸门挂住 leader）→
    沙箱只执行一次，5 路共享结果并带 shared 标记，计数器精确。"""
    import app.data.sandbox as sandbox_mod

    s = get_settings()
    reset_query_cache()
    s.cache_enabled = True
    gate = threading.Event()
    calls = {"n": 0}
    orig_execute = sandbox_mod.Sandbox.execute

    async def counting_execute(self, sql: str):
        calls["n"] += 1
        await asyncio.to_thread(gate.wait, 15.0)
        return await orig_execute(self, sql)

    sandbox_mod.Sandbox.execute = counting_execute
    try:
        q = "上个月的销售额是多少"
        tasks = [asyncio.create_task(run_ask_helper(q)) for _ in range(6)]
        assert await _apoll(lambda: calls["n"] == 1), "恰一路 leader 进入沙箱执行"
        assert await _apoll(lambda: get_query_cache().stats()["misses"] >= 6), "其余 5 路已到达 claim"
        assert calls["n"] == 1, "风暴下沙箱必须只执行一次（击穿防护）"
        gate.set()
        states = await asyncio.gather(*tasks)
        rows = [st["result"]["rows"] for st in states]
        assert all(r == rows[0] for r in rows), "共享结果与 leader 一致"
        shared_flags = [bool((st["result"] or {}).get("from_cache")) for st in states]
        assert sum(shared_flags) == 5, "恰 5 路共享回放"
        st = get_query_cache().stats()
        assert st["singleflight_coalesced"] == 5 and st["singleflight_timeouts"] == 0
    finally:
        sandbox_mod.Sandbox.execute = orig_execute
        gate.set()
        s.cache_enabled = False
        reset_query_cache()


async def run_ask_helper(q: str):
    from app.agents.graph import run_ask

    return await run_ask(q)


# ===========================================================================
# 2. 审计日志注入（X-Owner-Token 等头注入落审计字段的转义与只读性，≥10 项）
# ===========================================================================

HOSTILE_FIELDS = [
    "line1\nline2-forged-record",
    "x\r\nDROP TABLE audit;--",
    "quote'\"double\"quote",
    "</script><script>alert(1)</script>",
    "\u202e\u202dRTL/PDF overrides",   # RLO/PDF 双向控制符（ruff PLE2502 拒绝源码裸控制字符，用转义书写）
    "{\"injected\": true}",
    "A" * 10000,
]


@pytest.fixture
def audit_log(tmp_path):
    from app.audit import AuditLog

    return AuditLog(path=tmp_path / "audit.jsonl")


def test_rt2_audit_hostile_fields_stay_single_line_and_roundtrip(audit_log):
    """对抗载荷入 actor/action/target_id/detail：JSONL 每事件恒一行、逐行可解析、roundtrip 无损。"""
    for i, payload in enumerate(HOSTILE_FIELDS):
        audit_log.record(payload, "attack.action", "target", payload, {"detail": payload})
    raw = audit_log.path.read_text(encoding="utf-8")
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert len(lines) == len(HOSTILE_FIELDS), "注入串不得伪造出额外审计行"
    for ln, payload in zip(lines, HOSTILE_FIELDS):
        ev = json.loads(ln)
        assert ev["actor"] == payload and ev["target_id"] == payload
        assert ev["detail"]["detail"] == payload
    assert audit_log.list()[0]["detail"]["detail"] == HOSTILE_FIELDS[-1]  # 最新在前


def test_rt2_audit_storm_50_hostile_events_line_exact(audit_log):
    for i in range(50):
        audit_log.record("admin", "task.clear", "task_store", f"t{i}\nX", {"removed": i, "payload": "a\nb"})
    lines = [ln for ln in audit_log.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 50
    assert all(isinstance(json.loads(ln), dict) for ln in lines)


def test_rt2_audit_corrupted_lines_skipped_not_executed(audit_log):
    """损坏行/伪记录注入（攻击者直写文件的二阶形态）：跳过不崩溃、不执行、不回显。"""
    good = {"ts": "2026-11-18T00:00:00", "actor": "admin", "action": "cache.clear",
            "target_type": "query_cache", "target_id": "*", "detail": {"removed": 1}}
    audit_log.path.write_text(
        "this is not json\n"
        '{"ts": broken, "actor": }\n'
        '{"nested": {"unclosed\n'
        + json.dumps(good, ensure_ascii=False) + "\n"
        + '{"ts":"x","actor":"attacker","action":"forge","target_type":"x","target_id":"y","detail":{}}\n',
        encoding="utf-8",
    )
    events = audit_log.list()
    assert len(events) == 2
    assert {e["action"] for e in events} == {"cache.clear", "forge"}   # 伪记录可读出（追加日志容错口径）但不可执行
    assert not any(e["action"] == "DROP" for e in events)


def test_rt2_audit_list_is_read_only_bytes_unchanged(audit_log):
    audit_log.record("admin", "task.clear", "task_store", "*", {"removed": 3})
    audit_log.record("admin', 'x')--", "config.update", "settings", "*", {})
    before = audit_log.path.read_bytes()
    events = audit_log.list(limit=100)
    assert len(events) == 2
    assert audit_log.path.read_bytes() == before, "审计查询必须只读，文件字节零变化"


async def test_rt2_audit_endpoint_has_no_mutation_method(client):
    """审计查询端点只读：POST/PUT/DELETE 到 /api/admin/audit 一律 405。"""
    for method in ("post", "put", "delete"):
        r = await getattr(client, method)("/api/admin/audit")
        assert r.status_code == 405, f"{method.upper()} 不应可用（只读面）"


async def test_rt2_audit_echo_hostile_payloads_json_safe(client, tmp_path, monkeypatch):
    """审计回显端点二阶注入面：预埋含 HTML/JS/RLO 载荷的合法事件 → 回显必为
    application/json 数据（载荷只是数据不解释），且损坏行跳过。"""
    import app.audit as audit_mod
    from app.audit import AuditLog

    hostile = AuditLog(path=tmp_path / "audit.jsonl")
    for i, payload in enumerate(HOSTILE_FIELDS[:5]):
        hostile.record("admin", "cache.clear", "query_cache", payload, {"removed": i})
    (tmp_path / "audit.jsonl").open("a", encoding="utf-8").write("garbage line\n")
    monkeypatch.setattr(audit_mod, "_AUDIT", hostile)
    r = await client.get("/api/admin/audit")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert len(body) == 5                            # 损坏行被跳过
    assert body[0]["target_id"] == HOSTILE_FIELDS[4]  # 最新在前；载荷原样作为数据往返
    assert body[-1]["target_id"] == HOSTILE_FIELDS[0]  # 最旧记录


async def test_rt2_admin_audit_token_gate(client, monkeypatch):
    """admin_token 配置后审计查询须带 X-Admin-Token（常量时间比较）；错误/缺失 401。"""
    s = get_settings()
    monkeypatch.setattr(s, "admin_token", "sekrit-token")
    assert (await client.get("/api/admin/audit")).status_code == 401
    assert (await client.get("/api/admin/audit", headers={"X-Admin-Token": "wrong"})).status_code == 401
    r = await client.get("/api/admin/audit", headers={"X-Admin-Token": "sekrit-token"})
    assert r.status_code == 200
    monkeypatch.setattr(s, "admin_token", "")


async def test_rt2_owner_token_header_injection_stored_as_hex_only(client, tmp_path, monkeypatch):
    """X-Owner-Token 头注入（换行/脚本/10KB）：落盘 owner 字段只可能是 64 位十六进制
    SHA-256——头注入在派生层被结构性中和，tasks.jsonl 不被伪造行。"""
    s = get_settings()
    monkeypatch.setattr(s, "owner_check_enabled", True)
    from app.api.task_store import get_task_store

    store = get_task_store()
    before_lines = len([ln for ln in store.path.read_text(encoding="utf-8").splitlines() if ln.strip()])         if store.path.exists() else 0
    for token in HOSTILE_FIELDS[:4]:
        async with client.stream(
            "POST", "/api/ask",
            json={"question": "上个月的销售额是多少", "role": "boss"},
            headers={"X-Owner-Token": token},
        ) as resp:
            assert resp.status_code == 200
            _final((await resp.aread()).decode())
    lines = [ln for ln in store.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    # 每任务 create+finish 两次快照追加（每行都是完整任务记录、逐行合法 JSON）——
    # 头注入不得伪造出快照数之外的行
    assert len(lines) == before_lines + 8, "头注入不得伪造额外落盘行"
    new_records = [json.loads(ln) for ln in lines[before_lines:]]
    assert all(re.fullmatch(r"[0-9a-f]{64}", t["owner"]) for t in new_records)
    monkeypatch.setattr(s, "owner_check_enabled", False)


async def test_rt2_owner_token_plaintext_never_enters_audit(client, tmp_path, monkeypatch):
    """属主 token 明文永不入审计面：带 token 的管理操作后，审计文件不含任何 token 材料。"""
    s = get_settings()
    monkeypatch.setattr(s, "owner_check_enabled", True)
    monkeypatch.setattr(s, "admin_token", "admintok")
    import app.audit as audit_mod
    from app.audit import AuditLog

    log = AuditLog(path=tmp_path / "audit.jsonl")
    monkeypatch.setattr(audit_mod, "_AUDIT", log)
    secret = "super-secret-owner-token-9876"
    r = await client.post(
        "/api/admin/cache/clear",
        headers={"X-Admin-Token": "admintok", "X-Owner-Token": secret},
    )
    assert r.status_code == 200
    blob = log.path.read_text(encoding="utf-8")
    assert secret not in blob, "属主 token 明文不得进入审计日志"
    assert "admintok" not in blob, "管理 token 明文同样不得进入审计日志"
    for ev in log.list():
        assert set(ev) == {"ts", "actor", "action", "target_type", "target_id", "detail"}
    monkeypatch.setattr(s, "owner_check_enabled", False)
    monkeypatch.setattr(s, "admin_token", "")


def test_rt2_guard_still_rejects_injection_via_audit_ish_paths():
    """回归锚：审计批次不放松沙箱——注入串过守卫仍被拒（纵深不因新面松动）。"""
    for sql in ("SELECT 1; DROP TABLE audit_log", "SELECT read_text('../../data/audit.jsonl')"):
        with pytest.raises(SqlRejected):
            guard(sql)


# ===========================================================================
# 3. resume 竞态新姿势（acquire 后异常路径释放 / 409 孤儿任务，≥8 项）
# ===========================================================================


def test_rt2_acquire_mutex_release_frees(tmp_path):
    from app.api.task_store import TaskStore

    store = TaskStore(path=tmp_path / "tasks.jsonl")
    assert store.acquire_thread_resume("t1") is True
    assert store.acquire_thread_resume("t1") is False     # 互斥
    store.release_thread_resume("t1")
    assert store.acquire_thread_resume("t1") is True, "release 必须真正释放占用（可再竞选）"


def test_rt2_release_unknown_and_idempotent(tmp_path):
    from app.api.task_store import TaskStore

    store = TaskStore(path=tmp_path / "tasks.jsonl")
    store.release_thread_resume("never-acquired")        # no-op 不抛
    store.release_thread_resume("never-acquired")        # 幂等
    assert store.acquire_thread_resume("x") is True
    store.release_thread_resume("x")
    store.release_thread_resume("x")
    assert store.acquire_thread_resume("x") is True


def test_rt2_exception_after_acquire_releases_marker(tmp_path):
    """异常路径契约（gen finally 语义）：acquire 后任何异常退出都必须释放标记——
    释放后线程可再次 resume，否则线程永久 409 不可恢复。"""
    from app.api.task_store import TaskStore

    store = TaskStore(path=tmp_path / "tasks.jsonl")
    assert store.acquire_thread_resume("t9") is True
    with pytest.raises(RuntimeError):
        try:
            raise RuntimeError("astream 爆炸（模拟）")
        finally:
            store.release_thread_resume("t9")            # API gen() finally 必经
    assert store.acquire_thread_resume("t9") is True


def test_rt2_thread_owner_registry_bounded(tmp_path):
    """属主注册表 4096 上限对抗：超量绑定不得无界增长（ASK-9 同思路的内存边界）。"""
    from app.api.task_store import TaskStore

    store = TaskStore(path=tmp_path / "tasks.jsonl")
    for i in range(4200):
        store.bind_thread_owner(f"th-{i}", "a" * 64)
    assert len(store._thread_owners) <= store._MAX_THREAD_OWNERS


async def test_rt2_resume_conflict_409_leaves_no_orphan_running_task(client, monkeypatch):
    """RT2-1（红队真缺陷）：并发第二个 resume 得 409 拒绝——但 create 先于 acquire，
    409 后该任务无兜底终态，**永久滞留 running**（running 永不被 _evict_if_over
    淘汰 → 任务列表/内存索引泄漏）。修复后 409 路径必须把本次任务落 aborted 终态。"""
    s = get_settings()
    monkeypatch.setattr(s, "clarify_suspend_enabled", True)
    from app.api.task_store import get_task_store

    store = get_task_store()
    tid = f"rt2-orphan-{int(time.time() * 1000)}"
    # 第一步：制造挂起 thread
    async with client.stream(
        "POST", "/api/ask",
        json={"question": "最近卖得怎么样", "role": "boss", "thread_id": tid},
    ) as resp:
        assert resp.status_code == 200
        text = (await resp.aread()).decode()
    assert "clarify_pending" in text
    # 第二步：人为占用 resume 标记（模拟另一个 resume 在途）
    assert get_task_store().acquire_thread_resume(tid) is True
    # 第三步：第二个 resume → 409
    r = await client.post("/api/ask", json={"question": "我想看销售额", "role": "boss",
                                            "thread_id": tid, "resume": True})
    assert r.status_code == 409
    # 修复后：本次 resume 创建的任务不得滞留 running
    strays = [t for t in store.list(limit=200)
              if t.get("status") == "running" and t.get("question") == "我想看销售额"]
    assert not strays, f"409 拒绝路径不得孤儿 running 任务: {strays}"
    get_task_store().release_thread_resume(tid)
    monkeypatch.setattr(s, "clarify_suspend_enabled", False)


async def test_rt2_after_conflict_cleanup_retry_resume_succeeds(client, monkeypatch):
    """RT2-1 修复配套：409 冲突清理后，等在途 resume 结束再重试仍可正常恢复出数
    （清理不得破坏 thread 状态或 resume 标记）。"""
    s = get_settings()
    monkeypatch.setattr(s, "clarify_suspend_enabled", True)
    from app.api.task_store import get_task_store

    tid = f"rt2-retry-{int(time.time() * 1000)}"
    async with client.stream(
        "POST", "/api/ask",
        json={"question": "最近卖得怎么样", "role": "boss", "thread_id": tid},
    ) as resp:
        assert "clarify_pending" in (await resp.aread()).decode()
    r1 = await client.post("/api/ask", json={"question": "我想看销售额", "role": "boss",
                                             "thread_id": tid, "resume": True})
    assert r1.status_code == 200                       # 第一次 resume 正常完成并释放
    r2 = await client.post("/api/ask", json={"question": "再看看毛利率", "role": "boss",
                                             "thread_id": tid, "resume": True})
    assert r2.status_code == 409                       # 已无挂起可恢复（快照检查）
    strays = [t for t in get_task_store().list(limit=200)
              if t.get("status") == "running" and t.get("question") == "再看看毛利率"]
    assert not strays, "无挂起可恢复的 409 同样不得孤儿 running 任务"
    monkeypatch.setattr(s, "clarify_suspend_enabled", False)


async def test_rt2_finish_if_running_cleans_orphan(tmp_path):
    """终态兜底单元：running 孤儿经 finish_if_running 落 aborted（修复用原语的可执行证据）。"""
    from app.api.task_store import TaskStore

    store = TaskStore(path=tmp_path / "tasks.jsonl")
    t = store.create("orphan", "boss")
    assert t["status"] == "running"
    ok = store.finish_if_running(t["id"], {"kind": "aborted", "message": "清理"}, {}, [], 1.0)
    assert ok is True
    assert store.get(t["id"])["kind"] == "aborted"
    assert store.finish_if_running(t["id"], {"kind": "answer"}, {}, [], 1.0) is False  # 已终态 no-op


# ===========================================================================
# 4. 深度参数滥用计费逃逸（≥9 项）
# ===========================================================================


async def test_rt2_question_length_boundary_500_422(client):
    assert (await client.post("/api/ask", json={"question": "上" * 500, "role": "boss"})).status_code == 200
    assert (await client.post("/api/ask", json={"question": "上" * 501, "role": "boss"})).status_code == 422


async def test_rt2_thread_id_overlong_rejected(client):
    """RT2-2（红队真缺陷）：thread_id 自由长度 → checkpointer 键/属主注册表/JSONL
    落盘面滥用。修复后 >200 字符一律 422。"""
    r = await client.post("/api/ask", json={"question": "上月销售额", "role": "boss",
                                            "thread_id": "T" * 300})
    assert r.status_code == 422, "thread_id 必须有长度上限"


async def test_rt2_session_id_overlong_rejected(client):
    r = await client.post("/api/ask", json={"question": "上月销售额", "role": "boss",
                                            "session_id": "S" * 300})
    assert r.status_code == 422, "session_id 必须有长度上限"


async def test_rt2_feedback_task_id_overlong_rejected(client):
    r = await client.post("/api/feedback", json={"task_id": "F" * 300, "value": "up"})
    assert r.status_code == 422, "feedback task_id 必须有长度上限"


async def test_rt2_export_task_id_overlong_rejected(client):
    r = await client.get("/api/export", params={"task_id": "E" * 300})
    assert r.status_code == 422, "export task_id 必须有长度上限"


async def test_rt2_depth_full_forced_still_bounded_usage(client):
    """depth=full 强制滥用：链路节点集有界（clarifier/sql_agent/insight/qa/finalizer），
    用量照记不逃逸，无无限循环。"""
    async with client.stream("POST", "/api/ask",
                             json={"question": "上个月的销售额是多少", "role": "boss", "depth": "full"}) as resp:
        text = (await resp.aread()).decode()
    ans = _final(text)
    assert ans["kind"] == "answer"
    from app.api.task_store import get_task_store

    detail = get_task_store().get(_task_id_of(text))
    usage = detail["usage"] or {}
    assert set(usage) <= {"clarifier", "sql_agent", "insight", "qa", "finalizer"}
    assert sum(u["prompt_tokens"] + u["completion_tokens"] for u in usage.values()) > 0


async def test_rt2_depth_light_skips_heavy_nodes_usage_lower(client):
    """depth=light 显式指定：跳过 insight/qa → 计费节点减少（深度滥用方向=升账单，
    轻量方向=省账单，两向都有界且如实记账）。"""
    async with client.stream("POST", "/api/ask",
                             json={"question": "上个月的销售额是多少", "role": "boss", "depth": "light"}) as resp:
        text = (await resp.aread()).decode()
    final = _final(text)
    assert final["kind"] == "answer"
    from app.api.task_store import get_task_store

    detail = get_task_store().get(_task_id_of(text))
    usage = detail["usage"] or {}
    assert "qa" not in usage and "insight" not in usage


async def test_rt2_sql_repair_loop_bounded_under_forced_failures(db_path, monkeypatch):
    """SQL 连败滥用（计费逃逸方向=无限修复烧 token）：修复循环受 max_sql_repairs
    硬上界约束——沙箱执行次数 ≤ 1+上限，耗尽后拒答。"""
    import app.data.sandbox as sandbox_mod

    s = get_settings()
    calls = {"n": 0}
    orig_execute = sandbox_mod.Sandbox.execute

    async def always_fail(self, sql: str):
        calls["n"] += 1
        raise RuntimeError("forced sandbox failure")

    sandbox_mod.Sandbox.execute = always_fail
    try:
        from app.agents.graph import run_ask

        st = await run_ask("上个月的销售额是多少", role="boss")
        ans = st["answer"]
        assert ans["kind"] == "refusal", "耗尽修复次数后必须拒答而非无限循环"
        assert calls["n"] == 1 + s.max_sql_repairs, f"修复循环必须精确受上界约束（{calls['n']} 次）"
    finally:
        sandbox_mod.Sandbox.execute = orig_execute


async def test_rt2_refusal_path_still_records_usage(client):
    """拒答路径不逃逸计费：枚举外取值拒答的链路，clarifier/sql_agent 用量照记。"""
    async with client.stream("POST", "/api/ask",
                             json={"question": "上月火星地区的销售额", "role": "boss"}) as resp:
        text = (await resp.aread()).decode()
    assert _final(text)["kind"] == "refusal"
    from app.api.task_store import get_task_store

    detail = get_task_store().get(_task_id_of(text))
    usage = detail["usage"] or {}
    assert "clarifier" in usage and "sql_agent" in usage


# ===========================================================================
# 5. prompt 注入二阶（audit 查询端点回显转义等，≥6 项）
# ===========================================================================


async def test_rt2_hostile_question_task_detail_json_safe(client):
    """一阶注入串作为问题入库 → 任务明细回显是 JSON 数据不是 HTML（二阶 XSS 在
    API 层不可达；前端渲染 esc() 由 round 1 静态钉死）。"""
    q = "<img src=x onerror=alert(1)> 外卖'; DROP TABLE orders;--"
    async with client.stream("POST", "/api/ask", json={"question": q, "role": "boss"}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        text = (await resp.aread()).decode()
    final = _final(text)
    assert final["kind"] in ("answer", "clarify", "refusal")

    task_id = _task_id_of(text)
    r = await client.get(f"/api/tasks/{task_id}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["question"] == q                  # 原样作为数据往返（不解释不执行）


async def test_rt2_hostile_question_tasks_jsonl_single_line_valid(client):
    """多行注入问题落 tasks.jsonl：每任务恒一行、逐行可解析（落盘层转义）。"""
    from app.api.task_store import get_task_store

    store = get_task_store()
    before = len([ln for ln in store.path.read_text(encoding="utf-8").splitlines() if ln.strip()])
    q = "line1\nline2\r\n<script>alert(1)</script>"
    async with client.stream("POST", "/api/ask", json={"question": q, "role": "boss"}) as resp:
        assert resp.status_code == 200
        _final((await resp.aread()).decode())
    lines = [ln for ln in store.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == before + 2, "多行注入问题落盘：create+finish 恒两次快照，不得伪造额外行"
    recs = [json.loads(ln) for ln in lines[-2:]]
    assert all(rec["question"] == q for rec in recs)


async def test_rt2_tasks_list_strips_heavy_fields(client):
    """任务列表不回显 answer/events/usage（大载荷不进列表面，缩小二阶注入面）。"""
    await _ask(client, "上个月的销售额是多少")
    tasks = (await client.get("/api/tasks")).json()
    assert tasks
    for t in tasks:
        assert not {"answer", "events", "usage"} & set(t)


async def test_rt2_admin_config_has_no_free_text_injection_surface(client, tmp_path, monkeypatch):
    """管理配置面只有三个布尔开关：额外字段被忽略、审计 detail 只含布尔 from→to
    ——自由文本在结构上进不了审计面。"""
    import app.audit as audit_mod
    from app.audit import AuditLog

    log = AuditLog(path=tmp_path / "audit.jsonl")
    monkeypatch.setattr(audit_mod, "_AUDIT", log)
    r = await client.post("/api/admin/config", json={
        "cache_enabled": True,
        "actor": "attacker</script>",
        "detail": {"payload": "x"},
    })
    assert r.status_code == 200
    assert set(r.json()["applied"]) == {"cache_enabled"}
    ev = log.list()[0]
    changes = ev["detail"]["changes"]
    assert set(changes) == {"cache_enabled"}
    assert changes["cache_enabled"]["from"] is False and changes["cache_enabled"]["to"] is True
    assert "attacker" not in json.dumps(ev)
    monkeypatch.setattr(get_settings(), "cache_enabled", False)


async def test_rt2_audit_echo_rtl_and_quote_payloads_never_html(client, tmp_path, monkeypatch):
    """审计回显对 RLO/引号/换行载荷：content-type 恒 JSON、载荷不入任何 HTML 上下文。"""
    import app.audit as audit_mod
    from app.audit import AuditLog

    log = AuditLog(path=tmp_path / "audit.jsonl")
    log.record("admin", "config.update", "settings", "*",
               {"changes": {"followup_enabled": {"from": "\u202er-n", "to": "</textarea>"}}})
    monkeypatch.setattr(audit_mod, "_AUDIT", log)
    r = await client.get("/api/admin/audit")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    raw = r.content.decode()
    assert not raw.lstrip().startswith("<"), "审计回显不得渲染为 HTML"
    ev = r.json()[0]
    assert ev["detail"]["changes"]["followup_enabled"]["to"] == "</textarea>"


def test_rt2_web_esc_still_covers_dynamic_sinks():
    """回归锚：二阶注入的最后防线仍是前端 esc()——round 1 的关键 sink 断言保持在场。"""
    import pathlib

    js = (pathlib.Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert "esc(msg.message)" in js
    assert "esc(a.sql)" in js
    assert "esc(a.caliber" in js
