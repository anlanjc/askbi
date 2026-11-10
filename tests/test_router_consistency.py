"""router 文案同源校验（S9-OPT2）：AB-HR4 教训制度化。

AB-HR4（0221df3）：route_reason 文案宣称「归因」语义走 full，heavy 正则却漏该词——
文案与实现两处手维护，漂移只有靠评测撞上才知道（冻结 v2-089 靠「各」字侥幸 heavy）。

守门口径（防误报）：
- 单一来源 = nodes._ROUTE_HEAVY_VOCAB（语义类别 → 触发词），文案与 heavy 判定都从它生成；
- 「任一侧独有词即 fail」具体化为四条不变式：
  1) 文案侧：route_reason 实际输出的类别宣称（「/」分隔）与词表类别逐一相等——
     文案里的普通叙述词（走完整图/含下钻与质检修订 等）不参与比对；
  2) 实现侧：_HEAVY_PATTERN 与词表派生词表逐词相等（无幽灵词），
     且每个词表词都真实触发 full（逐词探针，防「词表在、判定漏」的 AB-HR4 形态）；
  3) 词表卫生：类别非空、词不跨类重复——否则类别宣称无法追溯到具体触发词；
  4) 行为零变化 pin：触发词集合与重构前（正则 ∪ _WHY_WORDS，0221df3 后）完全一致。
"""

from __future__ import annotations

import re

from app.agents.nodes import (
    _HEAVY_PATTERN,
    _ROUTE_HEAVY_VOCAB,
    _ROUTE_HEAVY_WORDS,
    route_node,
)

# 重构前实现的真实触发词集合（0221df3 修复后的 正则 ∪ _WHY_WORDS，评测五套 100% 的口径）
_LEGACY_TRIGGER_SET = {
    "为什么", "原因", "为何", "归因", "趋势", "走势", "对比", "vs", "各", "每个", "按",
    "下滑", "增长", "怎么回事", "分析下", "分析一下",
}


def _flatten_words() -> list[str]:
    return [w for _label, words in _ROUTE_HEAVY_VOCAB for w in words]


# ---------------- 文案侧：宣称类别与词表同源 ----------------


def test_route_reason_claims_equal_vocab_labels():
    """heavy 文案实际输出的类别宣称必须与词表类别逐一相等（多宣称/少宣称都 fail）。"""
    st = route_node({"question": "为什么上月毛利率下滑了"})
    m = re.search(r"包含(.+?)语义", st["route_reason"])
    assert m, st["route_reason"]
    assert m.group(1).split("/") == [label for label, _ws in _ROUTE_HEAVY_VOCAB]


def test_heavy_reason_is_generated_from_vocab():
    """文案逐字节等于词表生成值——手写文案回潮即 fail（AB-HR4 的根因是两处手维护）。"""
    st = route_node({"question": "为什么上月毛利率下滑了"})
    expected = (
        f"包含{'/'.join(label for label, _ws in _ROUTE_HEAVY_VOCAB)}语义，"
        "走完整图（含下钻与质检修订）"
    )
    assert st["route_reason"] == expected


# ---------------- 实现侧：触发词与词表同源 ----------------


def test_heavy_pattern_is_exactly_vocab_words():
    """判定正则必须逐词等于词表派生词表——词表外幽灵词（实现独有）即 fail。"""
    assert _HEAVY_PATTERN.split("|") == [re.escape(w) for w in _ROUTE_HEAVY_WORDS]
    assert list(_ROUTE_HEAVY_WORDS) == list(dict.fromkeys(_flatten_words()))


def test_every_vocab_word_actually_triggers_full():
    """逐词探针：每个词表词都必须真实路由 full（防「词表在、判定漏」）。"""
    for word in _ROUTE_HEAVY_WORDS:
        st = route_node({"question": f"上月{word}数据如何"})
        assert st["depth"] == "full", f"词表词『{word}』未触发 full（AB-HR4 形态复发）"


def test_vocab_groups_nonempty_no_cross_group_dup():
    """词表卫生：每类至少一个触发词、词不跨类重复（否则宣称无法追溯）。"""
    words = _flatten_words()
    assert words, "词表为空"
    assert all(ws for _label, ws in _ROUTE_HEAVY_VOCAB), "存在无触发词的类别（宣称了不存在的语义）"
    assert len(words) == len(set(words)), "触发词跨类重复"


# ---------------- 行为零变化与历史锚 ----------------


def test_trigger_set_unchanged_from_pre_refactor():
    """触发词集合与重构前逐词一致——同源化是结构治理，不是语义调整。"""
    assert set(_ROUTE_HEAVY_WORDS) == _LEGACY_TRIGGER_SET


def test_abhr4_regression_pins():
    """0221df3 修复点 + 轻量链路文案 + 显式 depth 路径逐字稳定。"""
    assert "归因" in _ROUTE_HEAVY_WORDS
    assert route_node({"question": "归因一下上月美妆品类的客单价变化"})["depth"] == "full"
    light = route_node({"question": "上月销售额是多少"})
    assert light["depth"] == "light"
    assert light["route_reason"] == "简单聚合查询，走轻量链路（跳过下钻叙述）"
    explicit = route_node({"question": "x", "depth": "full"})
    assert explicit["route_reason"] == "请求显式指定 depth=full（跳过自动路由）"
