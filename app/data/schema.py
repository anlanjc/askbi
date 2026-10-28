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


def schema_prompt(spec=None) -> str:
    """给 LLM 看的 schema 摘要（不含物理表名，只暴露逻辑视图）。

    spec=None 时使用默认数据集（registry.DEFAULT_SPEC），输出与硬编码时期逐字节一致。
    """
    if spec is None:
        from .registry import DEFAULT_SPEC  # 延迟导入避免循环

        spec = DEFAULT_SPEC
    dims = "\n".join(f"  - {k}" for k in spec.dimensions)
    vals = "\n".join(f"  - {k}: {'/'.join(v)}" for k, v in spec.dimension_values.items() if v)
    return (
        f"数据集：{spec.title}。\n"
        "可用维度：\n" + dims + "\n"
        "维度取值（过滤值必须取自这里，取值之外一律拒绝）：\n" + vals + "\n"
        f"可用时间范围：{spec.start_month} 至 {spec.end_month}（按月）。数据截止月为 {spec.end_month}；"
        f"相对时间（上月/本月/最近N个月）一律以 {spec.end_month} 为基准（上月=本月={spec.end_month}），"
        "这是系统约定，无需向用户澄清。\n"
        "指标见指标字典（metrics）。不支持的字段/指标一律拒绝，不允许编造。"
    )
