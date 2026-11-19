"""构建多轮长对话评测集扩容（eval/multiturn_deep_v2.jsonl，[1B6]）：冻结 deep 20 组逐行保留 + 新增 40 组。

与 scripts/build_multiturn_deep.py（20 组 × 4-6 轮 = 95 轮，冻结零字节）的分工：
- 冻结的 eval/multiturn_deep.jsonl 一个字节不改——本脚本只读它；构建产物内冻结 20 组
  会话行**逐字节保留**（md01..md20 在前），新增 40 组（md21..md60，batch=deep_v2）在后；
- deep 集定位 = ecommerce 单源深评（跨源多轮已有 multiturn_cross_b/c/d 三件专门集），
  本集在单源内把多轮能力面做深：9 类新能力场景（复用 deep 的 ITEMS/轮结构）。

新增 40 组能力面（对照 deep 9 类 scenario 的扩展，全部 ecommerce 源）：
- role_x_chain ×3        实体链×角色交叉加深：south/north 的 resolve_chain 5 轮链，
                          scope 金标随角色编译（ROLE_SCOPES 注入 region IN (...)）；
- between_window ×4      显式年月区间多轮继承与改写：区间文本经实体换血继承 /
                          显式改写区间 / 区间×趋势双维 / 微改窗口缓存边界；
- double_suspend ×3      双挂起链：suspend→resume→新问题→再 suspend→resume
                          （同 thread 两次 interrupt，clarify_suspend_node 合并规则两轮生效）；
- cache_boundary ×4      缓存指纹边界：表层改写 DSL 恒等命中 / 微改时间窗不命中 /
                          换 role 不串（turn 级 role 覆盖，指纹含 role）/ 消解面恒等命中；
- refusal_downgrade ×3   拒答后降维追问：不支持指标拒答→改问支持指标→归因/下钻；
- constraint_deep ×5     口径约束保持加深：L6→L12 窗口切换继承 / 净销售额口径跨 6 轮 /
                          双过滤跨轮保留+中途显式解除 / 品类分组骨架跨 5 指标切换；
- chain_deep ×8          实体链式换血加深：三实体轮转链 / 回环链 / 链中插 drill /
                          链中插 clarify / drill 中换血 / 链中回环重问缓存命中；
- mixed_long ×6          混合长流加长：7-8 轮（deep 集 4-6 轮上限之上），混合
                          换血/约束/拒答/澄清/between/趋势多能力；
- multi_entity_guard ×4  多实体守卫：同类型双实体歧义回退（note「上轮含多个」可观测）/
                          一问双实体（不同类型）各换血一次且另一类型保持。

纪律（对照 build_multiturn_deep.py / build_dataset_v3_300.py 同款）：
- answer 轮金标一律 compile_sql(role) 生成，禁止手写 SQL；turn 级 role 覆盖（cache_boundary
  的换角色组）按 turn role 编译金标并在 JSON 轮上回写 role 字段（runner 按轮取 role）；
- 构建期自检（四重，冻结 20 组 + 新增 40 组 = 60 组逐轮）：
  1. 逐轮模拟 runner 接线（ctx.set 存改写后问题，挂起轮含 resume 合并）推导有效问题，
     mock 解析器重答与金标同库比对（LIMIT 剥离口径 + LIMIT 行相等）；
  2. clarify/refusal/suspend 轮 mock clarifier 实判 verdict；resume 轮按
     clarify_suspend_node 合并规则构造合并问题后同样探针；
  3. note_contains 期望与 resolver 实际产出逐条核对（指代消解/保守回退/多实体歧义）；
  4. 金标过 app.eval.limit_invariant.limit_boundary_strict 截断边界 strict 断言
     （AB-29，并列边界 fail-fast）——1B6 装置作第四重；
  另：from_cache 钉死双向校验（True：同组早前存在同「指纹×role」的 answer 轮；False：
  不存在——deep builder 只查 True 向，本集缓存边界组需要 False 向防误标；指纹取自
  有效问题 mock 解析结果，与 runner 缓存键同源：fingerprint(state.dsl, role)）。
- 判分复用 app.eval.runner.judge_query（runner 不改判分器）。

用法（项目根，须用 askbi/.venv/Scripts/python.exe）：
    python scripts/build_multiturn_deep_v2.py            # 构建 + 自检，写入 eval/multiturn_deep_v2.jsonl
    python scripts/build_multiturn_deep_v2.py --check    # 只自检 + 完整性校验，不写盘
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.cache import fingerprint
from app.context import resolve_followup
from app.data.dsl import QueryDSL, compile_sql
from app.data.sandbox import execute_sync
from app.eval.limit_invariant import limit_boundary_strict, strip_limit
from app.llm.mock import _clarifier, _parse_question

FROZEN = ROOT / "eval" / "multiturn_deep.jsonl"
TARGET = ROOT / "eval" / "multiturn_deep_v2.jsonl"

BATCH = "deep_v2"
FROZEN_GROUP_COUNT = 20
NEW_GROUP_COUNT = 40
FROZEN_IDS = [f"md{i:02d}" for i in range(1, FROZEN_GROUP_COUNT + 1)]
NEW_IDS = [f"md{i:02d}" for i in range(FROZEN_GROUP_COUNT + 1, FROZEN_GROUP_COUNT + 41)]

# 跨源物理表名（ecommerce 零变化守门：deep 集定位单源，金标绝不可混入他源表）
FOREIGN_TABLES = ("ticket_lines", "invoice_lines", "stay_lines")

# 能力面配比（场景 → 组数）：构建期断言，扩缩容时先改这里
AREA_PLAN: dict[str, int] = {
    "role_x_chain": 3,
    "between_window": 4,
    "double_suspend": 3,
    "cache_boundary": 4,
    "refusal_downgrade": 3,
    "constraint_deep": 5,
    "chain_deep": 8,
    "mixed_long": 6,
    "multi_entity_guard": 4,
}

# 轮数边界：mixed_long 7-8（deep 集 4-6 上限的加长位），双挂起链 5-6，其余 4-6
TURN_BOUNDS: dict[str, tuple[int, int]] = {
    "mixed_long": (7, 8),
    "double_suspend": (5, 6),
    "_default": (4, 6),
}


def dsl(
    metric: str,
    dims: list[str] | None = None,
    filters: list[tuple[str, object]] | None = None,
    time: dict | None = None,
    limit: int | None = None,
    direction: str | None = None,
) -> dict:
    """意图 DSL 简写构造器（与 build_multiturn_deep.dsl 同款）。"""
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
_NOTE_AMBIGUOUS = "上轮含多个"      # 评审 D2 多实体歧义回退（multi_entity_guard 钉死可观测）
_MERGED = "用户澄清"


def T(q: str, kind: str = "answer", dsl: dict | None = None,
      note: str | None = None, merged: bool = False, from_cache: bool | None = None,
      resume: bool = False, role: str | None = None) -> dict:
    """轮构造器（deep 同款 + turn 级 role 覆盖：cache_boundary 换角色组用）。"""
    expect: dict = {"kind": kind}
    if dsl is not None:
        expect["dsl"] = dsl
    if note is not None:
        expect["note_contains"] = note
    if merged:
        expect["merged_contains"] = _MERGED
    if from_cache is not None:
        expect["from_cache"] = from_cache
    turn = {"q": q, "resume": resume, "expect": expect}
    if role is not None:
        turn["role"] = role
    return turn


ITEMS_V2: list[dict] = [
    # ===== 能力一：实体链×角色交叉加深（role_x_chain）×3 =====
    {
        "id": "md21", "scenario": "role_x_chain", "role": "south", "batch": BATCH,
        "desc": "south 角色渠道实体 5 轮链：直播→搜索→官网→社交，末轮渠道分组（金标全链 region IN ('华南') 编译）",
        "turns": [
            T("近6个月直播渠道的销售额", dsl=dsl("revenue", filters=[("channel", "直播")], time=L6)),
            T("那搜索呢", dsl=dsl("revenue", filters=[("channel", "搜索")], time=L6), note=_NOTE_RESOLVED),
            T("换成官网呢", dsl=dsl("revenue", filters=[("channel", "官网")], time=L6), note=_NOTE_RESOLVED),
            T("那社交呢", dsl=dsl("revenue", filters=[("channel", "社交")], time=L6), note=_NOTE_RESOLVED),
            T("近6个月各渠道销售额", dsl=dsl("revenue", dims=["channel"], time=L6)),
        ],
    },
    {
        "id": "md22", "scenario": "role_x_chain", "role": "north", "batch": BATCH,
        "desc": "north 角色 region 实体链在 scope 内换血：华北→东北→换回华北（同指纹缓存命中）→区域分组→东北毛利额（scope IN ('华北','东北')）",
        "turns": [
            T("华北地区上月销售额", dsl=dsl("revenue", filters=[("region", "华北")], time=L1)),
            T("那东北呢", dsl=dsl("revenue", filters=[("region", "东北")], time=L1), note=_NOTE_RESOLVED),
            T("换成华北呢", dsl=dsl("revenue", filters=[("region", "华北")], time=L1),
              note=_NOTE_RESOLVED, from_cache=True),
            T("上月各区域销售额", dsl=dsl("revenue", dims=["region"], time=L1)),
            T("上月东北地区毛利额", dsl=dsl("gross_margin", filters=[("region", "东北")], time=L1)),
        ],
    },
    {
        "id": "md23", "scenario": "role_x_chain", "role": "south", "batch": BATCH,
        "desc": "south 角色会员等级 5 轮链：黑卡→普通→银卡→金卡，末轮等级分组（scope 华南收窄下客户数链）",
        "turns": [
            T("黑卡会员近12个月的购买客户数", dsl=dsl("customer_count", filters=[("member_level", "黑卡")], time=L12)),
            T("那普通呢", dsl=dsl("customer_count", filters=[("member_level", "普通")], time=L12), note=_NOTE_RESOLVED),
            T("那银卡呢", dsl=dsl("customer_count", filters=[("member_level", "银卡")], time=L12), note=_NOTE_RESOLVED),
            T("那金卡呢", dsl=dsl("customer_count", filters=[("member_level", "金卡")], time=L12), note=_NOTE_RESOLVED),
            T("近12个月各会员等级的购买客户数", dsl=dsl("customer_count", dims=["member_level"], time=L12)),
        ],
    },
    # ===== 能力二：between 窗口继承（between_window）×4 =====
    {
        "id": "md24", "scenario": "between_window", "role": "boss", "batch": BATCH,
        "desc": "显式年月区间经 region 换血继承：2025-01..03 华南→华北；区间改写 2025-06..08 华东→华中",
        "turns": [
            T("2025年1月到3月华南地区的销售额", dsl=dsl("revenue", filters=[("region", "华南")], time=BT("2025-01", "2025-03"))),
            T("那华北呢", dsl=dsl("revenue", filters=[("region", "华北")], time=BT("2025-01", "2025-03")), note=_NOTE_RESOLVED),
            T("2025年6月到8月华东地区的销售额", dsl=dsl("revenue", filters=[("region", "华东")], time=BT("2025-06", "2025-08"))),
            T("那华中呢", dsl=dsl("revenue", filters=[("region", "华中")], time=BT("2025-06", "2025-08")), note=_NOTE_RESOLVED),
        ],
    },
    {
        "id": "md25", "scenario": "between_window", "role": "boss", "batch": BATCH,
        "desc": "区间+销量口径经品类换血继承：2025-03..05 食品→家居；改写 2025-06..08 家居→运动",
        "turns": [
            T("2025年3月到5月食品品类的销量", dsl=dsl("item_quantity", filters=[("category", "食品")], time=BT("2025-03", "2025-05"))),
            T("那家居呢", dsl=dsl("item_quantity", filters=[("category", "家居")], time=BT("2025-03", "2025-05")), note=_NOTE_RESOLVED),
            T("2025年6月到8月家居品类的销量", dsl=dsl("item_quantity", filters=[("category", "家居")], time=BT("2025-06", "2025-08"))),
            T("那运动呢", dsl=dsl("item_quantity", filters=[("category", "运动")], time=BT("2025-06", "2025-08")), note=_NOTE_RESOLVED),
        ],
    },
    {
        "id": "md26", "scenario": "between_window", "role": "boss", "batch": BATCH,
        "desc": "区间×趋势双维：2025-01..06 渠道趋势→改写 2025-07..12（不命中缓存）→叠加大区过滤→地区换血继承区间与趋势骨架",
        "turns": [
            T("2025年1月到6月各渠道销售额趋势", dsl=dsl("revenue", dims=["month", "channel"], time=BT("2025-01", "2025-06"))),
            T("2025年7月到12月各渠道销售额趋势", dsl=dsl("revenue", dims=["month", "channel"], time=BT("2025-07", "2025-12")), from_cache=False),
            T("2025年7月到12月华南地区各渠道销售额趋势",
              dsl=dsl("revenue", dims=["month", "channel"], filters=[("region", "华南")], time=BT("2025-07", "2025-12"))),
            T("那华北地区呢",
              dsl=dsl("revenue", dims=["month", "channel"], filters=[("region", "华北")], time=BT("2025-07", "2025-12")),
              note=_NOTE_RESOLVED),
        ],
    },
    {
        "id": "md27", "scenario": "between_window", "role": "boss", "batch": BATCH,
        "desc": "区间微改缓存边界：2025-01..03 → 窗口尾月 +1（指纹变，不命中）→ 复问原区间（命中）→ 叠加品类过滤",
        "turns": [
            T("2025年1月到3月的毛利率", dsl=dsl("gross_margin_rate", time=BT("2025-01", "2025-03"))),
            T("2025年1月到4月的毛利率", dsl=dsl("gross_margin_rate", time=BT("2025-01", "2025-04")), from_cache=False),
            T("2025年1月到3月的毛利率", dsl=dsl("gross_margin_rate", time=BT("2025-01", "2025-03")), from_cache=True),
            T("2025年1月到3月美妆品类的毛利率", dsl=dsl("gross_margin_rate", filters=[("category", "美妆")], time=BT("2025-01", "2025-03"))),
        ],
    },
    # ===== 能力三：双挂起链（double_suspend）×3 =====
    {
        "id": "md28", "scenario": "double_suspend", "role": "boss", "suspend": True, "batch": BATCH,
        "desc": "双挂起链：挂起→澄清销售额出数→品类分组→新问题再挂起→澄清毛利率出数（同 thread 两次 interrupt）",
        "turns": [
            T("最近卖得怎么样", kind="suspend"),
            T("销售额", kind="answer", resume=True, merged=True, dsl=dsl("revenue")),
            T("上月各品类销售额", dsl=dsl("revenue", dims=["category"], time=L1)),
            T("整体表现如何", kind="suspend"),
            T("毛利率", kind="answer", resume=True, merged=True, dsl=dsl("gross_margin_rate")),
        ],
    },
    {
        "id": "md29", "scenario": "double_suspend", "role": "boss", "suspend": True, "batch": BATCH,
        "desc": "双挂起链+中途实体换血：挂起→客单价→黑卡→普通换血→再挂起→订单量收束",
        "turns": [
            T("最近经营情况怎么样", kind="suspend"),
            T("客单价", kind="answer", resume=True, merged=True, dsl=dsl("avg_order_value")),
            T("黑卡会员上月客单价", dsl=dsl("avg_order_value", filters=[("member_level", "黑卡")], time=L1)),
            T("那普通呢", dsl=dsl("avg_order_value", filters=[("member_level", "普通")], time=L1), note=_NOTE_RESOLVED),
            T("最近整体表现怎么样", kind="suspend"),
            T("订单量", kind="answer", resume=True, merged=True, dsl=dsl("order_count")),
        ],
    },
    {
        "id": "md30", "scenario": "double_suspend", "role": "boss", "suspend": True, "batch": BATCH,
        "desc": "双挂起链+指标接力：挂起→毛利额→渠道分组→再挂起→净销售额→品类净销售额",
        "turns": [
            T("最近表现怎么样", kind="suspend"),
            T("毛利额", kind="answer", resume=True, merged=True, dsl=dsl("gross_margin")),
            T("上月各渠道毛利额", dsl=dsl("gross_margin", dims=["channel"], time=L1)),
            T("最近卖得如何", kind="suspend"),
            T("净销售额", kind="answer", resume=True, merged=True, dsl=dsl("net_revenue")),
            T("上月各品类净销售额", dsl=dsl("net_revenue", dims=["category"], time=L1)),
        ],
    },
    # ===== 能力四：缓存指纹边界（cache_boundary）×4 =====
    {
        "id": "md31", "scenario": "cache_boundary", "role": "boss", "batch": BATCH,
        "desc": "表层改写 DSL 恒等命中：各品类销售额↔各品类的销售额（同指纹）↔复问（命中）→换指标（不命中）",
        "turns": [
            T("上月各品类销售额", dsl=dsl("revenue", dims=["category"], time=L1)),
            T("上月各品类的销售额", dsl=dsl("revenue", dims=["category"], time=L1), from_cache=True),
            T("上月各品类销售额", dsl=dsl("revenue", dims=["category"], time=L1), from_cache=True),
            T("上月各品类毛利额", dsl=dsl("gross_margin", dims=["category"], time=L1), from_cache=False),
        ],
    },
    {
        "id": "md32", "scenario": "cache_boundary", "role": "boss", "batch": BATCH,
        "desc": "时间窗微改不命中：上月→近3个月→近6个月（指纹逐轮变）→复问上月（命中首轮）",
        "turns": [
            T("上月销售额", dsl=dsl("revenue", time=L1)),
            T("近3个月销售额", dsl=dsl("revenue", time=L3), from_cache=False),
            T("近6个月销售额", dsl=dsl("revenue", time=L6), from_cache=False),
            T("上月销售额", dsl=dsl("revenue", time=L1), from_cache=True),
        ],
    },
    {
        "id": "md33", "scenario": "cache_boundary", "role": "boss", "batch": BATCH,
        "desc": "换角色不串缓存（turn 级 role 覆盖）：boss→south（指纹含 role 不命中，金标 scope 华南）→south 复问命中→回 boss 命中首轮",
        "turns": [
            T("上月销售额", dsl=dsl("revenue", time=L1)),
            T("上月销售额", role="south", dsl=dsl("revenue", time=L1), from_cache=False),
            T("上月销售额", role="south", dsl=dsl("revenue", time=L1), from_cache=True),
            T("上月销售额", dsl=dsl("revenue", time=L1), from_cache=True),
        ],
    },
    {
        "id": "md34", "scenario": "cache_boundary", "role": "boss", "batch": BATCH,
        "desc": "消解面恒等命中：华南地区上月销售额↔上月华南地区的销售额（换说法同指纹命中）→那华北呢（换血不命中）→品类分组",
        "turns": [
            T("华南地区上月销售额", dsl=dsl("revenue", filters=[("region", "华南")], time=L1)),
            T("上月华南地区的销售额", dsl=dsl("revenue", filters=[("region", "华南")], time=L1), from_cache=True),
            T("那华北呢", dsl=dsl("revenue", filters=[("region", "华北")], time=L1), from_cache=False, note=_NOTE_RESOLVED),
            T("上月各品类销售额", dsl=dsl("revenue", dims=["category"], time=L1), from_cache=False),
        ],
    },
    # ===== 能力五：拒答后降维追问（refusal_downgrade）×3 =====
    {
        "id": "md35", "scenario": "refusal_downgrade", "role": "boss", "batch": BATCH,
        "desc": "复购率未采集拒答→降维问退货率分组→数码退货率归因→渠道退货率",
        "turns": [
            T("上个月的复购率是多少", kind="refusal"),
            T("上月各品类退货率", dsl=dsl("refund_rate", dims=["category"], time=L1, direction="asc")),
            T("为什么上月数码品类的退货率下滑了", dsl=dsl("refund_rate", filters=[("category", "数码")], time=L1, direction="asc")),
            T("上月各渠道的退货率", dsl=dsl("refund_rate", dims=["channel"], time=L1, direction="asc")),
        ],
    },
    {
        "id": "md36", "scenario": "refusal_downgrade", "role": "boss", "batch": BATCH,
        "desc": "满意度未采集拒答→降维问销售额→净销售额口径渠道查询→渠道换血",
        "turns": [
            T("客户满意度怎么样", kind="refusal"),
            T("上月销售额", dsl=dsl("revenue", time=L1)),
            T("直播渠道上月净销售额", dsl=dsl("net_revenue", filters=[("channel", "直播")], time=L1)),
            T("那搜索呢", dsl=dsl("net_revenue", filters=[("channel", "搜索")], time=L1), note=_NOTE_RESOLVED),
        ],
    },
    {
        "id": "md37", "scenario": "refusal_downgrade", "role": "boss", "batch": BATCH,
        "desc": "留存率未采集拒答→降维问品类毛利额→食品毛利额归因→品类内渠道下钻",
        "turns": [
            T("上个月的留存率是多少", kind="refusal"),
            T("上月各品类毛利额", dsl=dsl("gross_margin", dims=["category"], time=L1)),
            T("为什么上月食品品类的毛利额下滑了", dsl=dsl("gross_margin", filters=[("category", "食品")], time=L1, direction="asc")),
            T("上月食品品类各渠道的毛利额", dsl=dsl("gross_margin", dims=["channel"], filters=[("category", "食品")], time=L1)),
        ],
    },
    # ===== 能力六：口径约束保持加深（constraint_deep）×5 =====
    {
        "id": "md38", "scenario": "constraint_deep", "role": "boss", "batch": BATCH,
        "desc": "L6→L12 窗口切换继承：华南近6个月→华北换血（L6 保持）→显式切近12个月→西北换血（L12 继承）→切回上月",
        "turns": [
            T("华南地区近6个月销售额", dsl=dsl("revenue", filters=[("region", "华南")], time=L6)),
            T("那华北呢", dsl=dsl("revenue", filters=[("region", "华北")], time=L6), note=_NOTE_RESOLVED),
            T("近12个月华北地区的销售额", dsl=dsl("revenue", filters=[("region", "华北")], time=L12)),
            T("那西北呢", dsl=dsl("revenue", filters=[("region", "西北")], time=L12), note=_NOTE_RESOLVED),
            T("上月华北地区的销售额", dsl=dsl("revenue", filters=[("region", "华北")], time=L1)),
        ],
    },
    {
        "id": "md39", "scenario": "constraint_deep", "role": "boss", "batch": BATCH,
        "desc": "净销售额口径跨 6 轮延续：渠道换血×2→渠道分组（近3个月）→品类过滤→品类分组（上月）",
        "turns": [
            T("官网渠道上月净销售额", dsl=dsl("net_revenue", filters=[("channel", "官网")], time=L1)),
            T("那社交呢", dsl=dsl("net_revenue", filters=[("channel", "社交")], time=L1), note=_NOTE_RESOLVED),
            T("那直播呢", dsl=dsl("net_revenue", filters=[("channel", "直播")], time=L1), note=_NOTE_RESOLVED),
            T("近3个月各渠道净销售额", dsl=dsl("net_revenue", dims=["channel"], time=L3)),
            T("近3个月美妆品类的净销售额", dsl=dsl("net_revenue", filters=[("category", "美妆")], time=L3)),
            T("上月各品类净销售额", dsl=dsl("net_revenue", dims=["category"], time=L1)),
        ],
    },
    {
        "id": "md40", "scenario": "constraint_deep", "role": "boss", "batch": BATCH,
        "desc": "双过滤（region×channel）跨轮保留：换渠道保持大区→换大区保持渠道→显式解除（上月总销售额）→region 回归",
        "turns": [
            T("华南地区直播渠道上月销售额", dsl=dsl("revenue", filters=[("region", "华南"), ("channel", "直播")], time=L1)),
            T("那搜索呢", dsl=dsl("revenue", filters=[("region", "华南"), ("channel", "搜索")], time=L1), note=_NOTE_RESOLVED),
            T("那华北呢", dsl=dsl("revenue", filters=[("region", "华北"), ("channel", "搜索")], time=L1), note=_NOTE_RESOLVED),
            T("上月总销售额", dsl=dsl("revenue", time=L1), from_cache=False),
            T("华南地区上月销售额", dsl=dsl("revenue", filters=[("region", "华南")], time=L1)),
        ],
    },
    {
        "id": "md41", "scenario": "constraint_deep", "role": "boss", "batch": BATCH,
        "desc": "购买客户数 L12 链→L6 切换：黑卡→普通→银卡换血→等级分组→黑卡切近6个月→等级分组跟随",
        "turns": [
            T("黑卡会员近12个月的购买客户数", dsl=dsl("customer_count", filters=[("member_level", "黑卡")], time=L12)),
            T("那普通呢", dsl=dsl("customer_count", filters=[("member_level", "普通")], time=L12), note=_NOTE_RESOLVED),
            T("那银卡呢", dsl=dsl("customer_count", filters=[("member_level", "银卡")], time=L12), note=_NOTE_RESOLVED),
            T("近12个月各会员等级的购买客户数", dsl=dsl("customer_count", dims=["member_level"], time=L12)),
            T("近6个月黑卡会员的购买客户数", dsl=dsl("customer_count", filters=[("member_level", "黑卡")], time=L6)),
            T("近6个月各会员等级的购买客户数", dsl=dsl("customer_count", dims=["member_level"], time=L6)),
        ],
    },
    {
        "id": "md42", "scenario": "constraint_deep", "role": "boss", "batch": BATCH,
        "desc": "品类分组骨架跨 5 指标切换保持：毛利额→毛利率→净销售额→客单价→退货率（asc 翻转），时间窗恒上月",
        "turns": [
            T("上月各品类毛利额", dsl=dsl("gross_margin", dims=["category"], time=L1)),
            T("上月各品类毛利率", dsl=dsl("gross_margin_rate", dims=["category"], time=L1), from_cache=False),
            T("上月各品类净销售额", dsl=dsl("net_revenue", dims=["category"], time=L1)),
            T("上月各品类客单价", dsl=dsl("avg_order_value", dims=["category"], time=L1), from_cache=False),
            T("上月各品类退货率", dsl=dsl("refund_rate", dims=["category"], time=L1, direction="asc")),
        ],
    },
    # ===== 能力七：实体链式换血加深（chain_deep）×8 =====
    {
        "id": "md43", "scenario": "chain_deep", "role": "boss", "batch": BATCH,
        "desc": "region→channel→region 回环链：华南→华北换血→直播渠道→搜索换血→华中（回到 region 类型）→西北换血",
        "turns": [
            T("华南地区上月销售额", dsl=dsl("revenue", filters=[("region", "华南")], time=L1)),
            T("那华北呢", dsl=dsl("revenue", filters=[("region", "华北")], time=L1), note=_NOTE_RESOLVED),
            T("直播渠道上月销售额", dsl=dsl("revenue", filters=[("channel", "直播")], time=L1)),
            T("那搜索呢", dsl=dsl("revenue", filters=[("channel", "搜索")], time=L1), note=_NOTE_RESOLVED),
            T("华中地区上月销售额", dsl=dsl("revenue", filters=[("region", "华中")], time=L1)),
            T("那西北呢", dsl=dsl("revenue", filters=[("region", "西北")], time=L1), note=_NOTE_RESOLVED),
        ],
    },
    {
        "id": "md44", "scenario": "chain_deep", "role": "boss", "batch": BATCH,
        "desc": "三实体类型轮转链：region 华北→华东→channel 搜索→社交→category 美妆→数码（指标随链切换）",
        "turns": [
            T("华北地区上月销售额", dsl=dsl("revenue", filters=[("region", "华北")], time=L1)),
            T("那华东呢", dsl=dsl("revenue", filters=[("region", "华东")], time=L1), note=_NOTE_RESOLVED),
            T("搜索渠道上月净销售额", dsl=dsl("net_revenue", filters=[("channel", "搜索")], time=L1)),
            T("那社交呢", dsl=dsl("net_revenue", filters=[("channel", "社交")], time=L1), note=_NOTE_RESOLVED),
            T("美妆品类上月毛利率", dsl=dsl("gross_margin_rate", filters=[("category", "美妆")], time=L1)),
            T("那数码呢", dsl=dsl("gross_margin_rate", filters=[("category", "数码")], time=L1), note=_NOTE_RESOLVED),
        ],
    },
    {
        "id": "md45", "scenario": "chain_deep", "role": "boss", "batch": BATCH,
        "desc": "member→category 链中插 drill：黑卡→银卡换血→数码毛利额→食品换血→为什么…下滑（归因）→官网净销售额收束",
        "turns": [
            T("黑卡会员上月订单量", dsl=dsl("order_count", filters=[("member_level", "黑卡")], time=L1)),
            T("那银卡呢", dsl=dsl("order_count", filters=[("member_level", "银卡")], time=L1), note=_NOTE_RESOLVED),
            T("数码品类上月毛利额", dsl=dsl("gross_margin", filters=[("category", "数码")], time=L1)),
            T("那食品呢", dsl=dsl("gross_margin", filters=[("category", "食品")], time=L1), note=_NOTE_RESOLVED),
            T("为什么上月食品品类的毛利额下滑了", dsl=dsl("gross_margin", filters=[("category", "食品")], time=L1, direction="asc")),
            T("官网渠道上月净销售额", dsl=dsl("net_revenue", filters=[("channel", "官网")], time=L1)),
        ],
    },
    {
        "id": "md46", "scenario": "chain_deep", "role": "boss", "batch": BATCH,
        "desc": "链中插 clarify：region 链→模糊反问（挂澄清不挂起）→渠道毛利率恢复→搜索→官网换血收尾",
        "turns": [
            T("华南地区上月销售额", dsl=dsl("revenue", filters=[("region", "华南")], time=L1)),
            T("那华北呢", dsl=dsl("revenue", filters=[("region", "华北")], time=L1), note=_NOTE_RESOLVED),
            T("各渠道表现如何", kind="clarify"),
            T("上月各渠道毛利率", dsl=dsl("gross_margin_rate", dims=["channel"], time=L1)),
            T("搜索渠道上月毛利率", dsl=dsl("gross_margin_rate", filters=[("channel", "搜索")], time=L1)),
            T("那官网呢", dsl=dsl("gross_margin_rate", filters=[("channel", "官网")], time=L1), note=_NOTE_RESOLVED),
        ],
    },
    {
        "id": "md47", "scenario": "chain_deep", "role": "boss", "batch": BATCH,
        "desc": "category→region 回环链+drill 中换血：美妆→数码→华南→华北→为什么华北下滑（归因）→那西北呢（drill 问句内换血）",
        "turns": [
            T("美妆品类上月毛利额", dsl=dsl("gross_margin", filters=[("category", "美妆")], time=L1)),
            T("那数码呢", dsl=dsl("gross_margin", filters=[("category", "数码")], time=L1), note=_NOTE_RESOLVED),
            T("华南地区上月销售额", dsl=dsl("revenue", filters=[("region", "华南")], time=L1)),
            T("那华北呢", dsl=dsl("revenue", filters=[("region", "华北")], time=L1), note=_NOTE_RESOLVED),
            T("为什么上月华北地区的销售额下滑了", dsl=dsl("revenue", filters=[("region", "华北")], time=L1, direction="asc")),
            T("那西北呢", dsl=dsl("revenue", filters=[("region", "西北")], time=L1, direction="asc"), note=_NOTE_RESOLVED),
        ],
    },
    {
        "id": "md48", "scenario": "chain_deep", "role": "boss", "batch": BATCH,
        "desc": "channel→member→region 三类型轮转：直播客单价→搜索→金卡客户数→银卡→华东销售额→华中换血",
        "turns": [
            T("直播渠道上月客单价", dsl=dsl("avg_order_value", filters=[("channel", "直播")], time=L1)),
            T("那搜索呢", dsl=dsl("avg_order_value", filters=[("channel", "搜索")], time=L1), note=_NOTE_RESOLVED),
            T("金卡会员上月购买客户数", dsl=dsl("customer_count", filters=[("member_level", "金卡")], time=L1)),
            T("那银卡呢", dsl=dsl("customer_count", filters=[("member_level", "银卡")], time=L1), note=_NOTE_RESOLVED),
            T("华东地区上月销售额", dsl=dsl("revenue", filters=[("region", "华东")], time=L1)),
            T("那华中呢", dsl=dsl("revenue", filters=[("region", "华中")], time=L1), note=_NOTE_RESOLVED),
        ],
    },
    {
        "id": "md49", "scenario": "chain_deep", "role": "boss", "batch": BATCH,
        "desc": "三链×时间窗保持：华南→西南（L1）→食品近3个月销量→家居换血（L3 保持）→银卡近3个月订单量→金卡换血",
        "turns": [
            T("华南地区上月销售额", dsl=dsl("revenue", filters=[("region", "华南")], time=L1)),
            T("那西南呢", dsl=dsl("revenue", filters=[("region", "西南")], time=L1), note=_NOTE_RESOLVED),
            T("食品品类近3个月销量", dsl=dsl("item_quantity", filters=[("category", "食品")], time=L3)),
            T("那家居呢", dsl=dsl("item_quantity", filters=[("category", "家居")], time=L3), note=_NOTE_RESOLVED),
            T("银卡会员近3个月的订单量", dsl=dsl("order_count", filters=[("member_level", "银卡")], time=L3)),
            T("那金卡呢", dsl=dsl("order_count", filters=[("member_level", "金卡")], time=L3), note=_NOTE_RESOLVED),
        ],
    },
    {
        "id": "md50", "scenario": "chain_deep", "role": "boss", "batch": BATCH,
        "desc": "链中回环重问缓存命中：华南→华北换血→复问华南（同指纹命中）→直播净销售额→社交换血→品类分组收束",
        "turns": [
            T("华南地区上月销售额", dsl=dsl("revenue", filters=[("region", "华南")], time=L1)),
            T("那华北呢", dsl=dsl("revenue", filters=[("region", "华北")], time=L1), note=_NOTE_RESOLVED),
            T("华南地区上月销售额", dsl=dsl("revenue", filters=[("region", "华南")], time=L1), from_cache=True),
            T("直播渠道上月净销售额", dsl=dsl("net_revenue", filters=[("channel", "直播")], time=L1)),
            T("那社交呢", dsl=dsl("net_revenue", filters=[("channel", "社交")], time=L1), note=_NOTE_RESOLVED),
            T("上月各品类销售额", dsl=dsl("revenue", dims=["category"], time=L1)),
        ],
    },
    # ===== 能力八：mixed_long 加长（mixed_long）×6（7-8 轮） =====
    {
        "id": "md51", "scenario": "mixed_long", "role": "boss", "batch": BATCH,
        "desc": "八轮混合长流：趋势→会员换血→渠道趋势→净销售额→显式区间→退货率→模糊反问",
        "turns": [
            T("近12个月销售额趋势", dsl=dsl("revenue", dims=["month"], time=L12)),
            T("黑卡会员上月客单价", dsl=dsl("avg_order_value", filters=[("member_level", "黑卡")], time=L1)),
            T("那普通呢", dsl=dsl("avg_order_value", filters=[("member_level", "普通")], time=L1), note=_NOTE_RESOLVED),
            T("近6个月各渠道客单价趋势", dsl=dsl("avg_order_value", dims=["month", "channel"], time=L6)),
            T("上月净销售额", dsl=dsl("net_revenue", time=L1)),
            T("2025年1月到6月的毛利额", dsl=dsl("gross_margin", time=BT("2025-01", "2025-06"))),
            T("上月各品类退货率", dsl=dsl("refund_rate", dims=["category"], time=L1, direction="asc")),
            T("整体怎么样", kind="clarify"),
        ],
    },
    {
        "id": "md52", "scenario": "mixed_long", "role": "boss", "batch": BATCH,
        "desc": "七轮混合长流：region 链→渠道毛利率→回指 fallback→恢复→换血→预测拒答收尾",
        "turns": [
            T("华南地区上月销售额", dsl=dsl("revenue", filters=[("region", "华南")], time=L1)),
            T("那华北呢", dsl=dsl("revenue", filters=[("region", "华北")], time=L1), note=_NOTE_RESOLVED),
            T("上月各渠道毛利率", dsl=dsl("gross_margin_rate", dims=["channel"], time=L1)),
            T("那华东呢", kind="clarify", note=_NOTE_FALLBACK),
            T("华东地区上月订单量", dsl=dsl("order_count", filters=[("region", "华东")], time=L1)),
            T("那西北呢", dsl=dsl("order_count", filters=[("region", "西北")], time=L1), note=_NOTE_RESOLVED),
            T("预测下季度的销售额", kind="refusal"),
        ],
    },
    {
        "id": "md53", "scenario": "mixed_long", "role": "boss", "batch": BATCH,
        "desc": "七轮混合长流：复购率拒答插队→毛利率→净销售额渠道链→between×品类销量→等级客单价分组",
        "turns": [
            T("上月销售额", dsl=dsl("revenue", time=L1)),
            T("上个月的复购率是多少", kind="refusal"),
            T("上月毛利率", dsl=dsl("gross_margin_rate", time=L1)),
            T("官网渠道上月净销售额", dsl=dsl("net_revenue", filters=[("channel", "官网")], time=L1)),
            T("那社交呢", dsl=dsl("net_revenue", filters=[("channel", "社交")], time=L1), note=_NOTE_RESOLVED),
            T("2025年1月到3月美妆品类的销量", dsl=dsl("item_quantity", filters=[("category", "美妆")], time=BT("2025-01", "2025-03"))),
            T("上月各会员等级客单价", dsl=dsl("avg_order_value", dims=["member_level"], time=L1)),
        ],
    },
    {
        "id": "md54", "scenario": "mixed_long", "role": "boss", "batch": BATCH,
        "desc": "八轮混合长流：趋势→品类毛利率换血→净销售额分组→会员客户数换血→between 客单价→模糊反问",
        "turns": [
            T("近12个月销售额趋势", dsl=dsl("revenue", dims=["month"], time=L12)),
            T("数码品类上月毛利率", dsl=dsl("gross_margin_rate", filters=[("category", "数码")], time=L1)),
            T("那食品呢", dsl=dsl("gross_margin_rate", filters=[("category", "食品")], time=L1), note=_NOTE_RESOLVED),
            T("上月各品类净销售额", dsl=dsl("net_revenue", dims=["category"], time=L1)),
            T("黑卡会员上月购买客户数", dsl=dsl("customer_count", filters=[("member_level", "黑卡")], time=L1)),
            T("那银卡呢", dsl=dsl("customer_count", filters=[("member_level", "银卡")], time=L1), note=_NOTE_RESOLVED),
            T("2025年2月到4月的客单价", dsl=dsl("avg_order_value", time=BT("2025-02", "2025-04"))),
            T("整体表现如何", kind="clarify"),
        ],
    },
    {
        "id": "md55", "scenario": "mixed_long", "role": "boss", "batch": BATCH,
        "desc": "七轮混合长流：渠道链→品类毛利额→归因 drill→渠道毛利额→region 回归→换血收尾",
        "turns": [
            T("官网渠道近3个月销售额", dsl=dsl("revenue", filters=[("channel", "官网")], time=L3)),
            T("那直播呢", dsl=dsl("revenue", filters=[("channel", "直播")], time=L3), note=_NOTE_RESOLVED),
            T("上月各品类毛利额", dsl=dsl("gross_margin", dims=["category"], time=L1)),
            T("为什么上月家居品类的毛利额下滑了", dsl=dsl("gross_margin", filters=[("category", "家居")], time=L1, direction="asc")),
            T("上月各渠道毛利额", dsl=dsl("gross_margin", dims=["channel"], time=L1)),
            T("华北地区上月销售额", dsl=dsl("revenue", filters=[("region", "华北")], time=L1)),
            T("那东北呢", dsl=dsl("revenue", filters=[("region", "东北")], time=L1), note=_NOTE_RESOLVED),
        ],
    },
    {
        "id": "md56", "scenario": "mixed_long", "role": "boss", "batch": BATCH,
        "desc": "八轮混合长流：品类毛利率换血→渠道净销售额→回指 fallback→恢复→客户数 L12→between×渠道趋势收尾",
        "turns": [
            T("上月销售额", dsl=dsl("revenue", time=L1)),
            T("美妆品类上月毛利率", dsl=dsl("gross_margin_rate", filters=[("category", "美妆")], time=L1)),
            T("那数码呢", dsl=dsl("gross_margin_rate", filters=[("category", "数码")], time=L1), note=_NOTE_RESOLVED),
            T("上月各渠道净销售额", dsl=dsl("net_revenue", dims=["channel"], time=L1)),
            T("那官网呢", kind="clarify", note=_NOTE_FALLBACK),
            T("官网渠道上月净销售额", dsl=dsl("net_revenue", filters=[("channel", "官网")], time=L1)),
            T("近12个月购买客户数", dsl=dsl("customer_count", time=L12)),
            T("2025年1月到6月各渠道销售额趋势", dsl=dsl("revenue", dims=["month", "channel"], time=BT("2025-01", "2025-06"))),
        ],
    },
    # ===== 能力九：多实体守卫（multi_entity_guard）×4 =====
    {
        "id": "md57", "scenario": "multi_entity_guard", "role": "boss", "batch": BATCH,
        "desc": "同类型双实体歧义回退（region）：对比华南和华北→那华东呢（上轮含多个 region 实体，保守回退可观测）→完整问题恢复→单实体换血恢复",
        "turns": [
            T("对比华南地区和华北地区的销售额", dsl=dsl("revenue", filters=[("region", "华南")], time={"mode": "all"})),
            T("那华东呢", kind="clarify", note=_NOTE_AMBIGUOUS),
            T("华东地区上月销售额", dsl=dsl("revenue", filters=[("region", "华东")], time=L1)),
            T("那华中呢", dsl=dsl("revenue", filters=[("region", "华中")], time=L1), note=_NOTE_RESOLVED),
        ],
    },
    {
        "id": "md58", "scenario": "multi_entity_guard", "role": "boss", "batch": BATCH,
        "desc": "同类型双实体歧义回退（channel）：对比直播和搜索净销售额→那官网呢（歧义回退）→完整问题恢复→换血恢复",
        "turns": [
            T("对比直播渠道和搜索渠道的净销售额", dsl=dsl("net_revenue", filters=[("channel", "直播")], time={"mode": "all"})),
            T("那官网呢", kind="clarify", note=_NOTE_AMBIGUOUS),
            T("官网渠道上月净销售额", dsl=dsl("net_revenue", filters=[("channel", "官网")], time=L1)),
            T("那社交呢", dsl=dsl("net_revenue", filters=[("channel", "社交")], time=L1), note=_NOTE_RESOLVED),
        ],
    },
    {
        "id": "md59", "scenario": "multi_entity_guard", "role": "boss", "batch": BATCH,
        "desc": "一问双实体（region×channel）各换血一次且另一类型保持：华南×直播→换渠道（大区保持）→换大区（渠道保持）→渠道分组收束",
        "turns": [
            T("华南地区直播渠道上月销售额", dsl=dsl("revenue", filters=[("region", "华南"), ("channel", "直播")], time=L1)),
            T("那搜索呢", dsl=dsl("revenue", filters=[("region", "华南"), ("channel", "搜索")], time=L1), note=_NOTE_RESOLVED),
            T("那华北呢", dsl=dsl("revenue", filters=[("region", "华北"), ("channel", "搜索")], time=L1), note=_NOTE_RESOLVED),
            T("上月各渠道销售额", dsl=dsl("revenue", dims=["channel"], time=L1)),
        ],
    },
    {
        "id": "md60", "scenario": "multi_entity_guard", "role": "boss", "batch": BATCH,
        "desc": "一问双实体（member×category）各换血一次：黑卡×美妆客单价→换品类（等级保持）→换等级（品类保持）→等级分组收束",
        "turns": [
            T("黑卡会员美妆品类上月客单价", dsl=dsl("avg_order_value", filters=[("member_level", "黑卡"), ("category", "美妆")], time=L1)),
            T("那数码品类呢", dsl=dsl("avg_order_value", filters=[("member_level", "黑卡"), ("category", "数码")], time=L1), note=_NOTE_RESOLVED),
            T("那银卡会员呢", dsl=dsl("avg_order_value", filters=[("member_level", "银卡"), ("category", "数码")], time=L1), note=_NOTE_RESOLVED),
            T("上月各会员等级客单价", dsl=dsl("avg_order_value", dims=["member_level"], time=L1)),
        ],
    },
]


# ---------------- 冻结前缀（只读，逐字节保留） ----------------

def _frozen_lines() -> list[str]:
    """冻结文件全部非空行；[0] 为 _note 元信息行，[1:] 为 20 组会话行（字节原样）。"""
    lines = [ln for ln in FROZEN.read_text(encoding="utf-8").splitlines() if ln.strip()]
    groups = lines[1:]
    ids = [json.loads(ln)["id"] for ln in groups]
    if ids != FROZEN_IDS:
        raise SystemExit(f"冻结源会话行 id 与 {FROZEN_IDS[0]}..{FROZEN_IDS[-1]} 不符: {ids}")
    return lines


# ---------------- 四重探针（与 build_multiturn_deep 同款口径） ----------------

def _canon(rows: list[list]) -> list[tuple]:
    return sorted(
        tuple(round(v, 4) if isinstance(v, (int, float)) and not isinstance(v, bool) else v for v in r)
        for r in rows
    )


def _probe_answer(effective_q: str, role: str, gold_sql: str, tag: str) -> list[str]:
    """mock 重答有效问题与金标同库比对（LIMIT 剥离口径 + 截断边界 strict 断言）。"""
    problems: list[str] = []
    # 第四重（AB-29 / 1B6 limit_invariant 装置）：金标截断边界并列 → 该轮金标不得入集
    ok, detail = limit_boundary_strict(gold_sql)
    if not ok:
        return [f"{tag} [{role}] {detail}"]
    try:
        parsed = QueryDSL(**_parse_question(effective_q))
        got_sql, _ = compile_sql(parsed, role=role)
    except Exception as e:  # noqa: BLE001
        return [f"{tag} [{role}] mock 解析/编译失败: {e}\n    Q: {effective_q}"]
    gold_body, gold_limit = strip_limit(gold_sql)
    got_body, got_limit = strip_limit(got_sql)
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


def _effective_role(item: dict, turn: dict) -> str:
    return turn.get("role", item.get("role", "boss"))


def self_check() -> list[str]:
    """构建期四重自检：冻结 20 组 + 新增 40 组 = 60 组逐轮（模拟 runner 接线）。"""
    problems: list[str] = []

    frozen_groups = [json.loads(ln) for ln in _frozen_lines()[1:]]
    turns_total = 0
    fc_true = fc_false = 0

    for groups, is_frozen in ((frozen_groups, True), (ITEMS_V2, False)):
        for item in groups:
            ctx = ""            # 与 runner 一致：ctx.set(sid, 改写后问题)——每轮都存
            suspended_q = ""
            fp_seen: set[tuple[str, str]] = set()   # 早前 answer 轮的 (指纹, role)，指纹取 mock 解析结果
            for tno, turn in enumerate(item["turns"], 1):
                turns_total += 1
                tag = f"{item['id']}.t{tno}"
                exp = turn["expect"]
                kind = exp["kind"]
                role = _effective_role(item, turn)
                if turn.get("resume"):
                    # runner 接线次序：先 resolve（resume 答案含指标词恒不变）→ ctx.set；
                    # 图内 clarify_suspend_node 合并 f'{挂起时问题}（用户澄清：{answer}）'
                    raw_q, _ = resolve_followup(turn["q"], ctx, enabled=True)
                    ctx = raw_q
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
                # kind == answer：金标（冻结组取 gold_sql；新增组由意图 DSL 按 role 编译）
                if is_frozen:
                    gold_sql = exp.get("gold_sql")
                    if not gold_sql:
                        problems.append(f"{tag} [冻结] answer 轮缺 gold_sql")
                        continue
                else:
                    gold_sql = compile_sql(QueryDSL(**exp["dsl"]), role=role)[0]
                problems += _probe_answer(effective, role, gold_sql, tag)
                # from_cache 钉死双向校验（指纹 = fingerprint(mock 解析 DSL, role)，与 runner 缓存键同源）
                fc = exp.get("from_cache")
                if fc is not None:
                    fp = fingerprint(_parse_question(effective), role)
                    if fc and (fp, role) not in fp_seen:
                        problems.append(f"{tag} from_cache=True 但同组早前无同「指纹×role」轮（不会命中）")
                    if not fc and (fp, role) in fp_seen:
                        problems.append(f"{tag} from_cache=False 但同组早前已有同「指纹×role」轮（会误命中）")
                    if fc:
                        fc_true += 1
                    else:
                        fc_false += 1
                fp_seen.add((fingerprint(_parse_question(effective), role), role))
            # 新增组策略自检（冻结组由其构建脚本与测试守门，此处只做四重探针）
            if not is_frozen:
                problems += _policy_check(item)
    kind_stat = Counter(
        t["expect"]["kind"] for g in ([json.loads(ln) for ln in _frozen_lines()[1:]], ITEMS_V2)
        for it in g for t in it["turns"]
    )
    print(f"自检统计：{FROZEN_GROUP_COUNT + len(ITEMS_V2)} 组 / {turns_total} 轮"
          f"（冻结 {FROZEN_GROUP_COUNT} + 新增 {len(ITEMS_V2)}），轮型分布 {dict(sorted(kind_stat.items()))}，"
          f"from_cache 钉死 True×{fc_true} / False×{fc_false}")
    return problems


def _policy_check(item: dict) -> list[str]:
    """新增组策略自检：id/batch/轮数边界/能力覆盖/场景专属钉子。"""
    problems: list[str] = []
    sid, scen = item["id"], item["scenario"]
    if item.get("batch") != BATCH:
        problems.append(f"{sid} 新增组缺 batch={BATCH}")
    lo, hi = TURN_BOUNDS.get(scen, TURN_BOUNDS["_default"])
    n = len(item["turns"])
    if not (lo <= n <= hi):
        problems.append(f"{sid} [{scen}] 轮数 {n} 越界（要求 {lo}-{hi}）")
    # 能力覆盖：每组至少一个超出裸 answer(boss 金标) 的期望——
    # resolve/constraint/chain 深水组的能力钉在 note_contains/from_cache/非 answer 轮/
    # turn 级 role 覆盖/非 boss 组的 scope 编译金标上
    has_capability = any(
        t["expect"].get("note_contains") or t["expect"].get("merged_contains")
        or t["expect"].get("from_cache") is not None or t["expect"]["kind"] != "answer"
        or t.get("role") not in (None, "boss")
        for t in item["turns"]
    ) or item.get("role", "boss") != "boss"
    if not has_capability:
        problems.append(f"{sid} 无任何超出裸 answer 的期望（缺能力覆盖）")
    if scen == "double_suspend":
        n_suspend = sum(1 for t in item["turns"] if t["expect"]["kind"] == "suspend")
        if n_suspend != 2:
            problems.append(f"{sid} 双挂起链 suspend 轮数 {n_suspend} != 2")
        if not item.get("suspend"):
            problems.append(f"{sid} double_suspend 组缺 suspend=true（runner 走一次性路径挂不起）")
    if scen == "cache_boundary":
        pins = [t["expect"].get("from_cache") for t in item["turns"] if t["expect"].get("from_cache") is not None]
        if not (True in pins and False in pins):
            problems.append(f"{sid} 缓存边界组须同时钉死命中与不命中（现钉 {pins}）")
    if scen == "multi_entity_guard":
        notes = [t["expect"].get("note_contains", "") for t in item["turns"]]
        if not any(_NOTE_AMBIGUOUS in (x or "") or _NOTE_RESOLVED in (x or "") for x in notes):
            problems.append(f"{sid} 多实体守卫组须有消解/歧义 note 可观测")
    return problems


def integrity_check() -> list[str]:
    """完整性校验（不写盘口径）：60 组 / 冻结 20 组逐字节保留 / 新增 40 组 id 接续 / 金标单源归属。"""
    problems: list[str] = []
    frozen_lines = _frozen_lines()
    frozen_group_lines = frozen_lines[1:]

    if len(ITEMS_V2) != NEW_GROUP_COUNT:
        problems.append(f"新增组数应为 {NEW_GROUP_COUNT}，实际 {len(ITEMS_V2)}")
    if [it["id"] for it in ITEMS_V2] != NEW_IDS:
        problems.append(f"新增组 id 应为 {NEW_IDS[0]}..{NEW_IDS[-1]} 连续，实际 {[it['id'] for it in ITEMS_V2]}")
    scen_stat = Counter(it["scenario"] for it in ITEMS_V2)
    for scen, want in AREA_PLAN.items():
        if scen_stat.get(scen, 0) != want:
            problems.append(f"场景 {scen} 组数应为 {want}，实际 {scen_stat.get(scen, 0)}")
    extra = set(scen_stat) - set(AREA_PLAN)
    if extra:
        problems.append(f"未登记场景：{sorted(extra)}")

    # ecommerce 单源归属：新增组金标必含本源 base_from、绝不含他源表
    for it in ITEMS_V2:
        for tno, t in enumerate(it["turns"], 1):
            sql = t["expect"].get("gold_sql")
            if not sql:
                continue
            if "FROM orders o JOIN order_items" not in sql:
                problems.append(f"{it['id']}.t{tno} 金标缺 ecommerce base_from（FROM orders o JOIN order_items）")
            for foreign in FOREIGN_TABLES:
                if foreign in sql:
                    problems.append(f"{it['id']}.t{tno} 金标混入他源表 {foreign}")

    # 组装口径（与 build 同一函数）：冻结 20 组会话行逐字节保留在前
    out_lines, _ = _assemble()
    if out_lines[1:1 + FROZEN_GROUP_COUNT] != frozen_group_lines:
        drifted = next((i for i, (a, b) in enumerate(zip(out_lines[1:], frozen_group_lines)) if a != b), -1)
        problems.append(f"冻结 20 组会话行逐字节保留被破坏（首个漂移组行号 {drifted}）")
    total_groups = FROZEN_GROUP_COUNT + len(ITEMS_V2)
    if len(out_lines) - 1 != total_groups:
        problems.append(f"产物会话行数应为 {total_groups}，实际 {len(out_lines) - 1}")
    return problems


def _serialize_item(item: dict) -> str:
    """新增组 → jsonl 行：金标按 effective role 编译；turn 级 role 覆盖回写轮对象。"""
    out_item = {k: item[k] for k in ("id", "scenario", "role", "desc", "batch") if k in item}
    if item.get("suspend"):
        out_item["suspend"] = True
    turns = []
    for turn in item["turns"]:
        exp = turn["expect"]
        out_exp: dict = {"kind": exp["kind"]}
        if exp.get("dsl") is not None:
            role = _effective_role(item, turn)
            out_exp["gold_sql"] = compile_sql(QueryDSL(**exp["dsl"]), role=role)[0]
        for k in ("from_cache", "note_contains", "merged_contains"):
            if exp.get(k) is not None:
                out_exp[k] = exp[k]
        t = {"q": turn["q"]}
        if turn.get("resume"):
            t["resume"] = True
        if turn.get("role") is not None:
            t["role"] = turn["role"]
        t["expect"] = out_exp
        turns.append(t)
    out_item["turns"] = turns
    return json.dumps(out_item, ensure_ascii=False)


def _assemble() -> tuple[list[str], int]:
    """产物行：v2 元信息行 + 冻结 20 组会话行（字节原样）+ 新增 40 组行。"""
    frozen_lines = _frozen_lines()
    new_lines = [_serialize_item(it) for it in ITEMS_V2]
    new_turns = sum(len(it["turns"]) for it in ITEMS_V2)
    note = (
        f"多轮长对话评测集扩容（[1B6]，2026-11-20）：{FROZEN_GROUP_COUNT + len(ITEMS_V2)} 组 = "
        f"冻结 multiturn_deep.jsonl {FROZEN_GROUP_COUNT} 组（md01..md20）逐行字节保留 + "
        f"新增 {len(ITEMS_V2)} 组（md21..md60，batch={BATCH}，{new_turns} 轮），ecommerce 单源深评"
        "（跨源多轮见 multiturn_cross_b/c/d）。新增能力面：实体链×角色交叉（role_x_chain）/显式年月区间"
        "多轮继承（between_window）/双挂起链（double_suspend）/缓存指纹边界（cache_boundary，含 turn 级"
        "role 覆盖）/拒答后降维追问（refusal_downgrade）/口径约束保持加深（constraint_deep）/实体链式"
        "换血加深（chain_deep）/混合长流加长 7-8 轮（mixed_long）/多实体守卫（multi_entity_guard）。"
        "answer 轮金标由 compile_sql(role) 生成（scripts/build_multiturn_deep_v2.py 可重建/自检，"
        "四重探针含 limit_invariant 截断边界 strict 与 from_cache 双向指纹钉死）。"
        "判分复用 app.eval.runner.judge_query；本行为元信息行（runner 按 id+turns 识别会话行）。"
    )
    lines = [json.dumps({"_note": note}, ensure_ascii=False)] + frozen_lines[1:] + new_lines
    return lines, new_turns


def build(path: Path | None) -> str:
    lines, new_turns = _assemble()
    if path is not None:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    total_turns = sum(len(json.loads(ln)["turns"]) for ln in lines[1:])
    return (f"已构建 {FROZEN_GROUP_COUNT + len(ITEMS_V2)} 组 / {total_turns} 轮"
            f"（冻结 {FROZEN_GROUP_COUNT} 组逐字节保留 + 新增 {len(ITEMS_V2)} 组 {new_turns} 轮，金标 compile_sql 生成）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只跑自检 + 完整性校验，不写盘")
    args = ap.parse_args()

    problems = self_check() + integrity_check()
    if problems:
        print(f"自检未通过 {len(problems)} 条：")
        for p in problems:
            print("  -", p)
        return 1
    print("自检通过：60 组逐轮有效问题（消解/合并后）mock 重答与金标同库一致；"
          "clarify/refusal/suspend verdict 实判一致；note/merged 期望与 resolver 产出核对；"
          "金标过 limit_boundary_strict；from_cache 双向指纹钉死；"
          "冻结 20 组会话行逐字节保留 + 新增 40 组 id 接续 + 配比正确 + 金标单源归属")

    msg = build(None if args.check else TARGET)
    print(msg)
    if not args.check:
        print(f"已写入 {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
