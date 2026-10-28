"""AskBI MCP Server 启动入口（stdio 传输）。

用法：
    python scripts/mcp_server.py
然后在 Claude Desktop 等客户端的 MCP 配置中添加本命令。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.mcp.server import mcp  # noqa: E402

if __name__ == "__main__":
    mcp.run()  # stdio
