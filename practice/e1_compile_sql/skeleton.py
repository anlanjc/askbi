"""E1 · 白板重写 DSL 编译器（对应 app/data/dsl.py，讲义 docs/lectures/L1_dsl编译器.md）。

规则：
- 词表（METRICS/DIMENSIONS/FILTER_FIELDS/DIMENSION_VALUES/BASE_FROM）允许直接 import——
  练习目标是"编译器"这一层，不是背词表；
- 除标注 TODO 的部分外，不要改动本文件其他内容；完成后本文件就是被测对象。

行为规格（务必逐条满足）：
1. QueryDSL/Filter 的枚举校验：metric 必须 in METRICS、dimensions 每个 in DIMENSIONS、
   filter.field 必须 in FILTER_FIELDS，否则抛 pydantic ValidationError；
2. limit 钳制而非报错：None 保持 None，其余钳到 [1, MAX_LIMIT]；
3. TimeRange.to_sql()：last_n_months 以 _END_MONTH 为锚；between 产出 start/end 条件（无值产出 TRUE）；
   all 产出 "TRUE"；
4. compile_sql(dsl, role) -> (sql, caliber)：
   - 未知角色（ROLE_SCOPES.get(role) is None 且 role != "boss"）抛 CompileError；
   - selects = 维度列(AS 字段名) + 指标表达式(AS value)；group_by = 维度表达式；
   - conds 顺序：时间 → 权限 IN → 各 filter；in/not in 需要非空列表，标量 op 不接受列表（CompileError）；
   - FROM 用 BASE_FROM；有维度则 GROUP BY；ORDER BY value <方向>（无 order_by 默认按主指标 desc）；
   - LIMIT：min(dsl.limit or 100, MAX_LIMIT)，永远输出 LIMIT 行；
   - caliber = "指标口径｜{指标名}：{口径}" +（有权限范围时）"；权限范围：{'+'.join(scope)}（角色 {role}）"；
5. _q(v)：数字（排除 bool）直出；字符串先做单引号转义（' -> ''）再加引号；
6. validate_filter_values(dsl)：对 DIMENSION_VALUES 覆盖的枚举字段校验取值，
   返回问题列表（字符串），字段不在 DIMENSION_VALUES 则跳过。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Ops = Literal["=", "!=", "in", "not in", ">", "<", ">=", "<="]

# 数据截止月：last_n_months 的时间锚点（给定，与生成器一致）
_END_MONTH = "2026-09"

MAX_LIMIT = 200

# 行级权限：角色 -> 可见区域（None = 不限）。给定词表。
ROLE_SCOPES: dict[str, list[str] | None] = {
    "boss": None,
    "south": ["华南"],
    "north": ["华北", "东北"],
}


class CompileError(ValueError):
    pass


# ---------------- TODO 1: 三个 pydantic 模型 ----------------


class Filter(BaseModel):
    """过滤条件：field 必须在 FILTER_FIELDS 白名单内。"""

    field: str
    op: Ops
    value: Any

    # TODO: field_validator —— v not in FILTER_FIELDS 时 raise ValueError


class TimeRange(BaseModel):
    """时间范围。to_sql() 渲染为 strftime('%Y-%m', o.order_date) 上的比较条件。"""

    mode: Literal["last_n_months", "between", "all"] = "all"
    n: int | None = None
    start: str | None = None  # YYYY-MM
    end: str | None = None

    def to_sql(self) -> str:
        # TODO:
        #  f = "strftime('%Y-%m', o.order_date)"
        #  last_n_months 且 n 非空:  f >= strftime('%Y-%m', date_trunc('month', DATE '{_END_MONTH}-01') - INTERVAL ({int(n)-1}) MONTH)
        #  between: start/end 各一条（>=/<=），AND 连接；两者都缺则 "TRUE"
        #  all: "TRUE"
        raise NotImplementedError


class OrderBy(BaseModel):
    metric: str
    direction: Literal["asc", "desc"] = "desc"


class QueryDSL(BaseModel):
    """查询意图：metric/dimensions 全枚举校验，limit 钳制不报错。"""

    intent: Literal["aggregate", "breakdown", "topn", "trend", "compare"] = "aggregate"
    metric: str
    dimensions: list[str] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    time: TimeRange = Field(default_factory=TimeRange)
    order_by: OrderBy | None = None
    limit: int | None = None

    # TODO: 三个 field_validator
    #  _check_metric: v not in METRICS -> ValueError("未知指标: ...")
    #  _check_dims: 任一 d not in DIMENSIONS -> ValueError("未知维度: ...")
    #  _check_limit: None -> None；否则 max(1, min(v, MAX_LIMIT))（钳制而非报错！）


# ---------------- TODO 2: 值转义 ----------------


def _q(v: Any) -> str:
    """白名单校验 + 转义。数字直出（注意先排除 bool）；字符串 ' -> '' 后加引号。"""
    raise NotImplementedError


# ---------------- TODO 3: 编译器主体 ----------------


def compile_sql(dsl: QueryDSL, role: str = "boss") -> tuple[str, str]:
    """DSL -> (SQL, 口径说明)。编译期统一注入：权限、时间、LIMIT。

    五段骨架（讲义 L1 第 3 节）：
    ① metric = METRICS[dsl.metric]；scope = ROLE_SCOPES.get(role)，未知角色 raise CompileError
    ② selects/group_by：维度列在前、指标在后（AS value）
    ③ conds：时间 + 权限 IN + filters（in/not in 校验列表、标量拒绝列表）
    ④ 组装：FROM BASE_FROM、GROUP BY、ORDER BY value、LIMIT（默认100，上限 MAX_LIMIT）
    ⑤ caliber：口径 + 权限范围说明
    """
    raise NotImplementedError


# ---------------- TODO 4: 取值校验 ----------------


def validate_filter_values(dsl: QueryDSL) -> list[str]:
    """对枚举维度做取值校验，返回问题列表（clarifier/sql 修复循环用）。

    规则：filter.field 在 DIMENSION_VALUES 中时，其 value（标量或列表逐个）
    不在允许取值内 -> 追加一条 "过滤值 {field}={v} 不在数据集取值内（可选：{allowed}）"。
    """
    raise NotImplementedError
