"""[1B5] AST 白名单守卫测试：sqlglot 解析级收口的合法面与拒绝面。

与 test_sandbox（历史修复单点回归）/ test_redteam（round 1）/ test_redteam2
（round 2）/ test_redteam3（round 3）的分工：本套件钉住 guard 升级为纵深四层
（解析层 → AST 白名单层 → 掩码列 → 关键词黑名单）后的两类不变量——

1. 合法面：DSL 编译器产物（四数据源全量真实生成，非手写臆造 SQL）必须全部
   穿过新 guard——AST 白名单对编译器产物零误伤是升级的兼容性红线；
2. 拒绝面：解析失败 / 多语句（文本层+解析层双口径）/ 写节点各族 / 禁用函数 /
   表外引用 / 文件字面量 FROM / 掩码列 / 性能上界，且消息带层标签可断言。

AST 白名单设计决策（实读 + 探针实证）：
- 根节点白名单 Select/Union/Except/Intersect（互不为父子类，逐一列举）；
- 全树遍历拒绝写/DDL/连接/配置/事务节点（根节点可为 Select 而体内夹带写语句）；
- 函数白名单 = 编译器产物全集（导入期自检键）+ duckdb 常用安全函数，
  Anonymous 一律拒绝；文件/网络面（read_*、sniff_*、parquet_*、*_scan、glob、
  st_read、write_* 系）与 query/query_table 不在名单；
- 表白名单 = 当前数据集 spec 声明表 ∪ 本查询 CTE 名；元数据面与文件字面量收紧。
"""

from __future__ import annotations

import time
from itertools import product as iproduct

import pytest

from app.data.sandbox import (
    _COMPILER_REQUIRED_KEYS,
    _FUNC_WHITELIST,
    SqlRejected,
    _parse_single,
    guard,
)

# ===========================================================================
# 0. 辅助：四数据源编译器产物生成器（真实生成，非手写臆造）
# ===========================================================================


def _compiled_products():
    """四数据源编译器产物生成器：全指标 × 维度 0/1/2 个 × 三种时间窗 × 三角色。

    SQL 一律由 compile_sql 真实生成（与 test_redteam 的手写 payload 相对），
    这是"AST 白名单对编译器产物零误伤"的兼容性证据面，也是任务要求的
    四数据源（ecommerce/coffee/saas/hotel）编译样例穿 guard 实测。
    db_path 随 spec 走：表白名单按"在哪个库上执行就用哪个 spec 的表集合"对齐。
    """
    from app.data.dsl import build_dsl, compile_sql
    from app.data.registry import get_spec, reset_specs

    reset_specs()
    for spec_name in ("ecommerce", "coffee", "saas", "hotel"):
        spec = get_spec(spec_name)
        dim_ids = list(spec.dimensions)
        for metric, n_dims, (tmode, targs), role in iproduct(
            spec.metrics,
            (0, 1, 2),
            (("all", {}), ("last_n_months", {"n": 3}),
             ("between", {"start": "2025-01", "end": "2025-06"})),
            ("boss", "south", "north"),
        ):
            dsl = build_dsl(
                {"metric": metric, "dimensions": dim_ids[:n_dims],
                 "time": dict(mode=tmode, **targs)},
                spec=spec,
            )
            sql, caliber = compile_sql(dsl, role=role, spec=spec)
            yield spec_name, sql, caliber, str(spec.db_abs_path)


# ===========================================================================
# 1. 合法面：编译器产物全过（零误伤红线）
# ===========================================================================


@pytest.mark.parametrize("dataset", ["ecommerce", "coffee", "saas", "hotel"])
def test_compiler_products_pass_guard_per_dataset(dataset):
    """按数据源分组验证：该数据集全部编译产物（指标×维度×时间×角色）穿新 guard，
    且强制 LIMIT 口径不变（编译器恒注入 LIMIT 100，守卫不重复追加）。"""
    products = [p for p in _compiled_products() if p[0] == dataset]
    assert products, f"{dataset} 应有编译产物"
    for _name, sql, _caliber, db in products:
        safe = guard(sql, db_path=db)
        assert safe.upper().endswith("LIMIT 100")


def test_compiler_products_full_sweep_zero_rejection():
    """四数据源全量编译产物零拒绝的总断言（数量下界防生成器退化成空跑）。"""
    n = 0
    for _name, sql, _caliber, db in _compiled_products():
        guard(sql, db_path=db)
        n += 1
    assert n >= 800


