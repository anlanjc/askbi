"""红队 round 3（[1B5]）——针对 SQL 守卫升级（关键词黑名单 → sqlglot AST 白名单）的对抗性证据包。

与 test_redteam（round 1：注入/绕过/提示注入经典面）、test_redteam2（round 2：
singleflight/审计/resume/计费面）的分工：本套件攻击**守卫本体升级**，全部用例
断言被新 guard 拒绝且消息指向正确的防御层（解析层 / AST 白名单层 / 掩码列 /
关键词黑名单 / 语句层），每条注明攻击假设。

攻击面与结论总览（payload → 防御层 → 机制归因）：
1. RT3-CTE      写语句藏进 CTE：sqlglot 解析产物根节点仍是 Select——仅查根节点
                必漏，AST 层全树遍历兜住（1B5 设计决策 (1) 的红队实证）；
2. RT3-UNION    UNION 包装读掩码列 / 调文件函数：查询树合法但载荷非法，
                分别由掩码列层与 AST 函数层收口；
3. RT3-OBFUSC   函数名大小写 / 双引号 / 别名混淆：sqlglot 解析归一后按
                sql_name 判定，三变体一网打尽；
4. RT3-NESTED   嵌套子查询内的表函数：find_all 全树遍历，藏多深都一样；
5. RT3-COMMENT  注释夹关键词：AST 层视注释为透明（非结构性攻击面），文本层
                关键词黑名单"宁可错杀"口径保留——两层互补（1B5 设计决策保留理由）；
6. RT3-SEMI     字符串内分号 vs 外部分号：文本层连字符串内分号一并拒
                （合法值域由枚举校验保证不含分号，宁枉勿纵口径延续）；
7. RT3-FILELIT  FROM '文件路径' 字面量：duckdb 允许直读文件，sqlglot 解析为
                带扩展名的 Identifier → 表白名单拒绝；
8. RT3-READFAM  read_* 同族变体（round1-3 三代黑名单补词的整个家族）：AST
                函数白名单 fail-closed，不再依赖名单追赶；
9. RT3-TABLEFN  query_table/query 表函数封装：ASK-15 时代"放行但受全文扫描
                约束"的混合口径在本代明确收紧为拒绝（1B5 设计决策 (4)）；
10. RT3-META    duckdb 元数据探查（duckdb_* 函数 / information_schema /
                pg_catalog）：实读现有测试与 drill 路径零使用 → 收紧拒绝；
11. RT3-PREPARE PREPARE/EXECUTE 预编译旁路：语句层前缀 + AST 层 Command/Execute；
12. RT3-ROOT    WITH 前缀的根节点写语句：AST 根节点白名单收口；
13. RT3-GENSER  generate_series 资源消耗：AST 层口径=放行（纯内存生成器无 IO），
                资源层（强制 LIMIT + 超时 interrupt）兜底——口径在用例注释声明；
14. RT3-FUTURE  未来同族新词（round1-3 演进规律推演）：未建模函数（Anonymous）
                fail-closed，不依赖名单更新——本代升级的核心主张。
"""

from __future__ import annotations

import pytest

from app.data.sandbox import SqlRejected, guard

# ===========================================================================
# 1. RT3-CTE：写语句藏进 CTE（根节点仍是 Select）
# ===========================================================================


@pytest.mark.parametrize("sql,label", [
    ("WITH t AS (INSERT INTO orders VALUES (1) RETURNING 1) SELECT * FROM t", "INSERT"),
    ("WITH t AS (UPDATE orders SET status='已完成' RETURNING 1) SELECT * FROM t", "UPDATE"),
    ("WITH t AS (DELETE FROM orders RETURNING 1) SELECT * FROM t", "DELETE"),
    ("WITH a AS (SELECT 1), b AS (INSERT INTO orders VALUES (1) RETURNING 2) SELECT * FROM a, b", "INSERT"),
])
def test_rt3_write_smuggled_into_cte_rejected(sql, label):
    """攻击假设：SELECT/WITH 前缀检查只看语句开头，写语句塞进 CTE 体内即可
    通过前缀检查与根节点白名单。防御：AST 层全树遍历拒绝写节点——消息指向
    语句类型标签（INSERT/UPDATE/DELETE），藏多深都一样。"""
    with pytest.raises(SqlRejected, match=f"AST 白名单: 禁止语句类型 {label}"):
        guard(sql)


