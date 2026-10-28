"""数据模式与词表：暴露给 LLM 的"最小面"由这里集中控制。

安全设计：
- customers 表刻意不含手机号等敏感字段（最小暴露面），DSL 白名单外的字段一律不可达；
- MASKED_FIELDS 为纵深防御：即使有人手工构造 SQL 引用这些列，沙箱也会拒绝。
"""

from __future__ import annotations

# 基表连接（所有指标共用的事实表join）
BASE_FROM = (
    "orders o JOIN order_items oi ON o.order_id = oi.order_id "
    "JOIN products p ON oi.product_id = p.product_id "
    "JOIN customers c ON o.customer_id = c.customer_id"
)

# 维度白名单：field -> SQL 表达式（防注入的根基：标识符全部硬编码）
DIMENSIONS: dict[str, str] = {
    "month": "strftime('%Y-%m', o.order_date)",
    "category": "p.category",
    "subcategory": "p.subcategory",
    "region": "o.region",
    "province": "o.province",
    "city": "o.city",
    "channel": "o.channel",
    "member_level": "c.member_level",
}

# 过滤字段白名单：维度字段 + 可比较的数值/日期字段
FILTER_FIELDS: dict[str, str] = {**DIMENSIONS}

# 时间字段（TimeRange 专用）
TIME_FIELD = "o.order_date"

# 纵深防御：这些列名不允许出现在任何生成的 SQL 中
MASKED_FIELDS = ("phone", "email", "id_card", "address", "password")

DIMENSION_VALUES: dict[str, list[str]] = {
    "region": ["华南", "华北", "华东", "华中", "西南", "西北", "东北"],
    "category": ["数码", "服饰", "美妆", "家居", "食品", "运动"],
    "channel": ["直播", "搜索", "推荐", "社交", "官网"],
    "member_level": ["普通", "银卡", "金卡", "黑卡"],
    "status": ["已完成", "已退货", "已取消"],
}


def schema_prompt() -> str:
    """给 LLM 看的 schema 摘要（不含物理表名，只暴露逻辑视图）。"""
    dims = "\n".join(f"  - {k}" for k in DIMENSIONS)
    vals = "\n".join(f"  - {k}: {'/'.join(v)}" for k, v in DIMENSION_VALUES.items() if v)
    return (
        "数据集：某电商公司 24 个月的交易明细（orders / order_items / products / customers 逻辑视图）。\n"
        "可用维度：\n" + dims + "\n"
        "维度取值（过滤值必须取自这里，取值之外一律拒绝）：\n" + vals + "\n"
        "可用时间范围：2024-10 至 2026-09（按月）。数据截止月为 2026-09；"
        "相对时间（上月/本月/最近N个月）一律以 2026-09 为基准（上月=本月=2026-09），"
        "这是系统约定，无需向用户澄清。\n"
        "指标见指标字典（metrics）。不支持的字段/指标一律拒绝，不允许编造。"
    )
