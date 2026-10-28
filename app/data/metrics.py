"""指标字典（口径治理）：每个指标一个权威 SQL 表达式 + 口径说明。

借鉴 WrenAI MDL 语义层理念：LLM 只选指标 id，表达式由字典渲染——
"毛利率怎么算"永远有唯一答案，且带版本可追溯的口径说明。
"""

from __future__ import annotations

from pydantic import BaseModel

# 收入口径：折后销售额（扣折扣、含已退货订单，退货单列 refund_rate 刻画）
_REVENUE_ROW = "oi.quantity * oi.unit_price * (1 - oi.discount)"
_REVENUE = f"SUM({_REVENUE_ROW})"
_COST = "SUM(oi.quantity * p.supply_cost)"


class MetricDef(BaseModel):
    id: str
    name: str                    # 展示名
    aliases: list[str]           # 口语别名，供检索/澄清用
    expr: str                    # SQL 聚合表达式（权威口径）
    unit: str = "value"          # value | yuan | percent | count
    higher_is_better: bool = True
    caliber: str                 # 口径说明（进最终答案，治理可追溯）
    fmt: str = "{:,.2f}"


METRICS: dict[str, MetricDef] = {
    m.id: m
    for m in [
        MetricDef(
            id="revenue", name="销售额", aliases=["营业额", "营收", "GMV", "销售金额"],
            expr=_REVENUE, unit="yuan", caliber="折后销售额 = Σ(数量×单价×(1-折扣))，含所有订单状态",
        ),
        MetricDef(
            id="order_count", name="订单量", aliases=["订单数", "单量"],
            expr="COUNT(DISTINCT o.order_id)", unit="count", fmt="{:,.0f}",
            caliber="去重订单数，含所有状态",
        ),
        MetricDef(
            id="item_quantity", name="销量", aliases=["卖了多少件", "件数", "商品数"],
            expr="SUM(oi.quantity)", unit="count", fmt="{:,.0f}",
            caliber="售出商品件数（订单行数量求和）",
        ),
        MetricDef(
            id="avg_order_value", name="客单价", aliases=["AOV", "平均订单金额"],
            expr=f"({_REVENUE}) / NULLIF(COUNT(DISTINCT o.order_id), 0)", unit="yuan",
            caliber="折后销售额 / 去重订单数",
        ),
        MetricDef(
            id="gross_margin", name="毛利额", aliases=["毛利", "利润额"],
            expr=f"({_REVENUE}) - ({_COST})", unit="yuan",
            caliber="折后销售额 − 供货成本（数量×单件供货成本）",
        ),
        MetricDef(
            id="gross_margin_rate", name="毛利率", aliases=["利润率", "margin"],
            expr=f"(({_REVENUE}) - ({_COST})) / NULLIF(({_REVENUE}), 0)", unit="percent",
            higher_is_better=True, fmt="{:.2%}",
            caliber="毛利额 / 折后销售额",
        ),
        MetricDef(
            id="refund_rate", name="退货率", aliases=["退货比例"],
            # CASE 内只能用行级表达式：聚合套聚合会直接 Binder Error
            expr=(
                f"SUM(CASE WHEN o.status = '已退货' THEN ({_REVENUE_ROW}) ELSE 0 END)"
                f" / NULLIF(SUM({_REVENUE_ROW}), 0)"
            ),
            unit="percent", higher_is_better=False, fmt="{:.2%}",
            caliber="已退货订单销售额 / 总销售额（按金额计）",
        ),
        MetricDef(
            id="net_revenue", name="净销售额", aliases=["净收入", "净营收"],
            expr=f"SUM(CASE WHEN o.status = '已退货' THEN 0 ELSE ({_REVENUE_ROW}) END)",
            unit="yuan",
            caliber="折后销售额剔除已退货订单（与销售额的差即退货金额，两口径并存供对照）",
        ),
        MetricDef(
            id="customer_count", name="购买客户数", aliases=["客户数", "买家数", "购买人数", "购买客户"],
            expr="COUNT(DISTINCT o.customer_id)", unit="count", fmt="{:,.0f}",
            caliber="期间内有成交的去重客户数",
        ),
    ]
}


def metrics_prompt(spec=None) -> str:
    if spec is None:
        from .registry import DEFAULT_SPEC  # 延迟导入避免循环

        spec = DEFAULT_SPEC
    lines = []
    for m in spec.metrics.values():
        lines.append(f"  - {m.id}（{m.name}，别名：{'/'.join(m.aliases)}）：{m.caliber}")
    return "可用指标：\n" + "\n".join(lines)
