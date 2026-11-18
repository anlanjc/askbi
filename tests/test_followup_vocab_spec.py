"""多轮指代消解词表随激活 spec（registry）走——单一来源收口测试（[1B5]）。

背景（EVIDENCE §6 已知边界收口）：context.py 曾在模块导入期从全局
DIMENSION_VALUES（默认电商词表）构建实体词表——切 DATASET=coffee/saas/hotel
后多轮消解仍在用默认集词表（coffee 实体"那外卖呢"消解不了/ecommerce 词
"那直播呢"在 coffee 上错命中）。收口后词表每次现算自
get_spec().dimension_values ∩ spec.dimensions（可分组维度的取值域）。

红线裁判（本文件前半）：默认 ecommerce 词表与改动前硬编码快照逐项相同——
"从哪取"变了，"取到什么"没变，followup 30 条冻结集与 multiturn_deep 95 轮
的金标指纹不受影响。
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.context import _entity_vocab, is_followup, resolve_followup
from app.data.registry import get_spec, register_spec, reset_specs

# ---------------- 改动前词表的冻结快照（默认源零变化裁判） ----------------
# 旧实现（context.py 模块级 _ENTITY_TYPES）：region 剔除"全国" + channel/category/
# member_level 直取 schema.DIMENSION_VALUES——四键值序逐项如下。
LEGACY_ECOMMERCE_VOCAB: dict[str, tuple[str, ...]] = {
    "region": ("华南", "华北", "华东", "华中", "西南", "西北", "东北"),
    "channel": ("直播", "搜索", "推荐", "社交", "官网"),
    "category": ("数码", "服饰", "美妆", "家居", "食品", "运动"),
    "member_level": ("普通", "银卡", "金卡", "黑卡"),
}


@pytest.fixture
def dataset(monkeypatch):
    """进程内切换 settings.dataset（get_settings() 是 lru_cache 单例，monkeypatch 自动还原）。"""
    def _use(name: str) -> None:
        monkeypatch.setattr(get_settings(), "dataset", name)
    return _use


# ---------------- 1. 默认源零变化 ----------------


def test_default_vocab_snapshot_unchanged():
    """默认 ecommerce 词表与改动前硬编码逐项相同（dict 相等含每键值序）。"""
    assert dict(_entity_vocab()) == LEGACY_ECOMMERCE_VOCAB


def test_default_vocab_keys_are_dimension_domains_only():
    """词表只含声明了取值域的可分组维度；订单状态（已完成/已退货）不进词表。

    status 取值域存在于 dimension_values 但不是 spec.dimensions 的键——
    intersection 推导规则把它挡在词表外（与旧硬编码四键一致）。
    """
    assert set(_entity_vocab()) == {"region", "channel", "category", "member_level"}
    assert not is_followup("那已退货呢")
    resolved, note = resolve_followup(
        "那已完成呢", "上月已完成订单的销售额", enabled=True)
    assert resolved == "那已完成呢" and note == ""


def test_default_resolution_output_byte_identical():
    """默认源消解输出逐字节等于历史实现（multiturn_deep 金标指纹钉死的前提）。"""
    resolved, note = resolve_followup(
        "那华北呢", "上月华南地区的毛利率", enabled=True)
    assert resolved == "上月华北地区的毛利率"
    assert note == "指代消解：'那华北呢' → '上月华北地区的毛利率'（替换 region: 华南→华北）"


def test_resolved_question_compiles_like_direct_input():
    """消解产物与用户直接输入的完整问题走同一 DSL 语义：解析结果逐键相同、
    指纹相同、取值过 spec 校验——词表来源切换不进编译/缓存语义。"""
    from app.cache import fingerprint
    from app.data.dsl import build_dsl, validate_filter_values
    from app.data.registry import DEFAULT_SPEC
    from app.llm.mock import _parse_question

    resolved, _ = resolve_followup(
        "换成搜索渠道呢", "上月直播渠道的销售额", enabled=True)
    assert resolved == "上月搜索渠道的销售额"
    raw_resolved = _parse_question(resolved, spec=DEFAULT_SPEC)
    raw_direct = _parse_question("上月搜索渠道的销售额", spec=DEFAULT_SPEC)
    assert raw_resolved == raw_direct                     # 消解文本与直接输入同一解析
    assert {"field": "channel", "op": "=", "value": "搜索"} in raw_resolved["filters"]
    dsl = build_dsl(raw_resolved, DEFAULT_SPEC)
    assert validate_filter_values(dsl, DEFAULT_SPEC) == []
    assert fingerprint(raw_resolved) == fingerprint(raw_direct)


# ---------------- 2. 切源后词表随激活 spec ----------------


def test_coffee_vocab_from_spec(dataset):
    """DATASET=coffee：词表=coffee spec 的可分组维度取值域，外卖/堂食可消解。"""
    dataset("coffee")
    spec = get_spec("coffee")
    assert set(_entity_vocab()) == set(spec.dimensions) & set(spec.dimension_values)
    assert _entity_vocab()["channel"] == ("堂食", "外带", "外卖")
    assert _entity_vocab()["category"] == ("饮品", "烘焙")
    resolved, note = resolve_followup(
        "那外卖呢", "上月堂食渠道的销售额", enabled=True)
    assert resolved == "上月外卖渠道的销售额"
    assert "堂食→外卖" in note


def test_saas_vocab_from_spec(dataset):
    """DATASET=saas：tier/industry 进词表（默认集词表没有的维度类型）。"""
    dataset("saas")
    vocab = _entity_vocab()
    assert vocab["tier"] == ("基础版", "专业版", "旗舰版")
    assert vocab["industry"] == ("制造", "互联网", "零售", "金融", "教育")
    resolved, note = resolve_followup(
        "那旗舰版呢", "上月专业版的MRR", enabled=True)
    assert resolved == "上月旗舰版的MRR"
    assert "专业版→旗舰版" in note


def test_hotel_vocab_from_spec(dataset):
    """DATASET=hotel：店型/房型进词表，"那度假呢"消解换店型。"""
    dataset("hotel")
    vocab = _entity_vocab()
    assert vocab["hotel_type"] == ("商务", "度假", "精品")
    assert vocab["room_type"] == ("大床", "双床", "套房", "亲子")
    resolved, _ = resolve_followup(
        "那度假呢", "上月商务店型的间夜数", enabled=True)
    assert resolved == "上月度假店型的间夜数"


def test_channel_suffix_rule_holds_on_coffee(dataset):
    """通用后缀剥离（AB-2 重复后缀修复）跨源有效：换入词根不留双后缀。"""
    dataset("coffee")
    resolved, _ = resolve_followup(
        "换成外卖渠道呢", "上月堂食渠道的销售额", enabled=True)
    assert resolved == "上月外卖渠道的销售额"


def test_region_shared_across_sources_by_design(dataset):
    """region 七大区是组织级概念（ADR：ROLE_SCOPES 全局约定，四源同一划分）——
    coffee 下"那华北呢"仍消解是设计语义，不是默认词表泄漏（coffee spec 自带同划分）。"""
    dataset("coffee")
    assert "华北" in _entity_vocab()["region"]
    resolved, _ = resolve_followup(
        "那华北呢", "上月华南门店的销售额", enabled=True)
    assert resolved == "上月华北门店的销售额"


# ---------------- 3. 跨源词表隔离 ----------------


def test_coffee_words_do_not_resolve_on_default():
    """coffee 专属词在默认源不进追问（is_followup 即判否，保守回退闲聊语义）。"""
    assert not is_followup("那外卖呢")
    assert not is_followup("换成烘焙品类呢")
    resolved, note = resolve_followup(
        "那外卖呢", "上月直播渠道的销售额", enabled=True)
    assert resolved == "那外卖呢" and note == ""


def test_default_words_do_not_resolve_on_coffee(dataset):
    """ecommerce 专属词在 coffee 上同样不命中——隔离是双向的。"""
    dataset("coffee")
    assert not is_followup("那黑卡呢")       # member_level 维度 coffee spec 未声明
    assert not is_followup("那直播呢")
    resolved, note = resolve_followup(
        "那直播呢", "上月外卖渠道的销售额", enabled=True)
    assert resolved == "那直播呢" and note == ""


# ---------------- 4. 机制保障：现算即时生效 / 缓存指纹解耦 ----------------


def test_vocab_follows_registered_variant_immediately(dataset):
    """测试注册 replace 变体后词表即时跟随——每调用现算，无缓存失效面。"""
    from dataclasses import replace

    from app.data.dataset_b import COFFEE_SPEC

    variant = replace(COFFEE_SPEC, dimension_values={
        "region": ["华南", "华北", "华东", "华中", "西南", "西北", "东北"],
        "category": ["饮品", "烘焙"],
        "channel": ["堂食", "自提"],
    })
    register_spec(variant)
    try:
        dataset("coffee")
        assert _entity_vocab()["channel"] == ("堂食", "自提")
        resolved, _ = resolve_followup(
            "那自提呢", "上月堂食渠道的销售额", enabled=True)
        assert resolved == "上月自提渠道的销售额"
    finally:
        reset_specs()


def test_cache_fingerprint_decoupled_from_active_vocab(dataset):
    """词表只影响问题改写文本，不进 DSL 指纹：显式 dataset 参数的指纹与
    settings.dataset（激活词表来源）解耦——切源不制造缓存全量 miss。"""
    from app.cache import fingerprint

    dsl = {
        "intent": "single",
        "metric": "revenue",
        "dimensions": ["region"],
        "filters": [{"field": "region", "op": "=", "value": "华北"}],
        "time": {"mode": "last_n_months", "n": 3},
    }
    fp_default = fingerprint(dsl, "boss", "ecommerce")
    dataset("coffee")   # 激活词表已切 coffee；显式 dataset 的指纹不变
    assert fingerprint(dsl, "boss", "ecommerce") == fp_default
    dataset("ecommerce")
    assert fingerprint(dsl, "boss", "ecommerce") == fp_default
