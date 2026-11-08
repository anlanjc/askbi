"""AB-HR2 thread 属主隔离测试：开关关=存量行为零变化；开=绑定+403 全路径+属主正常。

红线：开关关时存量测试断言一个不改——"关"用例只断言既有契约；
"开"用例全部走 OWNER_CHECK_ENABLED flag（monkeypatch 自动还原，不污染其他测试）。
设计记录：docs/owner_isolation_adr.md（含被否方案与剩余面）。
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.config import get_settings


@pytest.fixture
def client(db_path):
    return TestClient(app)


def _h(token: str) -> dict:
    return {"X-Owner-Token": token}


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _final_frame(text: str) -> dict:
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                frame = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if frame.get("type") == "final":
                return frame
    raise AssertionError(f"未收到 final 帧: {text[:200]}")


# ---------------- 开关关（默认）：存量行为逐字零变化 ----------------

def test_disabled_roundtrip_without_any_token(client):
    """无 token 走完 问→明细→反馈→导出→列表 全链（存量契约不变）。"""
    r = client.post("/api/ask", json={"question": "上月销售额", "role": "boss"})
    assert r.status_code == 200
    frame = _final_frame(r.text)
    assert frame["answer"]["kind"] == "answer"
    tid = frame["task_id"]

    detail = client.get(f"/api/tasks/{tid}")
    assert detail.status_code == 200
    assert "owner" not in detail.json()              # 关：任务记录不出现 owner 键

    fb = client.post("/api/feedback", json={"task_id": tid, "value": "up"})
    assert fb.status_code == 200
    ex = client.get(f"/api/export?task_id={tid}")
    assert ex.status_code == 200
    tasks = client.get("/api/tasks")
    assert tasks.status_code == 200
    # tasks.jsonl 跨 pytest run 持久化（store 单例回放）——只断言本测试新建的任务
    mine = next(t for t in tasks.json() if t["id"] == tid)
    assert "owner" not in mine


def test_disabled_resume_still_works(client, monkeypatch):
    """关：挂起+恢复全链与属主无关（既有行为零变化）。"""
    monkeypatch.setattr(get_settings(), "clarify_suspend_enabled", True)
    r1 = client.post("/api/ask", json={"question": "最近卖得怎么样", "thread_id": "t-off"})
    assert r1.status_code == 200 and "clarify_pending" in r1.text
    r2 = client.post("/api/ask", json={"question": "我想看销售额",
                                       "thread_id": "t-off", "resume": True})
    assert r2.status_code == 200
    assert '"kind": "answer"' in r2.text


# ---------------- 开关开：绑定 / 401 / 403 全路径 ----------------

@pytest.fixture
def owner_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "owner_check_enabled", True)


def test_enabled_create_requires_token(client, owner_on):
    r = client.post("/api/ask", json={"question": "上月销售额", "role": "boss"})
    assert r.status_code == 401


def test_enabled_bind_sha256_and_owner_paths_ok(client, owner_on):
    """属主正常路径：创建绑定 SHA-256（明文不落盘），明细/反馈/导出全通。"""
    r = client.post("/api/ask", json={"question": "上月销售额", "role": "boss"},
                    headers=_h("alice-token"))
    assert r.status_code == 200
    tid = _final_frame(r.text)["task_id"]

    detail = client.get(f"/api/tasks/{tid}", headers=_h("alice-token"))
    assert detail.status_code == 200
    assert detail.json()["owner"] == _sha("alice-token")
    assert "alice-token" not in detail.text          # 明文 token 不落盘

    fb = client.post("/api/feedback", json={"task_id": tid, "value": "up"},
                     headers=_h("alice-token"))
    assert fb.status_code == 200
    ex = client.get(f"/api/export?task_id={tid}", headers=_h("alice-token"))
    assert ex.status_code == 200


def test_enabled_cross_owner_403_and_missing_401(client, owner_on):
    """越权全路径：他人 token → 403（明细/反馈/导出），未出示 → 401。"""
    r = client.post("/api/ask", json={"question": "上月销售额", "role": "boss"},
                    headers=_h("alice-token"))
    tid = _final_frame(r.text)["task_id"]

    assert client.get(f"/api/tasks/{tid}", headers=_h("bob-token")).status_code == 403
    assert client.get(f"/api/tasks/{tid}").status_code == 401
    assert client.post("/api/feedback", json={"task_id": tid, "value": "up"},
                       headers=_h("bob-token")).status_code == 403
    assert client.post("/api/feedback", json={"task_id": tid, "value": "up"}).status_code == 401
    assert client.get(f"/api/export?task_id={tid}", headers=_h("bob-token")).status_code == 403
    assert client.get(f"/api/export?task_id={tid}").status_code == 401


def test_enabled_list_filtered_per_owner(client, owner_on):
    """列表按属主过滤：A 只见 A 的任务；未出示 token → 401。"""
    ra = client.post("/api/ask", json={"question": "上月销售额"}, headers=_h("alice-token"))
    rb = client.post("/api/ask", json={"question": "上月销售额"}, headers=_h("bob-token"))
    tid_a = _final_frame(ra.text)["task_id"]
    tid_b = _final_frame(rb.text)["task_id"]

    la = client.get("/api/tasks", headers=_h("alice-token")).json()
    assert la and all(t.get("owner") == _sha("alice-token") for t in la)
    assert tid_a in {t["id"] for t in la}
    assert tid_b not in {t["id"] for t in la}
    lb = client.get("/api/tasks", headers=_h("bob-token")).json()
    assert tid_b in {t["id"] for t in lb} and tid_a not in {t["id"] for t in lb}
    assert client.get("/api/tasks").status_code == 401


def test_enabled_unbound_task_fail_closed(client, monkeypatch):
    """开关开启前创建的无绑定任务：任何调用方（含任意 token）→ 403 fail-closed。"""
    r = client.post("/api/ask", json={"question": "上月销售额"})   # 开关还关着
    tid = _final_frame(r.text)["task_id"]
    monkeypatch.setattr(get_settings(), "owner_check_enabled", True)

    assert client.get(f"/api/tasks/{tid}").status_code == 403
    assert client.get(f"/api/tasks/{tid}", headers=_h("anyone")).status_code == 403
    assert client.post("/api/feedback", json={"task_id": tid, "value": "up"}).status_code == 403
    assert client.get(f"/api/export?task_id={tid}").status_code == 403


# ---------------- resume 路径的属主校验 ----------------

@pytest.fixture
def suspend_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "clarify_suspend_enabled", True)


def test_resume_owner_allow_deny_and_401(client, owner_on, suspend_on):
    """挂起 thread 属主：属主恢复 200 出数；他人 403；未出示 401。"""
    r1 = client.post("/api/ask", json={"question": "最近卖得怎么样", "thread_id": "t-own"},
                     headers=_h("alice-token"))
    assert r1.status_code == 200 and "clarify_pending" in r1.text

    r_bob = client.post("/api/ask", json={"question": "我想看销售额",
                                          "thread_id": "t-own", "resume": True},
                        headers=_h("bob-token"))
    assert r_bob.status_code == 403
    r_anon = client.post("/api/ask", json={"question": "我想看销售额",
                                           "thread_id": "t-own", "resume": True})
    assert r_anon.status_code == 401
    r_ok = client.post("/api/ask", json={"question": "我想看销售额",
                                         "thread_id": "t-own", "resume": True},
                       headers=_h("alice-token"))
    assert r_ok.status_code == 200
    assert '"kind": "answer"' in r_ok.text


def test_resume_unbound_thread_fail_closed(client, monkeypatch, suspend_on):
    """挂起时属主开关还关着（无绑定）→ 之后开启，resume 一律 403（宁拒不误放）。"""
    r1 = client.post("/api/ask", json={"question": "最近卖得怎么样", "thread_id": "t-legacy"})
    assert "clarify_pending" in r1.text
    monkeypatch.setattr(get_settings(), "owner_check_enabled", True)
    r2 = client.post("/api/ask", json={"question": "我想看销售额",
                                       "thread_id": "t-legacy", "resume": True},
                     headers=_h("alice-token"))
    assert r2.status_code == 403


def test_resume_unknown_thread_fail_closed(client, owner_on, suspend_on):
    """不存在的 thread：无法证明属主 → 403（先于 ASK-7 的 409 语义，宁拒不探测）。"""
    r = client.post("/api/ask", json={"question": "我想看销售额",
                                      "thread_id": "t-ghost", "resume": True},
                    headers=_h("alice-token"))
    assert r.status_code == 403


def test_thread_binding_cannot_be_hijacked(client, owner_on, suspend_on):
    """攻击者以同 thread_id 重新提问不能改写属主绑定（setdefault 语义）。"""
    r1 = client.post("/api/ask", json={"question": "最近卖得怎么样", "thread_id": "t-hij"},
                     headers=_h("alice-token"))
    assert "clarify_pending" in r1.text
    # 攻击者用同 thread_id 发起新问题（非 resume）
    client.post("/api/ask", json={"question": "上月销售额", "thread_id": "t-hij"},
                headers=_h("bob-token"))
    # 绑定仍是 alice：bob 恢复 → 403
    r = client.post("/api/ask", json={"question": "我想看销售额",
                                      "thread_id": "t-hij", "resume": True},
                    headers=_h("bob-token"))
    assert r.status_code == 403
