"""多数据源注册表测试：语义层解耦的实证（AB-3）。

三层断言：
1. 默认数据集逐位不变——DEFAULT_SPEC 引用原对象、prompt 逐字节一致、编译输出含原物理标识符；
2. spec B（coffee）编译器单测——白名单/取值校验/跨源拒编译按 spec 生效；
3. 同问题双数据源全链路——编译结果不同、schema prompt 不同、缓存互不命中。
"""

from __future__ import annotations

import os
import pathlib

import pytest
from pydantic import ValidationError

from app.config import get_settings
from app.data.dsl import CompileError, QueryDSL, TimeRange, build_dsl, compile_sql, validate_filter_values
from app.data.metrics import METRICS
from app.data.registry import DEFAULT_SPEC, get_spec, register_spec, reset_specs
from app.data.schema import BASE_FROM, DIMENSIONS, DIMENSION_VALUES, FILTER_FIELDS, schema_prompt

TEST_DB_DIR = pathlib.Path(__file__).parent / "_db"


# ---------------- 1. 默认数据集：逐位不变 ----------------


def test_default_spec_shares_original_objects():
    """DEFAULT_SPEC 不是复制品：直接引用 schema.py/metrics.py 的同一批常量对象。"""
    assert DEFAULT_SPEC.base_from is BASE_FROM
    assert DEFAULT_SPEC.dimensions is DIMENSIONS
    assert DEFAULT_SPEC.filter_fields is FILTER_FIELDS
    assert DEFAULT_SPEC.dimension_values is DIMENSION_VALUES
    assert DEFAULT_SPEC.metrics is METRICS


def test_default_schema_prompt_bit_identical():
    """默认 prompt 与硬编码时期逐字节一致（回归裁判之一）。"""
    expected = (
        "数据集：某电商公司 24 个月的交易明细（orders / order_items / products / customers 逻辑视图）。\n"
        "可用维度：\n"
        "  - month\n  - category\n  - subcategory\n  - region\n  - province\n  - city\n"
        "  - channel\n  - member_level\n"
        "维度取值（过滤值必须取自这里，取值之外一律拒绝）：\n"
        "  - region: 华南/华北/华东/华中/西南/西北/东北\n"
        "  - category: 数码/服饰/美妆/家居/食品/运动\n"
        "  - channel: 直播/搜索/推荐/社交/官网\n"
        "  - member_level: 普通/银卡/金卡/黑卡\n"
        "  - status: 已完成/已退货/已取消\n"
        "可用时间范围：2024-10 至 2026-09（按月）。数据截止月为 2026-09；"
        "相对时间（上月/本月/最近N个月）一律以 2026-09 为基准（上月=本月=2026-09），"
        "这是系统约定，无需向用户澄清。\n"
        "指标见指标字典（metrics）。不支持的字段/指标一律拒绝，不允许编造。"
    )
    assert schema_prompt() == expected
    assert schema_prompt(DEFAULT_SPEC) == expected


def test_default_compile_uses_original_identifiers():
    dsl = QueryDSL(metric="revenue", time=TimeRange(mode="last_n_months", n=1))
    sql, _ = compile_sql(dsl)
    assert "FROM orders o JOIN order_items oi" in sql
    assert "strftime('%Y-%m', o.order_date)" in sql
    assert "DATE '2026-09-01'" in sql
    sql_s, _ = compile_sql(dsl, role="south")
    assert "o.region IN ('华南')" in sql_s


# ---------------- 2. 注册表行为 ----------------


def test_get_spec_default_follows_settings_and_unknown_raises():
    assert get_spec().name == "ecommerce"          # settings.dataset 默认
    assert get_spec("coffee").name == "coffee"
    with pytest.raises(KeyError):
        get_spec("no_such_dataset")


def test_register_and_reset_specs():
    from dataclasses import replace

    variant = replace(DEFAULT_SPEC, name="test_ds")
    register_spec(variant)
    assert get_spec("test_ds") is variant
    reset_specs()
    with pytest.raises(KeyError):
        get_spec("test_ds")
    assert get_spec("ecommerce").name == "ecommerce"   # 内置仍在


# ---------------- 3. spec B（coffee）编译器 ----------------


def _coffee():
    return get_spec("coffee")


def test_coffee_compile_differs_from_default():
    spec = _coffee()
    dsl = build_dsl({"metric": "revenue", "time": {"mode": "last_n_months", "n": 1}}, spec)
    sql, caliber = compile_sql(dsl, spec=spec)
    assert "FROM ticket_lines tl JOIN tickets t" in sql
    assert "strftime('%Y-%m', t.sale_date)" in sql
    assert "销售额" in caliber and "不采集退款" in caliber
    sql_s, cal_s = compile_sql(dsl, role="south", spec=spec)
    assert "s.region IN ('华南')" in sql_s          # 作用域表达式随 spec（A 是 o.region）


def test_coffee_whitelist_enforced_at_validation():
    spec = _coffee()
    with pytest.raises(ValidationError):            # A 独有指标在 B 不可用
        build_dsl({"metric": "refund_rate"}, spec)
    with pytest.raises(ValidationError):            # B 独有指标在默认白名单不可用
        QueryDSL(metric="member_count")
    with pytest.raises(ValidationError):            # B 独有维度
        build_dsl({"metric": "revenue", "dimensions": ["store"]}, DEFAULT_SPEC)


def test_cross_spec_compile_rejected():
    """DSL 与 spec 错配（A 的 DSL 拿去 B 编译）在编译期被 CompileError 拦截。"""
    spec = _coffee()
    dsl_a = QueryDSL(metric="refund_rate")          # 合法（默认白名单）
    with pytest.raises(CompileError):
        compile_sql(dsl_a, spec=spec)


