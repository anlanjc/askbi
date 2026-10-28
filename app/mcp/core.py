"""MCP 服务核心逻辑：与传输层解耦，可被 stdio server / 测试直接复用。

对外暴露三个工具（任何 MCP 客户端——如 Claude Desktop——都能接入本数据服务）：
- list_metrics：指标字典（口径治理对外的窗口）
- describe_schema：维度与取值
- query_metric：DSL 参数 -> 沙箱执行 -> 结构化结果（同样的安全与口径约束）
"""

from __future__ import annotations

import json

from ..config import get_settings
from ..data.dsl import build_dsl, compile_sql
from ..data.registry import get_spec
from ..data.sandbox import execute_sync

TOOLS_SPEC = [
    {
        "name": "list_metrics",
        "description": "列出可用指标及其权威口径（指标字典）",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "describe_schema",
        "description": "列出可用维度、取值范围与数据时间范围",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "query_metric",
        "description": (
            "按 DSL 查询指标。metric 用 list_metrics 返回的 id；"
            "dimensions 可多选；filters 形如 [{field,op,value}]；"
            "time 为 {mode: last_n_months|between|all, n, start, end}"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string"},
                "dimensions": {"type": "array", "items": {"type": "string"}},
                "filters": {"type": "array", "items": {"type": "object"}},
                "time": {"type": "object"},
                "limit": {"type": "integer"},
            },
            "required": ["metric"],
        },
    },
]


def handle_tool(name: str, args: dict) -> str:
    """同步核心：供 MCP server 与测试直接调用。数据集跟随 settings.dataset。"""
    spec = get_spec(get_settings().dataset)
    if name == "list_metrics":
        return json.dumps(
            {mid: {"name": m.name, "unit": m.unit, "caliber": m.caliber} for mid, m in spec.metrics.items()},
            ensure_ascii=False,
        )
    if name == "describe_schema":
        return json.dumps(
            {
                "dimensions": {k: spec.dimension_values.get(k, "（连续值）") for k in spec.dimensions},
                "time_range": f"{spec.start_month} ~ {spec.end_month}（月粒度）",
            },
            ensure_ascii=False,
        )
    if name == "query_metric":
        dsl = build_dsl(
            {
                "metric": args["metric"],
                "dimensions": args.get("dimensions") or [],
                "filters": args.get("filters") or [],
                "time": args.get("time") or {"mode": "all"},
                "order_by": {"metric": args["metric"], "direction": "desc"},
                "limit": args.get("limit") or 50,
            },
            spec,
        )
        sql, caliber = compile_sql(dsl, role="boss", spec=spec)
        res = execute_sync(sql, db_path=spec.db_abs_path)
        return json.dumps(
            {
                "sql": sql,
                "caliber": caliber,
                "columns": res.columns,
                "rows": res.rows,
                "row_count": res.row_count,
            },
            ensure_ascii=False,
        )
    raise ValueError(f"未知工具: {name}")