def test_default_dataset_products_pass_without_db_path():
    """兼容锚：guard(sql) 旧签名（不传 db_path）对默认数据集产物行为不变。"""
    from app.data.registry import get_spec

    assert get_spec().name == "ecommerce"  # 缺省激活 spec = 默认数据集
    for name, sql, _caliber, _db in _compiled_products():
        if name == "ecommerce":
            guard(sql)  # 缺省 db_path → settings.dataset 激活 spec（ecommerce）


def test_function_whitelist_covers_compiler_required_keys():
    """自检键（编译器产物实测函数键全集）必须在白名单内——sqlglot 升级漂移的
    导入期 RuntimeError 之外的测试侧双保险。"""
    assert _COMPILER_REQUIRED_KEYS <= _FUNC_WHITELIST


# ===========================================================================
# 2. 合法面：AST 白名单的口径边界
# ===========================================================================


def test_union_and_cte_pass():
    """UNION 与 CTE 是纯查询树：根节点白名单放行，CTE 名并入表集合。"""
    guard("SELECT region FROM orders UNION SELECT region FROM customers")
    guard("WITH t AS (SELECT order_id FROM orders) SELECT COUNT(*) FROM t")


def test_table_name_case_folding_pass():
    """duckdb 未引号标识符大小写不敏感：ORDERS 折叠后命中 spec 表集合。"""
    guard("SELECT * FROM ORDERS LIMIT 3")


def test_db_qualified_table_pass():
    """库名/schema 前缀不参与判定：main.orders 最内名 orders 命中白名单。"""
    guard("SELECT * FROM main.orders LIMIT 3")


def test_scalar_and_window_functions_pass():
    """白名单函数（标量/字符串/日期/窗口）组合查询放行。"""
    guard("SELECT length(upper(substr(region, 1, 2))) FROM orders")
    guard("SELECT row_number() OVER (ORDER BY order_id) FROM orders")
    guard("SELECT strftime('%Y-%m', o.order_date) FROM orders o")


def test_generate_series_allowed_by_ast_bounded_by_resource_layer():
    """generate_series 口径（文档化决策）：纯内存数据生成器、无 IO，AST 层放行；
    大资源消耗由资源层兜底——强制 LIMIT + 执行超时 interrupt()（见
    test_sandbox::test_timeout_interrupts_heavy_query）。本测试只钉 AST 口径，
    不实际执行大参数（资源层行为已有专项测试）。"""
    safe = guard("SELECT * FROM generate_series(1, 100000000)")
    assert "LIMIT" in safe.upper()


# ===========================================================================
# 3. 拒绝面：解析层 / 语句层
# ===========================================================================


def test_parse_error_rejected_with_truncated_original():
    """解析层口径：ParseError → SqlRejected，消息含失败原因与原文截断。"""
    with pytest.raises(SqlRejected, match="解析层") as info:
        guard("SELECT FROM WHERE")
    assert "原文" in str(info.value)


def test_multistatement_rejected_at_text_layer():
    """文本层多语句口径：字符串字面量内的分号同样拒绝（宁可错杀，
    合法值域由枚举校验保证不含分号——test_redteam 值注入链的口径延续）。"""
    with pytest.raises(SqlRejected, match="语句层"):
        guard("SELECT 1; DROP TABLE orders")
    with pytest.raises(SqlRejected, match="语句层"):
        guard("SELECT 'x; DROP TABLE orders' FROM orders")


def test_multistatement_ast_layer_backstop():
    """解析层多语句兜底：文本 ';' 检查已拦住常规堆叠，解析级"多表达式拒绝"
    作为独立防线直接钉守卫内部（防未来文本检查被放松时多表达式直达引擎）。"""
    with pytest.raises(SqlRejected, match="解析层: 解析出 2 条语句"):
        _parse_single("SELECT 1; DROP TABLE orders")


def test_non_query_prefix_rejected_at_statement_layer():
    """语句层前缀口径保留：非 SELECT/WITH 开头在解析前即拒（旧口径不变）。
    PRAGMA/SET/CALL/LOAD/PREPARE/DROP 各族在此层统一收口。"""
    for sql in ("PRAGMA database_list", "SET x=1", "CALL dbgen()", "LOAD httpfs",
                "PREPARE p AS SELECT 1", "DROP TABLE orders", "EXPORT DATABASE 'x'"):
        with pytest.raises(SqlRejected, match="语句层"):
            guard(sql)


