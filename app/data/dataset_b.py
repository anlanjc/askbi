"""数据集 B 语义层：线下连锁咖啡（门店 × 饮品/烘焙品类 × 堂食/外带/外卖渠道）。

"换数据源不改上层"的实证：本文件只声明语义层（DatasetSpec）+ 数据生成器入口，
图结构 / 编译器 / QA / 评测 runner 全部复用——上层代码没有一行 if dataset == "coffee"。

物理模型（与电商 A 完全不同的表结构，证明编译器只依赖 spec 声明）：
- tickets 小票表（ticket_id / store_id / channel / sale_date / member_id 可空）
- ticket_lines 小票行（sku_id / quantity / unit_price）
- stores 门店（store_name / city / region / store_type）
- skus 商品（sku_name / category / subcategory / unit_cost）

注入的业务故事（可归因，generator_b 实现）：
1. 外卖占比逐月上升（渠道权重随月份线性演化，drill_down 按渠道分解可见正贡献）；
2. 烘焙品类毛利率系统性高于饮品（成本结构差异，各品类毛利率对比可复算）。

行级权限：region 维度沿用全局 ROLE_SCOPES 的七大区划分（全国连锁门店分布），
boss/south/north 语义不变——权限角色是组织级概念，不随数据集重定义。
"""

from __future__ import annotations

from .metrics import MetricDef
from .registry import DatasetSpec, register_spec

# 收入口径：门店零售额（B 数据集不采集退款，口径说明如实声明）
_REV = "SUM(tl.quantity * tl.unit_price)"
_COST = "SUM(tl.quantity * k.unit_cost)"

COFFEE_METRICS: dict[str, MetricDef] = {
    m.id: m
    for m in [
        MetricDef(
            id="revenue", name="销售额", aliases=["营业额", "营收", "销售金额"],
            expr=_REV, unit="yuan",
            caliber="门店零售额 = Σ(数量×单价)，含堂食/外带/外卖全渠道（B 数据集不采集退款）",
        ),
        MetricDef(
            id="order_count", name="订单量", aliases=["订单数", "小票数", "单量"],
            expr="COUNT(DISTINCT t.ticket_id)", unit="count", fmt="{:,.0f}",
            caliber="去重小票数（一单多品计一单）",
        ),
        MetricDef(
            id="item_quantity", name="销量", aliases=["杯数", "件数"],
            expr="SUM(tl.quantity)", unit="count", fmt="{:,.0f}",
            caliber="售出商品件数（饮品按杯、烘焙按件）",
        ),
        MetricDef(
            id="avg_order_value", name="客单价", aliases=["AOV", "平均订单金额"],
            expr=f"({_REV}) / NULLIF(COUNT(DISTINCT t.ticket_id), 0)", unit="yuan",
            caliber="门店零售额 / 去重小票数",
        ),
        MetricDef(
            id="gross_margin", name="毛利额", aliases=["毛利", "利润额"],
            expr=f"({_REV}) - ({_COST})", unit="yuan",
            caliber="门店零售额 − 原料成本（数量×单杯/单件原料成本）",
        ),
        MetricDef(
            id="gross_margin_rate", name="毛利率", aliases=["利润率", "margin"],
            expr=f"(({_REV}) - ({_COST})) / NULLIF(({_REV}), 0)", unit="percent",
            higher_is_better=True, fmt="{:.2%}",
            caliber="毛利额 / 门店零售额",
        ),
        MetricDef(
            id="member_count", name="消费会员数", aliases=["会员数", "会员人数"],
            expr="COUNT(DISTINCT t.member_id)", unit="count", fmt="{:,.0f}",
            caliber="期间内有消费记录的去重会员数（非会员小票不计入）",
        ),
    ]
}

COFFEE_DIMS: dict[str, str] = {
    "month": "strftime('%Y-%m', t.sale_date)",
    "region": "s.region",
    "city": "s.city",
    "store": "s.store_name",
    "store_type": "s.store_type",
    "category": "k.category",
    "subcategory": "k.subcategory",
    "channel": "t.channel",
}

COFFEE_SPEC = DatasetSpec(
    name="coffee",
    title="某线下连锁咖啡品牌 24 个月的门店交易明细（tickets / ticket_lines / stores / skus 逻辑视图）",
    db_path="data/askbi_b.duckdb",
    base_from=(
        "ticket_lines tl JOIN tickets t ON tl.ticket_id = t.ticket_id "
        "JOIN stores s ON t.store_id = s.store_id "
        "JOIN skus k ON tl.sku_id = k.sku_id"
    ),
    version_probe_sql=(
        "SELECT (SELECT COUNT(*) FROM tickets),"
        " (SELECT COALESCE(MAX(sale_date), '-') FROM tickets),"
        " (SELECT COUNT(*) FROM ticket_lines),"
        " (SELECT COUNT(DISTINCT ticket_id) FROM ticket_lines)"
    ),
    dimensions=COFFEE_DIMS,
    filter_fields=dict(COFFEE_DIMS),
    dimension_values={
        "region": ["华南", "华北", "华东", "华中", "西南", "西北", "东北"],
        "category": ["饮品", "烘焙"],
        "subcategory": ["咖啡", "茶饮", "面包", "蛋糕", "酥点"],
        "channel": ["堂食", "外带", "外卖"],
        "store_type": ["旗舰店", "标准店", "店中店"],
    },
    metrics=COFFEE_METRICS,
    time_field="t.sale_date",
    start_month="2024-10",
    end_month="2026-09",
    dim_keywords={
        "category": ["品类", "类别", "类目"],
        "subcategory": ["细分品类", "子类"],
        "region": ["大区", "区域", "地区"],
        "city": ["城市"],
        "store": ["门店"],
        "store_type": ["店型"],
        "channel": ["渠道"],
    },
    filter_keywords={
        "region": ("大区", "区域", "地区"),
        "category": ("品类", "类别", "类目"),
        "subcategory": ("细分品类", "子类"),
        "channel": ("渠道",),
        "store_type": ("店型",),
    },
    drill_dims=("category", "channel", "region"),
)

register_spec(COFFEE_SPEC)
