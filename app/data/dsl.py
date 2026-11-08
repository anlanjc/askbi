"""查询 DSL 与编译器 —— NL2DSL2SQL 的核心。

设计主张（面试答"为什么不直接 NL2SQL"）：
1. LLM 只产出受白名单约束的结构化意图（metric/dimension/filter 均为枚举校验），
   物理标识符由编译器渲染 —— 提示注入/幻觉表名在编译期就不可达；
2. DSL 可解释、可 diff、可单测（编译器是纯函数），SQL 复杂度收敛在指标字典；
3. 行级权限、口径、LIMIT 策略在编译期统一注入，LLM 无法绕过。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from .metrics import METRICS
from .registry import DEFAULT_SPEC, DatasetSpec
from .schema import DIMENSIONS, FILTER_FIELDS

Ops = Literal["=", "!=", "in", "not in", ">", "<", ">=", "<="]

# 行级权限作用域维度（全局约定）：角色 -> 可见 region 取值（None = 不限）
_SCOPE_DIM = "region"


def _whitelists(info: ValidationInfo | None) -> tuple[dict, dict, dict]:
    """校验白名单解析：context 携带 spec 用 spec；否则模块全局（默认数据集，行为不变）。

    pydantic v2 的 context 会自动传播到嵌套模型（QueryDSL -> Filter），
    因此 Filter 单独构造（无 context）时同样回退全局白名单。
    """
    ctx = getattr(info, "context", None)
    spec = ctx.get("spec") if isinstance(ctx, dict) else None
    if spec is None:
        return METRICS, DIMENSIONS, FILTER_FIELDS
    return spec.metrics, spec.dimensions, spec.filter_fields


class Filter(BaseModel):
    field: str
    op: Ops
    value: Any

    @field_validator("field")
    @classmethod
    def _check_field(cls, v: str, info: ValidationInfo) -> str:
        _metrics, _dimensions, filter_fields = _whitelists(info)
        if v not in filter_fields:
            raise ValueError(f"未知过滤字段: {v}，可用: {list(filter_fields)}")
        return v


class TimeRange(BaseModel):
    mode: Literal["last_n_months", "between", "all"] = "all"
    n: int | None = None
    start: str | None = None  # YYYY-MM
    end: str | None = None

    @field_validator("start", "end")
    @classmethod
    def _check_month(cls, v: str | None) -> str | None:
        # ASK-1 修复：start/end 曾未校验直拼 SQL，可注释行级权限或借表函数读文件——
        # 现强制 YYYY-MM 白名单格式（评审 2.0 P0）
        if v is None:
            return v
        if not re.fullmatch(r"\d{4}-\d{2}", v):
            raise ValueError(f"时间格式必须为 YYYY-MM，收到: {v!r}")
        return v

    @field_validator("n")
    @classmethod
    def _check_n(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError(f"n 必须为正整数，收到: {v}")
        return v

    def to_sql(self, time_field: str = "o.order_date", end_month: str | None = None) -> str:
        f = f"strftime('%Y-%m', {time_field})"
        em = end_month or _END_MONTH
        if self.mode == "last_n_months" and self.n:
            return (
                f"{f} >= strftime('%Y-%m', date_trunc('month', "
                f"DATE '{em}-01') - INTERVAL ({int(self.n) - 1}) MONTH)"
            )
        if self.mode == "between":
            parts = []
            if self.start:
                parts.append(f"{f} >= {_q(self.start)}")  # 纵深：校验之上再走统一转义
            if self.end:
                parts.append(f"{f} <= {_q(self.end)}")
            return " AND ".join(parts) or "TRUE"
        return "TRUE"


# 数据截止月：与生成器一致，保证 last_n_months 语义稳定
_END_MONTH = "2026-09"

MAX_LIMIT = 200


class OrderBy(BaseModel):
    metric: str  # 排序依据指标（通常是主指标）
    direction: Literal["asc", "desc"] = "desc"


class QueryDSL(BaseModel):
    intent: Literal["aggregate", "breakdown", "topn", "trend", "compare"] = "aggregate"
    metric: str
    dimensions: list[str] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    time: TimeRange = Field(default_factory=TimeRange)
    order_by: OrderBy | None = None
    limit: int | None = None

    @field_validator("metric")
    @classmethod
    def _check_metric(cls, v: str, info: ValidationInfo) -> str:
        metrics, _dims, _ff = _whitelists(info)
        if v not in metrics:
            raise ValueError(f"未知指标: {v}，可用: {list(metrics)}")
        return v

    @field_validator("dimensions")
    @classmethod
    def _check_dims(cls, v: list[str], info: ValidationInfo) -> list[str]:
        _metrics, dims, _ff = _whitelists(info)
        for d in v:
            if d not in dims:
                raise ValueError(f"未知维度: {d}，可用: {list(dims)}")
        return v

    @field_validator("limit")
    @classmethod
    def _check_limit(cls, v: int | None) -> int | None:
        if v is None:
            return None
        # 钳制而非报错：LLM 输出的越界 limit 不值得一次修复循环
        return max(1, min(v, MAX_LIMIT))


# 行级权限：角色 -> 可见区域（None = 不限）。作用域维度全局约定为 region（_SCOPE_DIM），
# 角色表不随数据集重定义——组织级概念（详见 docs/registry_adr.md）
ROLE_SCOPES: dict[str, list[str] | None] = {
    "boss": None,
    "south": ["华南"],
    "north": ["华北", "东北"],
}


class CompileError(ValueError):
    pass


def build_dsl(payload: dict, spec: DatasetSpec | None = None) -> QueryDSL:
    """按 spec 校验并构建 DSL。spec=None 时与 QueryDSL(**payload) 行为完全一致。"""
    ctx = {"spec": spec} if spec is not None else None
    return QueryDSL.model_validate(payload, context=ctx)


def compile_sql(dsl: QueryDSL, role: str = "boss", spec: DatasetSpec | None = None) -> tuple[str, str]:
    """DSL -> (SQL, 口径说明)。编译期统一注入：权限、时间、LIMIT。

    spec 指定目标数据集的语义层（None = 默认数据集，行为与硬编码时期一致）。
    纵深防御：值参数化不现实（DuckDB线程限制），因此维度值走
    dimension_values 白名单 + 引号转义双保险；标识符全部来自 spec 声明。
    """
    spec = spec or DEFAULT_SPEC
    metric = spec.metrics.get(dsl.metric)
    if metric is None:
        raise CompileError(f"未知指标: {dsl.metric}（数据集 {spec.name} 不含该指标）")
    scope = ROLE_SCOPES.get(role)
    if scope is None and role != "boss":
        raise CompileError(f"未知角色: {role}")

    def _dim(d: str) -> str:
        expr = spec.dimensions.get(d)
        if expr is None:
            raise CompileError(f"未知维度: {d}（数据集 {spec.name} 不支持）")
        return expr

    # 维度列在前，指标值在后
    selects = [f"{_dim(d)} AS {d}" for d in dsl.dimensions] + [f"{metric.expr} AS value"]
    group_by = [_dim(d) for d in dsl.dimensions]

    conds: list[str] = []
    conds.append(dsl.time.to_sql(spec.time_field, spec.end_month))
    if scope is not None:
        scope_expr = spec.dimensions.get(_SCOPE_DIM)
        if scope_expr is None:
            raise CompileError(f"数据集 {spec.name} 未定义行级权限维度 {_SCOPE_DIM}")
        vals = ", ".join(_q(v) for v in scope)
        conds.append(f"{scope_expr} IN ({vals})")
    for f in dsl.filters:
        col = spec.filter_fields.get(f.field)
        if col is None:
            raise CompileError(f"未知过滤字段: {f.field}（数据集 {spec.name} 不支持）")
        if f.op in ("in", "not in"):
            if not isinstance(f.value, list) or not f.value:
                raise CompileError(f"filter {f.field} {f.op} 需要非空列表值")
            vals = ", ".join(_q(v) for v in f.value)
            conds.append(f"{col} {f.op.upper()} ({vals})")
        elif isinstance(f.value, list):
            raise CompileError(f"filter {f.field} {f.op} 不接受列表")
        else:
            conds.append(f"{col} {f.op} {_q(f.value)}")

    sql = f"SELECT {', '.join(selects)}\nFROM {spec.base_from}\nWHERE {' AND '.join(conds)}"
    if group_by:
        sql += "\nGROUP BY " + ", ".join(str(g) for g in group_by)
    ob = dsl.order_by or OrderBy(metric=dsl.metric, direction="desc")
    sql += f"\nORDER BY value {ob.direction.upper()}"
    limit = min(dsl.limit or 100, MAX_LIMIT)
    sql += f"\nLIMIT {limit}"

    caliber = f"指标口径｜{metric.name}：{metric.caliber}" + (
        f"；权限范围：{'+'.join(scope)}（角色 {role}）" if scope else ""
    )
    return sql, caliber


def _q(v: Any) -> str:
    """白名单校验 + 转义。数字直出；字符串校验枚举/格式后加引号。"""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    s = str(v).replace("'", "''")
    return f"'{s}'"


def validate_filter_values(dsl: QueryDSL, spec: DatasetSpec | None = None) -> list[str]:
    """对枚举维度做取值校验，返回问题列表（clarifier/sql 修复循环用）。"""
    spec = spec or DEFAULT_SPEC
    problems = []
    for f in dsl.filters:
        allowed = spec.dimension_values.get(f.field)
        if not allowed:
            continue
        vals = f.value if isinstance(f.value, list) else [f.value]
        for v in vals:
            if str(v) not in allowed:
                problems.append(f"过滤值 {f.field}={v} 不在数据集取值内（可选：{allowed}）")
    return problems
