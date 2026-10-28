"""MCP stdio server：把指标查询能力开放给任意 MCP 客户端。

运行：python scripts/mcp_server.py
验证：用任意 MCP 客户端（如 Claude Desktop）配置本命令即可调用
list_metrics / describe_schema / query_metric。
（mcp 2.x：FastMCP 已更名为 MCPServer）
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from .core import handle_tool

mcp = MCPServer("askbi-metrics")


@mcp.tool(description="列出可用指标及其权威口径（指标字典）")
def list_metrics() -> str:
    return handle_tool("list_metrics", {})


@mcp.tool(description="列出可用维度、取值范围与数据时间范围")
def describe_schema() -> str:
    return handle_tool("describe_schema", {})


@mcp.tool(
    description=(
        "按 DSL 查询指标。metric 用 list_metrics 返回的 id；"
        "dimensions 可多选；filters 形如 [{field,op,value}]；"
        "time 为 {mode: last_n_months|between|all, n, start, end}"
    )
)
def query_metric(
    metric: str,
    dimensions: list[str] | None = None,
    filters: list[dict] | None = None,
    time: dict | None = None,
    limit: int | None = None,
) -> str:
    return handle_tool(
        "query_metric",
        {
            "metric": metric,
            "dimensions": dimensions or [],
            "filters": filters or [],
            "time": time or {"mode": "all"},
            "limit": limit,
        },
    )


if __name__ == "__main__":
    mcp.run()  # stdio 传输
