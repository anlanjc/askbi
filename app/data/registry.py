"""数据源注册表：语义层从"硬编码"升级为"可注册数据"（多数据源适配的落点）。

设计主张（完整决策与失败模式见 docs/registry_adr.md）：
1. 语义层 = DatasetSpec：物理连接、维度/指标白名单、取值域、NL 词表全部随数据集走；
   编译器 / 提示词 / 校验器 / 缓存指纹从 spec 取值——LLM 上层（图/节点/QA/评测 runner）零感知。
2. DEFAULT_SPEC 逐位复刻原 schema.py/metrics.py 行为：直接引用同一批常量对象，
   默认路径（默认参数不传 spec）输出与改造前逐字节一致——这是回归裁判的前提。
3. 注册机制：get_spec(name) 按 settings.dataset 解析；dataset_b.py 等新数据集
   在模块导入时 register_spec 自注册（惰性加载避免循环导入）。

什么保持全局（有意为之，见 ADR）：行级权限角色表 ROLE_SCOPES（作用域维度约定为
region，两个数据集共享同一大区划分）、掩码列 MASKED_FIELDS（纵深防御不随数据集放松）、
mock 拒答词表（满意度/复购率等跨业务域通用的"未采集指标"）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import PROJECT_ROOT, get_settings
from .metrics import METRICS, MetricDef
from .schema import BASE_FROM, DIMENSIONS, DIMENSION_VALUES, FILTER_FIELDS


@dataclass(frozen=True)
class DatasetSpec:
    """一个数据集的完整语义层描述。字段分三组：物理 / 白名单 / NL 接口。"""

    # ---- 标识与物理层 ----
    name: str                    # 数据集标识（缓存指纹 / 评测归属 / settings.dataset 取值）
    title: str                   # schema prompt 的"数据集"行
    db_path: str | None          # None = 跟随 settings.db_abs_path（默认数据集保持配置驱动）
    base_from: str               # 指标/维度共用的事实表 JOIN（别名在此定义）
    version_probe_sql: str       # 缓存数据版本探测（表名随数据集不同）

    # ---- 白名单（防注入的根基：标识符全部来自这里） ----
    dimensions: dict[str, str]           # 维度 id -> SQL 表达式
    filter_fields: dict[str, str]        # 可过滤字段 -> SQL 表达式
    dimension_values: dict[str, list[str]]  # 枚举维度的合法取值（过滤值校验）
    metrics: dict[str, MetricDef]        # 指标字典（口径治理的单一来源）
    time_field: str                      # TimeRange 专用时间字段
    start_month: str                     # 数据起始月（YYYY-MM）
    end_month: str                       # 数据截止月（相对时间锚点，与生成器一致）

    # ---- NL 接口（mock 规则解析器与真实 LLM 的词表底座） ----
    dim_keywords: dict[str, list[str]] = field(default_factory=dict)
    filter_keywords: dict[str, tuple[str, ...]] = field(default_factory=dict)
    metric_parse_order: tuple[str, ...] | None = None  # 别名匹配优先序；None = metrics 定义序
    drill_dims: tuple[str, ...] = ("category", "channel", "region")  # 归因下钻分解维度

    @property
    def db_abs_path(self) -> Path:
        if self.db_path is None:
            return get_settings().db_abs_path
        p = Path(self.db_path)
        return p if p.is_absolute() else PROJECT_ROOT / p


# ---------------- 默认数据集（电商 A）：与原硬编码逐位一致 ----------------

DEFAULT_SPEC = DatasetSpec(
    name="ecommerce",
    title="某电商公司 24 个月的交易明细（orders / order_items / products / customers 逻辑视图）",
    db_path=None,
    base_from=BASE_FROM,
    version_probe_sql=(
        "SELECT (SELECT COUNT(*) FROM orders),"
        " (SELECT COALESCE(MAX(order_date), '-') FROM orders),"
        " (SELECT COUNT(*) FROM order_items),"
        " (SELECT COUNT(DISTINCT order_id) FROM order_items)"
    ),
    dimensions=DIMENSIONS,
    filter_fields=FILTER_FIELDS,
    dimension_values=DIMENSION_VALUES,
    metrics=METRICS,
    time_field="o.order_date",
    start_month="2024-10",
    end_month="2026-09",
    # mock 解析词表自 mock.py 原样上收（值逐字节一致，默认解析行为不变）
    dim_keywords={
        "category": ["品类", "类别", "类目", "商品类"],
        "subcategory": ["子类", "细分品类"],
        "region": ["大区", "区域", "地区"],
        "province": ["省份", "省"],
        "city": ["城市"],
        "channel": ["渠道"],
        "member_level": ["会员等级", "会员"],
    },
    filter_keywords={
        "region": ("大区", "区域", "地区"),
        "category": ("品类", "类别", "类目"),
        "channel": ("渠道",),
        "member_level": ("会员",),
    },
    metric_parse_order=(
        "gross_margin_rate", "gross_margin", "revenue", "net_revenue", "order_count",
        "item_quantity", "avg_order_value", "refund_rate", "customer_count",
    ),
)


# ---------------- 注册表 ----------------

_SPECS: dict[str, DatasetSpec] = {"ecommerce": DEFAULT_SPEC}
_BUILTINS_LOADED = False


def register_spec(spec: DatasetSpec) -> None:
    """注册/覆盖一个数据集。测试可用 dataclasses.replace 派生变体后注册。"""
    _SPECS[spec.name] = spec


def reset_specs() -> None:
    """测试用：清掉非内置 spec，恢复初始注册表。

    注意必须直接重注册（而非依赖惰性 import）——dataset_b 模块导入一次后
    Python 会缓存模块，重跑其自注册代码不会发生。
    """
    global _BUILTINS_LOADED
    _SPECS.clear()
    _SPECS["ecommerce"] = DEFAULT_SPEC
    from .dataset_b import COFFEE_SPEC

    _SPECS["coffee"] = COFFEE_SPEC
    _BUILTINS_LOADED = True


def _ensure_builtins() -> None:
    global _BUILTINS_LOADED
    if not _BUILTINS_LOADED:
        from . import dataset_b  # noqa: F401  导入即自注册 coffee

        _BUILTINS_LOADED = True


def get_spec(name: str | None = None) -> DatasetSpec:
    """按名取 spec；None = settings.dataset（进程级数据集选择，默认 ecommerce）。"""
    _ensure_builtins()
    key = name if name is not None else get_settings().dataset
    try:
        return _SPECS[key]
    except KeyError:
        raise KeyError(
            f"未知数据集: {key}（已注册: {sorted(_SPECS)}）。"
            "新数据集需在 app/data/ 下定义并 register_spec 自注册。"
        ) from None