# ===========================================================================
# 4. 拒绝面：AST 白名单层——写节点各族
# ===========================================================================


@pytest.mark.parametrize("sql,label", [
    ("WITH t AS (INSERT INTO orders VALUES (1) RETURNING 1) SELECT * FROM t", "INSERT"),
    ("WITH t AS (DELETE FROM orders RETURNING 1) SELECT * FROM t", "DELETE"),
    ("WITH t AS (UPDATE orders SET status='x' RETURNING 1) SELECT * FROM t", "UPDATE"),
    ("WITH t AS (CREATE TABLE h(x INT)) SELECT * FROM t", "CREATE"),
    ("WITH t AS (SELECT 1) INSERT INTO orders VALUES (1)", "INSERT"),  # 根节点 Insert
])
def test_write_nodes_rejected_anywhere_in_tree(sql, label):
    """写节点各族（CTE 内夹带 / 根节点）：全树遍历拒绝，消息带语句类型标签。
    攻击面：sqlglot 对 CTE 夹带写的解析产物根节点是 Select——仅查根节点必漏。"""
    with pytest.raises(SqlRejected, match=f"AST 白名单.*{label}"):
        guard(sql)


def test_merge_and_transaction_rejected():
    """合并写入与事务控制：前缀层（非 SELECT/WITH 开头）与 AST 层口径一致。"""
    with pytest.raises(SqlRejected, match="语句层"):
        guard("MERGE INTO orders USING customers ON true WHEN MATCHED THEN UPDATE SET x=1")
    with pytest.raises(SqlRejected, match="语句层"):
        guard("BEGIN TRANSACTION")


def test_attach_copy_pragma_rejected():
    """连接/导入导出/配置面（旧黑名单关键词的 AST 复验——两层口径一致）。"""
    for sql in ("ATTACH 'evil.db' AS e", "COPY orders TO 'x.csv'", "PRAGMA version"):
        with pytest.raises(SqlRejected, match="(语句层|AST 白名单)"):
            guard(sql)


# ===========================================================================
# 5. 拒绝面：AST 白名单层——函数 / 表
# ===========================================================================


@pytest.mark.parametrize("fn", [
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto",
    "read_ndjson", "read_duckdb", "read_blob", "read_text", "read_arrow",
    "sniff_csv", "parquet_scan", "parquet_metadata", "parquet_schema",
    "iceberg_scan", "delta_scan", "postgres_scan", "sqlite_scan", "st_read",
    "glob", "query_table", "query", "duckdb_tables", "pragma_version",
    "load_extension", "getenv",
])
def test_disabled_functions_rejected_with_name(fn):
    """禁用函数族（文件/网络/元数据/环境面）逐一拒绝，消息含函数名。
    read_csv/read_parquet 在 sqlglot 有专类（非 Anonymous）同样落选——
    白名单按 sql_name 判定，书写变体（大小写/双引号）一并归一拒绝。"""
    with pytest.raises(SqlRejected, match=f"AST 白名单: 禁止函数 {fn.lower()}"):
        guard(f"SELECT * FROM {fn}('evil')")


def test_function_obfuscation_variants_normalized():
    """函数名混淆三变体（大小写 / 双引号 / 别名掩护）都被 sqlglot 归一后拒绝。"""
    for sql in ("SELECT * FROM READ_CSV('x')", 'SELECT * FROM "read_csv"(1)',
                "SELECT read_text('../../.env') AS business_metric FROM orders"):
        with pytest.raises(SqlRejected, match="AST 白名单: 禁止函数"):
            guard(sql)


@pytest.mark.parametrize("literal", [
    "'file.csv'",                       # duckdb 直读 csv
    "'data/askbi.duckdb'",              # 项目自身库文件（最敏感的库面）
    "'../.env'",                        # 环境配置文件
    "'x.parquet'",                      # parquet 文件
])
def test_file_literal_from_rejected_by_table_whitelist(literal):
    """duckdb 允许 FROM '文件路径' 字面量直读文件——sqlglot 解析为带扩展名的
    Identifier 表引用，无 catalog/schema，不在 spec 表集合 → 表白名单拒绝
    （消息含文件名，便于审计定位载荷）。"""
    with pytest.raises(SqlRejected, match="AST 白名单: 禁止表引用"):
        guard(f"SELECT * FROM {literal}")