def test_coffee_filter_values_validated():
    spec = _coffee()
    ok = build_dsl({"metric": "revenue", "filters": [{"field": "channel", "op": "=", "value": "外卖"}]}, spec)
    assert validate_filter_values(ok, spec) == []
    bad = build_dsl({"metric": "revenue", "filters": [{"field": "channel", "op": "=", "value": "直播"}]}, spec)
    problems = validate_filter_values(bad, spec)
    assert problems and "外卖" in problems[0]        # 提示可选值来自 spec B


# ---------------- 4. mock 解析器与缓存 ----------------


def test_mock_parser_uses_spec_vocab():
    from app.llm.mock import _parse_question

    spec = _coffee()
    d = _parse_question("外卖渠道上月销售额", spec)
    assert d["metric"] == "revenue"
    assert {"field": "channel", "op": "=", "value": "外卖"} in d["filters"]
    d2 = _parse_question("上月消费会员数是多少", spec)
    assert d2["metric"] == "member_count"
    # 默认词表不受影响（美妆是 A 的取值）
    d3 = _parse_question("美妆品类近6个月每月毛利额")
    assert {"field": "category", "op": "=", "value": "美妆"} in d3["filters"]


def test_cache_fingerprint_dataset_isolation():
    """同 DSL 不同数据源不可跨缓存：数据集标识参与指纹（红线要求）。"""
    from app.cache import QueryCache, fingerprint

    dsl = {"metric": "revenue", "dimensions": ["channel"], "time": {"mode": "last_n_months", "n": 1}}
    ka = fingerprint(dsl, "boss", "ecommerce")
    kb = fingerprint(dsl, "boss", "coffee")
    assert ka != kb
    assert ka == fingerprint(dsl, "boss", "ecommerce")          # 同源稳定
    assert fingerprint(dsl, "south", "ecommerce") != ka         # role 依旧参与
    c = QueryCache(enabled=True, version_provider=lambda: "v1")
    c.set(ka, {"columns": ["value"], "rows": [[1]], "row_count": 1})
    assert c.get(kb) is None                                    # 跨源不命中
    assert c.get(ka) is not None


def test_data_version_uses_spec_probe(db_path_b):
    """版本探测 SQL 随数据集走：对 B 库跑 A 的探测 SQL 会直接报错。"""
    import duckdb

    from app.cache import data_version

    spec = _coffee()
    v = data_version(db_path_b, spec)
    assert v.count("|") == 3
    with pytest.raises(duckdb.CatalogException):                # A 的探测 SQL 对 B 库不可用
        data_version(db_path_b)                                 # 默认 spec 探测 orders 表


# ---------------- 5. 同问题双数据源全链路 ----------------


@pytest.fixture(scope="module")
def db_path_b():
    p = TEST_DB_DIR / "test_b.duckdb"
    if not p.exists():
        from app.data.generator_b import generate_b

        generate_b(p, seed=42, months=12, end_month=get_settings().generator_end_month)
    return p


@pytest.fixture
def coffee_env(db_path_b):
    """进程内切到数据集 B（测试库路径），结束后恢复默认。"""
    from dataclasses import replace

    import app.data.registry as reg
    from app.data.dataset_b import COFFEE_SPEC

    reg.register_spec(replace(COFFEE_SPEC, db_path=str(db_path_b)))
    os.environ["DATASET"] = "coffee"
    get_settings.cache_clear()
    yield
    os.environ.pop("DATASET", None)
    get_settings.cache_clear()
    reg.reset_specs()


@pytest.mark.asyncio
async def test_full_chain_default_dataset(db_path):
    """同问题默认数据源（A）侧锚点：电商物理表与时间字段出现在编译 SQL 中。"""
    from app.agents.graph import run_ask

    st_a = await run_ask("上个月的销售额是多少", role="boss")
    assert st_a["answer"]["kind"] == "answer"
    sql_a = st_a["answer"]["sql"]
    assert "o.order_date" in sql_a
    assert "order_items" in sql_a


@pytest.mark.asyncio
async def test_full_chain_coffee(coffee_env):
    """DATASET=coffee 时同一条链路落到 B 的语义层与 B 库。"""
    from app.agents.graph import run_ask

    st = await run_ask("上个月的销售额是多少", role="boss")
    assert st["answer"]["kind"] == "answer"
    sql = st["answer"]["sql"]
    assert "t.sale_date" in sql and "ticket_lines" in sql
    assert st["answer"]["table"]["rows"], "B 库上月应有销售额"
    # 轻量链路 summary 引用的是 B 的指标名（同为"销售额"，校验口径行来自 B）
    assert "不采集退款" in st["answer"]["caliber"]


@pytest.mark.asyncio
async def test_full_chain_coffee_drill(coffee_env):
    """B 上归因链路（full 图）走通：drill_down 按 spec 的库与分解维度计算。"""
    from app.agents.graph import run_ask

    st = await run_ask("为什么上月外卖渠道销售额增长", role="boss")
    ans = st["answer"]
    assert ans["kind"] == "answer"
    assert ans.get("narrative"), "归因叙述不应为空"
    assert (ans.get("qa") or {}).get("passed"), "QA 数值核验应通过"
    facts = st.get("facts") or {}
    assert facts.get("metric") == "销售额"
    dims = {b["dimension"] for b in facts.get("维度分解", [])}
    assert "channel" not in dims          # 渠道已被过滤，分解只剩品类/大区
    assert {"category", "region"} <= dims
