"""第四数据源 hotel（连锁酒店酒旅）注册表测试（[1B4-AB] 任务二）。

与 test_registry.py（A 默认逐位不变 / B coffee / C saas）同构的第四套实证：
1. 注册表行为——get_spec("hotel")、reset 后内置仍在；
2. 编译器随 spec——hotel 物理标识符（stay_lines / b.checkin_date / h.region）出现在
   编译 SQL 与角色作用域中；跨源白名单/DSL-spec 错配在验证层被拒；
3. mock 解析器随 hotel 词表——"房费收入→room_revenue"等映射 + 默认词表不受影响；
4. 缓存指纹与版本探测随 spec——跨源不命中、探测 SQL 对他库报错；
5. 同问题全链路（DATASET=hotel）——answer/drill 两条链路落到 D 的语义层与 D 库。
"""

from __future__ import annotations

import os
import pathlib

import pytest
from pydantic import ValidationError

from app.config import get_settings
from app.data.dsl import (
    CompileError,
    QueryDSL,
    build_dsl,
    compile_sql,
    validate_filter_values,
)
from app.data.registry import get_spec, reset_specs

TEST_DB_DIR = pathlib.Path(__file__).parent / "_db"


# ---------------- 1. 注册表行为 ----------------


def test_hotel_spec_registered():
    spec = get_spec("hotel")
    assert spec.name == "hotel"
    assert spec.db_path == "data/askbi_d.duckdb"
    assert "房费收入" in {m.name for m in spec.metrics.values()}
    # reset_specs 后内置四源全部仍在（hotel 必须在 reset 重注册清单里）
    reset_specs()
    assert {get_spec(n).name for n in ("ecommerce", "coffee", "saas", "hotel")} == {
        "ecommerce", "coffee", "saas", "hotel",
    }


# ---------------- 2. 编译器随 spec ----------------


def _hotel():
    return get_spec("hotel")


def test_hotel_compile_uses_hotel_identifiers():
    spec = _hotel()
    dsl = build_dsl({"metric": "room_revenue", "time": {"mode": "last_n_months", "n": 1}}, spec)
    sql, caliber = compile_sql(dsl, spec=spec)
    assert "FROM stay_lines sl JOIN bookings b" in sql
    assert "strftime('%Y-%m', b.checkin_date)" in sql
    assert "不采集取消/未入住" in caliber
    sql_s, _ = compile_sql(dsl, role="south", spec=spec)
    assert "h.region IN ('华南')" in sql_s          # 作用域表达式随 spec（A 是 o.region）
    sql_n, _ = compile_sql(dsl, role="north", spec=spec)
    assert "h.region IN ('华北', '东北')" in sql_n


def test_hotel_whitelist_enforced_at_validation():
    spec = _hotel()
    with pytest.raises(ValidationError):            # A 独有指标在 D 不可用
        build_dsl({"metric": "refund_rate"}, spec)
    with pytest.raises(ValidationError):            # D 独有指标在默认白名单不可用
        QueryDSL(metric="room_nights")
    with pytest.raises(ValidationError):            # D 独有维度
        build_dsl({"metric": "revenue", "dimensions": ["hotel_type"]}, get_spec("ecommerce"))


def test_cross_spec_compile_rejected_for_hotel():
    """DSL 与 spec 错配（A 的 DSL 拿去 D 编译）在编译期被 CompileError 拦截。"""
    dsl_a = QueryDSL(metric="refund_rate")          # 合法（默认白名单）
    with pytest.raises(CompileError):
        compile_sql(dsl_a, spec=_hotel())


def test_hotel_filter_values_validated():
    spec = _hotel()
    ok = build_dsl({"metric": "room_revenue", "filters": [{"field": "channel", "op": "=", "value": "旅行社"}]}, spec)
    assert validate_filter_values(ok, spec) == []
    bad = build_dsl({"metric": "room_revenue", "filters": [{"field": "channel", "op": "=", "value": "直播"}]}, spec)
    problems = validate_filter_values(bad, spec)
    assert problems and "旅行社" in problems[0]       # 提示可选值来自 spec D


# ---------------- 3. mock 解析器随 hotel 词表 ----------------