def test_rt3_with_prefixed_root_write_rejected():
    """攻击假设：WITH 开头的语句天然通过前缀检查——WITH t AS (...) INSERT 的
    根节点是 Insert（非 Select），AST 根节点白名单收口。"""
    with pytest.raises(SqlRejected, match="AST 白名单: 根节点必须为查询语句"):
        guard("WITH t AS (SELECT 1) INSERT INTO orders VALUES (1)")


# ===========================================================================
# 2. RT3-UNION：查询树合法、载荷非法的包装攻击
# ===========================================================================


def test_rt3_union_wraps_masked_column_exfiltration():
    """攻击假设：UNION 本身是合法查询树，把掩码列藏进第二支绕过列白名单印象。
    防御：掩码列层文本扫描，第二支的 phone/email 原文必现。"""
    with pytest.raises(SqlRejected, match="掩码列守卫: 引用了受限列: phone"):
        guard("SELECT region FROM orders UNION ALL SELECT phone FROM customers")


def test_rt3_union_wraps_file_read_function():
    """攻击假设：合法查询 + UNION 文件读取支——希望白名单只看第一支。
    防御：AST 函数层全树遍历，read_csv 落选白名单。"""
    with pytest.raises(SqlRejected, match="AST 白名单: 禁止函数 read_csv"):
        guard("SELECT 1 UNION ALL SELECT * FROM read_csv('evil.csv')")


def test_rt3_union_wraps_outside_table():
    """攻击假设：UNION 包装表外引用（别的数据集的表）。防御：AST 表白名单
    fail-closed——非当前 spec 表集合一律拒绝（跨数据源隔离的守卫级证据）。"""
    with pytest.raises(SqlRejected, match="AST 白名单: 禁止表引用 ticket_lines"):
        guard("SELECT region FROM orders UNION ALL SELECT region FROM ticket_lines")


# ===========================================================================
# 3. RT3-OBFUSC：函数名混淆三变体
# ===========================================================================


@pytest.mark.parametrize("sql,fn", [
    ("SELECT * FROM READ_CSV('x.csv')", "read_csv"),            # 大小写
    ("SELECT * FROM Read_Csv_Auto('x.csv')", "read_csv_auto"),  # 驼峰
    ('SELECT * FROM "read_parquet"(\'x\')', "read_parquet"),    # 双引号
    ("SELECT read_text('../../.env') AS business_metric FROM orders", "read_text"),  # 别名掩护
])
def test_rt3_function_name_obfuscation_normalized_then_rejected(sql, fn):
    """攻击假设：黑名单正则按原文匹配，大小写/引号/别名可能漏词（AB-HR1 时代
    的攻击面）。防御：sqlglot 解析把书写变体归一为同一产物类，白名单按
    sql_name 判定一次覆盖全部变体——消息中给出归一后的函数名。"""
    with pytest.raises(SqlRejected, match=f"AST 白名单: 禁止函数 {fn}"):
        guard(sql)


# ===========================================================================
# 4. RT3-NESTED：嵌套子查询内的表函数
# ===========================================================================


@pytest.mark.parametrize("sql", [
    "SELECT * FROM (SELECT * FROM read_parquet('e.parquet')) sub",
    "SELECT * FROM orders WHERE region IN (SELECT region FROM read_csv('e.csv'))",
    "SELECT (SELECT count(*) FROM read_json('e.json')) AS c FROM orders",
])
def test_rt3_table_function_in_nested_subquery_rejected(sql):
    """攻击假设：表函数藏在子查询/IN 列表/标量子查询里，希望守卫只扫顶层。
    防御：find_all(exp.Func) 全树遍历，嵌套多深均在函数白名单落选。"""
    with pytest.raises(SqlRejected, match="AST 白名单: 禁止函数"):
        guard(sql)


