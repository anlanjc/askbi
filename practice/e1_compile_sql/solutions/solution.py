"""E1 参考实现 —— 与 app/data/dsl.py 的编译器核心逐行为对应（先自己写，跑不过再看）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.data.metrics import METRICS
from app.data.schema import BASE_FROM, DIMENSION_VALUES, DIMENSIONS, FILTER_FIELDS

Ops = Literal["=", "!=", "in", "not in", ">", "<", ">=", "<="]

_END_MONTH = "2026-09"

MAX_LIMIT = 200

ROLE_SCOPES: dict[str, list[str] | None] = {
    "boss": None,
    "south": ["华南"],
    "north": ["华北", "东北"],
}


class CompileError(ValueError):
    pass


class Filter(BaseModel):
    field: str
    op: Ops
    value: Any

    @field_validator("field")
    @classmethod
    def _check_field(cls, v: str) -> str:
        if v not in FILTER_FIELDS:
            raise ValueError(f"未知过滤字段: {v}，可用: {list(FILTER_FIELDS)}")
        return v


class TimeRange(BaseModel):
    mode: Literal["last_n_months", "between", "all"] = "all"
    n: int | None = None
    start: str | None = None
    end: str | None = None

    def to_sql(self) -> str:
        f = "strftime('%Y-%m', o.order_date)"
        if self.mode == "last_n_months" and self.n:
            return (
                f"{f} >= strftime('%Y-%m', date_trunc('month', "
                f"DATE '{_END_MONTH}-01') - INTERVAL ({int(self.n) - 1}) MONTH)"
            )
        if self.mode == "between":
            parts = []
            if self.start:
                parts.append(f"{f} >= '{self.start}'")
            if self.end:
                parts.append(f"{f} <= '{self.end}'")
            return " AND ".join(parts) or "TRUE"
        return "TRUE"


class OrderBy(BaseModel):
    metric: str
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
    def _check_metric(cls, v: str) -> str:
        if v not in METRICS:
            raise ValueError(f"未知指标: {v}，可用: {list(METRICS)}")
        return v

    @field_validator("dimensions")
    @classmethod
    def _check_dims(cls, v: list[str]) -> list[str]:
        for d in v:
            if d not in DIMENSIONS:
                raise ValueError(f"未知维度: {d}，可用: {list(DIMENSIONS)}")
        return v

    @field_validator("limit")
    @classmethod
    def _check_limit(cls, v: int | None) -> int | None:
        if v is None:
            return None
        # 钳制而非报错：LLM 输出的越界 limit 不值得一次修复循环
        return max(1, min(v, MAX_LIMIT))


def _q(v: Any) -> str:
    """白名单校验 + 转义。数字直出；字符串校验枚举/格式后加引号。"""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    s = str(v).replace("'", "''")
    return f"'{s}'"


def compile_sql(dsl: QueryDSL, role: str = "boss") -> tuple[str, str]:
    """DSL -> (SQL, 口径说明)。编译期统一注入：权限、时间、LIMIT。"""
    metric = METRICS[dsl.metric]
    scope = ROLE_SCOPES.get(role)
    if scope is None and role != "boss":
        raise CompileError(f"未知角色: {role}")

    # 维度列在前，指标值在后
    selects = [f"{DIMENSIONS[d]} AS {d}" for d in dsl.dimensions] + [f"{metric.expr} AS value"]
    group_by = [DIMENSIONS[d] for d in dsl.dimensions]

    conds: list[str] = []
    conds.append(dsl.time.to_sql())
    if scope is not None:
        vals = ", ".join(_q(v) for v in scope)
        conds.append(f"o.region IN ({vals})")
    for f in dsl.filters:
        col = FILTER_FIELDS[f.field]
        if f.op in ("in", "not in"):
            if not isinstance(f.value, list) or not f.value:
                raise CompileError(f"filter {f.field} {f.op} 需要非空列表值")
            vals = ", ".join(_q(v) for v in f.value)
            conds.append(f"{col} {f.op.upper()} ({vals})")
        elif isinstance(f.value, list):
            raise CompileError(f"filter {f.field} {f.op} 不接受列表")
        else:
            conds.append(f"{col} {f.op} {_q(f.value)}")

    sql = f"SELECT {', '.join(selects)}\nFROM {BASE_FROM}\nWHERE {' AND '.join(conds)}"
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


def validate_filter_values(dsl: QueryDSL) -> list[str]:
    """对枚举维度做取值校验，返回问题列表（clarifier/sql 修复循环用）。"""
    problems = []
    for f in dsl.filters:
        allowed = DIMENSION_VALUES.get(f.field)
        if not allowed:
            continue
        vals = f.value if isinstance(f.value, list) else [f.value]
        for v in vals:
            if str(v) not in allowed:
                problems.append(f"过滤值 {f.field}={v} 不在数据集取值内（可选：{allowed}）")
    return problems