def test_mock_parser_uses_hotel_vocab():
    from app.llm.mock import _parse_question

    spec = _hotel()
    d = _parse_question("旅行社渠道上月房费收入", spec)
    assert d["metric"] == "room_revenue"
    assert {"field": "channel", "op": "=", "value": "旅行社"} in d["filters"]
    d2 = _parse_question("上月间夜数有多少", spec)
    assert d2["metric"] == "room_nights"
    d3 = _parse_question("度假店型上月毛利额", spec)
    assert {"field": "hotel_type", "op": "=", "value": "度假"} in d3["filters"]
    # 默认词表不受影响（美妆是 A 的取值，D 无此值）
    d4 = _parse_question("美妆品类近6个月每月毛利额")
    assert {"field": "category", "op": "=", "value": "美妆"} in d4["filters"]


# ---------------- 4. 缓存指纹与版本探测随 spec ----------------


def test_cache_fingerprint_hotel_isolation():
    """同 DSL 不同数据源不可跨缓存：数据集标识参与指纹（hotel 与 A 互不命中）。"""
    from app.cache import QueryCache, fingerprint

    dsl = {"metric": "room_revenue", "dimensions": ["channel"], "time": {"mode": "last_n_months", "n": 1}}
    # 同一份 DSL 字面量在 A 白名单下不可编译，但指纹只消费规范化字段——跨源键必异
    ka = fingerprint(dsl, "boss", "ecommerce")
    kd = fingerprint(dsl, "boss", "hotel")
    assert ka != kd
    assert kd == fingerprint(dsl, "boss", "hotel")   # 同源稳定
    c = QueryCache(enabled=True, version_provider=lambda: "v1")
    c.set(kd, {"columns": ["value"], "rows": [[1]], "row_count": 1})
    assert c.get(ka) is None                         # 跨源不命中
    assert c.get(kd) is not None


def test_data_version_uses_hotel_probe(db_path_d):
    """版本探测 SQL 随数据集走：对 D 库跑 A 的探测 SQL 会直接报错。"""
    import duckdb

    from app.cache import data_version

    spec = _hotel()
    v = data_version(db_path_d, spec)
    assert v.count("|") == 3
    with pytest.raises(duckdb.CatalogException):     # A 的探测 SQL 对 D 库不可用
        data_version(db_path_d)                      # 默认 spec 探测 orders 表


# ---------------- 5. 同问题全链路（DATASET=hotel） ----------------


@pytest.fixture(scope="module")
def db_path_d():
    p = TEST_DB_DIR / "test_d.duckdb"
    if not p.exists():
        from app.data.generator_d import generate_d

        generate_d(p, seed=42, months=12, end_month=get_settings().generator_end_month)
    return p


@pytest.fixture
def hotel_env(db_path_d):
    """进程内切到数据集 D（测试库路径），结束后恢复默认。"""
    from dataclasses import replace

    import app.data.registry as reg
    from app.data.dataset_d import HOTEL_SPEC

    reg.register_spec(replace(HOTEL_SPEC, db_path=str(db_path_d)))
    os.environ["DATASET"] = "hotel"
    get_settings.cache_clear()
    yield
    os.environ.pop("DATASET", None)
    get_settings.cache_clear()
    reg.reset_specs()


@pytest.mark.asyncio
async def test_full_chain_hotel(hotel_env):
    """DATASET=hotel 时同一条链路落到 D 的语义层与 D 库。"""
    from app.agents.graph import run_ask

    st = await run_ask("上个月的房费收入是多少", role="boss")
    assert st["answer"]["kind"] == "answer"
    sql = st["answer"]["sql"]
    assert "b.checkin_date" in sql and "stay_lines" in sql
    assert st["answer"]["table"]["rows"], "D 库上月应有房费收入"
    # 轻量链路 summary 引用的是 D 的指标口径行
    assert "不采集取消/未入住" in st["answer"]["caliber"]


@pytest.mark.asyncio
async def test_full_chain_hotel_drill(hotel_env):
    """D 上归因链路（full 图）走通：drill_down 按 spec 的库与分解维度计算。"""
    from app.agents.graph import run_ask

    st = await run_ask("为什么上月旅行社渠道房费收入增长", role="boss")
    ans = st["answer"]
    assert ans["kind"] == "answer"
    assert ans.get("narrative"), "归因叙述不应为空"
    assert (ans.get("qa") or {}).get("passed"), "QA 数值核验应通过"
    facts = st.get("facts") or {}
    assert facts.get("metric") == "房费收入"
    dims = {b["dimension"] for b in facts.get("维度分解", [])}
    assert "channel" not in dims          # 渠道已被过滤，分解只剩店型/大区
    assert {"hotel_type", "region"} <= dims
