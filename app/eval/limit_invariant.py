"""LIMIT 剥离口径的构建期不变量（AB-29，评审 5.0）：金标截断边界 strict 程序化断言。

背景：三个构建脚本（build_dataset_v3_300 / build_multiturn_deep / build_dataset_d）
的金标一律 compile_sql 生成、尾部恒带 LIMIT；自检用"LIMIT 剥离口径"——剥掉尾行
LIMIT 后比对无界全结果集。该口径下评测条目必须保证：无界行数 ≤ LIMIT，**或**
截断边界（第 lim 行与 lim+1 行的排序值）无并列——否则 LIMIT 截断集合在 DuckDB
并行聚合的并列组顺序漂移下执行间非确定，判分可能随执行翻转。

此前的口径承诺写在 docstring 里（"评测条目本身已保证"），是人工纪律无程序化守门
（复核实证：7 条 top-N 条目 v2-033/036/039/124、v3-212/214/215 无界行数 > LIMIT，
当前边界 strict 无翻转、但不挡未来新增条目引入并列边界）。本模块把不变量落成
可执行断言，构建脚本对每条 query 金标 fail-fast（并列即拒绝该条目入集，报条目 id）。

断言口径 = 边界 strict 即可入库：允许无界行数 > LIMIT，只要截断边界无并列——
这正是 LIMIT 剥离口径的语义（截断集合确定，比对与 top-k 等价判分都不受
行数差影响），不收紧以免破坏现有冻结集。
"""

from __future__ import annotations

from ..data.sandbox import execute_sync


def strip_limit(sql: str) -> tuple[str, str]:
    """剥掉编译器恒定的尾行 LIMIT，返回 (主体 SQL, LIMIT 行)——与三个构建脚本同口径。"""
    body, _, limit_line = sql.rpartition("\nLIMIT")
    return body, ("LIMIT" + limit_line)


def limit_boundary_strict(sql: str, *, db_path=None) -> tuple[bool, str]:
    """金标 SQL 的截断边界不变量：剥 LIMIT 执行无界主体，边界 strict 才算通过。

    返回 (ok, detail)。ok=True：
      - 无界行数 ≤ LIMIT（无截断，天然确定）；
      - 或第 lim 行与 lim+1 行的排序值（末列 = ORDER BY value）不相等
        （截断集合确定——允许无界行数 > LIMIT，这正是 LIMIT 剥离口径的语义）。
    ok=False：边界并列，LIMIT 截断集合执行间非确定，该条目不得入集。

    db_path：金标执行库（多数据源脚本传对应 spec 库路径；缺省默认数据集）。
    剥出的主体经 execute_sync 执行（guard 追加 LIMIT 1000 资源上界），边界
    行（lim ≤ 200 < 1000）恒在取回窗口内，截断取回不影响边界判定。
    """
    body, limit_line = strip_limit(sql)
    lim = int(limit_line.rsplit(" ", 1)[-1])
    rows = execute_sync(body, db_path=db_path).rows
    if len(rows) <= lim:
        return True, f"无界行数 {len(rows)} ≤ LIMIT {lim}（无截断）"
    if rows[lim - 1][-1] != rows[lim][-1]:
        return True, f"无界行数 {len(rows)} > LIMIT {lim}，截断边界 strict"
    return False, (
        f"无界行数 {len(rows)} > LIMIT {lim}，截断边界并列 "
        f"（第 {lim} 行值 {rows[lim - 1][-1]!r} = 第 {lim + 1} 行值）"
        "——LIMIT 截断集合执行间非确定，拒绝入集"
    )