# ===========================================================================
# 5. RT3-COMMENT：注释混淆
# ===========================================================================


def test_rt3_keyword_smuggled_in_comment_rejected_by_regex_layer():
    """攻击假设：AST 解析视注释为透明，夹在注释里的关键词会"通过结构校验"。
    防御：两层口径互补——AST 层只裁决结构（注释确实不是结构面），文本层
    关键词黑名单保留"宁可错杀"：注释里出现 DROP 仍被正则层拒（消息指向
    关键词黑名单层，证明旧层仍在场）。"""
    with pytest.raises(SqlRejected, match="关键词黑名单: 包含被禁止的关键词: DROP"):
        guard("SELECT 1 /* DROP TABLE orders */ FROM orders")


def test_rt3_comment_split_function_call_still_rejected_by_ast():
    """攻击假设：/**/ 切词让正则 \b 边界失配（round1 曾实证切词绕不过 \b，
    本代从 AST 角度复验：注释在解析期被剥离，函数调用完整还原）。防御：
    AST 函数白名单按解析产物判定——read_text 原形毕露。"""
    with pytest.raises(SqlRejected, match="AST 白名单: 禁止函数 read_text"):
        guard("SELECT/**/read_text/**/('../../.env')")


# ===========================================================================
# 6. RT3-SEMI：字符串内分号 vs 外部分号
# ===========================================================================


@pytest.mark.parametrize("sql", [
    "SELECT 'a; DROP TABLE orders' FROM orders",      # 分号在字符串字面量内
    "SELECT 1; SELECT 2",                             # 外部真堆叠
    "WITH t AS (SELECT 1) SELECT * FROM t; SELECT read_text('x')",  # 堆叠+禁函数
])
def test_rt3_semicolons_rejected_regardless_of_position(sql):
    """攻击假设：分号藏进字符串字面量可骗过"多语句"文本判断，随后靠解析器
    差异逃逸。防御口径（文档化）：文本层连字符串内分号一并拒——合法值域由
    枚举校验保证不含分号（宁枉勿纵，与 round1 值注入链口径一致）。"""
    with pytest.raises(SqlRejected, match="语句层: 检测到多语句"):
        guard(sql)


# ===========================================================================
# 7. RT3-FILELIT：FROM '文件路径' 字面量
# ===========================================================================


@pytest.mark.parametrize("literal", [
    "'file.csv'",
    "'x.parquet'",
    "'../.env'",
    "'data/askbi.duckdb'",   # 项目自身库文件——最敏感的库面
    "'/etc/passwd'",
])
def test_rt3_file_literal_from_rejected(literal):
    """攻击假设：duckdb 允许 FROM '路径' 直读文件/挂库，且这种 Table 无
    catalog/schema、名字带扩展名，传统表名枚举容易漏。防御：表白名单
    fail-closed——不在 spec 表集合（含扩展名/路径分隔符）一律拒绝，
    消息回显文件名便于审计定位载荷。"""
    with pytest.raises(SqlRejected, match="AST 白名单: 禁止表引用"):
        guard(f"SELECT * FROM {literal}")


# ===========================================================================
# 8. RT3-READFAM：read_* 同族变体全家桶（round1-3 三代黑名单补词的整个家族）
# ===========================================================================


