"""[1B6] 车道 B 修复回归测试：AB-7 注入诱导 DoS 链 / AB-17 PROFILE 枚举 / AB-29 LIMIT 边界不变量。

AB-7（评审 5.0，P1）三层收口——复核实证链路：20k 合法 filter → 0.4MB SQL →
guard 同步解析 1277ms 冻结事件循环，drill_down ≤9 次 ≈11s：
  ① QueryDSL.filters 数量上界 16（客户端可达面：MCP/DSL 直传，pydantic 422）；
  ② guard 入口 64KB 字节上界（超界入口即拒 SqlRejected，消息注明上界）；
  ③ async 路径 guard 挪入 to_thread 并纳入 wait_for 超时范围——事件循环不再
    被解析段冻结（execute_sync 同步路径无事件循环，字节/条数上界已兜住主害）。

AB-17（评审 5.0，P2）：PROFILE 无枚举校验静默失效（ful/production 静默按
conservative 行为跑）——Literal["conservative","full"] 启动期 ValidationError fail-fast。

AB-29（评审 5.0，P2）：LIMIT 剥离口径的构建期不变量此前只有 docstring 人工承诺
（"评测条目本身已保证"）无程序化断言——app/eval/limit_invariant.limit_boundary_strict
落成可执行断言，三个构建脚本接线；断言口径 = 截断边界 strict 即可入库（允许无界
行数 > LIMIT，只要截断边界无并列——这正是 LIMIT 剥离口径的语义），并列即拒绝。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent.parent

# ===========================================================================
# AB-7① filters 数量上界 16
# ===========================================================================


def _n_filters(n: int) -> list[dict]:
    return [{"field": "region", "op": "=", "value": "华南"} for _ in range(n)]


def test_filters_over_16_rejected_at_dsl_layer():
    """17 个 filter 的 DSL 在校验层即拒绝（此前无上界，20k filter 直达 SQL 编译）。"""
    from app.data.dsl import build_dsl

    with pytest.raises(ValidationError):
        build_dsl({"metric": "revenue", "filters": _n_filters(17)})


def test_filters_at_16_still_accepted_compat_anchor():
    """兼容锚：16 个 filter（现有四源合法 DSL 远低于此）零影响。"""
    from app.data.dsl import build_dsl

    dsl = build_dsl({"metric": "revenue", "filters": _n_filters(16)})
    assert len(dsl.filters) == 16


# ===========================================================================
# AB-7② guard 入口 64KB 字节上界
# ===========================================================================


def test_guard_rejects_sql_over_64kb_at_entry():
    """>64KB 的 SQL 在 guard 入口即拒（消息注明上界），不做昂贵解析。

    复核实证锚：0.3MB 合法 SQL 曾被 guard 全量解析（实测 1035ms）后放行。
    """
    from app.data.sandbox import MAX_SQL_BYTES, SqlRejected, guard

    big = "SELECT 1 WHERE " + " OR ".join(["region='华南'"] * 20000)
    assert len(big.encode("utf-8")) > MAX_SQL_BYTES
    t0 = time.perf_counter()
    with pytest.raises(SqlRejected) as ei:
        guard(big)
    assert "64KB" in str(ei.value)
    assert (time.perf_counter() - t0) < 0.2, "超界 SQL 必须入口即拒，不得走解析层"


def test_guard_byte_bound_compat_anchor_normal_sql_passes():
    """兼容锚：常规 SQL（冻结金标最大几百字节）远低于 64KB，零影响。"""
    from app.data.sandbox import guard

    assert guard("SELECT region FROM orders LIMIT 3").upper().endswith("LIMIT 3")


# ===========================================================================
# AB-7③ async 路径 guard 挪入工作线程（事件循环不被解析段冻结）
# ===========================================================================


async def test_async_execute_guard_runs_off_event_loop(db_path, monkeypatch):
    """慢 guard（0.3s）期间事件循环必须保持响应——guard 挪入 to_thread 且在
    wait_for 超时范围内。修复前 guard 同步执行冻结循环 0.3s，ping 全部被推迟。"""
    from app.data import sandbox

    real_guard = sandbox.guard

    def slow_guard(sql, **kw):
        time.sleep(0.3)
        return real_guard(sql, **kw)

    monkeypatch.setattr(sandbox, "guard", slow_guard)
    s = sandbox.Sandbox(db_path=db_path)
    pings: list[float] = []

    async def ping():
        for _ in range(6):
            await asyncio.sleep(0.02)
            pings.append(time.perf_counter())

    t0 = time.perf_counter()
    exec_task = asyncio.create_task(s.execute("SELECT 1"))
    await ping()
    assert await exec_task is not None
    assert pings[0] - t0 < 0.15, "事件循环在 guard 执行期间被冻结（ping 首响应 >0.15s）"


async def test_async_execute_still_propagates_sql_rejected(db_path):
    """行为锚：guard 挪入线程后 SqlRejected 仍从 execute 正常传播（拒绝语义不变）。"""
    from app.data.sandbox import Sandbox, SqlRejected

    s = Sandbox(db_path=db_path)
    with pytest.raises(SqlRejected):
        await s.execute("DROP TABLE orders")


async def test_async_execute_guard_counted_in_timeout(db_path, monkeypatch):
    """guard 慢解析段纳入 wait_for 超时范围：超过 sql_timeout 整体抛 SqlTimeout
    （修复前 guard 在 wait_for 之外，解析段无限等待不受超时保护）。"""
    from app.data import sandbox

    real_guard = sandbox.guard

    def slow_guard(sql, **kw):
        time.sleep(0.5)
        return real_guard(sql, **kw)

    monkeypatch.setattr(sandbox, "guard", slow_guard)
    s = sandbox.Sandbox(db_path=db_path, timeout=0.1)
    with pytest.raises(sandbox.SqlTimeout):
        await s.execute("SELECT 1")


# ===========================================================================
# AB-17 PROFILE 枚举校验（Literal fail-fast）
# ===========================================================================


@pytest.mark.parametrize("bad", ["ful", "production"])
def test_profile_literal_rejects_unknown_values(monkeypatch, bad):
    """非枚举值启动期 ValidationError（此前静默失效：ful 按 conservative 跑）。"""
    from app.config import Settings

    monkeypatch.delenv("PROFILE", raising=False)
    with pytest.raises(ValidationError):
        Settings(profile=bad, llm_provider="mock")


def test_profile_env_var_rejects_unknown_value(monkeypatch):
    """环境变量入口同样 fail-fast：PROFILE=production 拒绝启动。"""
    from app.config import Settings

    monkeypatch.setenv("PROFILE", "production")
    with pytest.raises(ValidationError):
        Settings(llm_provider="mock")


# ===========================================================================
# AB-29 LIMIT 剥离口径：截断边界 strict 构建期不变量
# ===========================================================================


def _values_sql(values: list[int], lim: int) -> str:
    vals = ",".join(f"({v})" for v in values)
    return f"SELECT v FROM (VALUES {vals}) t(v) ORDER BY v DESC\nLIMIT {lim}"


def test_limit_invariant_tie_at_boundary_rejected():
    """第 lim 行与 lim+1 行排序值并列 → 拒绝（LIMIT 截断集合执行间非确定）。"""
    from app.eval.limit_invariant import limit_boundary_strict

    ok, detail = limit_boundary_strict(_values_sql([5, 4, 3, 3, 1], 3))
    assert not ok
    assert "并列" in detail


def test_limit_invariant_strict_boundary_accepted():
    """无界行数 > LIMIT 但边界 strict → 通过（这正是 LIMIT 剥离口径的语义）。"""
    from app.eval.limit_invariant import limit_boundary_strict

    ok, detail = limit_boundary_strict(_values_sql([5, 4, 3, 2, 1], 3))
    assert ok
    assert "strict" in detail


def test_limit_invariant_rows_within_limit_accepted():
    """无界行数 ≤ LIMIT：无截断，天然确定。"""
    from app.eval.limit_invariant import limit_boundary_strict

    ok, _ = limit_boundary_strict(_values_sql([5, 4], 3))
    assert ok


def test_limit_invariant_ties_above_boundary_accepted():
    """并列组整体在截断线之内 → 允许（截断集合仍确定）。"""
    from app.eval.limit_invariant import limit_boundary_strict

    ok, _ = limit_boundary_strict(_values_sql([5, 5, 5, 2, 1], 3))
    assert ok


def test_build_scripts_wire_limit_boundary_assertion():
    """三个构建脚本必须接线共享断言（构建期对每条 query 金标 fail-fast）。"""
    for script in ("build_dataset_v3_300.py", "build_multiturn_deep.py", "build_dataset_d.py"):
        src = (ROOT / "scripts" / script).read_text(encoding="utf-8")
        assert "limit_boundary_strict(" in src, f"{script} 未接线 LIMIT 边界不变量断言"


# ---- 冻结集实数据守门（DB 文件为本地构建产物，缺库跳过；重跑构建脚本可复现） ----

def _check_frozen(path: str, db_name: str, gold_path: callable):
    from app.eval.limit_invariant import limit_boundary_strict

    db = ROOT / "data" / db_name
    if not db.exists():
        pytest.skip(f"本地库不存在（{db_name}）：构建期不变量由构建脚本 --check 兜底")
    violations = []
    for entry_id, gold_sql in gold_path(path):
        ok, detail = limit_boundary_strict(gold_sql, db_path=db)
        if not ok:
            violations.append(f"{entry_id}: {detail}")
    assert not violations, f"LIMIT 边界并列（截断集合非确定）{len(violations)} 条: {violations}"


def _v3_gold(path: str):
    items = json.loads((ROOT / path).read_text(encoding="utf-8"))["items"]
    for it in items:
        if it["kind"] == "query" and it.get("gold_sql"):
            yield it["id"], it["gold_sql"]


def _multiturn_gold(path: str):
    for line in (ROOT / path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        for i, t in enumerate(d.get("turns", []), 1):
            sql = (t.get("expect") or {}).get("gold_sql")
            if sql:
                yield f"{d['id']}.t{i}", sql


def test_limit_invariant_over_frozen_v3_300_gold():
    """v3 冻结集 189 条 query 金标逐条过边界 strict 断言（含已知 top-N 条目）。"""
    _check_frozen("eval/dataset_v3_300.json", "askbi.duckdb", _v3_gold)


def test_limit_invariant_over_frozen_multiturn_gold():
    """multiturn_deep 冻结集全部 answer 轮金标过边界 strict 断言。"""
    _check_frozen("eval/multiturn_deep.jsonl", "askbi.duckdb", _multiturn_gold)


def _d_gold(path: str):
    items = json.loads((ROOT / path).read_text(encoding="utf-8"))["items"]
    for it in items:
        if it["kind"] == "query" and it.get("gold_sql"):
            yield it["id"], it["gold_sql"]


def test_limit_invariant_over_frozen_dataset_d_gold():
    """dataset_d 冻结集 18 条 query 金标（hotel 库）过边界 strict 断言。"""
    _check_frozen("eval/dataset_d.json", "askbi_d.duckdb", _d_gold)
