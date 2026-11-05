"""沙箱守卫测试：注入/多语句/危险词/掩码列必须在执行前被拦截。"""

import pytest

from app.data.sandbox import SqlRejected, execute_sync, guard


def test_guard_rejects_multistatement():
    with pytest.raises(SqlRejected):
        guard("SELECT 1; DROP TABLE orders")


def test_guard_rejects_non_select():
    with pytest.raises(SqlRejected):
        guard("UPDATE orders SET status='已完成'")


def test_guard_rejects_danger_keywords():
    for sql in ["PRAGMA database_list", "COPY orders TO 'x.csv'", "ATTACH 'evil.db' AS e"]:
        with pytest.raises(SqlRejected):
            guard(sql)


def test_guard_rejects_masked_columns():
    with pytest.raises(SqlRejected):
        guard("SELECT phone FROM customers")


def test_guard_appends_limit():
    assert "LIMIT" in guard("SELECT COUNT(*) FROM orders").upper()


def test_execute_returns_rows(db_path):
    res = execute_sync("SELECT COUNT(*) AS n FROM orders")
    assert res.rows[0][0] > 0


def test_readonly_is_physical(db_path):
    """物理只读：即使守卫被绕过，写语句在只读连接上也会失败。"""
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        with pytest.raises(Exception):
            con.execute("CREATE TABLE hack(x INT)")
    finally:
        con.close()


async def test_timeout_interrupts_heavy_query(db_path):
    """超时必须真正打断查询（interrupt），而不是放任线程继续烧 CPU。

    SUM(a.x + b.x) 交叉连接约 1e10 行，优化器无法化简，必然超过 0.3s。
    """
    from app.data.sandbox import Sandbox, SqlTimeout

    sb = Sandbox(db_path=db_path, timeout=0.3)
    with pytest.raises(SqlTimeout):
        await sb.execute(
            "SELECT SUM(a.order_id + b.order_id) FROM orders a CROSS JOIN orders b"
        )


async def test_timeout_does_not_poison_pool(db_path):
    """被打断的连接关闭后，后续查询必须照常可用（连接生命周期无泄漏）。"""
    from app.data.sandbox import Sandbox, SqlTimeout

    sb = Sandbox(db_path=db_path, timeout=0.3)
    with pytest.raises(SqlTimeout):
        await sb.execute(
            "SELECT SUM(a.order_id + b.order_id) FROM orders a CROSS JOIN orders b"
        )
    res = await sb.execute("SELECT COUNT(*) FROM orders")
    assert res.rows[0][0] > 0


# ---- ASK-1 红队回归（评审 2.0 P0）：表函数读文件/装扩展 ----

def test_guard_rejects_file_read_table_functions():
    """read_text/read_csv/glob 等表函数读文件必须被黑名单拦截。"""
    for sql in [
        "SELECT * FROM read_text('../.env')",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM glob('**/*.py')",
        "INSTALL httpfs",
        "LOAD httpfs",
    ]:
        with pytest.raises(SqlRejected):
            guard(sql)


def test_engine_blocks_external_access(db_path):
    """物理封禁纵深：即便未来黑名单漏词，enable_external_access=False 让引擎层直接失败。"""
    with pytest.raises(Exception):  # duckdb.IO Error / Invalid Input Error 家族
        execute_sync("SELECT * FROM read_text('../no_such_file.txt')", db_path=db_path)