def test_table_outside_registry_rejected():
    """表外引用：不在当前 spec 表集合的一律拒绝（fail-closed，非黑名单枚举）。"""
    with pytest.raises(SqlRejected, match="AST 白名单: 禁止表引用 secret_table"):
        guard("SELECT * FROM secret_table")


def test_cte_name_is_allowed_as_table_reference():
    """CTE 名并入表集合的边界：CTE 是查询内派生表，引用它不引入外部读取面。"""
    guard("WITH monthly AS (SELECT strftime('%Y-%m', order_date) AS m FROM orders) "
          "SELECT m, COUNT(*) FROM monthly GROUP BY m")


def test_metadata_tables_are_tightened_not_allowed():
    """元数据面口径：information_schema/pg_catalog 现有测试与 drill 路径零使用
    → AST 层收紧只放 spec 表（information_schema.tables 最内名 tables 不在集合）。"""
    with pytest.raises(SqlRejected, match="AST 白名单: 禁止表引用 tables"):
        guard("SELECT table_name FROM information_schema.tables")
    with pytest.raises(SqlRejected, match="AST 白名单: 禁止表引用 pg_tables"):
        guard("SELECT * FROM pg_catalog.pg_tables")


# ===========================================================================
# 6. 拒绝面：掩码列 / 黑名单（旧层语义保留）
# ===========================================================================


@pytest.mark.parametrize("sql", [
    "SELECT phone FROM customers",
    'SELECT c."phone" FROM customers c',      # 引号变体
    "SELECT region FROM orders UNION ALL SELECT email FROM customers",  # UNION 包装
])
def test_masked_columns_still_rejected(sql):
    """掩码列守卫（c 层）语义与旧版一致：文本层扫描，引号/UNION 变体同拦。"""
    with pytest.raises(SqlRejected, match="掩码列守卫"):
        guard(sql)


def test_keyword_blacklist_still_last_regex_layer():
    """d 层口径：AST 层把注释/字符串视为透明（非结构性攻击面），文本层的
    关键词黑名单继续"宁可错杀"——注释夹 DROP 仍被正则层拒（两层口径互补）。"""
    with pytest.raises(SqlRejected, match="关键词黑名单"):
        guard("SELECT 1 /* DROP TABLE orders */ FROM orders")
    with pytest.raises(SqlRejected, match="关键词黑名单"):
        guard("SELECT * FROM orders WHERE region = 'DROP TABLE x'")


def test_limit_forced_still_appended():
    """资源层：无 LIMIT 查询强制追加（旧口径不变）；已有 LIMIT 不重复追加。"""
    out = guard("SELECT COUNT(*) FROM orders")
    assert out.upper().endswith("LIMIT 1000")
    out2 = guard("SELECT order_id FROM orders LIMIT 5")
    assert out2.upper().endswith("LIMIT 5") and out2.count("LIMIT") == 1


# ===========================================================================
# 7. 性能守门
# ===========================================================================


def test_guard_latency_under_loose_bound():
    """性能上界（宽松 50ms）：guard 每次执行前跑，sqlglot 解析 + AST 遍历的
    开销实测典型编译产物中位 ~2ms（sqlglot 30.33 / duckdb 方言 / 本指标字典
    最复杂产物 gross_margin_rate），50ms 上界留足 20 倍余量给 CI 噪声。"""
    from app.data.dsl import build_dsl, compile_sql

    dsl = build_dsl(
        {"metric": "gross_margin_rate", "dimensions": ["category", "month", "region"],
         "filters": [{"field": "channel", "op": "=", "value": "直播"}],
         "time": {"mode": "last_n_months", "n": 12}},
    )
    sql, _ = compile_sql(dsl, role="boss")
    guard(sql)  # 预热（首次含 sqlglot 常量初始化，不计入上界）
    worst = 0.0
    for _ in range(20):
        t0 = time.perf_counter()
        guard(sql)
        worst = max(worst, (time.perf_counter() - t0) * 1000)
    assert worst < 50.0, f"guard 单次耗时 {worst:.1f}ms 超出宽松上界 50ms"
