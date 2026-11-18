"""SQL 执行沙箱：只读连接 + 语句守卫（纵深四层） + 超时中断 + 结果规模控制。

安全层次（DESIGN.md 的"失败降级矩阵"之一环）：
1. 连接层：duckdb read_only=True，物理上不可写；
2. 语句层（[1B5] 起纵深四层，按序）：
   a. 解析层：sqlglot duckdb 方言解析（ParseError / 多表达式 → 拒绝）；
   b. AST 白名单层：根节点必须是纯查询树、全树无写/DDL/连接/配置/事务语句
      节点、全部函数名 ∈ 安全函数白名单、全部表引用 ∈ 当前数据集 spec 表集合；
   c. 掩码列守卫：MASKED_FIELDS 不允许出现在任何生成的 SQL 中；
   d. 关键词黑名单（正则）：防 sqlglot 方言解析盲区的最后一道正则层；
3. 资源层：强制 LIMIT、执行超时（interrupt() 打断）。

红队演进实录（防御随代际攻击面升级，体例延续 AB-HR1 → ASK-15）：
- AB-HR1（2026-11-09）：parquet_scan 文件读取家族不在黑名单（探查实锤穿过
  guard，仅剩引擎 Permission Error 兜底）→ 补齐 *_scan 家族恢复双层纵深；
- ASK-15（评审 3.0，2026-11-11）：*_scan 逐词枚举仍有同族漏网——黑名单通配化
  （read_\\w+|sniff_\\w+|parquet_\\w+|\\w*_scan；duckdb 2.0.0 实测 105 个表函数
  全量过 guard：漏网 19 个全在此族）；
- 1B5（2026-11-19）：黑名单的攻击面永远是"漏词"——duckdb 表函数/扩展生态持续
  膨胀，逐词与通配枚举都在追赶同族新词。本代把守卫从关键词黑名单升级为
  sqlglot 解析级 AST 白名单：攻击面从"黑名单漏词"收敛为"解析器结构校验"——
  未经白名单放行的函数/表/语句类型一律默认拒绝（fail-closed），同族新词不再
  依赖名单更新。四点结构性收口：
    (1) 根节点必须是纯查询树，且写语句即使藏进 CTE/子查询也被全树遍历拒绝
        （sqlglot 实证：WITH t AS (INSERT ...) SELECT ... 的根节点是 Select，
        仅查根节点会漏——红队 round3 用例 RT3-CTE 钉死）；
    (2) 函数白名单 fail-closed：sqlglot 未建模的函数（Anonymous）一律拒绝；
        duckdb 的 read_csv/read_parquet 在 sqlglot 有专类（READ_CSV/
        READ_PARQUET）同样不在白名单——旧黑名单时代靠通配追赶的文件读取
        家族在 AST 层自然收口；
    (3) 表引用白名单：只放行当前数据集 spec 声明的表（base_from +
        version_probe_sql 提取；随 DATASET 切源与"按库判分"自动对齐，
        见 _active_tables）。duckdb 元数据面（information_schema/pg_catalog/
        duckdb_* 系统表与函数）经实读现有测试与 drill 路径零使用 → 一并收紧；
        duckdb 允许 FROM '文件路径' 字面量直读文件——sqlglot 把它解析为带
        扩展名的 Identifier 表引用，不在 spec 表集合，拒绝（RT3-FILELIT）；
    (4) query/query_table：ASK-15 时代口径为"放行但受黑名单全文扫描约束"
        （见下方 ASK-15 注释块）——本代明确拒绝：表函数封装可携带任意字符串/
        嵌套 SQL，AST 白名单 fail-closed，不留"放行+扫描"的混合口径。
  旧关键词黑名单保留为最后一道正则层（纵深）：其一，sqlglot 对个别方言特有
  语法会退化为 Command 节点，AST 层虽同样拒绝，正则层与"字符串字面量内的
  关键词一并拒绝"（宁可错杀）的旧口径继续兜底解析盲区；其二，黑名单在文本层
  连注释/字符串里的关键词一起拒——AST 层把注释/字符串视为透明（它们不是
  结构性攻击面），保守口径由正则层保持（RT3-COMMENT 实证两层口径互补）。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import cache
from pathlib import Path

import duckdb
import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from ..config import get_settings
from .schema import MASKED_FIELDS

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|export|import|"
    r"pragma|call|set|prepare|execute|vacuum|checkpoint|"
    r"read_\w+|sniff_\w+|parquet_\w+|\w*_scan|st_read|glob|install|load)\b",
    re.IGNORECASE,
)
# ASK-1 修复：黑名单是黑名单，物理封禁才是城墙——连接层禁用外部访问后，
# 表函数读文件/装扩展在 DuckDB 引擎层直接失败（评审 2.0 P0 纵深加固）。
# AB-HR1 红队发现（2026-11-09）：parquet_scan 文件读取家族不在黑名单——曾只剩
# enable_external_access=False 单层防御（探查实锤：parquet_scan 穿过 guard 后
# 由引擎 Permission Error 兜住），现补齐已知 *_scan 家族恢复双层纵深。
# ASK-15（评审 3.0，2026-11-11）：*_scan 逐词枚举仍有同族漏网——黑名单通配化收口：
#   read_\w+   → read_json_auto/read_ndjson/read_duckdb/read_json_objects*/read_single_* 等
#                （duckdb 2.0.0 实测 105 个表函数全量过 guard：漏网 19 个全在此族）
#   sniff_\w+  → sniff_csv（探查 guard 放行的 csv 探针函数）
#   parquet_\w+ → parquet_metadata/parquet_schema/parquet_*_metadata/parquet_bloom_probe
#                ——通配建议只提 *_scan 后缀，实测这批同样**打开 parquet 文件**，是
#                第三代同族漏网（read_\w+|\w*_scan 收不掉它们）
#   st_read    → spatial 扩展的 GDAL 文件读取器（本构建未装载、install/load 已禁，
#                引擎墙双覆盖——名单内显式点名，防未来默认内置时静默穿 guard）
# 通配后 guard 对沙箱内编译器产物零误伤：demo schema 无 read_*/sniff_*/parquet_*/
# *_scan 命名的表或列（app/data 侧 grep 核对）。剩余放行的表函数均为元数据/内存
# 对象/数据生成器类（duckdb_*、pragma_*、query/query_table——嵌套 SQL 仍受同一
# 黑名单全文扫描约束、range/unnest 等），无文件读取面。
# 【1B5 起口径更新】上文"放行 query/query_table/duckdb_*/pragma_*"仅描述关键词
# 黑名单代际的历史状态；AST 白名单层对本代起明确拒绝这些表函数（表函数封装可
# 携带任意字符串/嵌套 SQL，fail-closed，见文件头红队演进实录 1B5 段第 4 点）。
_CONNECT_CONFIG = {"enable_external_access": False}
_MAX_ROWS = 1000

# ---------------------------------------------------------------------------
# [1B5] sqlglot AST 白名单（解析级收口，设计决策见文件头"红队演进实录"1B5 段）
# ---------------------------------------------------------------------------

# 根节点白名单：只允许纯查询树。UNION/EXCEPT/INTERSECT 与 Select 同属
# SetOperation 家族且互不为父子类（sqlglot 30.x MRO 实证），逐一列举不设基类口子。
_QUERY_ROOTS = (exp.Select, exp.Union, exp.Except, exp.Intersect)

# 全树禁用的语句类型（写 / DDL / 连接 / 配置 / 事务 / 装载 / 权限面，逐类经
# sqlglot 30.33 对 duckdb 方言的解析产物核对：PRAGMA/SET→Pragma/Set，
# INSTALL→Install，PREPARE/CALL/LOAD→Command（方言未建模语法退化为 Command
# 携带原文），EXPORT DATA→Export 等）。根节点检查拦住顶层语句，这里拦住
# 藏进 CTE / 子查询的变体（根节点仍是 Select，仅查根节点必漏）。
_FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop,
    exp.Alter, exp.TruncateTable, exp.Pragma, exp.Copy, exp.Attach, exp.Detach,
    exp.Set, exp.Command, exp.Execute, exp.Export, exp.Install, exp.Transaction,
    exp.Commit, exp.Rollback, exp.Analyze, exp.Use, exp.Describe, exp.Comment,
    exp.Grant, exp.Revoke, exp.Refresh, exp.Cache, exp.Uncache,
)

# 拒绝消息用中文短标签（红队测试断言消息指向正确层）
_FORBIDDEN_NODE_LABELS = {
    exp.Insert: "INSERT（写入）", exp.Update: "UPDATE（改写）",
    exp.Delete: "DELETE（删除）", exp.Merge: "MERGE（合并写入）",
    exp.Create: "CREATE（DDL）", exp.Drop: "DROP（DDL）",
    exp.Alter: "ALTER（DDL）", exp.Pragma: "PRAGMA（配置）",
    exp.Copy: "COPY（导入导出）", exp.Attach: "ATTACH（挂载外部库）",
    exp.Detach: "DETACH（卸载库）", exp.Set: "SET（配置改写）",
    exp.Command: "方言特有命令（PREPARE/CALL/LOAD 等）",
    exp.Execute: "EXECUTE（执行预编译语句）", exp.Export: "EXPORT（导出）",
    exp.Install: "INSTALL（装扩展）", exp.Transaction: "事务控制",
}

# 函数白名单来源（两步实读，缺一不可）：
# ① 编译器产物全集（实读 app/data/metrics.py、dsl.py、dataset_b/c/d.py，并对
#    四数据源全量编译产物跑探针）：聚合 SUM/COUNT/NULLIF、标量 strftime/
#    date_trunc、控制流 CASE、类型转换 CAST、逻辑 AND/OR、条件 IF——
#    即下方 _COMPILER_REQUIRED_KEYS，模块导入时自检缺失即抛错（防 sqlglot
#    升级静默漂移导致编译产物被误拒）；
# ② duckdb 常用安全函数余量（聚合/算术/字符串/日期/窗口/条件/正则/生成器）：
#    为指标字典未来扩展留余量。文件/网络面函数（read_*、sniff_*、parquet_*、
#    *_scan、glob、st_read、write_* 系、url/http 系）一律不在名单；
#    sqlglot 未建模的函数（Anonymous）一律拒绝——不设"未知即放行"的口子。
_FUNC_SAFE_NAMES: tuple[str, ...] = (
    # 聚合
    "sum", "count", "avg", "min", "max", "median", "mode", "string_agg",
    "group_concat", "list", "array_agg", "bool_and", "bool_or", "bit_and",
    "bit_or", "bit_xor", "count_if", "approx_count_distinct", "approx_quantile",
    "stddev", "stddev_samp", "stddev_pop", "variance", "var_samp", "var_pop",
    "corr", "covar_samp", "covar_pop", "first", "last", "arg_min", "arg_max",
    # 算术/数值
    "abs", "round", "ceil", "ceiling", "floor", "sqrt", "cbrt", "exp", "ln",
    "log", "log10", "log2", "mod", "power", "pow", "sign", "greatest", "least",
    "pi", "degrees", "radians", "sin", "cos", "tan", "asin", "acos", "atan",
    "atan2", "cot", "isnan", "isinf", "isfinite", "random", "uuid",
    # 字符串
    "upper", "lower", "length", "strlen", "char_length", "character_length",
    "len", "substr", "substring", "left", "right", "trim", "ltrim", "rtrim",
    "btrim", "concat", "concat_ws", "contains", "starts_with", "ends_with",
    "position", "strpos", "instr", "strcmp", "replace", "reverse", "repeat",
    "lpad", "rpad", "initcap", "md5", "chr", "ascii", "unicode", "translate",
    "format", "printf", "split_part", "string_split", "str_split",
    "string_to_array", "regexp_matches", "regexp_replace", "regexp_extract",
    "regexp_full_match", "levenshtein", "jaro_winkler_similarity", "soundex",
    # 日期/时间
    "strftime", "date_trunc", "date_part", "datepart", "date_diff", "datediff",
    "date_add", "dateadd", "date_sub", "datesub", "last_day", "monthname",
    "dayname", "year", "quarter", "month", "week", "day", "dayofweek",
    "dayofmonth", "dayofyear", "hour", "minute", "second", "epoch", "epoch_ms",
    "epoch_us", "now", "today", "to_timestamp", "strptime", "make_date",
    "make_timestamp", "weekofyear", "weekday", "julianday", "age",
    # 条件/空值
    "coalesce", "nullif", "ifnull", "nvl", "nvl2", "if", "iif", "isnull",
    # 窗口
    "row_number", "rank", "dense_rank", "percent_rank", "cume_dist", "ntile",
    "lag", "lead", "first_value", "last_value", "nth_value",
    # 生成器/集合（纯内存，无 IO；资源消耗由资源层超时 + 强制 LIMIT 兜底）
    "unnest", "generate_series", "range", "array", "list_value", "list_concat",
    "array_length", "exists",
)

# 控制流/类型转换/逻辑连接词：sqlglot 的表达式类（Case/Cast/And/Or），不是
# 可调用的具名函数（无法从 duckdb 函数名推导），逐键手工列入。
_STRUCT_FUNC_KEYS = frozenset({"case", "cast", "try_cast", "and", "or"})


def _derive_func_keys(names: tuple[str, ...]) -> frozenset[str]:
    """把 duckdb 函数名清单展开为 sqlglot 解析产物类名集合（f.sql_name() 小写）。

    白名单按解析产物名判定而非原始书写名：同名函数的大小写/双引号变体在
    sqlglot 解析后归一（READ_CSV、"read_csv"→ReadCSV 类），按产物名判定一次
    覆盖全部书写变体。个别函数 sqlglot 类名与 duckdb 名不同
    （strftime→TIME_TO_STR、string_agg→GROUP_CONCAT、unnest→EXPLODE 等），
    由映射自动推导，不手工对齐。参数形状逐一尝试收集全部产物键——同一函数
    在 sqlglot 可能映射多个类（实证：date_trunc('month', DATE 列) → DATE_TRUNC，
    date_trunc('month', 未知类型) → TIMESTAMP_TRUNC，两个键都要放行）。
    全部尝试均 ParseError 的名字（含 sqlglot 未建模而落为 Anonymous 的）自动
    落选——它们在运行时只会解析为 Anonymous，本就被一律拒绝。
    """
    keys: set[str] = set()
    arg_shapes = (
        "1", "1, 2", "1, 2, 3", "'a', 1", "'a', 1, 2",
        "'a', CAST('2024-01-01' AS DATE)",  # date_trunc 的 DATE 型产物形状
    )
    for name in names:
        for args in arg_shapes:
            try:
                tree = sqlglot.parse_one(f"SELECT {name}({args})", read="duckdb")
            except ParseError:
                continue
            f = tree.find(exp.Func)
            if f is not None and not isinstance(f, exp.Anonymous):
                keys.add(f.sql_name().lower())
    return frozenset(keys)


_FUNC_WHITELIST = _STRUCT_FUNC_KEYS | _derive_func_keys(_FUNC_SAFE_NAMES)

# 编译器产物必需键自检（fail-fast）：四数据源全量编译产物探针实测的函数键全集。
# sqlglot 升级若改名/移类导致键漂移，导入期即报错，不带病上线。
_COMPILER_REQUIRED_KEYS = frozenset(
    {"sum", "count", "nullif", "date_trunc", "time_to_str", "case", "cast",
     "and", "or", "if"}
)
_MISSING_KEYS = _COMPILER_REQUIRED_KEYS - _FUNC_WHITELIST
if _MISSING_KEYS:  # pragma: no cover - 依赖漂移防御，常规安装不可达
    raise RuntimeError(
        f"sqlglot 函数白名单自检失败：编译器产物必需键缺失 {sorted(_MISSING_KEYS)}"
        "——sqlglot 版本漂移？请核对 _FUNC_SAFE_NAMES 与 _derive_func_keys 推导。"
    )

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")  # sqlglot ParseError 消息含 ANSI 高亮码


@cache
def _spec_table_set(base_from: str, version_probe_sql: str) -> frozenset[str]:
    """从 spec 声明提取物理表集合（registry 实读：base_from 是事实表 JOIN 串、
    version_probe_sql 探测同库事实表——表名的单一来源随数据集声明走）。
    键为声明原文：四数据集 spec 均为"表名 别名 JOIN/ON"固定格式，FROM/JOIN
    后取首标识符即可；测试注册的 replace(spec, ...) 变体表集合不变，缓存安全。"""
    pat = re.compile(r"\b(?:from|join)\s+([A-Za-z_]\w*)", re.IGNORECASE)
    names = set(pat.findall(base_from))
    names |= set(re.findall(r"\bfrom\s+([A-Za-z_]\w*)", version_probe_sql, re.IGNORECASE))
    return frozenset(n.lower() for n in names)


def _active_tables(db_path: str | Path | None) -> frozenset[str]:
    """表白名单来源：在哪个库上执行，就用哪个 spec 声明的表集合。

    db_path 能反查到注册 spec（评测按库判分 / 脚本直连 / 测试 replace 变体）
    则用该 spec；否则 settings.dataset 当前激活 spec（DATASET 环境变量切源）。
    """
    from .registry import get_spec, spec_for_db

    spec = (spec_for_db(db_path) if db_path else None) or get_spec()
    return _spec_table_set(spec.base_from, spec.version_probe_sql)


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


def _ast_checks(tree: exp.Expression, allowed_tables: frozenset[str], original: str) -> None:
    """AST 白名单层（[1B5] 第 2b 层）：语句类型 / 函数 / 表引用三道结构校验。

    拒绝消息统一带 "AST 白名单" 层标签，便于测试断言消息指向正确层。
    """
    # (1) 根节点必须是纯查询树
    if not isinstance(tree, _QUERY_ROOTS):
        raise SqlRejected(
            f"AST 白名单: 根节点必须为查询语句，收到 {type(tree).__name__.upper()}"
        )
    # (2) 全树遍历拒绝写/DDL/连接/配置/事务节点（根节点为 Select 也会在 CTE/
    #     子查询里夹带写语句——sqlglot 实证，见 RT3-CTE）
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            label = _FORBIDDEN_NODE_LABELS.get(type(node), type(node).__name__.upper())
            raise SqlRejected(f"AST 白名单: 禁止语句类型 {label}（原文: {original[:60]!r}）")
    # (3) 函数白名单：全部函数节点（含聚合/标量/窗口/表函数）逐一判定。
    #     Anonymous = sqlglot 未建模的函数（duckdb 生态新词/文件读取家族大本营），
    #     fail-closed 一律拒绝。
    for f in tree.find_all(exp.Func):
        key = f.sql_name().lower()
        if key in _FUNC_WHITELIST:
            continue
        if isinstance(f, exp.Anonymous) and isinstance(f.this, str):
            raise SqlRejected(f"AST 白名单: 禁止函数 {f.this.lower()}（不在安全函数白名单）")
        raise SqlRejected(f"AST 白名单: 禁止函数 {key}（不在安全函数白名单）")
    # (4) 表引用白名单：表名（小写折叠——duckdb 未引号标识符大小写不敏感）
    #     必须 ∈ 当前数据集 spec 表集合 ∪ 本查询 CTE 名。CTE 是查询内派生表，
    #     不引入外部读取面；引号怪名（文件路径字面量解析产物）折叠后自然落榜。
    #     duckdb 元数据面（information_schema/pg_catalog/duckdb_*）不特设放行：
    #     实读现有测试与 drill 路径零使用，收紧只放 spec 表。
    #     表函数形态（FROM read_csv(...) / FROM generate_series(...) 的 Table.this
    #     不是 Identifier）不在本道裁决——由 (3) 函数白名单统一处理。
    cte_names = {str(cte.alias_or_name).lower() for cte in tree.find_all(exp.CTE)}
    for tbl in tree.find_all(exp.Table):
        if not isinstance(tbl.this, exp.Identifier):
            continue
        name = str(tbl.this.this).lower()
        if name not in allowed_tables and name not in cte_names:
            raise SqlRejected(
                f"AST 白名单: 禁止表引用 {name}（仅允许当前数据集注册表）"
            )


def _parse_single(s: str) -> exp.Expression:
    """解析层（[1B5] 第 2a 层）：duckdb 方言解析为唯一表达式，失败即拒绝。

    ParseError → SqlRejected（含原因与原文截断）；解析出多条语句（分号分隔的
    多表达式）→ SqlRejected。文本层 ';' 检查已拦住常规堆叠，此处为解析级兜底
    （防未来文本检查被放松时多表达式直达引擎）——双重口径一致：仅允许单条查询。
    """
    try:
        trees = sqlglot.parse(s, read="duckdb")
    except ParseError as e:
        raise SqlRejected(
            f"解析层: SQL 解析失败: {_ANSI_RE.sub('', str(e))[:120]}｜原文: {s[:80]!r}"
        ) from None
    trees = [t for t in trees if t is not None]
    if len(trees) != 1:
        raise SqlRejected(f"解析层: 解析出 {len(trees)} 条语句，已拒绝（仅允许单条查询）")
    return trees[0]


def guard(sql: str, *, db_path: str | Path | None = None) -> str:
    """守卫：返回追加 LIMIT 后的安全 SQL；不通过抛 SqlRejected。

    层序（[1B5] 纵深四层）：文本快速通道（空/多语句/前缀）→ 解析层（sqlglot）
    → AST 白名单（语句类型/函数/表）→ 掩码列 → 关键词黑名单 → 强制 LIMIT。

    db_path：执行库路径。表白名单按"在哪个库上执行就用哪个 spec 的表集合"
    解析（registry.spec_for_db 反查），缺省用 settings.dataset 当前激活 spec
    ——guard(sql) 旧调用签名完全兼容。
    """
    s = sql.strip().rstrip(";").strip()
    if not s:
        raise SqlRejected("空语句")
    if ";" in s:
        # 文本层多语句检查连字符串字面量内的分号一并拒（宁可错杀；合法值域由
        # 枚举校验保证不含分号）——RT3-SEMI 口径。
        raise SqlRejected("语句层: 检测到多语句，已拒绝（仅允许单条查询）")
    if not re.match(r"^(select|with)\b", s, re.IGNORECASE):
        raise SqlRejected("语句层: 仅允许 SELECT / WITH 查询")
    # ---- a. 解析层 ----
    tree = _parse_single(s)
    # ---- b. AST 白名单层 ----
    _ast_checks(tree, _active_tables(db_path), s)
    # ---- c. 掩码列守卫（文本层扫描，语义与通配黑名单时代一致） ----
    low = s.lower()
    for col in MASKED_FIELDS:
        if col in low:
            raise SqlRejected(f"掩码列守卫: 引用了受限列: {col}")
    # ---- d. 关键词黑名单（最后一道正则层，纵深见文件头红队演进实录 1B5 段） ----
    m = _FORBIDDEN.search(s)
    if m:
        raise SqlRejected(f"关键词黑名单: 包含被禁止的关键词: {m.group(0)}")
    # ---- 资源层：强制 LIMIT ----
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
        # [1B5] 表白名单按执行库对齐 spec：传 db_path 使 guard 的表集合与
        # 本连接的库一致（评测按库判分 / 多数据源 Sandbox(db_path=spec...)）。
        safe_sql = guard(sql, db_path=self.db_path)
        con = duckdb.connect(self.db_path, read_only=True, config=_CONNECT_CONFIG)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._run, con, safe_sql), timeout=self.timeout
            )
        except TimeoutError:
            raise SqlTimeout(f"查询超过 {self.timeout}s 被中断") from None
        finally:
            # ASKBI-8（评审 4.0）：interrupt 提到 finally——取消传播不完整收口。
            # 曾 interrupt() 只接在 except TimeoutError 分支：客户端断连等外层
            # CancelledError（BaseException，except TimeoutError 接不住）直达
            # finally 的 con.close()，to_thread 里仍在跑的查询撞上被关连接——
            # :127 注释承诺的「必须用 interrupt 打断工作线程」在取消路径缺席。
            # 超时/取消/出错三条路径统一先打断工作线程再关闭。
            # 查询已正常结束时 interrupt() 无害：无后续查询（连接立即关闭），
            # 标记不落在任何可被误伤的执行上。
            # （asyncio.to_thread 无法取消已开跑的线程，必须用 interrupt()
            # 从事件循环线程打断工作线程中的查询——duckdb 支持跨线程调用）
            try:
                con.interrupt()
            except Exception:  # noqa: BLE001, S110  连接已失效时 interrupt 自身可能失败，交给 close
                pass
            try:
                con.close()
            except Exception:  # noqa: BLE001, S110  极端竞态下 close 可能撞上未完全中止的查询
                pass


# 同步版本（脚本/测试用）
def execute_sync(sql: str, db_path: Path | str | None = None) -> QueryResult:
    safe = guard(sql, db_path=db_path)
    con = duckdb.connect(str(db_path or get_settings().db_abs_path), read_only=True, config=_CONNECT_CONFIG)
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