@pytest.mark.parametrize("fn", [
    # round1（AB-HR1）补词：parquet_scan / read_parquet 家族
    "parquet_scan", "read_parquet",
    # round2（ASK-15 通配化）补词：read_json_auto / read_ndjson / sniff_csv
    "read_json_auto", "read_ndjson", "sniff_csv",
    # round3 探针扩展：duckdb 2.x 表函数家族其余成员（read_duckdb 读库文件等）
    "read_json", "read_jsonl", "read_duckdb", "read_blob", "read_arrow",
    "read_text", "read_single_parquet_file", "parquet_metadata", "parquet_schema",
    "parquet_kv_metadata", "iceberg_scan", "delta_scan", "postgres_scan",
    "mysql_scan", "sqlite_scan", "st_read", "st_readraster", "glob",
])
def test_rt3_read_family_all_rejected_at_ast_layer(fn):
    """攻击假设：黑名单三代补词始终在追赶同族新词。防御（本代核心主张）：
    AST 函数白名单 fail-closed——整个家族一次性收口，不依赖名单更新。
    逐成员断言消息指向 AST 函数层（含归一后函数名）。"""
    with pytest.raises(SqlRejected, match=f"AST 白名单: 禁止函数 {fn.lower()}"):
        guard(f"SELECT * FROM {fn}('evil')")


# ===========================================================================
# 9. RT3-TABLEFN：query_table/query 表函数封装（ASK-15 混合口径的收紧）
# ===========================================================================


@pytest.mark.parametrize("sql,fn", [
    ("SELECT * FROM query_table('orders')", "query_table"),
    ("SELECT * FROM query('SELECT phone FROM customers')", "query"),
])
def test_rt3_query_table_wrapper_rejected(sql, fn):
    """攻击假设：query_table/query 接收字符串参数并在引擎内展开为任意查询——
    ASK-15 时代口径为"放行但受黑名单全文扫描约束"，参数里的关键词确实会被
    文本层拦住；但封装函数可携带任意字符串/嵌套 SQL，"放行+扫描"是混合口径。
    防御（1B5 设计决策 (4)）：AST 层明确拒绝——Anonymous 函数落选白名单。
    口径变更注记：app/data/sandbox.py 文件头 1B5 段与 ASK-15 注释块同步更新。"""
    with pytest.raises(SqlRejected, match=f"AST 白名单: 禁止函数 {fn}"):
        guard(sql)


# ===========================================================================
# 10. RT3-META：duckdb 元数据探查
# ===========================================================================


@pytest.mark.parametrize("sql,layer_msg", [
    ("SELECT * FROM duckdb_tables()", "禁止函数 duckdb_tables"),
    ("SELECT * FROM duckdb_columns()", "禁止函数 duckdb_columns"),
    ("SELECT table_name FROM information_schema.tables", "禁止表引用 tables"),
    ("SELECT * FROM pg_catalog.pg_tables", "禁止表引用 pg_tables"),
    ("SELECT * FROM pragma_table_info('orders')", "禁止函数 pragma_table_info"),
])
def test_rt3_metadata_probing_rejected(sql, layer_msg):
    """攻击假设：元数据表/函数可枚举全库结构（表名/列名/类型），为下一步
    载荷做侦察。防御口径（文档化决策）：实读现有测试与 drill 路径对元数据面
    零使用 → 收紧只放 spec 表，duckdb_* 函数不在白名单——侦察面同样关闭。"""
    with pytest.raises(SqlRejected, match=layer_msg):
        guard(sql)


# ===========================================================================
# 11. RT3-PREPARE / RT3-ROOT：预编译旁路与根节点写语句
# ===========================================================================


@pytest.mark.parametrize("sql", [
    "PREPARE p AS SELECT 1",
    "PREPARE p AS INSERT INTO orders VALUES (1)",
    "EXECUTE p",
])
def test_rt3_prepare_execute_bypass_rejected(sql):
    """攻击假设：把载荷预编译为 p 再 EXECUTE，正文不直接出现在查询里。
    防御：语句层前缀（非 SELECT/WITH 开头）先拦；假设前缀被放松，
    sqlglot 将方言未建模语法退化为 Command、EXECUTE 为 Execute——
    AST 层同样拒绝（双层口径一致）。"""
    with pytest.raises(SqlRejected, match="(语句层|AST 白名单)"):
        guard(sql)


# ===========================================================================
# 12. RT3-PRAGMA / RT3-SET：配置面变体
# ===========================================================================


