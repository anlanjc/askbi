"""数据集 C 语义层：B2B SaaS 订阅（客户公司 × 套餐档位 × 直销/代理/自助渠道）。

"换数据源不改上层"的第三个实证：本文件只声明语义层（DatasetSpec）+ 数据生成器入口，
图结构 / 编译器 / QA / 评测 runner 全部复用——上层代码没有一行 if dataset == "saas"。
三个数据源三套物理模型：电商（订单/明细）、咖啡（小票/小票行）、SaaS（账单/账单行），
证明编译器只依赖 spec 声明，不依赖任何特定表结构。

物理模型：
- invoices 月度账单（invoice_id / company_id / channel / invoice_date）
- invoice_lines 账单行（plan_id / seats 席位数 / unit_price 席位月价，代理渠道单价=价目表×0.85）
- companies 客户公司（company_name / industry / region）
- plans 套餐（plan_name / tier 基础·专业·旗舰 / seat_cost 席位服务成本）

注入的业务故事（generator_c 实现）：
1. 旗舰版收入占比逐月上升（20%→40%，drill_down 按档位分解可见正贡献）；
2. 代理渠道毛利率系统性低于直销（渠道折扣 0.85，成本不随渠道变化）；
3. 年末预算季账单放量、春节月回落。

行级权限：region 维度沿用全局 ROLE_SCOPES 的七大区划分（客户公司全国分布），
boss/south/north 语义不变——权限角色是组织级概念，不随数据集重定义。
"""

from __future__ import annotations

from .metrics import MetricDef
from .registry import DatasetSpec, register_spec

# 收入口径：月经常性收入（席位×席位月价；C 数据集不采集退款，口径说明如实声明）
_MRR = "SUM(l.seats * l.unit_price)"
_COST = "SUM(l.seats * p.seat_cost)"
_COMPANIES = "COUNT(DISTINCT i.company_id)"

SAAS_METRICS: dict[str, MetricDef] = {
    m.id: m
    for m in [
        MetricDef(
            id="mrr", name="MRR", aliases=["月经常性收入", "月度收入"],
            expr=_MRR, unit="yuan",
            caliber="月经常性收入 MRR = Σ(席位数×席位月价)，含直销/代理/自助全渠道（C 数据集不采集退款）",
        ),
        MetricDef(
            id="seat_count", name="席位数", aliases=["席位", "座位数"],
            expr="SUM(l.seats)", unit="count", fmt="{:,.0f}",
            caliber="计费席位总数（订阅的账号席位数之和）",
        ),
        MetricDef(
            id="invoice_count", name="账单数", aliases=["账单量"],
            expr="COUNT(DISTINCT i.invoice_id)", unit="count", fmt="{:,.0f}",
            caliber="去重月度账单数（一客户多套餐计多张）",
        ),
        MetricDef(
            id="company_count", name="付费客户数", aliases=["客户数"],
            expr=_COMPANIES, unit="count", fmt="{:,.0f}",
            caliber="期间内有计费账单的去重客户公司数",
        ),
        MetricDef(
            id="arpu", name="ARPU", aliases=["客均收入", "每客户平均收入"],
            expr=f"({_MRR}) / NULLIF({_COMPANIES}, 0)", unit="yuan",
            caliber="月经常性收入 / 付费客户数（每客户平均月收入）",
        ),
        MetricDef(
            id="gross_margin", name="毛利额", aliases=["毛利", "利润额"],
            expr=f"({_MRR}) - ({_COST})", unit="yuan",
            caliber="月经常性收入 − 席位服务成本（数量×席位成本）",
        ),
        MetricDef(
            id="gross_margin_rate", name="毛利率", aliases=["利润率", "margin"],
            expr=f"(({_MRR}) - ({_COST})) / NULLIF(({_MRR}), 0)", unit="percent",
            higher_is_better=True, fmt="{:.2%}",
            caliber="毛利额 / 月经常性收入（代理渠道因 0.85 折扣系统性低于直销）",
        ),
    ]
}

SAAS_DIMS: dict[str, str] = {
    "month": "strftime('%Y-%m', i.invoice_date)",
    "region": "c.region",
    "industry": "c.industry",
    "company": "c.company_name",
    "tier": "p.tier",
    "plan": "p.plan_name",
    "channel": "i.channel",
}

SAAS_SPEC = DatasetSpec(
    name="saas",
    title="某 B2B SaaS 公司 24 个月的订阅账单明细（invoices / invoice_lines / companies / plans 逻辑视图）",
    db_path="data/askbi_c.duckdb",
    base_from=(
        "invoice_lines l JOIN invoices i ON l.invoice_id = i.invoice_id "
        "JOIN companies c ON i.company_id = c.company_id "
        "JOIN plans p ON l.plan_id = p.plan_id"
    ),
    version_probe_sql=(
        "SELECT (SELECT COUNT(*) FROM invoices),"
        " (SELECT COALESCE(MAX(invoice_date), '-') FROM invoices),"
        " (SELECT COUNT(*) FROM invoice_lines),"
        " (SELECT COUNT(DISTINCT invoice_id) FROM invoice_lines)"
    ),
    dimensions=SAAS_DIMS,
    filter_fields=dict(SAAS_DIMS),
    dimension_values={
        "region": ["华南", "华北", "华东", "华中", "西南", "西北", "东北"],
        "industry": ["制造", "互联网", "零售", "金融", "教育"],
        "tier": ["基础版", "专业版", "旗舰版"],
        "channel": ["直销", "代理", "自助"],
    },
    metrics=SAAS_METRICS,
    time_field="i.invoice_date",
    start_month="2024-10",
    end_month="2026-09",
    # 取值过滤只对"有 filter_keywords 且声明 dimension_values"的维度生效（见 mock 解析器）；
    # 全部取值 ≤3 字：值提取正则最多回看 3 个汉字
    dim_keywords={
        # 长词在前：解析器对每个维度只记首个命中词（包含去重只比较已命中词），
        # "各套餐档位" 须以"套餐档位"命中 tier，否则"套餐"会把 plan 维也带进来
        "tier": ["套餐档位", "档位"],
        "plan": ["套餐"],
        "industry": ["行业"],
        "region": ["大区", "区域", "地区"],
        "company": ["公司"],
        "channel": ["渠道"],
    },
    filter_keywords={
        "tier": ("套餐",),
        "industry": ("行业",),
        "region": ("大区", "区域", "地区"),
        "channel": ("渠道",),
    },
    drill_dims=("tier", "channel", "industry"),
)

register_spec(SAAS_SPEC)
