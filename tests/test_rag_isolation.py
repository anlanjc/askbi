"""跨源 few-shot 污染隔离测试（S9-OPT2，AB-3 挂账）。

背景：样例库（few-shot RAG）曾是全局单库——DATASET=coffee/saas 下真 provider
的 B/C 问题会检索到电商样例，样例里的 DSL 形态把生成往电商维度带偏。

修复口径：seed 条目带 spec 标签，get_example_store(dataset) 只装载同标签样例——
任一数据集检索不到其他数据集的样例（双向）；默认数据集 ecommerce 的样例库与
改造前全局单库内容逐字节一致（同样的 10 条，检索行为零变化）。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.rag.example_store import SEED_PATH, get_example_store


def _seeds() -> list[dict]:
    return json.loads(Path(SEED_PATH).read_text(encoding="utf-8"))


def _store_questions(store) -> set[str]:
    """TfidfExampleStore 的全量文档（conftest 固定 RAG_BACKEND=tfidf）。"""
    return {q for q, _dsl in store._docs}  # 测试直读内部文档集


# ---------------- seed 文件卫生 ----------------


def test_seed_examples_explicitly_tagged_with_registered_spec():
    """每个 seed 条目必须显式带 spec 标签且标签是已注册数据集（防止静默落错库）。"""
    from app.data.registry import get_spec

    seeds = _seeds()
    assert {"ecommerce", "coffee", "saas"} <= {i["spec"] for i in seeds}
    for item in seeds:
        assert get_spec(item["spec"]).name == item["spec"], item["question"]


def test_seed_ecommerce_questions_untouched():
    """存量 10 条电商样例的 question/dsl 逐字节保留（默认路径回归裁判的锚）。"""
    legacy = [
        "上个月销售额是多少", "最近3个月各品类销售额", "上月哪个品类毛利率最低",
        "近12个月销售额趋势", "华南地区上月订单量", "各渠道最近6个月的客单价",
        "为什么上月毛利率下滑了", "上月退货率最高的前5个子类", "美妆品类近6个月每月毛利额",
        "各会员等级的购买客户数",
    ]
    eco = [i["question"] for i in _seeds() if i["spec"] == "ecommerce"]
    assert eco == legacy


# ---------------- 跨源隔离（核心回归） ----------------


def test_coffee_store_never_returns_ecommerce_examples():
    """DATASET=coffee：电商问题在 B 样例库检索不到任何电商样例。"""
    store = get_example_store("coffee")
    eco_questions = {i["question"] for i in _seeds() if i["spec"] == "ecommerce"}
    assert store.size() == len([i for i in _seeds() if i["spec"] == "coffee"]) > 0
    assert not (_store_questions(store) & eco_questions)
    for probe in eco_questions:  # 逐条电商问题探针：检索结果必须全部来自 coffee 样例
        for q, _dsl, _s in store.search(probe, k=3):
            assert q not in eco_questions, probe


def test_saas_store_never_returns_ecommerce_examples():
    """DATASET=saas：电商问题在 C 样例库检索不到任何电商样例。"""
    store = get_example_store("saas")
    eco_questions = {i["question"] for i in _seeds() if i["spec"] == "ecommerce"}
    assert store.size() == len([i for i in _seeds() if i["spec"] == "saas"]) > 0
    assert not (_store_questions(store) & eco_questions)
    for probe in eco_questions:
        for q, _dsl, _s in store.search(probe, k=3):
            assert q not in eco_questions, probe


def test_ecommerce_store_never_returns_coffee_saas_examples():
    """反之亦然：默认电商样例库不含任何 coffee/saas 样例（新增 B/C seed 不回流 A）。"""
    store = get_example_store("ecommerce")
    cross = {i["question"] for i in _seeds() if i["spec"] in ("coffee", "saas")}
    assert cross, "前置失效：seed 中应存在 coffee/saas 样例，否则本测试无判别力"
    assert not (_store_questions(store) & cross)
    for probe in cross:
        for q, _dsl, _s in store.search(probe, k=3):
            assert q not in cross, probe


# ---------------- 默认数据集行为零变化 ----------------


def test_none_resolves_to_settings_dataset():
    """get_example_store() 无参 = settings.dataset（与 get_spec 同一约定）。"""
    import types

    import app.rag.example_store as mod

    orig = mod.get_settings
    mod.get_settings = lambda: types.SimpleNamespace(dataset="coffee", rag_backend="tfidf")
    try:
        assert get_example_store() is get_example_store("coffee")
    finally:
        mod.get_settings = orig


def test_default_ecommerce_search_unchanged():
    """ecommerce 样例库 = 原 10 条，检索行为与全局单库时期逐位一致（金标 pin）。"""
    store = get_example_store("ecommerce")
    assert store.size() == 10
    top = store.search("上个月销售额是多少", k=3)
    # 分数为改造前全局单库（同样的算法、同样只有这 10 条 doc）的真实计算值
    assert [(q, s) for q, _d, s in top] == [
        ("上个月销售额是多少", 1.0),
        ("近12个月销售额趋势", 0.327),
        ("最近3个月各品类销售额", 0.211),
    ]
