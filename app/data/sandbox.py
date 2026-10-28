"""SQL 执行沙箱：只读连接 + 语句守卫 + 超时中断 + 结果规模控制。

安全层次（DESIGN.md 的"失败降级矩阵"之一环）：
1. 连接层：duckdb read_only=True，物理上不可写；
2. 语句层：单条 SELECT/WITH、黑名单关键词、禁引用掩码列；
3. 资源层：强制 LIMIT、执行超时（interrupt() 打断）。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb

from ..config import get_settings
from .schema import MASKED_FIELDS

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|export|import|"
    r"pragma|call|set|prepare|execute|vacuum|checkpoint)\b",
    re.IGNORECASE,
)
_MAX_ROWS = 1000


class SqlRejected(ValueError):
    """语句未通过守卫。"""


class SqlTimeout(TimeoutError):
    pass


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]
    row_count: int
    truncated: bool = False
    elapsed_ms: float = 0.0
    sql: str = ""
    note: str = ""


def guard(sql: str) -> str:
    s = sql.strip().rstrip(";").strip()
    if not s:
        raise SqlRejected("空语句")
    if ";" in s:
        raise SqlRejected("检测到多语句，已拒绝（仅允许单条查询）")
    if not re.match(r"^(select|with)\b", s, re.IGNORECASE):
        raise SqlRejected("仅允许 SELECT / WITH 查询")
    m = _FORBIDDEN.search(s)
    if m:
        raise SqlRejected(f"包含被禁止的关键词: {m.group(0)}")
    low = s.lower()
    for col in MASKED_FIELDS:
        if col in low:
            raise SqlRejected(f"引用了受限列: {col}")
    if "limit" not in low:
        s += f"\nLIMIT {_MAX_ROWS}"
    return s


def _jsonable(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


class Sandbox:
    def __init__(self, db_path: Path | str | None = None, timeout: float | None = None):
        settings = get_settings()
        self.db_path = str(db_path or settings.db_abs_path)
        self.timeout = timeout or settings.sql_timeout_seconds

    def _run(self, con: duckdb.DuckDBPyConnection, sql: str) -> QueryResult:
        t0 = time.perf_counter()
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = [[_jsonable(v) for v in row] for row in cur.fetchmany(_MAX_ROWS + 1)]
        truncated = len(rows) > _MAX_ROWS
        if truncated:
            rows = rows[:_MAX_ROWS]
        elapsed = (time.perf_counter() - t0) * 1000
        return QueryResult(
            columns=cols, rows=rows, row_count=len(rows), truncated=truncated,
            elapsed_ms=round(elapsed, 1), sql=sql,
        )

    async def execute(self, sql: str) -> QueryResult:
        safe_sql = guard(sql)
        con = duckdb.connect(self.db_path, read_only=True)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._run, con, safe_sql), timeout=self.timeout
            )
        except TimeoutError:
            # asyncio.to_thread 无法取消已开跑的线程，必须用 interrupt()
            # 从事件循环线程打断工作线程中的查询（duckdb 支持跨线程调用）
            con.interrupt()
            raise SqlTimeout(f"查询超过 {self.timeout}s 被中断") from None
        finally:
            try:
                con.close()
            except Exception:  # noqa: BLE001  # 极端竞态下 close 可能撞上未完全中止的查询
                pass


# 同步版本（脚本/测试用）
def execute_sync(sql: str, db_path: Path | str | None = None) -> QueryResult:
    safe = guard(sql)
    con = duckdb.connect(str(db_path or get_settings().db_abs_path), read_only=True)
    t0 = time.perf_counter()
    try:
        cur = con.execute(safe)
        cols = [d[0] for d in cur.description]
        rows = [[_jsonable(v) for v in r] for r in cur.fetchmany(_MAX_ROWS + 1)]
        truncated = len(rows) > _MAX_ROWS
        if truncated:
            rows = rows[:_MAX_ROWS]
        return QueryResult(
            columns=cols, rows=rows, row_count=len(rows), truncated=truncated,
            elapsed_ms=round((time.perf_counter() - t0) * 1000, 1), sql=safe,
        )
    finally:
        con.close()
