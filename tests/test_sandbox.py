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
        with pytest.raises(duckdb.Error):
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


# ---- ASKBI-8（评审 4.0）：取消路径也必须先 interrupt 再 close ----

async def test_cancel_path_interrupts_worker_before_close(monkeypatch):
    """外层取消（客户端断连 → CancelledError，BaseException）不经 except TimeoutError
    直达 finally——若 interrupt 只接在超时分支，to_thread 里仍在跑的查询会撞上被关闭
    的连接（:127 注释承诺的保护在取消路径缺席）。假连接记录事件时序：取消后必须
    出现 interrupt 且先于 close；CancelledError 本身必须继续向外传播。"""
    import asyncio
    import threading

    from app.data import sandbox as sandbox_mod
    from app.data.sandbox import Sandbox

    class _FakeCon:
        """模拟跨线程查询：execute 阻塞直到 interrupt()，事件顺序可断言。"""

        def __init__(self):
            self.events = []
            self._wake = threading.Event()

        def execute(self, sql):
            self.events.append("query_start")
            if not self._wake.wait(timeout=10):
                raise AssertionError("查询未被 interrupt 打断（等待超时）")
            self.events.append("query_end")

        def interrupt(self):
            self.events.append("interrupt")
            self._wake.set()

        def close(self):
            self.events.append("close")

    fake = _FakeCon()
    monkeypatch.setattr(sandbox_mod.duckdb, "connect", lambda *a, **k: fake)
    monkeypatch.setattr(Sandbox, "_run", lambda self, con, sql: con.execute(sql))

    sb = Sandbox(db_path="unused.db", timeout=30)
    task = asyncio.create_task(sb.execute("SELECT 1"))
    for _ in range(500):          # 等工作线程真正开始执行查询
        if "query_start" in fake.events:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("工作线程未启动")

    task.cancel()                 # 模拟客户端断连：外层任务被取消
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(500):          # 等工作线程被 interrupt 唤醒收尾
        if "query_end" in fake.events:
            break
        await asyncio.sleep(0.01)

    assert "interrupt" in fake.events, \
        f"取消路径未打断 to_thread 里仍在跑的查询: {fake.events}"
    assert fake.events.index("interrupt") < fake.events.index("close"), \
        f"必须先 interrupt 再 close（close 撞上未中止查询=连接竞写）: {fake.events}"


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
    """物理封禁纵深：绕过 guard 直连引擎（模拟黑名单未来漏词的最坏情形），
    enable_external_access=False 让文件读取在引擎层直接失败。

    注：本测试旧版用 read_text 载荷走 execute_sync——read_text 本就在黑名单，
    实际断言到的是 guard 层（SqlRejected ⊂ Exception 被宽断言吞掉），从未触达
    引擎墙；B017 收窄时暴露，现改为直连引擎的真墙测试（AB-HR3）。
    """
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True,
                         config={"enable_external_access": False})
    try:
        with pytest.raises(duckdb.Error):  # IO Error / Permission Error 家族
            con.execute("SELECT * FROM read_parquet('../no_such_file.txt')")
    finally:
        con.close()
