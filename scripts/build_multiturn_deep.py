"""构建多轮长对话评测集（eval/multiturn_deep.jsonl，[1B4-AB] 任务四）：20 组 × 4-6 轮。

与 eval/dataset_v2_followup.json（30 条 × 2-3 轮机制场景）的分工：本集是**长对话**
金标——每组 4-6 轮，覆盖四类能力：跨轮指代消解（实体链式换血）/ 口径约束保持
（骨架继承：换实体不丢指标与时间窗）/ 澄清后钻取（挂起→恢复→同 thread 继续追问
归因）/ 中途换话题回指（回指不可达时保守回退可观测）。

纪律（对照 build_dataset_v3_300.py 同款）：
- answer 轮金标一律 compile_sql 生成，禁止手写 SQL；
- 构建期自检（探针-定稿口径）：
  1. 逐轮模拟 runner 的 resolve_followup 接线（ctx.set 存改写后问题）推导有效问题，
     mock 解析器重答与金标同库比对（LIMIT 剥离口径 + LIMIT 行相等）；answer 轮
     金标过截断边界 strict 断言（AB-29，并列边界 fail-fast）；
  2. clarify/refusal 轮 mock clarifier 实判 verdict；suspend 轮断言 verdict=clarify
     （挂起由 clarify 触发）；merged_contains 轮按 clarify_suspend_node 的合并规则
     构造合并问题后同样探针；
  3. note_contains 期望与 resolver 实际产出逐条核对（指代消解/保守回退两形态）；
  4. from_cache=true 的轮断言与同组早前轮 DSL 指纹相等（指纹由 cache.canonical_dsl
     规范化——同 DSL 异写法同键）。
- 判分复用 app.eval.runner.judge_query（runner 不改判分器）。

用法（项目根，须用 askbi/.venv/Scripts/python.exe）：
    python scripts/build_multiturn_deep.py            # 构建 + 自检，写入 eval/multiturn_deep.jsonl
    python scripts/build_multiturn_deep.py --check    # 只自检不写盘
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.cache import fingerprint
from app.context import resolve_followup
from app.data.dsl import QueryDSL, compile_sql
from app.data.sandbox import execute_sync
from app.eval.limit_invariant import limit_boundary_strict
from app.llm.mock import _clarifier, _parse_question

TARGET = ROOT / "eval" / "multiturn_deep.jsonl"


def dsl(
    metric: str,
    dims: list[str] | None = None,
    filters: list[tuple[str, object]] | None = None,
    time: dict | None = None,
    limit: int | None = None,
    direction: str | None = None,
) -> dict | None:
    """意图 DSL 简写构造器；None = 无金标轮（suspend/clarify/refusal）。"""
    if metric is None:
        return None
    return {
        "intent": "aggregate",
        "metric": metric,
        "dimensions": dims or [],
        "filters": [{"field": f, "op": "in" if isinstance(v, list) else "=", "value": v}
                    for f, v in (filters or [])],
        "time": time or {"mode": "all"},
        "order_by": {"metric": metric, "direction": direction or "desc"},
        "limit": limit,
    }


L1 = {"mode": "last_n_months", "n": 1}
L3 = {"mode": "last_n_months", "n": 3}
L6 = {"mode": "last_n_months", "n": 6}
L12 = {"mode": "last_n_months", "n": 12}
BT = lambda s, e: {"mode": "between", "start": s, "end": e}

# 期望片段（与 app/context.py 的说明文案同源锚定）
_NOTE_RESOLVED = "指代消解"
_NOTE_FALLBACK = "无同类型实体可替换"
_MERGED = "用户澄清"

# 20 组长对话。轮格式：{q, resume?, expect:{kind, dsl, from_cache, note_contains, merged_contains}}
# ——dsl 为该轮有效问题（消解后/合并后）的意图 DSL，金标由 compile_sql(role) 生成。
def T(q: str, kind: str = "answer", dsl: dict | None = None,
      note: str | None = None, merged: bool = False, from_cache: bool | None = None,
      resume: bool = False) -> dict:
    expect: dict = {"kind": kind}
    if dsl is not None:
        expect["dsl"] = dsl
    if note is not None:
        expect["note_contains"] = note
    if merged:
        expect["merged_contains"] = _MERGED
    if from_cache is not None:
        expect["from_cache"] = from_cache
    return {"q": q, "resume": resume, "expect": expect}


ITEMS: list[dict] = [
    # ============ 能力一：跨轮指代消解（实体链式换血）×5 ============
    {
        "id": "md01", "scenario": "resolve_chain", "role": "boss",
        "desc": "region 实体五轮链式换血：华南→华北→华东→华中，末轮换完整问题",
        "turns": [
            T("上月华南地区的销售额", dsl=dsl("revenue", filters=[("region", "华南")], time=L1)),
            T("那华北呢", dsl=dsl("revenue", filters=[("region", "华北")], time=L1), note=_NOTE_RESOLVED),
            T("换成华东呢", dsl=dsl("revenue", filters=[("region", "华东")], time=L1), note=_NOTE_RESOLVED),
            T("那华中地区呢", dsl=dsl("revenue", filters=[("region", "华中")], time=L1), note=_NOTE_RESOLVED),
            T("上月各品类销售额", dsl=dsl("revenue", dims=["category"], time=L1)),
        ],
    },
    {
        "id": "md02", "scenario": "resolve_chain", "role": "boss",
        "desc": "channel 实体链：直播→搜索→官网，末轮渠道分组",
        "turns": [
            T("直播渠道近3个月的销售额", dsl=dsl("revenue", filters=[("channel", "直播")], time=L3)),
            T("那搜索呢", dsl=dsl("revenue", filters=[("channel", "搜索")], time=L3), note=_NOTE_RESOLVED),
            T("换成官网呢", dsl=dsl("revenue", filters=[("channel", "官网")], time=L3), note=_NOTE_RESOLVED),
            T("各渠道销售额", dsl=dsl("revenue", dims=["channel"])),
        ],
    },
    {
        "id": "md03", "scenario": "resolve_chain", "role": "boss",
        "desc": "member_level 实体链：金卡→银卡→黑卡，末轮等级分组",
        "turns": [
            T("金卡会员上月客单价", dsl=dsl("avg_order_value", filters=[("member_level", "金卡")], time=L1)),
            T("那银卡呢", dsl=dsl("avg_order_value", filters=[("member_level", "银卡")], time=L1), note=_NOTE_RESOLVED),
            T("那黑卡呢", dsl=dsl("avg_order_value", filters=[("member_level", "黑卡")], time=L1), note=_NOTE_RESOLVED),
            T("各会员等级的客单价", dsl=dsl("avg_order_value", dims=["member_level"])),
        ],
    },
    {
        "id": "md04", "scenario": "resolve_chain", "role": "boss",
        "desc": "category 实体链：美妆→数码→食品→家居，末轮退货率分组",
        "turns": [
            T("近6个月美妆品类的毛利率", dsl=dsl("gross_margin_rate", filters=[("category", "美妆")], time=L6)),
            T("那数码呢", dsl=dsl("gross_margin_rate", filters=[("category", "数码")], time=L6), note=_NOTE_RESOLVED),
            T("换成食品呢", dsl=dsl("gross_margin_rate", filters=[("category", "食品")], time=L6), note=_NOTE_RESOLVED),
            T("那家居呢", dsl=dsl("gross_margin_rate", filters=[("category", "家居")], time=L6), note=_NOTE_RESOLVED),
            T("上月各品类退货率", dsl=dsl("refund_rate", dims=["category"], time=L1, direction="asc")),
        ],
    },
    {
        "id": "md05", "scenario": "resolve_chain", "role": "boss",
        "desc": "跨维度实体链：region 换血→换话题 category 换血→fallback（渠道骨架无实体）",
        "turns": [
            T("华南地区上月销售额", dsl=dsl("revenue", filters=[("region", "华南")], time=L1)),
            T("那华北呢", dsl=dsl("revenue", filters=[("region", "华北")], time=L1), note=_NOTE_RESOLVED),
            T("美妆品类上月毛利率", dsl=dsl("gross_margin_rate", filters=[("category", "美妆")], time=L1)),
            T("那数码呢", dsl=dsl("gross_margin_rate", filters=[("category", "数码")], time=L1), note=_NOTE_RESOLVED),
            T("上月各渠道净销售额", dsl=dsl("net_revenue", dims=["channel"], time=L1)),
            T("那官网呢", kind="clarify", note=_NOTE_FALLBACK),
        ],
    },
    # ============ 能力二：口径约束保持（骨架继承）×4 ============
    {
        "id": "md06", "scenario": "constraint_keep", "role": "boss",
        "desc": "毛利率+近6个月窗口在实体换血中保持（华北/东北）",
        "turns": [
            T("华南地区近6个月毛利率", dsl=dsl("gross_margin_rate", filters=[("region", "华南")], time=L6)),
            T("那华北呢", dsl=dsl("gross_margin_rate", filters=[("region", "华北")], time=L6), note=_NOTE_RESOLVED),
            T("那东北呢", dsl=dsl("gross_margin_rate", filters=[("region", "东北")], time=L6), note=_NOTE_RESOLVED),
            T("上月华南地区毛利率", dsl=dsl("gross_margin_rate", filters=[("region", "华南")], time=L1)),
        ],
    },
    {
        "id": "md07", "scenario": "constraint_keep", "role": "boss",
        "desc": "净销售额口径在渠道换血中保持（社交/直播）",
        "turns": [
            T("官网渠道上月净销售额", dsl=dsl("net_revenue", filters=[("channel", "官网")], time=L1)),
            T("那社交呢", dsl=dsl("net_revenue", filters=[("channel", "社交")], time=L1), note=_NOTE_RESOLVED),
            T("那直播呢", dsl=dsl("net_revenue", filters=[("channel", "直播")], time=L1), note=_NOTE_RESOLVED),
            T("上月净销售额", dsl=dsl("net_revenue", time=L1)),
        ],
    },
    {
        "id": "md08", "scenario": "constraint_keep", "role": "boss",
        "desc": "购买客户数+近12个月窗口在会员等级换血中保持",
        "turns": [
            T("黑卡会员近12个月的购买客户数", dsl=dsl("customer_count", filters=[("member_level", "黑卡")], time=L12)),
            T("那普通呢", dsl=dsl("customer_count", filters=[("member_level", "普通")], time=L12), note=_NOTE_RESOLVED),
            T("那银卡呢", dsl=dsl("customer_count", filters=[("member_level", "银卡")], time=L12), note=_NOTE_RESOLVED),
            T("那金卡呢", dsl=dsl("customer_count", filters=[("member_level", "金卡")], time=L12), note=_NOTE_RESOLVED),
            T("近12个月购买客户数", dsl=dsl("customer_count", time=L12)),
        ],
    },
    {
        "id": "md09", "scenario": "constraint_keep", "role": "boss",
        "desc": "销量+近3个月窗口在品类换血中保持，末轮品类分组",
        "turns": [
            T("食品品类近3个月销量", dsl=dsl("item_quantity", filters=[("category", "食品")], time=L3)),
            T("那家居呢", dsl=dsl("item_quantity", filters=[("category", "家居")], time=L3), note=_NOTE_RESOLVED),
            T("那运动呢", dsl=dsl("item_quantity", filters=[("category", "运动")], time=L3), note=_NOTE_RESOLVED),
            T("近3个月各品类销量", dsl=dsl("item_quantity", dims=["category"], time=L3)),
        ],
    },
    # ============ 能力三：澄清后钻取（挂起→恢复→同 thread 追问）×3 ============
    {
        "id": "md10", "scenario": "clarify_then_drill", "role": "boss", "suspend": True,
        "desc": "澄清挂起→恢复出数→同 thread 换完整问题→归因追问（full 链路出叙述+QA）",
        "turns": [
            T("最近卖得怎么样", kind="suspend"),
            T("销售额", kind="answer", resume=True, merged=True,
              dsl=dsl("revenue")),
            T("上月各品类销售额", dsl=dsl("revenue", dims=["category"], time=L1)),
            T("为什么上月家居品类的销售额下滑了", dsl=dsl("revenue", filters=[("category", "家居")], time=L1)),
        ],
    },
    {
        "id": "md11", "scenario": "clarify_then_drill", "role": "boss", "suspend": True,
        "desc": "澄清挂起→恢复毛利率分组→归因对比追问→收束单指标",
        "turns": [
            T("上月各渠道表现如何", kind="suspend"),
            T("毛利率", kind="answer", resume=True, merged=True,
              dsl=dsl("gross_margin_rate", dims=["channel"], time=L1)),
            T("对比上月各渠道的毛利率差异", dsl=dsl("gross_margin_rate", dims=["channel"], time=L1)),
            T("上月净销售额", dsl=dsl("net_revenue", time=L1)),
        ],
    },
    {
        "id": "md12", "scenario": "clarify_then_drill", "role": "boss",
        "desc": "一次性澄清（不挂起）→补全指标→归因→回看总量（4 轮）",
        "turns": [
            T("最近表现怎么样", kind="clarify"),
            T("上月各品类毛利额", dsl=dsl("gross_margin", dims=["category"], time=L1)),
            T("为什么上月食品品类的客单价下滑了", dsl=dsl("avg_order_value", filters=[("category", "食品")], time=L1)),
            T("上月总毛利额", dsl=dsl("gross_margin", time=L1)),
        ],
    },
    # ============ 能力四：中途换话题回指（回指不可达→保守回退可观测）×4 ============
    {
        "id": "md13", "scenario": "topic_switch", "role": "boss",
        "desc": "region→渠道分组话题切换后回指失败（上轮无 region 实体→保守回退）→完整问题恢复→恢复后实体换血",
        "turns": [
            T("华南地区上月销售额", dsl=dsl("revenue", filters=[("region", "华南")], time=L1)),
            T("上月各渠道毛利率", dsl=dsl("gross_margin_rate", dims=["channel"], time=L1)),
            T("那华北呢", kind="clarify", note=_NOTE_FALLBACK),
            T("华北地区上月订单量", dsl=dsl("order_count", filters=[("region", "华北")], time=L1)),
            T("那西北呢", dsl=dsl("order_count", filters=[("region", "西北")], time=L1), note=_NOTE_RESOLVED),
        ],
    },
    {
        "id": "md14", "scenario": "topic_switch", "role": "boss",
        "desc": "category→会员等级话题切换后回指失败→恢复→恢复后换血",
        "turns": [
            T("美妆品类近6个月销售额", dsl=dsl("revenue", filters=[("category", "美妆")], time=L6)),
            T("上月各会员等级客单价", dsl=dsl("avg_order_value", dims=["member_level"], time=L1)),
            T("那数码呢", kind="clarify", note=_NOTE_FALLBACK),
            T("数码品类近6个月销售额", dsl=dsl("revenue", filters=[("category", "数码")], time=L6)),
            T("那食品呢", dsl=dsl("revenue", filters=[("category", "食品")], time=L6), note=_NOTE_RESOLVED),
        ],
    },
    {
        "id": "md15", "scenario": "topic_switch", "role": "boss",
        "desc": "member→品类退货率话题切换后回指失败→恢复→换血",
        "turns": [
            T("黑卡会员上月订单量", dsl=dsl("order_count", filters=[("member_level", "黑卡")], time=L1)),
            T("上月各品类退货率", dsl=dsl("refund_rate", dims=["category"], time=L1, direction="asc")),
            T("那金卡呢", kind="clarify", note=_NOTE_FALLBACK),
            T("金卡会员上月订单量", dsl=dsl("order_count", filters=[("member_level", "金卡")], time=L1)),
            T("那银卡呢", dsl=dsl("order_count", filters=[("member_level", "银卡")], time=L1), note=_NOTE_RESOLVED),
        ],
    },
    {
        "id": "md16", "scenario": "topic_switch_cache", "role": "boss",
        "desc": "话题切换后回指重问（与 t1 同 DSL 指纹→缓存命中）→实体换血→末轮 fallback",
        "turns": [
            T("上月华南地区的销售额", dsl=dsl("revenue", filters=[("region", "华南")], time=L1)),
            T("上月各品类销售额", dsl=dsl("revenue", dims=["category"], time=L1)),
            T("上月华南地区的销售额", dsl=dsl("revenue", filters=[("region", "华南")], time=L1), from_cache=True),
            T("那华北呢", dsl=dsl("revenue", filters=[("region", "华北")], time=L1), note=_NOTE_RESOLVED),
            T("上月各渠道净销售额", dsl=dsl("net_revenue", dims=["channel"], time=L1)),
            T("那直播呢", kind="clarify", note=_NOTE_FALLBACK),
        ],
    },
    # ============ 综合：角色权限×拒答×澄清×消解混合长流 ×4 ============
    {
        "id": "md17", "scenario": "role_mixed", "role": "south",
        "desc": "south 角色多轮：全链路按华南 scope 收窄（金标以 role=south 编译）",
        "turns": [
            T("上月各渠道销售额", dsl=dsl("revenue", dims=["channel"], time=L1)),
            T("上月各品类毛利率", dsl=dsl("gross_margin_rate", dims=["category"], time=L1)),
            T("上月净销售额", dsl=dsl("net_revenue", time=L1)),
            T("上月各细分品类销量", dsl=dsl("item_quantity", dims=["subcategory"], time=L1)),
            T("上月购买客户数", dsl=dsl("customer_count", time=L1)),
        ],
    },
    {
        "id": "md18", "scenario": "refusal_mixed", "role": "boss",
        "desc": "拒答插入对话：预测类拒答→回到历史数据→渠道换血",
        "turns": [
            T("上月销售额", dsl=dsl("revenue", time=L1)),
            T("预测下季度的销售额", kind="refusal"),
            T("上月毛利率", dsl=dsl("gross_margin_rate", time=L1)),
            T("直播渠道上月净销售额", dsl=dsl("net_revenue", filters=[("channel", "直播")], time=L1)),
            T("那搜索呢", dsl=dsl("net_revenue", filters=[("channel", "搜索")], time=L1), note=_NOTE_RESOLVED),
        ],
    },
    {
        "id": "md19", "scenario": "clarify_mixed", "role": "boss",
        "desc": "中途澄清→补全指标分组→归因→对比收束",
        "turns": [
            T("上月销售额", dsl=dsl("revenue", time=L1)),
            T("整体怎么样", kind="clarify"),
            T("上月各品类毛利额", dsl=dsl("gross_margin", dims=["category"], time=L1)),
            T("上月各渠道毛利额", dsl=dsl("gross_margin", dims=["channel"], time=L1)),
            T("对比上月各品类的毛利额差异", dsl=dsl("gross_margin", dims=["category"], time=L1)),
        ],
    },
    {
        "id": "md20", "scenario": "mixed_long", "role": "boss",
        "desc": "六轮混合长流：趋势→会员实体换血→渠道趋势→净销售额→显式区间",
        "turns": [
            T("近12个月销售额趋势", dsl=dsl("revenue", dims=["month"], time=L12)),
            T("黑卡会员上月客单价", dsl=dsl("avg_order_value", filters=[("member_level", "黑卡")], time=L1)),
            T("那普通呢", dsl=dsl("avg_order_value", filters=[("member_level", "普通")], time=L1), note=_NOTE_RESOLVED),
            T("近6个月各渠道客单价趋势", dsl=dsl("avg_order_value", dims=["month", "channel"], time=L6)),
            T("上月净销售额", dsl=dsl("net_revenue", time=L1)),
            T("2025年1月到6月的毛利额", dsl=dsl("gross_margin", time=BT("2025-01", "2025-06"))),
        ],
    },
]


def _canon(rows: list[list]) -> list[tuple]:
    return sorted(
        tuple(round(v, 4) if isinstance(v, (int, float)) and not isinstance(v, bool) else v for v in r)
        for r in rows
    )


def _strip_limit(sql: str) -> tuple[str, str]:
    body, _, limit_line = sql.rpartition("\nLIMIT")
    return body, ("LIMIT" + limit_line)


def _probe_answer(effective_q: str, role: str, gold_dsl: dict, tag: str) -> list[str]:
    """mock 重答有效问题与金标同库比对（LIMIT 剥离口径 + 截断边界 strict 断言）。"""
    problems: list[str] = []
    gold_sql, _ = compile_sql(QueryDSL(**gold_dsl), role=role)
    # AB-29（评审 5.0）：截断边界并列 → LIMIT 截断集合执行间非确定，该轮金标不得入集
    ok, detail = limit_boundary_strict(gold_sql)
    if not ok:
        return [f"{tag} [{role}] {detail}"]
    try:
        parsed = QueryDSL(**_parse_question(effective_q))
        got_sql, _ = compile_sql(parsed, role=role)
    except Exception as e:  # noqa: BLE001
        return [f"{tag} [{role}] mock 解析/编译失败: {e}\n    Q: {effective_q}"]
    gold_body, gold_limit = _strip_limit(gold_sql)
    got_body, got_limit = _strip_limit(got_sql)
    if got_limit != gold_limit:
        return [f"{tag} [{role}] LIMIT 不一致: {got_limit!r} vs {gold_limit!r}\n    Q: {effective_q}"]
    try:
        ok = _canon(execute_sync(got_body).rows) == _canon(execute_sync(gold_body).rows)
    except Exception as e:  # noqa: BLE001
        return [f"{tag} 执行比对失败: {e}\n    gold: {gold_sql}\n    got : {got_sql}"]
    if not ok:
        problems.append(
            f"{tag} [{role}] mock 结果与金标不一致\n    Q: {effective_q}\n    gold: {gold_sql}\n    got : {got_sql}"
        )
    return problems


def self_check() -> list[str]:
    """构建期自检：逐轮模拟 runner 接线（resolve→ctx.set）推导有效问题后三重探针。"""
    problems: list[str] = []
    from collections import Counter

    turns_total = 0
    for item in ITEMS:
        ctx = ""            # 与 runner 一致：ctx.set(sid, 改写后问题)
        suspended_q = ""
        turn_kinds: list[str] = []
        for tno, turn in enumerate(item["turns"], 1):
            turns_total += 1
            tag = f"{item['id']}.t{tno}"
            exp = turn["expect"]
            kind = exp["kind"]
            turn_kinds.append(kind)
            role = turn.get("role", item.get("role", "boss"))
            if turn.get("resume"):
                # clarify_suspend_node 的合并规则：f'{挂起时问题}（用户澄清：{answer}）'
                effective = f"{suspended_q}（用户澄清：{turn['q']}）"
            else:
                effective, note = resolve_followup(turn["q"], ctx, enabled=True)
                ctx = effective
                if exp.get("note_contains") is not None and exp["note_contains"] not in note:
                    problems.append(
                        f"{tag} note_contains 期望「{exp['note_contains']}」但 resolver 产出：{note or '<空>'}"
                    )
            if kind == "suspend":
                if _clarifier({"question": effective, "dataset": "ecommerce"})["verdict"] != "clarify":
                    problems.append(f"{tag} [suspend] mock verdict 非 clarify（挂不起）: {effective}")
                suspended_q = effective
                continue
            if kind in ("clarify", "refusal"):
                verdict = _clarifier({"question": effective, "dataset": "ecommerce"})["verdict"]
                expect_v = "refuse" if kind == "refusal" else "clarify"
                if verdict != expect_v:
                    problems.append(f"{tag} [{kind}] mock verdict={verdict} 期望 {expect_v}: {effective}")
                continue
            if kind == "answer":
                problems += _probe_answer(effective, role, exp["dsl"], tag)
                if exp.get("from_cache"):
                    # 缓存命中钉死：同组早前须存在同 DSL 指纹的 answer 轮（cache.clear 按组执行）
                    earlier_fps = {
                        fingerprint(t2["expect"]["dsl"], t2.get("role", item.get("role", "boss")))
                        for t2 in item["turns"][: tno - 1]
                        if t2["expect"]["kind"] == "answer" and t2["expect"].get("dsl") is not None
                    }
                    if fingerprint(exp["dsl"], role) not in earlier_fps:
                        problems.append(f"{tag} from_cache=True 但同组无同指纹早前轮（不会命中）")
        if not (4 <= len(item["turns"]) <= 6):
            problems.append(f"{item['id']} 轮数 {len(item['turns'])} 越界（要求 4-6）")
        # 能力维度覆盖：每组至少一个超出裸 answer(boss 金标) 的期望——
        # resolve_chain/constraint_keep 的能力钉在 note_contains 与金标上，
        # role_mixed 的能力钉在非 boss 角色的 scope 编译金标上，不要求非 answer 轮
        if not any(
            t["expect"].get("note_contains") or t["expect"].get("merged_contains")
            or t["expect"].get("from_cache") or t["expect"]["kind"] != "answer"
            for t in item["turns"]
        ) and item.get("role", "boss") == "boss":
            problems.append(f"{item['id']} 无任何超出裸 answer 的期望（缺能力覆盖）")
    if len(ITEMS) != 20:
        problems.append(f"会话数应为 20，实际 {len(ITEMS)}")
    kind_stat = Counter(t["expect"]["kind"] for it in ITEMS for t in it["turns"])
    if kind_stat.get("suspend", 0) < 2:
        problems.append(f"suspend 轮不足 2：{dict(kind_stat)}")
    print(f"自检统计：{len(ITEMS)} 组 / {turns_total} 轮，轮型分布 {dict(sorted(kind_stat.items()))}")
    return problems


def build(path: Path | None) -> str:
    out_items = []
    for item in ITEMS:
        out_item = {k: item[k] for k in ("id", "scenario", "role", "desc") if k in item}
        if item.get("suspend"):
            out_item["suspend"] = True
        turns = []
        for turn in item["turns"]:
            exp = turn["expect"]
            out_exp: dict = {"kind": exp["kind"]}
            if exp.get("dsl") is not None:
                role = turn.get("role", item.get("role", "boss"))
                out_exp["gold_sql"] = compile_sql(QueryDSL(**exp["dsl"]), role=role)[0]
            for k in ("from_cache", "note_contains", "merged_contains"):
                if exp.get(k) is not None:
                    out_exp[k] = exp[k]
            turns.append({"q": turn["q"], **({"resume": True} if turn.get("resume") else {}),
                          "expect": out_exp})
        out_item["turns"] = turns
        out_items.append(out_item)

    total_turns = sum(len(i["turns"]) for i in out_items)
    note = (
        "多轮长对话评测集（[1B4-AB] 任务四，2026-11-18）：20 组 × 4-6 轮（90 轮），"
        "覆盖 跨轮指代消解（实体链式换血）/口径约束保持（骨架继承）/澄清后钻取（挂起→恢复→同 thread 归因）"
        "/中途换话题回指（回指不可达→保守回退可观测）+ 角色×拒答×澄清混合长流。"
        "answer 轮金标由 compile_sql(role) 生成（scripts/build_multiturn_deep.py 可重建/自检）。"
        "判分复用 app.eval.runner.judge_query；本行为元信息行（runner 按 id+turns 识别会话行）。"
    )
    lines = [json.dumps({"_note": note}, ensure_ascii=False)]
    for it in out_items:
        lines.append(json.dumps(it, ensure_ascii=False))
    if path is not None:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"已构建 20 组 / {total_turns} 轮（金标 compile_sql 生成）"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只跑自检，不写盘")
    args = ap.parse_args()

    problems = self_check()
    if problems:
        print(f"自检未通过 {len(problems)} 条：")
        for p in problems:
            print("  -", p)
        return 1
    print("自检通过：逐轮有效问题（消解/合并后）mock 重答与金标同库一致；"
          "clarify/refusal/suspend verdict 实判一致；note/merged 期望与 resolver 产出核对")

    msg = build(None if args.check else TARGET)
    print(msg)
    if not args.check:
        print(f"已写入 {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
