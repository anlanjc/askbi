"""构建跨源多轮评测集（[1B6]）：eval/multiturn_cross_b/_c/_d.json —— coffee/saas/hotel 三源各 7-8 组 × 3-5 轮。

与 scripts/build_multiturn_deep.py（ecommerce 单源长对话金标，95 轮冻结）的分工：
deep 验证的是"默认源上的多轮能力"，本脚本把 deep 的 9 类 scenario 模板与四重
构建期自检（探针-定稿口径）**全部 spec 参数化**后搬到三个非默认数据源——
端到端验证 [1B5] 指代消解词表 spec 化后"跨源多轮"可判定：

- 切 DATASET=coffee 后"那外卖呢"随 coffee 词表消解（channel: 堂食/外带/外卖）、
  DATASET=saas 后"那旗舰版呢"随 tier 词表（基础/专业/旗舰）、DATASET=hotel 后
  "那度假呢"随 hotel_type 词表（商务/度假/精品）——1B5 收口后这些消解应正确；
- 金标一律 compile_sql(role, spec=X) 生成（ROLE_SCOPES 三源共享：boss/south/north
  随源编译注入 region scope）；角色金标即"同一组织角色跨数据源的口径收窄"。

自检四重（与 deep 同口径，逐点 spec 化——deep 硬编码 ecommerce 的位置逐一对账）：
1. 逐轮模拟 runner 接线（本进程临时切 settings.dataset → resolve_followup 词表随
   spec 走 → ctx.set 存改写后问题）推导有效问题；mock 解析器按 spec 重答
   （_parse_question(q, spec) + build_dsl(spec) + validate_filter_values(spec)——
   与 sql_agent_node 完全同构，产出非法过滤值即 fail-fast：线上会拒答而期望 answer），
   与金标在**对应 spec 库**（execute_sync(db_path=spec.db_abs_path)）比对
   （LIMIT 剥离口径 + LIMIT 行相等）；answer 轮金标过截断边界 strict 断言
   （AB-29，limit_boundary_strict(db_path=spec.db_abs_path)）；
2. clarify/refusal 轮 mock clarifier 实判 verdict（_clarifier(dataset=spec.name)，
   指标别名随 spec）；suspend 轮断言 verdict=clarify（挂起由 clarify 触发）；
   merged_contains 轮按 clarify_suspend_node 合并规则构造合并问题后同样探针；
3. note_contains 期望与 resolver 实际产出逐条核对（指代消解/保守回退两形态）；
4. from_cache=true 的轮断言与同组早前轮 DSL 指纹相等——指纹按 spec 计算
   （fingerprint(dsl, role, dataset=spec.name)：dataset 显式参与，跨源绝不串键）。

用法（项目根，须用 askbi/.venv/Scripts/python.exe）：
    python scripts/build_multiturn_cross_source.py            # 构建 + 自检，写三个文件
    python scripts/build_multiturn_cross_source.py --check    # 只自检不写盘

预期跑分命令（主会话执行，本脚本不跑全量评测）：
    DATASET=coffee LLM_PROVIDER=mock python scripts/run_multiturn_eval.py --dataset eval/multiturn_cross_b.json
    DATASET=saas   LLM_PROVIDER=mock python scripts/run_multiturn_eval.py --dataset eval/multiturn_cross_c.json
    DATASET=hotel  LLM_PROVIDER=mock python scripts/run_multiturn_eval.py --dataset eval/multiturn_cross_d.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.cache import fingerprint
from app.config import get_settings
from app.context import resolve_followup
from app.data.dataset_b import COFFEE_SPEC
from app.data.dataset_c import SAAS_SPEC
from app.data.dataset_d import HOTEL_SPEC
from app.data.dsl import build_dsl, compile_sql, validate_filter_values
from app.data.sandbox import execute_sync
from app.eval.limit_invariant import limit_boundary_strict
from app.llm.mock import _clarifier, _parse_question

TARGETS = {
    "coffee": ROOT / "eval" / "multiturn_cross_b.json",
    "saas": ROOT / "eval" / "multiturn_cross_c.json",
    "hotel": ROOT / "eval" / "multiturn_cross_d.json",
}

# deep 的 9 类 scenario 模板（三源覆盖并集必须齐）
SCENARIOS = (
    "resolve_chain", "constraint_keep", "clarify_then_drill", "topic_switch",
    "topic_switch_cache", "role_mixed", "refusal_mixed", "clarify_mixed", "mixed_long",
)


def dsl(
    metric: str,
    dims: list[str] | None = None,
    filters: list[tuple[str, object]] | None = None,
    time: dict | None = None,
    limit: int | None = None,
    direction: str | None = None,
) -> dict | None:
    """意图 DSL 简写构造器；None = 无金标轮（suspend/clarify/refusal）。字段 id 随源 spec。"""
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


# ================= coffee（数据集 B：堂食/外带/外卖 × 饮品/烘焙 × 七大区） =================
# 实体链换血用 coffee 词表：channel(堂食/外带/外卖) store_type(旗舰店/标准店/店中店)
# category(饮品/烘焙) subcategory(咖啡/茶饮/面包/蛋糕/酥点) region(七大区)

ITEMS_CB: list[dict] = [
    # ---- resolve_chain ×3（channel / store_type / category→subcategory 跨维链） ----
    {
        "id": "mcb01", "scenario": "resolve_chain", "role": "boss",
        "desc": "coffee channel 实体链：堂食→外卖→外带，末轮渠道分组",
        "turns": [
            T("上月堂食渠道的销售额", dsl=dsl("revenue", filters=[("channel", "堂食")], time=L1)),
            T("那外卖呢", dsl=dsl("revenue", filters=[("channel", "外卖")], time=L1), note=_NOTE_RESOLVED),
            T("那外带呢", dsl=dsl("revenue", filters=[("channel", "外带")], time=L1), note=_NOTE_RESOLVED),
            T("上月各渠道销售额", dsl=dsl("revenue", dims=["channel"], time=L1)),
        ],
    },
    {
        "id": "mcb02", "scenario": "resolve_chain", "role": "boss",
        "desc": "coffee store_type 实体链：旗舰店→标准店，中间话题回环重问（缓存命中）→店中店",
        "turns": [
            T("上月旗舰店店型的客单价", dsl=dsl("avg_order_value", filters=[("store_type", "旗舰店")], time=L1)),
            T("那标准店呢", dsl=dsl("avg_order_value", filters=[("store_type", "标准店")], time=L1), note=_NOTE_RESOLVED),
            T("上月旗舰店店型的客单价", dsl=dsl("avg_order_value", filters=[("store_type", "旗舰店")], time=L1),
              from_cache=True),
            T("上月各店型客单价", dsl=dsl("avg_order_value", dims=["store_type"], time=L1)),
        ],
    },
    {
        "id": "mcb03", "scenario": "resolve_chain", "role": "boss",
        "desc": "coffee 跨维度实体链：category 换血（烘焙→饮品）→换话题 subcategory 换血（咖啡→茶饮）→品类分组",
        "turns": [
            T("近6个月烘焙品类的毛利率", dsl=dsl("gross_margin_rate", filters=[("category", "烘焙")], time=L6)),
            T("那饮品呢", dsl=dsl("gross_margin_rate", filters=[("category", "饮品")], time=L6), note=_NOTE_RESOLVED),
            T("近6个月咖啡子类的销量", dsl=dsl("item_quantity", filters=[("subcategory", "咖啡")], time=L6)),
            T("那茶饮呢", dsl=dsl("item_quantity", filters=[("subcategory", "茶饮")], time=L6), note=_NOTE_RESOLVED),
            T("上月各品类毛利率", dsl=dsl("gross_margin_rate", dims=["category"], time=L1)),
        ],
    },
    # ---- constraint_keep（毛利额+L12 窗口在 region 换血中保持） ----
    {
        "id": "mcb04", "scenario": "constraint_keep", "role": "boss",
        "desc": "coffee 毛利额+近12个月窗口在 region 换血中保持（华南→华北→西南），末轮城市分组",
        "turns": [
            T("华南地区近12个月毛利额", dsl=dsl("gross_margin", filters=[("region", "华南")], time=L12)),
            T("那华北呢", dsl=dsl("gross_margin", filters=[("region", "华北")], time=L12), note=_NOTE_RESOLVED),
            T("那西南呢", dsl=dsl("gross_margin", filters=[("region", "西南")], time=L12), note=_NOTE_RESOLVED),
            T("近12个月各城市毛利额", dsl=dsl("gross_margin", dims=["city"], time=L12)),
        ],
    },
    # ---- clarify_then_drill（挂起→恢复→同 thread 追问） ----
    {
        "id": "mcb05", "scenario": "clarify_then_drill", "role": "boss", "suspend": True,
        "desc": "coffee 澄清挂起→恢复出数→渠道订单量→归因追问（客单价下滑）",
        "turns": [
            T("最近卖得怎么样", kind="suspend"),
            T("销售额", kind="answer", resume=True, merged=True, dsl=dsl("revenue")),
            T("上月各渠道订单量", dsl=dsl("order_count", dims=["channel"], time=L1)),
            T("为什么上月烘焙品类的客单价下滑了",
              dsl=dsl("avg_order_value", filters=[("category", "烘焙")], time=L1, direction="asc")),
        ],
    },
    # ---- topic_switch（回指不可达→保守回退→恢复→换血） ----
    {
        "id": "mcb06", "scenario": "topic_switch", "role": "boss",
        "desc": "coffee store_type→品类分组话题切换后回指失败（上轮无店型实体→保守回退）→恢复→店中店换血",
        "turns": [
            T("上月旗舰店店型的销售额", dsl=dsl("revenue", filters=[("store_type", "旗舰店")], time=L1)),
            T("上月各品类的销量", dsl=dsl("item_quantity", dims=["category"], time=L1)),
            T("那标准店呢", kind="clarify", note=_NOTE_FALLBACK),
            T("上月标准店店型的销售额", dsl=dsl("revenue", filters=[("store_type", "标准店")], time=L1)),
            T("那店中店呢", dsl=dsl("revenue", filters=[("store_type", "店中店")], time=L1), note=_NOTE_RESOLVED),
        ],
    },
    # ---- mixed_long（六轮混合长流） ----
    {
        "id": "mcb07", "scenario": "mixed_long", "role": "boss",
        "desc": "coffee 六轮混合长流：趋势→渠道换血→店型趋势→显式区间毛利额",
        "turns": [
            T("近12个月销售额趋势", dsl=dsl("revenue", dims=["month"], time=L12)),
            T("上月外卖渠道的客单价", dsl=dsl("avg_order_value", filters=[("channel", "外卖")], time=L1)),
            T("那堂食呢", dsl=dsl("avg_order_value", filters=[("channel", "堂食")], time=L1), note=_NOTE_RESOLVED),
            T("近6个月各店型销售额趋势", dsl=dsl("revenue", dims=["month", "store_type"], time=L6)),
            T("2025年1月到6月的毛利额", dsl=dsl("gross_margin", time=BT("2025-01", "2025-06"))),
        ],
    },
    # ---- refusal_mixed（拒答插入对话） ----
    {
        "id": "mcb08", "scenario": "refusal_mixed", "role": "boss",
        "desc": "coffee 拒答插入对话：预测类拒答→回到历史数据→店型分组→品类换血",
        "turns": [
            T("上月堂食渠道的销量", dsl=dsl("item_quantity", filters=[("channel", "堂食")], time=L1)),
            T("预测下季度的销售额", kind="refusal"),
            T("上月各店型的客单价", dsl=dsl("avg_order_value", dims=["store_type"], time=L1)),
            T("上月烘焙品类的订单量", dsl=dsl("order_count", filters=[("category", "烘焙")], time=L1)),
            T("那饮品呢", dsl=dsl("order_count", filters=[("category", "饮品")], time=L1), note=_NOTE_RESOLVED),
        ],
    },
]

# ================= saas（数据集 C：基础/专业/旗舰 × 五行业 × 直销/代理/自助） =================

ITEMS_CC: list[dict] = [
    # ---- resolve_chain（tier 实体链） ----
    {
        "id": "mcc01", "scenario": "resolve_chain", "role": "boss",
        "desc": "saas tier 实体链：旗舰版→专业版→基础版，末轮档位分组",
        "turns": [
            T("上月旗舰版套餐档位的MRR", dsl=dsl("mrr", filters=[("tier", "旗舰版")], time=L1)),
            T("那专业版呢", dsl=dsl("mrr", filters=[("tier", "专业版")], time=L1), note=_NOTE_RESOLVED),
            T("那基础版呢", dsl=dsl("mrr", filters=[("tier", "基础版")], time=L1), note=_NOTE_RESOLVED),
            T("上月各套餐档位的MRR", dsl=dsl("mrr", dims=["tier"], time=L1)),
        ],
    },
    # ---- clarify_mixed（中途澄清→补全分组→对比收束） ----
    {
        "id": "mcc02", "scenario": "clarify_mixed", "role": "boss",
        "desc": "saas 中途澄清→行业 ARPU 分组→档位毛利额→对比收束",
        "turns": [
            T("上月MRR", dsl=dsl("mrr", time=L1)),
            T("整体怎么样", kind="clarify"),
            T("上月各行业的ARPU", dsl=dsl("arpu", dims=["industry"], time=L1)),
            T("上月各套餐档位的毛利额", dsl=dsl("gross_margin", dims=["tier"], time=L1)),
            T("对比上月各套餐档位的毛利额差异", dsl=dsl("gross_margin", dims=["tier"], time=L1)),
        ],
    },
    # ---- constraint_keep（毛利率+L6 窗口在 channel 换血中保持，中间回环重问命中缓存） ----
    {
        "id": "mcc03", "scenario": "constraint_keep", "role": "boss",
        "desc": "saas 毛利率+近6个月窗口在渠道换血中保持（代理→直销→回环重问缓存→自助），末轮渠道分组",
        "turns": [
            T("代理渠道近6个月毛利率", dsl=dsl("gross_margin_rate", filters=[("channel", "代理")], time=L6)),
            T("那直销呢", dsl=dsl("gross_margin_rate", filters=[("channel", "直销")], time=L6), note=_NOTE_RESOLVED),
            T("代理渠道近6个月毛利率", dsl=dsl("gross_margin_rate", filters=[("channel", "代理")], time=L6),
              from_cache=True),
            T("那自助呢", dsl=dsl("gross_margin_rate", filters=[("channel", "自助")], time=L6), note=_NOTE_RESOLVED),
            T("近6个月各渠道毛利率", dsl=dsl("gross_margin_rate", dims=["channel"], time=L6)),
        ],
    },
    # ---- clarify_then_drill（挂起→恢复→同 thread 追问） ----
    {
        "id": "mcc04", "scenario": "clarify_then_drill", "role": "boss", "suspend": True,
        "desc": "saas 澄清挂起→恢复出数→档位席位数→归因追问（教育行业 ARPU 下滑）",
        "turns": [
            T("最近表现怎么样", kind="suspend"),
            T("MRR", kind="answer", resume=True, merged=True, dsl=dsl("mrr")),
            T("上月各套餐档位的席位数", dsl=dsl("seat_count", dims=["tier"], time=L1)),
            T("为什么上月教育行业的ARPU下滑了",
              dsl=dsl("arpu", filters=[("industry", "教育")], time=L1, direction="asc")),
        ],
    },
    # ---- topic_switch（industry→tier 分组后回指失败→恢复→换血） ----
    {
        "id": "mcc05", "scenario": "topic_switch", "role": "boss",
        "desc": "saas industry→档位分组话题切换后回指失败（上轮无行业实体→保守回退）→恢复→金融换血",
        "turns": [
            T("上月制造行业的席位数", dsl=dsl("seat_count", filters=[("industry", "制造")], time=L1)),
            T("上月各套餐档位的账单数", dsl=dsl("invoice_count", dims=["tier"], time=L1)),
            T("那零售呢", kind="clarify", note=_NOTE_FALLBACK),
            T("上月零售行业的账单数", dsl=dsl("invoice_count", filters=[("industry", "零售")], time=L1)),
            T("那金融呢", dsl=dsl("invoice_count", filters=[("industry", "金融")], time=L1), note=_NOTE_RESOLVED),
        ],
    },
    # ---- role_mixed（south 角色全链路 scope 收窄，金标 role=south 编译） ----
    {
        "id": "mcc06", "scenario": "role_mixed", "role": "south",
        "desc": "saas south 角色多轮：全链路按华南 scope 收窄（金标以 role=south + saas spec 编译）",
        "turns": [
            T("上月各套餐档位的MRR", dsl=dsl("mrr", dims=["tier"], time=L1)),
            T("上月各渠道毛利率", dsl=dsl("gross_margin_rate", dims=["channel"], time=L1)),
            T("上月付费客户数", dsl=dsl("company_count", time=L1)),
            T("上月各行业席位数", dsl=dsl("seat_count", dims=["industry"], time=L1)),
        ],
    },
    # ---- mixed_long（六轮混合长流） ----
    {
        "id": "mcc07", "scenario": "mixed_long", "role": "boss",
        "desc": "saas 六轮混合长流：MRR 趋势→档位换血→渠道席位数趋势→显式区间毛利额",
        "turns": [
            T("近12个月MRR趋势", dsl=dsl("mrr", dims=["month"], time=L12)),
            T("上月旗舰版套餐档位的席位数", dsl=dsl("seat_count", filters=[("tier", "旗舰版")], time=L1)),
            T("那基础版呢", dsl=dsl("seat_count", filters=[("tier", "基础版")], time=L1), note=_NOTE_RESOLVED),
            T("近6个月各渠道席位数趋势", dsl=dsl("seat_count", dims=["month", "channel"], time=L6)),
            T("2025年1月到6月的毛利额", dsl=dsl("gross_margin", time=BT("2025-01", "2025-06"))),
        ],
    },
]

# ================= hotel（数据集 D：商务/度假/精品 × 大床/双床/套房/亲子 × 四渠道） =================

ITEMS_CD: list[dict] = [
    # ---- resolve_chain（channel 实体链） ----
    {
        "id": "mcd01", "scenario": "resolve_chain", "role": "boss",
        "desc": "hotel channel 实体链：直销→旅行社→协议，末轮渠道间夜分组",
        "turns": [
            T("上月直销渠道的房费收入", dsl=dsl("room_revenue", filters=[("channel", "直销")], time=L1)),
            T("那旅行社呢", dsl=dsl("room_revenue", filters=[("channel", "旅行社")], time=L1), note=_NOTE_RESOLVED),
            T("那协议呢", dsl=dsl("room_revenue", filters=[("channel", "协议")], time=L1), note=_NOTE_RESOLVED),
            T("上月各渠道间夜数", dsl=dsl("room_nights", dims=["channel"], time=L1)),
        ],
    },
    # ---- resolve_chain（hotel_type 实体链） ----
    {
        "id": "mcd02", "scenario": "resolve_chain", "role": "boss",
        "desc": "hotel hotel_type 实体链：度假→商务→精品，末轮店型间夜分组",
        "turns": [
            T("近6个月度假店型的平均房价", dsl=dsl("adr", filters=[("hotel_type", "度假")], time=L6)),
            T("那商务呢", dsl=dsl("adr", filters=[("hotel_type", "商务")], time=L6), note=_NOTE_RESOLVED),
            T("那精品呢", dsl=dsl("adr", filters=[("hotel_type", "精品")], time=L6), note=_NOTE_RESOLVED),
            T("近6个月各酒店类型的间夜数", dsl=dsl("room_nights", dims=["hotel_type"], time=L6)),
        ],
    },
    # ---- constraint_keep（毛利率+L12 窗口在 region 换血中保持） ----
    {
        "id": "mcd03", "scenario": "constraint_keep", "role": "boss",
        "desc": "hotel 毛利率+近12个月窗口在 region 换血中保持（华东→华北→东北），末轮城市毛利额分组",
        "turns": [
            T("华东地区近12个月毛利率", dsl=dsl("gross_margin_rate", filters=[("region", "华东")], time=L12)),
            T("那华北呢", dsl=dsl("gross_margin_rate", filters=[("region", "华北")], time=L12), note=_NOTE_RESOLVED),
            T("那东北呢", dsl=dsl("gross_margin_rate", filters=[("region", "东北")], time=L12), note=_NOTE_RESOLVED),
            T("近12个月各城市毛利额", dsl=dsl("gross_margin", dims=["city"], time=L12)),
        ],
    },
    # ---- clarify_then_drill（挂起→恢复→同 thread 追问） ----
    {
        "id": "mcd04", "scenario": "clarify_then_drill", "role": "boss", "suspend": True,
        "desc": "hotel 澄清挂起→恢复出数→店型预订量→归因追问（大床房型间夜下滑）",
        "turns": [
            T("最近经营情况怎么样", kind="suspend"),
            T("房费收入", kind="answer", resume=True, merged=True, dsl=dsl("room_revenue")),
            T("上月各酒店类型的预订量", dsl=dsl("booking_count", dims=["hotel_type"], time=L1)),
            T("为什么上月大床房型的间夜数下滑了",
              dsl=dsl("room_nights", filters=[("room_type", "大床")], time=L1, direction="asc")),
        ],
    },
    # ---- topic_switch（hotel_type→channel 分组后回指失败→恢复→换血） ----
    {
        "id": "mcd05", "scenario": "topic_switch", "role": "boss",
        "desc": "hotel hotel_type→渠道平均房价话题切换后回指失败（上轮无店型实体→保守回退）→恢复→精品换血",
        "turns": [
            T("上月商务店型的房费收入", dsl=dsl("room_revenue", filters=[("hotel_type", "商务")], time=L1)),
            T("上月各渠道的平均房价", dsl=dsl("adr", dims=["channel"], time=L1)),
            T("那度假呢", kind="clarify", note=_NOTE_FALLBACK),
            T("上月度假店型的房费收入", dsl=dsl("room_revenue", filters=[("hotel_type", "度假")], time=L1)),
            T("那精品呢", dsl=dsl("room_revenue", filters=[("hotel_type", "精品")], time=L1), note=_NOTE_RESOLVED),
        ],
    },
    # ---- topic_switch_cache（话题切换后回环重问命中缓存→region 换血→末轮分组） ----
    {
        "id": "mcd06", "scenario": "topic_switch_cache", "role": "boss",
        "desc": "hotel 话题切换后回指重问（与 t1 同 DSL 指纹→缓存命中）→region 换血→渠道宾客数分组",
        "turns": [
            T("上月华东地区的间夜数", dsl=dsl("room_nights", filters=[("region", "华东")], time=L1)),
            T("上月各房型的平均房价", dsl=dsl("adr", dims=["room_type"], time=L1)),
            T("上月华东地区的间夜数", dsl=dsl("room_nights", filters=[("region", "华东")], time=L1),
              from_cache=True),
            T("那华南呢", dsl=dsl("room_nights", filters=[("region", "华南")], time=L1), note=_NOTE_RESOLVED),
            T("上月各渠道的入住宾客数", dsl=dsl("guest_count", dims=["channel"], time=L1)),
        ],
    },
    # ---- refusal_mixed（拒答插入对话） ----
    {
        "id": "mcd07", "scenario": "refusal_mixed", "role": "boss",
        "desc": "hotel 拒答插入对话：预测类拒答→回到历史数据→房型分组→亲子→套房换血",
        "turns": [
            T("上月直销渠道的房费收入", dsl=dsl("room_revenue", filters=[("channel", "直销")], time=L1)),
            T("预测下季度的房费收入", kind="refusal"),
            T("上月毛利率", dsl=dsl("gross_margin_rate", time=L1)),
            T("上月亲子房型的预订量", dsl=dsl("booking_count", filters=[("room_type", "亲子")], time=L1)),
            T("那套房呢", dsl=dsl("booking_count", filters=[("room_type", "套房")], time=L1), note=_NOTE_RESOLVED),
        ],
    },
]

SOURCES: list[tuple[str, object, list[dict], Path]] = [
    ("coffee", COFFEE_SPEC, ITEMS_CB, TARGETS["coffee"]),
    ("saas", SAAS_SPEC, ITEMS_CC, TARGETS["saas"]),
    ("hotel", HOTEL_SPEC, ITEMS_CD, TARGETS["hotel"]),
]


@contextmanager
def _dataset_scope(name: str):
    """自检期临时切 settings.dataset：resolve_followup 的词表随 spec 走（与 runner 同接线）。"""
    s = get_settings()
    saved = s.dataset
    s.dataset = name
    try:
        yield
    finally:
        s.dataset = saved


def _canon(rows: list[list]) -> list[tuple]:
    return sorted(
        tuple(round(v, 4) if isinstance(v, (int, float)) and not isinstance(v, bool) else v for v in r)
        for r in rows
    )


def _probe_answer(effective_q: str, role: str, gold_dsl: dict, spec, tag: str) -> list[str]:
    """mock 重答有效问题与金标在 spec 库上比对（LIMIT 剥离口径 + 截断边界 strict 断言）。

    与 build_multiturn_deep._probe_answer 的差异点（逐点 spec 化）：
    - 解析/校验/编译全部带 spec（build_dsl(spec) + validate_filter_values(spec) 与
      sql_agent_node 同构——deep 用裸 QueryDSL 是因为默认白名单即 ecommerce）；
    - execute_sync / limit_boundary_strict 传 spec.db_abs_path（金标与线上执行同源）。
    """
    problems: list[str] = []
    gold_sql, _ = compile_sql(build_dsl(gold_dsl, spec=spec), role=role, spec=spec)
    # AB-29（评审 5.0）：截断边界并列 → LIMIT 截断集合执行间非确定，该轮金标不得入集
    ok, detail = limit_boundary_strict(gold_sql, db_path=spec.db_abs_path)
    if not ok:
        return [f"{tag} [{spec.name}] {detail}"]
    try:
        parsed = build_dsl(_parse_question(effective_q, spec=spec), spec=spec)
    except Exception as e:  # noqa: BLE001
        return [f"{tag} [{spec.name}] mock 解析/编译失败: {e}\n    Q: {effective_q}"]
    invalid = validate_filter_values(parsed, spec)
    if invalid:
        return [(
            f"{tag} [{spec.name}] mock 解析产出非法过滤值（线上将拒答，期望 answer）: "
            f"{invalid}\n    Q: {effective_q}"
        )]
    got_sql, _ = compile_sql(parsed, role=role, spec=spec)
    body, _, limit_line = gold_sql.rpartition("\nLIMIT")
    gold_body, gold_limit = body, ("LIMIT" + limit_line)
    got_body, _, got_limit_line = got_sql.rpartition("\nLIMIT")
    got_limit = "LIMIT" + got_limit_line
    if got_limit != gold_limit:
        return [f"{tag} [{spec.name}] LIMIT 不一致: {got_limit!r} vs {gold_limit!r}\n    Q: {effective_q}"]
    try:
        ok = _canon(execute_sync(got_body, db_path=spec.db_abs_path).rows) == \
            _canon(execute_sync(gold_body, db_path=spec.db_abs_path).rows)
    except Exception as e:  # noqa: BLE001
        return [f"{tag} 执行比对失败: {e}\n    gold: {gold_sql}\n    got : {got_sql}"]
    if not ok:
        problems.append(
            f"{tag} [{spec.name}] mock 结果与金标不一致\n    Q: {effective_q}\n    gold: {gold_sql}\n    got : {got_sql}"
        )
    return problems


def self_check_source(spec, items: list[dict]) -> list[str]:
    """构建期自检（单源）：逐轮模拟 runner 接线（DATASET 切源 → resolve → ctx.set）后四重探针。"""
    problems: list[str] = []
    turns_total = 0
    with _dataset_scope(spec.name):
        for item in items:
            ctx = ""            # 与 runner 一致：ctx.set(sid, 改写后问题)
            suspended_q = ""
            for tno, turn in enumerate(item["turns"], 1):
                turns_total += 1
                tag = f"{item['id']}.t{tno}"
                exp = turn["expect"]
                kind = exp["kind"]
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
                    if _clarifier({"question": effective, "dataset": spec.name})["verdict"] != "clarify":
                        problems.append(f"{tag} [suspend] mock verdict 非 clarify（挂不起）: {effective}")
                    suspended_q = effective
                    continue
                if kind in ("clarify", "refusal"):
                    verdict = _clarifier({"question": effective, "dataset": spec.name})["verdict"]
                    expect_v = "refuse" if kind == "refusal" else "clarify"
                    if verdict != expect_v:
                        problems.append(f"{tag} [{kind}] mock verdict={verdict} 期望 {expect_v}: {effective}")
                    continue
                if kind == "answer":
                    problems += _probe_answer(effective, role, exp["dsl"], spec, tag)
                    if exp.get("from_cache"):
                        # 缓存命中钉死（指纹按 spec：dataset 显式参与，跨源绝不串键）：
                        # 同组早前须存在同 DSL 指纹的 answer 轮（cache.clear 按组执行）
                        earlier_fps = {
                            fingerprint(t2["expect"]["dsl"], t2.get("role", item.get("role", "boss")),
                                        dataset=spec.name)
                            for t2 in item["turns"][: tno - 1]
                            if t2["expect"]["kind"] == "answer" and t2["expect"].get("dsl") is not None
                        }
                        if fingerprint(exp["dsl"], role, dataset=spec.name) not in earlier_fps:
                            problems.append(f"{tag} from_cache=True 但同组无同指纹早前轮（不会命中）")
            if not (3 <= len(item["turns"]) <= 5):
                problems.append(f"{item['id']} 轮数 {len(item['turns'])} 越界（要求 3-5）")
            # 能力维度覆盖（与 deep 同规则）：每组至少一个超出裸 answer(boss 金标) 的期望
            if not any(
                t["expect"].get("note_contains") or t["expect"].get("merged_contains")
                or t["expect"].get("from_cache") or t["expect"]["kind"] != "answer"
                for t in item["turns"]
            ) and item.get("role", "boss") == "boss":
                problems.append(f"{item['id']} 无任何超出裸 answer 的期望（缺能力覆盖）")
    kind_stat = Counter(t["expect"]["kind"] for it in items for t in it["turns"])
    print(f"  自检统计 [{spec.name}]：{len(items)} 组 / {turns_total} 轮，"
          f"轮型分布 {dict(sorted(kind_stat.items()))}")
    return problems


def self_check() -> list[str]:
    """三源自检 + 跨源完整性断言（组数/轮数/场景模板覆盖/角色覆盖）。"""
    problems: list[str] = []
    all_scenarios: set[str] = set()
    total_groups = total_turns = 0
    for name, spec, items, _target in SOURCES:
        if not (6 <= len(items) <= 8):
            problems.append(f"[{name}] 组数 {len(items)} 越界（要求 6-8）")
        total_groups += len(items)
        total_turns += sum(len(i["turns"]) for i in items)
        all_scenarios |= {i["scenario"] for i in items}
        problems += self_check_source(spec, items)
    if not (18 <= total_groups <= 24):
        problems.append(f"三源合计组数 {total_groups} 越界（要求 18-24）")
    if not (70 <= total_turns <= 110):
        problems.append(f"三源合计轮数 {total_turns} 越界（要求 70-110）")
    missing = set(SCENARIOS) - all_scenarios
    if missing:
        problems.append(f"scenario 模板覆盖缺失：{sorted(missing)}（要求 9 类并集齐全）")
    unknown = all_scenarios - set(SCENARIOS)
    if unknown:
        problems.append(f"未知 scenario：{sorted(unknown)}")
    if sum(1 for _, _, items, _ in SOURCES for i in items if i.get("role", "boss") != "boss") < 1:
        problems.append("无非 boss 角色组（ROLE_SCOPES 三源共享覆盖缺失）")
    print(f"跨源完整性：合计 {total_groups} 组 / {total_turns} 轮，"
          f"scenario 并集 {len(all_scenarios)}/9 类")
    return problems


def build_source(spec, items: list[dict], path: Path, write: bool) -> str:
    """单源定稿：金标 compile_sql(role, spec) 生成后写入 {"_note", "items"} JSON。"""
    out_items = []
    for item in items:
        out_item = {k: item[k] for k in ("id", "scenario", "role", "desc") if k in item}
        if item.get("suspend"):
            out_item["suspend"] = True
        turns = []
        for turn in item["turns"]:
            exp = turn["expect"]
            out_exp: dict = {"kind": exp["kind"]}
            if exp.get("dsl") is not None:
                role = turn.get("role", item.get("role", "boss"))
                out_exp["gold_sql"] = compile_sql(build_dsl(exp["dsl"], spec=spec), role=role, spec=spec)[0]
            for k in ("from_cache", "note_contains", "merged_contains"):
                if exp.get(k) is not None:
                    out_exp[k] = exp[k]
            turns.append({"q": turn["q"], **({"resume": True} if turn.get("resume") else {}),
                          "expect": out_exp})
        out_item["turns"] = turns
        out_items.append(out_item)

    total_turns = sum(len(i["turns"]) for i in out_items)
    note = (
        f"跨源多轮评测集（[1B6]，2026-11-20）：{spec.name} 源 {len(out_items)} 组 × 3-5 轮（{total_turns} 轮），"
        "scenario 模板复用 multiturn_deep 的 9 类（指代消解词表已 spec 化——"
        "实体链换血用本源词表：'那外卖呢'coffee / '那旗舰版呢'saas / '那度假呢'hotel）。"
        f"answer 轮金标由 compile_sql(role, spec={spec.name}) 生成，判分库={spec.db_abs_path.name}"
        "（scripts/build_multiturn_cross_source.py 可重建/自检）。"
        "判分复用 app.eval.runner.judge_query（db_path 随 settings.dataset 走）。"
        "跑分命令：DATASET=%s LLM_PROVIDER=mock python scripts/run_multiturn_eval.py --dataset eval/multiturn_cross_%s.json"
        % (spec.name, {"coffee": "b", "saas": "c", "hotel": "d"}[spec.name])
    )
    doc = {"_note": note, "items": out_items}
    if write:
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return f"[{spec.name}] 已构建 {len(out_items)} 组 / {total_turns} 轮（金标 compile_sql(role, spec) 生成）"


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
          "clarify/refusal/suspend verdict 实判一致（clarifier 随 spec）；"
          "note/merged 期望与 resolver 产出核对；from_cache 指纹按 spec 钉死")

    for name, spec, items, target in SOURCES:
        write = not args.check
        msg = build_source(spec, items, target, write)
        print(msg)
        if write:
            print(f"  已写入 {target.relative_to(ROOT)}")
    if not args.check:
        print("三源跨源多轮评测集构建完成（冻结集零触碰：仅新增三个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
