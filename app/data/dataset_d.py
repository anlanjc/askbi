"""数据集 D 语义层：连锁酒店酒旅（酒店 × 房型 × 直销/旅行社/协议/门店渠道）。

"换数据源不改上层"的第四个实证：本文件只声明语义层（DatasetSpec）+ 数据生成器入口，
图结构 / 编译器 / QA / 评测 runner 全部复用——上层代码没有一行 if dataset == "hotel"。
四个数据源四套物理模型：电商（订单/明细）、咖啡（小票/小票行）、SaaS（账单/账单行）、
酒旅（预订单/住宿行，按"间夜"计量）——证明编译器只依赖 spec 声明，不依赖任何特定表结构。

物理模型（generator_d 实现）：
- bookings 预订单（booking_id / hotel_id / channel / checkin_date / guest_id 可空）
- stay_lines 住宿行（room_type_id / rooms 房间数 / nights 晚数 / unit_price 每晚房价）
- hotels 酒店（hotel_name / city / region / hotel_type 商务·度假·精品）
- room_types 房型（type_name 大床·双床·套房·亲子 / base_price / room_cost 每晚服务成本）

注入的业务故事（generator_d 实现）：
1. 度假型酒店预订占比逐月上升（强制改派份额 20%→40%，drill_down 按店型分解可见正贡献）；
2. 旅行社渠道毛利率系统性低于直销（渠道价 0.88 折，服务成本不随渠道变化）；
3. 暑期旺季（7-8 月）放量、国庆月（10 月）小高峰、淡季（12-1-2 月）回落。

行级权限：region 维度沿用全局 ROLE_SCOPES 的七大区划分（连锁酒店全国分布），
boss/south/north 语义不变——权限角色是组织级概念，不随数据集重定义。
"""

from __future__ import annotations

from .metrics import MetricDef
from .registry import DatasetSpec, register_spec

# 收入口径：房费收入（按间夜计价；D 数据集不采集取消/未入住，口径说明如实声明）
_REV = "SUM(sl.rooms * sl.nights * sl.unit_price)"
_NIGHTS = "SUM(sl.rooms * sl.nights)"
_COST = "SUM(sl.rooms * sl.nights * rt.room_cost)"

HOTEL_METRICS: dict[str, MetricDef] = {
    m.id: m
    for m in [
        MetricDef(
            id="room_revenue", name="房费收入", aliases=["房间收入", "收入", "营收", "房费"],
            expr=_REV, unit="yuan",
            caliber="房费收入 = Σ(房间数×晚数×每晚房价)，含直销/旅行社/协议/门店全渠道"
                    "（D 数据集不采集取消/未入住，预订单即成交）",
        ),
        MetricDef(
            id="booking_count", name="预订单数", aliases=["预订量", "预订数", "订单数", "单量"],
            expr="COUNT(DISTINCT b.booking_id)", unit="count", fmt="{:,.0f}",
            caliber="去重预订单数（一单多房型计一单）",
        ),
        MetricDef(
            id="room_nights", name="间夜数", aliases=["间夜", "入住间夜"],
            expr=_NIGHTS, unit="count", fmt="{:,.0f}",
            caliber="间夜数 = Σ(房间数×入住晚数)，住宿业计量单位",
        ),
        MetricDef(
            id="adr", name="平均房价", aliases=["ADR", "每日房价", "房价"],
            expr=f"({_REV}) / NULLIF({_NIGHTS}, 0)", unit="yuan",
            caliber="平均房价 ADR = 房费收入 / 间夜数（每间每晚实收均价）",
        ),
        MetricDef(
            id="guest_count", name="入住宾客数", aliases=["宾客数", "住客数"],
            expr="COUNT(DISTINCT b.guest_id)", unit="count", fmt="{:,.0f}",
            caliber="期间内有入住登记的去重宾客数（无宾客登记的预订单不计入）",
        ),
        MetricDef(
            id="gross_margin", name="毛利额", aliases=["毛利", "利润额"],
            expr=f"({_REV}) - ({_COST})", unit="yuan",
            caliber="房费收入 − 住宿服务成本（间夜数×每晚服务成本）",
        ),
        MetricDef(
            id="gross_margin_rate", name="毛利率", aliases=["利润率", "margin"],
            expr=f"(({_REV}) - ({_COST})) / NULLIF(({_REV}), 0)", unit="percent",
            higher_is_better=True, fmt="{:.2%}",
            caliber="毛利额 / 房费收入（旅行社渠道因 0.88 渠道价系统性低于直销）",
        ),
    ]
}

HOTEL_DIMS: dict[str, str] = {
    "month": "strftime('%Y-%m', b.checkin_date)",
    "region": "h.region",
    "city": "h.city",
    "hotel": "h.hotel_name",
    "hotel_type": "h.hotel_type",
    "room_type": "rt.type_name",
    "channel": "b.channel",
}

HOTEL_SPEC = DatasetSpec(
    name="hotel",
    title="某连锁酒店品牌 24 个月的住宿预订明细（bookings / stay_lines / hotels / room_types 逻辑视图）",
    db_path="data/askbi_d.duckdb",
    base_from=(
        "stay_lines sl JOIN bookings b ON sl.booking_id = b.booking_id "
        "JOIN hotels h ON b.hotel_id = h.hotel_id "
        "JOIN room_types rt ON sl.room_type_id = rt.room_type_id"
    ),
    version_probe_sql=(
        "SELECT (SELECT COUNT(*) FROM bookings),"
        " (SELECT COALESCE(MAX(checkin_date), '-') FROM bookings),"
        " (SELECT COUNT(*) FROM stay_lines),"
        " (SELECT COUNT(DISTINCT booking_id) FROM stay_lines)"
    ),
    dimensions=HOTEL_DIMS,
    filter_fields=dict(HOTEL_DIMS),
    dimension_values={
        "region": ["华南", "华北", "华东", "华中", "西南", "西北", "东北"],
        "hotel_type": ["商务", "度假", "精品"],
        "room_type": ["大床", "双床", "套房", "亲子"],
        "channel": ["直销", "旅行社", "协议", "门店"],
    },
    metrics=HOTEL_METRICS,
    time_field="b.checkin_date",
    start_month="2024-10",
    end_month="2026-09",
    # 取值过滤只对"有 filter_keywords 且声明 dimension_values"的维度生效（见 mock 解析器）；
    # 全部取值 ≤3 字：值提取正则最多回看 3 个汉字
    dim_keywords={
        # "酒店类型"长词在前：解析器对每个维度只记首个命中词（包含去重只比较已命中词），
        # "各酒店类型的收入"须以"酒店类型"命中 hotel_type，否则"酒店"会把 hotel 维也带进来
        "hotel_type": ["酒店类型", "店型"],
        "hotel": ["酒店"],
        "room_type": ["房型"],
        "region": ["大区", "区域", "地区"],
        "city": ["城市"],
        "channel": ["渠道"],
    },
    filter_keywords={
        "hotel_type": ("酒店", "店型"),
        "room_type": ("房型",),
        "region": ("大区", "区域", "地区"),
        "channel": ("渠道",),
    },
    drill_dims=("hotel_type", "channel", "region"),
)

register_spec(HOTEL_SPEC)