@pytest.mark.parametrize("sql", [
    "PRAGMA enable_external_access=true",
    "PRAGMA version",
    "SET enable_external_access=true",
    "SET memory_limit='10GB'",
    "SET GLOBAL enable_external_access=true",
])
def test_rt3_pragma_set_variants_rejected(sql):
    """攻击假设：改配置（开外部访问/改资源上限）为文件读取铺路——PRAGMA/SET
    不以 SELECT/WITH 开头，语句层前缀拦截；配置节点在 AST 层同样入禁用清单
    （Pragma/Set），两层口径一致。"""
    with pytest.raises(SqlRejected, match="(语句层|AST 白名单)"):
        guard(sql)


# ===========================================================================
# 13. RT3-GENSER：generate_series 大资源消耗（口径声明）
# ===========================================================================


def test_rt3_generate_series_ast_policy_documented():
    """攻击假设：generate_series(1, 1e8) 生成大结果集消耗资源。
    AST 层口径（文档化决策）：放行——纯内存数据生成器、无 IO，是合法分析
    原语（月份序列等）；大资源消耗由资源层兜底：强制 LIMIT（守卫注入）+
    执行超时 interrupt()（Sandbox.timeout，test_sandbox 已有专项测试实证
    interrupt 真打断）。本用例钉 AST 口径本身：不抛 SqlRejected 且 LIMIT 已注入。"""
    safe = guard("SELECT * FROM generate_series(1, 100000000)")
    assert "LIMIT" in safe.upper()


# ===========================================================================
# 14. RT3-FUTURE：未来同族新词 fail-closed（本代升级的核心主张）
# ===========================================================================


@pytest.mark.parametrize("fn", [
    "read_xml",          # 假想的 read_* 族新成员（round1-3 演进规律的下一站）
    "excel_scan",        # 假想的 *_scan 族新成员
    "some_future_reader",
])
def test_rt3_future_family_words_fail_closed(fn):
    """攻击假设：按 round1（parquet_scan）→ round2（read_json_auto 等 19 个）
    → round3 的演进规律，duckdb 生态会持续冒出同族新函数名，黑名单永远慢一拍。
    防御（1B5 主张的结构性收口）：sqlglot 未建模的函数解析为 Anonymous，
    fail-closed 一律拒绝——新词不需要名单更新即被收口（探针实证：
    未知名 → exp.Anonymous → 白名单落选）。"""
    with pytest.raises(SqlRejected, match=f"AST 白名单: 禁止函数 {fn}"):
        guard(f"SELECT * FROM {fn}('evil')")


# ===========================================================================
# 15. 回归锚：守卫升级后 round1/round2 的载荷面不放松
# ===========================================================================


@pytest.mark.parametrize("sql", [
    "SELECT 1; DROP TABLE orders",                     # round1 堆叠
    "UPDATE orders SET status='已完成'",               # round1 写语句
    "SELECT phone FROM customers",                     # round1 掩码列
    "WITH t AS (SELECT 1) INSERT INTO orders VALUES (1)",  # round1 CTE 写
    "SELECT/**/read_csv/**/('x')",                     # round1 注释切词
    "SELECT read_text('../../.env')",                  # round1 文件读取
])
def test_rt3_round12_payloads_still_rejected(sql):
    """回归锚：升级不放松历史防御面——round1 代表载荷全部仍被拒且分层清晰
    （堆叠→语句层、写语句→语句层、掩码列→掩码列层、CTE 写→AST 层、
    注释切词→AST 层、文件读取→AST 层）。"""
    with pytest.raises(SqlRejected):
        guard(sql)


def test_rt3_masked_column_via_subquery_alias_still_rejected():
    """攻击假设：掩码列藏在子查询里换别名输出，希望文本扫描失效。防御：
    文本层扫描不解析语义——phone 原文出现在子查询 SELECT 列表即拒
    （别名只是输出标签，改不了引用事实）。"""
    with pytest.raises(SqlRejected, match="掩码列守卫"):
        guard("SELECT * FROM (SELECT phone FROM customers) t")
