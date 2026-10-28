"""构建 v2 分层评测集（eval/dataset_v2.json）：金标一律由 compile_sql 生成，禁止手写 SQL。

设计（v1 33 条保持不动 = smoke 回归集，本文件产出 v2 ≈116 条 = full 分层集）：
- 每条 query 题目内嵌"意图 DSL"，gold_sql = compile_sql(意图DSL, role) —— 金标与线上编译器同源；
- 构建期自检：用 mock 解析器重答每道 query 题，把"mock 编译 SQL"与"金标"在同一 DuckDB 上
  执行比对（与 runner.judge_query 同口径），不通过即打印差异 —— 构建期就暴露
  "措辞超出解析器覆盖"或"意图 DSL 写错"两类问题；
- clarify/refusal/drill 无金标（runner 按答案 kind/叙述+QA 判分）；
- split: main / holdout（holdout 15 条不参与任何调参，报告单列）。

用法（项目根）：
    python scripts/build_dataset_v2.py            # 构建 + 自检，写入 eval/dataset_v2.json
    python scripts/build_dataset_v2.py --check    # 只自检不写盘
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.data.dsl import QueryDSL, compile_sql  # noqa: E402
from app.data.sandbox import execute_sync  # noqa: E402
from app.llm.mock import _parse_question  # noqa: E402


def dsl(
    metric: str,
    dims: list[str] | None = None,
    filters: list[tuple[str, object]] | None = None,
    time: dict | None = None,
    limit: int | None = None,
    direction: str | None = None,
) -> dict:
    """意图 DSL 简写构造器。filters 用 (field, value) 元组，value 为 list 时编译为 IN。"""
    out: dict = {
        "intent": "aggregate",
        "metric": metric,
        "dimensions": dims or [],
        "filters": [{"field": f, "op": "in" if isinstance(v, list) else "=", "value": v}
                    for f, v in (filters or [])],
        "time": time or {"mode": "all"},
        "order_by": {"metric": metric, "direction": direction or "desc"},
        "limit": limit,
    }
    return out


L1 = {"mode": "last_n_months", "n": 1}
L3 = {"mode": "last_n_months", "n": 3}
L6 = {"mode": "last_n_months", "n": 6}
L12 = {"mode": "last_n_months", "n": 12}
BT = lambda s, e: {"mode": "between", "start": s, "end": e}  # noqa: E731

# (id, kind, question, dsl | None, role, split)
SPECS: list[tuple[str, str, str, dict | None, str, str]] = [
    # ---------- AGG 单指标聚合 ×14 ----------
    ("v2-001", "query", "上个月的总销售额是多少", dsl("revenue", time=L1), "boss", "main"),
    ("v2-002", "query", "上月订单量有多少", dsl("order_count", time=L1), "boss", "main"),
    ("v2-003", "query", "上个月一共卖了多少件商品", dsl("item_quantity", time=L1), "boss", "main"),
    ("v2-004", "query", "上月客单价是多少", dsl("avg_order_value", time=L1), "boss", "main"),
    ("v2-005", "query", "上个月的毛利额", dsl("gross_margin", time=L1), "boss", "main"),
    ("v2-006", "query", "上月毛利率是多少", dsl("gross_margin_rate", time=L1), "boss", "main"),
    ("v2-007", "query", "上个月的退货率", dsl("refund_rate", time=L1), "boss", "main"),
    ("v2-008", "query", "上月有多少购买客户", dsl("customer_count", time=L1), "boss", "main"),
    ("v2-009", "query", "最近3个月销售额是多少", dsl("revenue", time=L3), "boss", "main"),
    ("v2-010", "query", "最近6个月的订单量", dsl("order_count", time=L6), "boss", "main"),
    ("v2-011", "query", "最近12个月的客单价", dsl("avg_order_value", time=L12), "boss", "main"),
    ("v2-012", "query", "2024年10月到2025年3月的销售额", dsl("revenue", time=BT("2024-10", "2025-03")), "boss", "holdout"),
    ("v2-013", "query", "上月净销售额是多少", dsl("net_revenue", time=L1), "boss", "main"),
    ("v2-014", "query", "全部时间内总销售额", dsl("revenue", time={"mode": "all"}), "boss", "main"),
    # ---------- BRK 维度分解 ×16 ----------
    ("v2-015", "query", "各品类销售额", dsl("revenue", dims=["category"]), "boss", "main"),
    ("v2-016", "query", "各大区毛利率", dsl("gross_margin_rate", dims=["region"]), "boss", "main"),
    ("v2-017", "query", "各渠道订单量", dsl("order_count", dims=["channel"]), "boss", "main"),
    ("v2-018", "query", "各会员等级客单价", dsl("avg_order_value", dims=["member_level"]), "boss", "main"),
    ("v2-019", "query", "各品类退货率", dsl("refund_rate", dims=["category"], direction="asc"), "boss", "main"),
    ("v2-020", "query", "各大区销售额", dsl("revenue", dims=["region"]), "boss", "main"),
    ("v2-021", "query", "各渠道毛利率", dsl("gross_margin_rate", dims=["channel"]), "boss", "main"),
    ("v2-022", "query", "各细分品类销量", dsl("item_quantity", dims=["subcategory"]), "boss", "main"),
    ("v2-023", "query", "各城市销售额", dsl("revenue", dims=["city"]), "boss", "main"),
    ("v2-024", "query", "各省份订单量", dsl("order_count", dims=["province"]), "boss", "main"),
    ("v2-025", "query", "上月各品类净销售额", dsl("net_revenue", dims=["category"], time=L1), "boss", "main"),
    ("v2-026", "query", "最近3个月各渠道销售额", dsl("revenue", dims=["channel"], time=L3), "boss", "main"),
    ("v2-027", "query", "各品类毛利额", dsl("gross_margin", dims=["category"]), "boss", "main"),
    ("v2-028", "query", "各会员等级购买客户数", dsl("customer_count", dims=["member_level"]), "boss", "main"),
    ("v2-029", "query", "各城市毛利率", dsl("gross_margin_rate", dims=["city"]), "boss", "holdout"),
    ("v2-030", "query", "最近6个月各品类退货率", dsl("refund_rate", dims=["category"], time=L6, direction="asc"), "boss", "main"),
    # ---------- TOP 排名 ×10 ----------
    ("v2-031", "query", "销售额最高的品类", dsl("revenue", dims=["category"]), "boss", "main"),
    ("v2-032", "query", "哪个大区订单量最多", dsl("order_count", dims=["region"]), "boss", "main"),
    ("v2-033", "query", "销售额排名前3的渠道", dsl("revenue", dims=["channel"], limit=3), "boss", "main"),
    ("v2-034", "query", "退货率最高的品类", dsl("refund_rate", dims=["category"], direction="asc"), "boss", "main"),
    ("v2-035", "query", "客单价最低的会员等级", dsl("avg_order_value", dims=["member_level"], direction="asc"), "boss", "main"),
    ("v2-036", "query", "销量前5的细分品类", dsl("item_quantity", dims=["subcategory"], limit=5), "boss", "holdout"),
    ("v2-037", "query", "毛利率最差的大区", dsl("gross_margin_rate", dims=["region"], direction="asc"), "boss", "main"),
    ("v2-038", "query", "购买客户数最多的城市", dsl("customer_count", dims=["city"]), "boss", "main"),
    ("v2-039", "query", "上月销售额前3的品类", dsl("revenue", dims=["category"], time=L1, limit=3), "boss", "main"),
    ("v2-040", "query", "最近3个月销售额最高的渠道", dsl("revenue", dims=["channel"], time=L3), "boss", "main"),
    # ---------- TRD 趋势 ×10 ----------
    ("v2-041", "query", "近6个月销售额趋势", dsl("revenue", dims=["month"], time=L6), "boss", "main"),
    ("v2-042", "query", "近12个月订单量趋势", dsl("order_count", dims=["month"], time=L12), "boss", "main"),
    ("v2-043", "query", "近3个月毛利率走势", dsl("gross_margin_rate", dims=["month"], time=L3), "boss", "main"),
    ("v2-044", "query", "近12个月客单价趋势", dsl("avg_order_value", dims=["month"], time=L12), "boss", "main"),
    ("v2-045", "query", "近6个月美妆品类销售额趋势", dsl("revenue", dims=["month"], filters=[("category", "美妆")], time=L6), "boss", "main"),
    ("v2-046", "query", "近6个月华南地区退货率趋势", dsl("refund_rate", dims=["month"], filters=[("region", "华南")], time=L6, direction="asc"), "boss", "main"),
    ("v2-047", "query", "近12个月直播渠道销量趋势", dsl("item_quantity", dims=["month"], filters=[("channel", "直播")], time=L12), "boss", "holdout"),
    ("v2-048", "query", "近3个月黑卡会员客单价趋势", dsl("avg_order_value", dims=["month"], filters=[("member_level", "黑卡")], time=L3), "boss", "main"),
    ("v2-049", "query", "近6个月销售额逐月走势", dsl("revenue", dims=["month"], time=L6), "boss", "main"),
    ("v2-050", "query", "近6个月各渠道销售额趋势", dsl("revenue", dims=["month", "channel"], time=L6), "boss", "main"),
    # ---------- FLT 取值过滤 ×14 ----------
    ("v2-051", "query", "华南地区上月销售额", dsl("revenue", filters=[("region", "华南")], time=L1), "boss", "main"),
    ("v2-052", "query", "美妆品类上月毛利率", dsl("gross_margin_rate", filters=[("category", "美妆")], time=L1), "boss", "main"),
    ("v2-053", "query", "直播渠道最近3个月订单量", dsl("order_count", filters=[("channel", "直播")], time=L3), "boss", "main"),
    ("v2-054", "query", "黑卡会员上月客单价", dsl("avg_order_value", filters=[("member_level", "黑卡")], time=L1), "boss", "main"),
    ("v2-055", "query", "食品品类最近6个月退货率", dsl("refund_rate", filters=[("category", "食品")], time=L6), "boss", "main"),
    ("v2-056", "query", "华东地区上月毛利额", dsl("gross_margin", filters=[("region", "华东")], time=L1), "boss", "main"),
    ("v2-057", "query", "数码品类近12个月销量", dsl("item_quantity", filters=[("category", "数码")], time=L12), "boss", "main"),
    ("v2-058", "query", "官网渠道上月净销售额", dsl("net_revenue", filters=[("channel", "官网")], time=L1), "boss", "main"),
    ("v2-059", "query", "银卡会员最近3个月购买客户数", dsl("customer_count", filters=[("member_level", "银卡")], time=L3), "boss", "main"),
    ("v2-060", "query", "西南地区近6个月订单量", dsl("order_count", filters=[("region", "西南")], time=L6), "boss", "main"),
    ("v2-061", "query", "家居品类上月销售额", dsl("revenue", filters=[("category", "家居")], time=L1), "boss", "holdout"),
    ("v2-062", "query", "搜索渠道上月毛利率", dsl("gross_margin_rate", filters=[("channel", "搜索")], time=L1), "boss", "main"),
    ("v2-063", "query", "东北地区上月退货率", dsl("refund_rate", filters=[("region", "东北")], time=L1), "boss", "main"),
    ("v2-064", "query", "运动品类近3个月客单价", dsl("avg_order_value", filters=[("category", "运动")], time=L3), "boss", "main"),
    # ---------- BTW 时间段（between 语义）×4 ----------
    ("v2-065", "query", "2024年10月到12月的销售额", dsl("revenue", time=BT("2024-10", "2024-12")), "boss", "main"),
    ("v2-066", "query", "2025年1月至2025年6月各品类销售额", dsl("revenue", dims=["category"], time=BT("2025-01", "2025-06")), "boss", "main"),
    ("v2-067", "query", "2025年7月到9月的毛利率", dsl("gross_margin_rate", time=BT("2025-07", "2025-09")), "boss", "main"),
    ("v2-068", "query", "2024-10至2025-09的订单量", dsl("order_count", time=BT("2024-10", "2025-09")), "boss", "main"),
    # ---------- NET 口径对照 ×2 ----------
    ("v2-069", "query", "上月美妆品类的净销售额", dsl("net_revenue", filters=[("category", "美妆")], time=L1), "boss", "main"),
    ("v2-070", "query", "上月华南地区净销售额", dsl("net_revenue", filters=[("region", "华南")], time=L1), "boss", "main"),
    # ---------- ROL 行级权限（role 端到端）×10 ----------
    ("v2-071", "query", "上月各品类销售额", dsl("revenue", dims=["category"], time=L1), "south", "main"),
    ("v2-072", "query", "上月销售额", dsl("revenue", time=L1), "south", "main"),
    ("v2-073", "query", "上月各渠道毛利率", dsl("gross_margin_rate", dims=["channel"], time=L1), "south", "main"),
    ("v2-074", "query", "最近3个月各品类退货率", dsl("refund_rate", dims=["category"], time=L3, direction="asc"), "south", "main"),
    ("v2-075", "query", "上月各品类销售额", dsl("revenue", dims=["category"], time=L1), "north", "main"),
    ("v2-076", "query", "上月订单量", dsl("order_count", time=L1), "north", "main"),
    ("v2-077", "query", "上月各会员等级客单价", dsl("avg_order_value", dims=["member_level"], time=L1), "north", "main"),
    ("v2-078", "query", "最近6个月各渠道销售额趋势", dsl("revenue", dims=["month", "channel"], time=L6), "north", "main"),
    ("v2-079", "query", "上月毛利率", dsl("gross_margin_rate", time=L1), "south", "holdout"),
    ("v2-080", "query", "上月净销售额", dsl("net_revenue", time=L1), "north", "holdout"),
    # ---------- DRK 归因/下钻 ×12 ----------
    ("v2-081", "drill", "为什么上月美妆品类的毛利率下滑了", None, "boss", "main"),
    ("v2-082", "drill", "上个月整体毛利率为什么下滑", None, "boss", "main"),
    ("v2-083", "drill", "为什么最近3个月直播渠道销售额增长", None, "boss", "main"),
    ("v2-084", "drill", "最近6个月数码品类销售额为什么增长", None, "boss", "main"),
    ("v2-085", "drill", "为什么上月华南地区退货率升高", None, "boss", "main"),
    ("v2-086", "drill", "分析上月各品类毛利率", None, "boss", "main"),
    ("v2-087", "drill", "对比最近6个月各渠道的销售额变化", None, "boss", "holdout"),
    ("v2-088", "drill", "为什么上月官网渠道的客单价波动", None, "boss", "main"),
    ("v2-089", "drill", "归因一下最近3个月各大区销售额的变化", None, "boss", "main"),
    ("v2-090", "drill", "上月退货率为什么上升", None, "boss", "main"),
    ("v2-091", "drill", "为什么各会员等级的客单价不同", None, "boss", "holdout"),
    ("v2-092", "drill", "分析一下上月各渠道的销售额结构", None, "boss", "main"),
    # ---------- CLA 澄清 ×12 ----------
    ("v2-093", "clarify", "卖得怎么样", None, "boss", "main"),
    ("v2-094", "clarify", "上个月卖得怎么样", None, "boss", "main"),
    ("v2-095", "clarify", "最近表现怎么样", None, "boss", "main"),
    ("v2-096", "clarify", "经营情况怎么样", None, "boss", "main"),
    ("v2-097", "clarify", "帮我看看情况", None, "boss", "main"),
    ("v2-098", "clarify", "哪个卖得好", None, "boss", "main"),
    ("v2-099", "clarify", "最近三个月表现如何", None, "boss", "main"),
    ("v2-100", "clarify", "分析一下销售情况", None, "boss", "main"),
    ("v2-101", "clarify", "哪个渠道比较好", None, "boss", "holdout"),
    ("v2-102", "clarify", "上个月华南的情况怎么样", None, "boss", "main"),
    ("v2-103", "clarify", "这阵子卖得怎么样", None, "boss", "main"),
    ("v2-104", "clarify", "数据整体怎么样", None, "boss", "holdout"),
    # ---------- REF 拒答 ×12 ----------
    ("v2-105", "refusal", "预测下季度的销售额", None, "boss", "main"),
    ("v2-106", "refusal", "明年销售额会怎样", None, "boss", "main"),
    ("v2-107", "refusal", "下个月哪个品类会卖得最好", None, "boss", "main"),
    ("v2-108", "refusal", "预计最近的销售趋势", None, "boss", "main"),
    ("v2-109", "refusal", "复购率是多少", None, "boss", "main"),
    ("v2-110", "refusal", "客户满意度怎么样", None, "boss", "main"),
    ("v2-111", "refusal", "上月留存率多少", None, "boss", "main"),
    ("v2-112", "refusal", "好评率最高的商品是哪个", None, "boss", "holdout"),
    ("v2-113", "refusal", "投诉量上个月有多少", None, "boss", "main"),
    ("v2-114", "refusal", "写一段上月销售总结", None, "boss", "main"),
    ("v2-115", "refusal", "推荐几个值得投入的方向", None, "boss", "holdout"),
    ("v2-116", "refusal", "评价一下上月的表现", None, "boss", "main"),
]


def _canon(rows: list[list]) -> list[tuple]:
    return sorted(
        tuple(round(v, 4) if isinstance(v, (int, float)) and not isinstance(v, bool) else v for v in r)
        for r in rows
    )


def self_check() -> list[str]:
    """构建期自检：mock 解析器重答 query 题，与金标同库执行比对（runner 同口径）。"""
    problems: list[str] = []
    for sid, kind, question, spec, role, split in SPECS:
        if kind != "query":
            continue
        gold_sql, _ = compile_sql(QueryDSL(**spec), role=role)
        try:
            parsed = QueryDSL(**_parse_question(question))
            got_sql, _ = compile_sql(parsed, role=role)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{sid} [{role}] mock 解析/编译失败: {e}\n    Q: {question}")
            continue
        try:
            ok = _canon(execute_sync(got_sql).rows) == _canon(execute_sync(gold_sql).rows)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{sid} 执行比对失败: {e}\n    gold: {gold_sql}\n    got : {got_sql}")
            continue
        if not ok:
            problems.append(
                f"{sid} [{role}] mock 结果与金标不一致\n    Q: {question}\n    gold: {gold_sql}\n    got : {got_sql}"
            )
    return problems


def build(path: Path | None) -> str:
    items = []
    for sid, kind, question, spec, role, split in SPECS:
        item: dict = {"id": sid, "kind": kind, "question": question, "role": role, "split": split}
        if spec is not None:
            item["gold_sql"] = compile_sql(QueryDSL(**spec), role=role)[0]
        items.append(item)
    from collections import Counter

    stats = Counter((i["kind"], i["split"]) for i in items)
    note = (
        "v2 分层评测集（full）：金标由 compile_sql 生成（scripts/build_dataset_v2.py 可重建/自检）。"
        "v1 33 条保留为 smoke 回归集。split=holdout 的 15 条不参与任何调参。"
        f"构成：{dict(sorted(stats.items()))}"
    )
    payload = {"note": note, "items": items}
    if path is not None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return note


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
    print("自检通过：全部 query 题 mock 重答与金标同库一致")

    note = build(None if args.check else ROOT / "eval" / "dataset_v2.json")
    print(note)
    if not args.check:
        print("已写入 eval/dataset_v2.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
