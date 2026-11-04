"""管理面审计日志测试：同窗口写、审计失败回滚、admin token 门禁。

对齐 agentgate admin 审计模式的三个主张：
- 结构化 JSONL（actor/action/target_type/target_id/detail）；
- "同事务"语义：变更与审计同锁窗口落盘，审计失败 → 变更回滚；
- 查询端点只读、最新在前。
"""

import json

import httpx
import pytest

from app.api.main import app
from app.api.task_store import TaskStore
from app.audit import AuditLog
from app.cache import QueryCache, get_query_cache
from app.config import get_settings

TOGGLES = ("cache_enabled", "followup_enabled", "clarify_suspend_enabled", "admin_token")


@pytest.fixture
async def env(tmp_path, monkeypatch):
    """隔离的 TaskStore/AuditLog 单例 + 设置快照恢复（get_settings 是会话级单例）。"""
    store = TaskStore(path=tmp_path / "tasks.jsonl")
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    monkeypatch.setattr("app.api.task_store._STORE", store)
    monkeypatch.setattr("app.audit._AUDIT", audit)
    s = get_settings()
    saved = {k: getattr(s, k) for k in TOGGLES}
    yield {"tmp": tmp_path, "settings": s, "store": store, "audit": audit}
    for k, v in saved.items():
        setattr(s, k, v)


@pytest.fixture
async def client(env):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_audit_record_and_list_roundtrip(env):
    audit = env["audit"]
    audit.record("admin", "task.clear", "task_store", "*", {"removed": 1})
    audit.record("admin", "config.update", "settings", "*", {"changes": {}})
    events = audit.list()
    assert [e["action"] for e in events] == ["config.update", "task.clear"]  # 最新在前
    assert events[0]["actor"] == "admin"
    assert set(events[0]) == {"ts", "actor", "action", "target_type", "target_id", "detail"}
    assert audit.list(limit=1)[0]["action"] == "config.update"
    lines = env["tmp"].joinpath("audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # 追加式 JSONL：一行一事件


async def test_tasks_clear_audited_and_archived(client, env):
    store = env["store"]
    store.create("上月销售额", "boss")
    store.create("上月客单价", "south")

    r = await client.post("/api/admin/tasks/clear")
    assert r.status_code == 200
    body = r.json()
    assert body["removed"] == 2
    assert body["archived_to"].startswith("tasks.jsonl.bak-")

    assert store.list() == []                     # 内存索引清空
    assert not (env["tmp"] / "tasks.jsonl").exists()   # 原文件已改名
    archived = env["tmp"] / body["archived_to"]
    replayed = [json.loads(x) for x in archived.read_text(encoding="utf-8").splitlines()]
    assert [t["question"] for t in replayed] == ["上月销售额", "上月客单价"]  # 归档可回放

    events = env["audit"].list()
    assert events[0]["action"] == "task.clear"
    assert events[0]["detail"] == {"removed": 2, "archived_to": body["archived_to"]}


async def test_config_update_audited_and_applied(client, env):
    s = env["settings"]
    assert s.cache_enabled is False

    r = await client.post("/api/admin/config", json={"cache_enabled": True})
    assert r.status_code == 200
    assert r.json()["applied"] == {"cache_enabled": True}
    assert s.cache_enabled is True                # 运行期生效（下一次请求即读到）

    event = env["audit"].list()[0]
    assert event["action"] == "config.update"
    assert event["detail"]["changes"] == {"cache_enabled": {"from": False, "to": True}}

    r = await client.post("/api/admin/config", json={})
    assert r.status_code == 422                   # 空变更拒绝
    r = await client.post("/api/admin/config", json={"cache_enabled": None})
    assert r.status_code == 422                   # 显式 null 视为未提供


async def test_cache_clear_audited(client, env, db_path):
    """配置变更 → 存入缓存 → 清空：全链路管理操作逐笔入审计。"""
    s = env["settings"]
    s.cache_enabled = True
    cache = get_query_cache()
    cache.set("fp-1", {"columns": ["region"], "rows": [["华南"]]})

    r = await client.post("/api/admin/cache/clear")
    assert r.status_code == 200
    assert r.json()["removed"] == 1
    assert r.json()["enabled"] is True
    assert cache.stats()["size"] == 0

    actions = [e["action"] for e in env["audit"].list()]
    assert actions == ["cache.clear"]             # 最新在前；本用例只产生这一笔审计


async def test_task_clear_rolls_back_when_audit_write_fails(client, env, monkeypatch):
    store = env["store"]
    store.create("上月销售额", "boss")
    store.create("上月客单价", "boss")

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(env["audit"], "record", boom)
    r = await client.post("/api/admin/tasks/clear")
    assert r.status_code == 500

    assert len(store.list()) == 2                 # 变更回滚：内存索引恢复
    lines = (env["tmp"] / "tasks.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2                        # 原文件恢复（归档改名回去）
    archives = list(env["tmp"].glob("tasks.jsonl.bak-*"))
    assert archives == []                         # 不留半状态
    assert env["audit"].list() == []              # 审计零写入（"记了但没改"同样关死）


async def test_cache_clear_rolls_back_and_preserves_lru_order():
    calls: list[dict] = []

    def failing_audit(info):
        calls.append(info)
        raise OSError("disk full")

    cache = QueryCache(enabled=True, version_provider=lambda: "v1", clock=lambda: 0.0)
    cache.set("k1", {"rows": [1]})
    cache.set("k2", {"rows": [2]})
    cache.get("k1")                               # k1 移到 LRU 尾部
    order_before = list(cache._entries.keys())

    with pytest.raises(OSError):
        cache.clear(audit=failing_audit)
    assert cache.stats()["size"] == 2
    assert list(cache._entries.keys()) == order_before   # 条目与 LRU 顺序都恢复

    cache.clear(audit=calls.append)               # 正常路径：审计拿到 removed=2
    assert calls[-1] == {"removed": 2}
    assert cache.stats()["size"] == 0


async def test_admin_token_gate(client, env):
    env["settings"].admin_token = "t0k3n"

    r = await client.post("/api/admin/tasks/clear")
    assert r.status_code == 401
    r = await client.post("/api/admin/tasks/clear", headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 401
    r = await client.get("/api/admin/audit")
    assert r.status_code == 401                   # 查询端点同样门禁

    r = await client.post("/api/admin/tasks/clear", headers={"X-Admin-Token": "t0k3n"})
    assert r.status_code == 200
    r = await client.get("/api/admin/audit", headers={"X-Admin-Token": "t0k3n"})
    assert r.status_code == 200
    assert [e["action"] for e in r.json()] == ["task.clear"]


async def test_admin_audit_listing_endpoint(client, env):
    await client.post("/api/admin/config", json={"followup_enabled": True})
    r = await client.get("/api/admin/audit")
    assert r.status_code == 200
    events = r.json()
    assert [e["action"] for e in events] == ["config.update"]
    assert events[0]["detail"]["changes"]["followup_enabled"]["to"] is True
