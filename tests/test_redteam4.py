"""红队 round 4（[1B7] askbi 车道）——评审 5.0 确认架构下的残余攻击面常驻化。

与前三代（test_redteam 经典注入面 / test_redteam2 并发·审计·参数面 / test_redteam3
AST 白名单守卫本体）的分工：本套件攻击 **评审 5.0（interview/reviews/review_report_5.md）
确认架构下仍然成立的 8 个攻击面**，逐条注明攻击假设；断言"被拒/被拦截/口径如此"
均带消息或行为验证；观察项未修的钉现状口径（注释指向 AB-x 编号），不打没准备的仗
——全部用例先实读修复代码与探针实测再落笔。

1. RT4-UNI    Unicode 同形字/零宽字符全链路（西里尔同形字、U+200B、RTL 进
              question→DSL 字段链：pydantic 枚举白名单 miss→拒答；进 SQL 面：
              sqlglot 归一→Anonymous/表白名单 fail-closed）——负向不变量钉死：
              同形字不产生新绕过；
2. RT4-DEEP   JSON 深度嵌套/超大请求体（review_report_5 AB-9 前半：深括号 SQL 的
              RecursionError 曾逃逸守卫分类——**本批最小修复 _parse_single 增捕
              收口为 SqlRejected**，先红后绿；后半"API 请求体无字节上限"未修
              ——口径钉）；
3. RT4-AUDIT  审计/任务 JSONL 洪水（AB-3/AB-5 观察项未修：只追加无轮转 +
              list/重启全量读 = 资源面明示取舍，本套件钉现状 + 计数/行为断言）；
4. RT4-CPT    checkpointer 状态无界/跨属主污染（AB-2 口径：_LRUSaver 淘汰挂起
              thread vs thread_owner 注册表 4096 两套存储生命周期错位——resume
              预检 409 fail-closed、属主绑定存续、绕过预检的图级语义钉死）；
5. RT4-DISC   真 ASGI 断连三线联动（AB-1/AB-18/ASKBI-1 修复的端到端红测）。
              方法论：httpx ASGITransport 全量缓冲响应（见
              httpx/_transports/asgi.py:handle_async_request——body_parts 聚齐才
              返回），不产生真断连；本套件按 uvicorn 0.54 同构（asgi spec_version
              2.3 < 2.4，starlette StreamingResponse 走 listen_for_disconnect
              塌缩任务组路径）直接驱动原始 ASGI，receive() 在测试置位后回
              http.disconnect——与生产断连同一条取消路径；
6. RT4-MCP    MCP 工具面契约（AB-7 MAX_FILTERS=16 对 MCP 客户端直达面的闸、
              值注入转义、掩码字段、limit 钳制、只读工具无回显面；AB-4
              execute_sync 无超时=观察项未修口径钉）；
7. RT4-FAULT  故障×并发风暴叠加（fault_inject 场景 × singleflight：LLM 故障风暴
              flight 零登记、库缺失+并发 claim 版本闸哨兵 fail-open（AB-19 修复
              红测的风暴化）、leader 挂死×等待名额上限双 fail-open 路径）；
8. RT4-ESC    掩码列转义书写（AB-8 观察项：U&"p\\0068one" 类转义标识符——实测
              duckdb 2.0.0/sqlglot 30.33 均不解码该形态：sqlglot 归一为位与表达式、
              引擎层 BinderException 兜底——负向不变量：任何转义书写都解不出
              掩码列，零数据泄漏）。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import threading
import time
import uuid
from types import SimpleNamespace

import duckdb
import httpx
import pytest
import sqlglot
from langgraph.types import Command
from pydantic import ValidationError
from sqlglot import exp

import app.api.main as api_main
import app.api.task_store as task_store_mod
from app.agents import nodes
from app.agents.graph import build_graph, run_ask
from app.api.main import _LRUSaver
from app.api.task_store import TaskStore
from app.audit import AuditLog
from app.cache import QueryCache, get_query_cache, reset_query_cache
from app.config import get_settings
from app.data.dsl import MAX_FILTERS, build_dsl, compile_sql, validate_filter_values
from app.data.sandbox import Sandbox, SqlRejected, execute_sync, guard
from app.llm.base import LLMError, LLMResponse, TokenUsage
from app.llm.mock import MockLLM
from app.mcp.core import handle_tool

# ===========================================================================
# 0. 辅助（载荷常量 / 夹具 / 原始 ASGI 驱动）
# ===========================================================================

ZWSP = "\u200b"    # 零宽空格
RTL = "\u202e"     # RLO 双向覆盖（ruff PLE2502 拒源码裸控制字符，转义书写）
CYR_A = "\u0430"   # 西里尔 а（拉丁 a 的同形字）
CYR_E = "\u0435"   # 西里尔 е（拉丁 e 的同形字）

_SF_RESULT = {"columns": ["value"], "rows": [[42]], "row_count": 1, "truncated": False, "elapsed_ms": 1.0}


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    """API 测试隔离：TaskStore 换 tmp 实例（不污染全局单例与真实 tasks.jsonl）。"""
    store = TaskStore(path=tmp_path / "tasks.jsonl")
    monkeypatch.setattr(task_store_mod, "_STORE", store)
    return store


@pytest.fixture
async def client(store_env):
    transport = httpx.ASGITransport(app=api_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def suspend_env(store_env, monkeypatch):
    """澄清挂起拓扑：开关开 + 独立小容量 checkpointer 图（不污染 _SUSPEND_GRAPH 单例）。
    缓存显式关：不依赖前置测试的遗留开关状态（rt2 admin 测试曾泄漏 cache_enabled=True）。"""
    s = get_settings()
    monkeypatch.setattr(s, "clarify_suspend_enabled", True)
    monkeypatch.setattr(s, "cache_enabled", False)
    saver = _LRUSaver(max_threads=8)
    monkeypatch.setattr(api_main, "_SUSPEND_GRAPH", build_graph(clarify_suspend=True, checkpointer=saver))
    return {"store": store_env, "saver": saver, "graph": api_main._SUSPEND_GRAPH}


def _cfg(tid: str) -> dict:
    # langgraph 会原地改写 config（注入 checkpoint_ns）——每次新建防跨用例污染
    return {"configurable": {"thread_id": tid}}


async def _drain(graph, payload, config) -> None:
    async for _chunk in graph.astream(payload, config=config, stream_mode="updates"):
        pass


def _asgi_scope(body: bytes) -> dict:
    """uvicorn 0.54 同构 scope：spec_version 2.3（< 2.4）——starlette
    StreamingResponse 走 listen_for_disconnect 塌缩任务组路径（生产断连路径）。"""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/ask",
        "raw_path": b"/api/ask",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 123),
        "server": ("testserver", 80),
    }


async def _asgi_request(app, payload: dict, disconnect: asyncio.Event | None = None):
    """原始 ASGI 驱动。receive() 首调用回请求体；disconnect 置位后回 http.disconnect
    （真实断连语义）。返回 (app_task, messages)——调用方可在请求在途时置位 disconnect
    模拟"客户端中途断连"，或传 None 让流自然跑完（listener 由塌缩任务组取消）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sent = asyncio.Event()
    messages: list[dict] = []

    async def receive():
        if not sent.is_set():
            sent.set()
            return {"type": "http.request", "body": body, "more_body": False}
        if disconnect is None:
            # 无断连语义：listener 永久阻塞等待（由塌缩任务组在 body 完成时取消）
            await asyncio.Event().wait()
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    task = asyncio.create_task(app(_asgi_scope(body), receive, send))
    return task, messages


def _sse_frames(messages: list[dict]) -> list[dict]:
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    frames = []
    for line in body.decode("utf-8", "ignore").splitlines():
        if line.startswith("data: "):
            try:
                frames.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                continue
    return frames


def _poll(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


async def _apoll(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


# ===========================================================================
# 1. RT4-UNI：Unicode 同形字/零宽字符全链路
# ===========================================================================


@pytest.mark.parametrize("metric", [
    f"revenue{ZWSP}",       # 尾部零宽
    f"re{ZWSP}venue",       # 中部零宽
    f"revenue{RTL}",        # RTL 覆盖符
    f"re{CYR_A}l_revenue",  # 西里尔同形字（real_revenue 的 a）
])
def test_rt4_uni_homoglyph_metric_whitelist_miss(metric):
    """攻击假设：同形字/不可见字符让枚举白名单比对失效（肉眼与 LLM 都难分辨），
    伪装指标直达编译器。防御：pydantic 枚举白名单按原串精确比对——任何注入变体
    miss 即拒（"未知指标"），后续在 sql_agent 层降级为"数据集不支持"拒答。"""
    with pytest.raises(ValidationError) as ei:
        build_dsl({"metric": metric})
    assert "未知指标" in str(ei.value)


@pytest.mark.parametrize("payload", [
    {"metric": "revenue", "dimensions": [f"region{ZWSP}"]},
    {"metric": "revenue", "filters": [{"field": f"region{ZWSP}", "op": "=", "value": "华南"}]},
])
def test_rt4_uni_zwsp_dimension_and_filter_field_miss(payload):
    """攻击假设：零宽字符混入维度/过滤字段名绕过白名单。防御：维度与过滤字段
    同样走枚举精确比对，miss 即拒。"""
    with pytest.raises(ValidationError) as ei:
        build_dsl(payload)
    msg = str(ei.value)
    assert "未知维度" in msg or "未知过滤字段" in msg


def test_rt4_uni_zwsp_time_month_format_miss():
    """攻击假设：零宽字符拆开 YYYY-MM 让时间白名单正则漏判。防御：fullmatch
    锚定正则对任何插入字符一律拒绝。"""
    with pytest.raises(ValidationError, match="YYYY-MM"):
        build_dsl({"metric": "revenue",
                   "time": {"mode": "between", "start": f"2026-0{ZWSP}1", "end": "2026-09"}})


async def test_rt4_uni_filter_value_zwsp_no_visibility_change(db_path):
    """攻击假设：过滤值夹带零宽字符绕过取值白名单看到"额外"数据（越权方向）。
    实测负向不变量：ZWSP 值编译放行（值面走 _q 转义，无结构面）但匹配不到任何行
    ——数据可见性只收敛不扩张；validate_filter_values 照常报"不在数据集取值内"
    （sql 修复循环据此拒答）。"""
    spec = None
    honest_dsl = build_dsl({"metric": "order_count", "dimensions": ["region"],
                            "filters": [{"field": "region", "op": "=", "value": "华南"}]}, spec)
    honest = execute_sync(guard(compile_sql(honest_dsl)[0]), db_path=db_path)
    hostile_dsl = build_dsl({"metric": "order_count", "dimensions": ["region"],
                             "filters": [{"field": "region", "op": "=", "value": f"华南{ZWSP}"}]}, spec)
    assert validate_filter_values(hostile_dsl)      # 取值白名单照常报问题（修复循环入口）
    hostile = execute_sync(guard(compile_sql(hostile_dsl)[0]), db_path=db_path)
    assert honest.row_count > 0
    assert hostile.row_count == 0, "ZWSP 值不得匹配任何行（可见性只收敛不扩张）"
    mixed_dsl = build_dsl({"metric": "order_count", "dimensions": ["region"],
                           "filters": [{"field": "region", "op": "in",
                                        "value": ["华南", f"华{ZWSP}南"]}]}, spec)
    mixed = execute_sync(guard(compile_sql(mixed_dsl)[0]), db_path=db_path)
    assert mixed.rows == honest.rows, "IN 混入 ZWSP 值不得增添任何行"


@pytest.mark.parametrize("sql", [
    "SELECT сun(*) FROM orders",                 # 西里尔 с 伪装 sum
    "SELECT re\u200bad_csv('x.csv') FROM orders",  # 零宽拆 read_csv
    "SELECT read\u202e_csv('x') FROM orders",    # RTL 拆 read_csv
    "SELECT su\u0301m(x) FROM orders",           # 组合附加符伪装 sum
])
def test_rt4_uni_sql_function_homoglyph_anonymous_fail_closed(sql):
    """攻击假设：同形字书写让函数名逃过白名单/黑名单的关键词匹配。防御：
    sqlglot 解析归一后按 sql_name 判定——同形字函数不在白名单（Anonymous/具名
    一律 fail-closed），消息指向 AST 白名单层。"""
    with pytest.raises(SqlRejected, match="AST 白名单: 禁止函数"):
        guard(sql)


@pytest.mark.parametrize("sql", [
    "SELECT 1 FROM сustomers",     # 西里尔 с 伪装 customers 表
    "SELECT 1 FROM orders\u200b",  # 表名尾部零宽
])
def test_rt4_uni_sql_table_homoglyph_table_whitelist_miss(sql):
    """攻击假设：同形字表名骗过表引用白名单。防御：表白名单按解析产物名精确
    比对（小写折叠），同形字变体一律落榜。"""
    with pytest.raises(SqlRejected, match="AST 白名单: 禁止表引用"):
        guard(sql)


async def test_rt4_uni_zwsp_identifier_guard_passes_engine_refuses(db_path):
    """口径钉（负向不变量的另一半）：零宽字符在**标识符列位置**不构成函数/表等
    结构面，guard 放行（文本层原样保留）——但引擎层解析器把 ZWSP 当分隔符，
    列不存在 BinderException 兜底。同形字在"能通过 guard 的位置"必然落到引擎拒绝，
    在"能被引擎执行的位置"必然被 guard 拒——两层无交集缝隙。"""
    safe = guard("SELECT re\u200bvenue FROM orders")
    assert "re\u200bvenue" in safe, "guard 文本层原样保留（不归一、不静默改写）"
    with pytest.raises(duckdb.BinderException, match="Referenced column"):
        execute_sync(safe, db_path=db_path)


async def test_rt4_uni_zwsp_string_literal_is_value_semantics(db_path):
    """口径钉：零宽字符进字符串字面量=纯值面语义——可执行、结果原样保留 ZWSP。
    结构面（函数/表/语句类型）不受影响，无绕过方向。"""
    res = execute_sync(guard("SELECT upper('a\u200bb') AS x"), db_path=db_path)
    assert res.rows[0][0] == "A\u200bB"


async def test_rt4_uni_sql_agent_refuses_homoglyph_metric_full_chain(db_path, monkeypatch):
    """全链路负向不变量：LLM 产出同形字指标（revenue 的 e 为西里尔 е）→
    build_dsl 枚举白名单 miss → sql_agent 层"数据集不支持该查询"诚实拒答，
    不带错误重试、不编译、不落沙箱。"""

    class HomoglyphLLM:
        async def chat(self, req):
            if req.node == "sql_agent":
                payload = {"intent": "aggregate", "metric": f"rev{CYR_E}nue",
                           "dimensions": [], "filters": [], "time": {"mode": "all"}}
                return LLMResponse(
                    content=json.dumps(payload, ensure_ascii=False),
                    usage=TokenUsage(prompt_tokens=10, completion_tokens=10),
                    provider="rt4", model="homoglyph",
                )
            return await MockLLM().chat(req)

    monkeypatch.setattr(nodes, "get_llm", lambda: HomoglyphLLM())
    state = await nodes.sql_agent_node(
        {"question": "上月销售额是多少", "role": "boss", "events": [], "usage": {}})
    assert state.get("refusal") is not None
    assert "数据集不支持该查询" in state["refusal"]
    assert "未知指标" in state["refusal"]


# ===========================================================================
# 2. RT4-DEEP：JSON 深度嵌套/超大请求体
# ===========================================================================


def test_rt4_deep_bracket_sql_rejected_parse_layer():
    """RT4-DEEP 攻击假设（review_report_5 AB-9 前半）：深括号 SQL 让 sqlglot
    递归解析栈溢出，RecursionError（RuntimeError 子类）曾逃逸守卫分类直穿调用方
    ——executor 修复循环当普通 SQL 失败多跑两轮、MCP execute_sync 面原样外抛。
    W2 观察项确认未修后，本批最小修复 _parse_single 增捕 RecursionError→
    SqlRejected（先红后绿）：与 ParseError 同口径归入"解析层"拒绝。修复前本用例
    实测红（RecursionError 冒出）。"""
    sql = "SELECT " + "(" * 3000 + "1" + ")" * 3000   # ~6KB，在 64KB 上界窗口内
    with pytest.raises(SqlRejected, match="解析层"):
        guard(sql)


def test_rt4_deep_bracket_over_byte_limit_rejected_at_entry():
    """极端深度（~200KB）在入口即被 AB-7 字节上界拦下——不走解析、无递归路径。"""
    sql = "SELECT " + "(" * 100_000 + "1" + ")" * 100_000
    with pytest.raises(SqlRejected, match="64KB 上界"):
        guard(sql)


async def test_rt4_deep_bracket_async_path_sqlrejected(db_path):
    """异步执行面（API 实际路径）：guard 在 to_thread 内抛 SqlRejected 原样传播
    （工作线程异常上抛语义），async 路径同样是 SqlRejected 而非 RecursionError。"""
    sql = "SELECT " + "(" * 3000 + "1" + ")" * 3000
    with pytest.raises(SqlRejected, match="解析层"):
        await Sandbox(db_path=db_path).execute(sql)


def test_rt4_deep_bracket_shallow_still_passes():
    """兼容性对照：浅层嵌套（编译产物/手写 SQL 常见形态）不受修复误伤。"""
    assert guard("SELECT " + "(" * 20 + "1" + ")" * 20).startswith("SELECT")


def _deep_json(depth: int) -> bytes:
    """手工构造深嵌套 JSON 原文字节（绕开客户端 json.dumps 自身的递归上限）。"""
    inner = '{"question": "上月销售额"}'
    for _ in range(depth):
        inner = '{"x": ' + inner + "}"
    return inner.encode()


async def test_rt4_deep_json_body_moderate_depth_422(client):
    """中等深度（200 层）：json.loads 可解析、pydantic 校验正常工作 → 422。
    校验层在自身能力范围内行为正确。"""
    r = await client.post("/api/ask", content=_deep_json(200),
                          headers={"content-type": "application/json"})
    assert r.status_code == 422


async def test_rt4_deep_json_body_extreme_depth_500_pinned():
    """口径钉（review_report_5 AB-9 后半"请求体无上限"之一）：极端深度（2000 层）
    JSON——FastAPI 422 校验错误渲染阶段 jsonable_encoder 递归溢出 →
    ServerErrorMiddleware 兜底 500。**无请求体深度/字节防护（观察项未修）**；
    现行口径=通用 500（进程不崩、后续请求正常），由 ASGI 服务器同款兜底。"""
    transport = httpx.ASGITransport(app=api_main.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/ask", content=_deep_json(2000),
                         headers={"content-type": "application/json"})
        assert r.status_code == 500
        # 进程仍健康：同一连接池后续请求正常应答
        r2 = await c.get("/api/health")
        assert r2.status_code == 200


async def test_rt4_oversized_body_no_byte_cap_pinned(client):
    """口径钉（AB-9 后半之二）：请求体无字节上限——2MB 未知字段（pydantic extra
    默认忽略）全量读入并正常应答 200。防线只剩字段级 max_length（question 500
    /id 200），传输体量面为明示取舍（PRODUCTION_GAP 资源面）。"""
    big = {"question": "上月销售额是多少", "padding": "A" * (2 * 1024 * 1024)}
    r = await client.post("/api/ask", content=json.dumps(big),
                          headers={"content-type": "application/json"})
    assert r.status_code == 200
    assert '"type": "final"' in r.text, "超大请求体被完整处理并正常出 final 帧（口径=无上限）"


# ===========================================================================
# 3. RT4-AUDIT：审计/任务 JSONL 洪水（AB-3/AB-5 观察项未修——资源面口径钉）
# ===========================================================================


def test_rt4_audit_flood_append_only_no_rotation(tmp_path):
    """攻击假设：持续写审计把磁盘填满 / 或写入放大。现状（AB-5 观察项未修）：
    只追加、无轮转、无字节上限——1200 条后仍单文件、无 .bak、每行完整可解析、
    全量可回放。本用例钉该取舍**是现状**（不伪装成有轮转）。"""
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    for i in range(1200):
        audit.record("admin", "task.clear", "task_store", f"t{i}", {"removed": i})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["audit.jsonl"], \
        "AB-5 口径：无轮转/无归档文件（资源面明示取舍，见 app/audit.py 模块头）"
    lines = audit.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1200
    assert all(isinstance(json.loads(ln), dict) for ln in lines)
    events = audit.list(limit=5000)
    assert len(events) == 1200
    assert events[0]["detail"]["removed"] == 1199   # 最新在前


def test_rt4_audit_list_reads_whole_file_for_limit_1(tmp_path, monkeypatch):
    """AB-5 读放大口径钉：list(limit=1) 也要**全量读文件**（read_text 一次性
    载入再切片）——读放大与文件大小线性相关，是"查询端点只读 + 无索引"取舍的
    直接后果。行为断言：读到的内容 == 全文件（含最老记录）。"""
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    for i in range(400):
        audit.record("admin", "cache.clear", "query_cache", "*", {"removed": i})
    reads: list[str] = []
    orig_read = pathlib.Path.read_text

    def spy(self, *a, **kw):
        data = orig_read(self, *a, **kw)
        if self == audit.path:
            reads.append(data)
        return data

    monkeypatch.setattr(pathlib.Path, "read_text", spy)
    assert len(audit.list(limit=1)) == 1            # limit 语义正确（最新在前切片）
    assert len(reads) == 1                          # 单次 list 只读一次
    full = orig_read(audit.path, encoding="utf-8")
    assert len(reads[0]) == len(full)               # 读的是全文件，不是尾部窗口
    assert '"removed": 0' in reads[0]               # 最老记录也在本次读取内


def test_rt4_audit_flood_with_corrupt_lines_survives(tmp_path):
    """洪水 + 损坏行混合（攻击者直写文件的二阶形态）：list 跳过损坏行不崩溃、
    顺序（最新在前）与过滤语义保持。"""
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    for i in range(300):
        audit.record("admin", "task.clear", "task_store", f"t{i}", {"removed": i})
    with audit.path.open("a", encoding="utf-8") as f:
        f.write("not-json-at-all\n")
        f.write('{"ts": "x", "truncated": "partial-lin')
    events = audit.list(limit=1000)
    assert len(events) == 300
    assert all(e.get("action") == "task.clear" for e in events)


def test_rt4_tasks_restart_full_file_read_and_cap_pinned(tmp_path, monkeypatch):
    """AB-3 口径钉：tasks.jsonl 重启恢复**全量读文件**（无增量/索引），内存索引
    受 ASK-9 的 200 上限淘汰（finished 淘最旧），JSONL 全量留盘可回放。行为断言：
    单次全量读 + 内存 200 + 盘上 320 行。"""
    path = tmp_path / "tasks.jsonl"
    lines = []
    for i in range(320):
        lines.append(json.dumps({
            "id": f"t{i:04d}", "question": f"q{i}", "role": "boss",
            "status": "done", "kind": "answer",
            "created_at": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}",
            "elapsed_ms": 1.0, "answer": {"kind": "answer"}, "usage": {}, "events": [],
        }, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    reads: list[str] = []
    orig_read = pathlib.Path.read_text

    def spy(self, *a, **kw):
        data = orig_read(self, *a, **kw)
        if self == path:
            reads.append(data)
        return data

    monkeypatch.setattr(pathlib.Path, "read_text", spy)
    store = TaskStore(path=path)
    full = orig_read(path, encoding="utf-8")
    assert len(reads) == 1 and len(reads[0]) == len(full), \
        "AB-3 口径：重启一次全量读文件（无增量恢复）"
    assert len(store._tasks) == 200                  # ASK-9 内存上限
    assert "t0000" not in store._tasks and "t0319" in store._tasks   # 淘最旧 finished
    assert full.count('"id": "t') == 320             # 盘上全量留盘（回放面不变）


def test_rt4_audit_storm_8_threads_lines_atomic(tmp_path):
    """并发风暴下的行原子性（RT2 50 行 storm 的资源面扩展）：8 线程 × 120 条
    并发追加——锁窗口内单行写入，960 行每行完整合法 JSON、字段集不变。"""
    audit = AuditLog(path=tmp_path / "audit.jsonl")

    def worker(w: int) -> None:
        for i in range(120):
            audit.record("admin", "task.clear", "task_store", f"w{w}-{i}", {"x": "a\nb"})

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    lines = audit.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 8 * 120
    parsed = [json.loads(ln) for ln in lines]
    assert all(set(e) == {"ts", "actor", "action", "target_type", "target_id", "detail"}
               for e in parsed)


# ===========================================================================
# 4. RT4-CPT：checkpointer 状态无界/跨属主污染（AB-2 口径）
# ===========================================================================


async def test_rt4_cpt_lru_eviction_deletes_suspended_checkpoints(suspend_env):
    """攻击假设：洪水新 thread 把挂起 thread 的 checkpoint 从 _LRUSaver 淘汰
    （淘汰 = delete_thread 清 storage）。实测：淘汰后 aget_state 空态——挂起
    状态不可恢复（AB-2 的成因面）。"""
    graph = suspend_env["graph"]
    victim = f"t-victim-{uuid.uuid4().hex[:6]}"
    await _drain(graph, {"question": "最近卖得怎么样", "role": "boss", "events": [], "usage": {}},
                 _cfg(victim))
    snap = await graph.aget_state(_cfg(victim))
    assert snap.next == ("clarify_suspend",)
    for i in range(10):
        await _drain(graph, {"question": "上月销售额", "role": "boss", "events": [], "usage": {}},
                     _cfg(f"t-flood-{i}"))
    snap2 = await graph.aget_state(_cfg(victim))
    assert snap2.next == () and not snap2.values, "LRU 淘汰后挂起 checkpoint 已删除"


async def test_rt4_cpt_resume_evicted_thread_api_409_fail_closed(suspend_env, client):
    """AB-2 口径钉：挂起 thread 被 LRU 淘汰后 resume——API 预检（snap.next 为空）
    fail-closed 409。文案"该 thread 无挂起的澄清可恢复"对用户有误导（挂起明明
    存在过），属观察项留档不修——fail-closed 方向正确（绝不带错误状态续跑）。"""
    tid = f"t-evict-{uuid.uuid4().hex[:6]}"
    async with client.stream("POST", "/api/ask",
                             json={"question": "最近卖得怎么样", "thread_id": tid}) as resp:
        await resp.aread()
    for i in range(10):
        async with client.stream("POST", "/api/ask",
                                 json={"question": "上月销售额",
                                       "thread_id": f"t-flood-{uuid.uuid4().hex[:4]}-{i}"}) as resp:
            await resp.aread()
    snap = await suspend_env["graph"].aget_state(_cfg(tid))
    assert snap.next == ()
    r = await client.post("/api/ask",
                          json={"question": "看销售额", "thread_id": tid, "resume": True})
    assert r.status_code == 409
    assert "无挂起的澄清" in r.json()["detail"]


async def test_rt4_cpt_owner_registry_survives_checkpoint_eviction(suspend_env, client, monkeypatch):
    """生命周期错位的双向口径：checkpointer LRU(8) 淘汰挂起 thread 后——
    ① thread_owner 注册表（4096 上限，另一册）绑定存续：跨属主对该 thread 的
    任何写路径仍 403（ASK-13 fail-closed 不因淘汰失效）；
    ② 属主本人可开新 run（fail-open 方向=属主本人，AB-2 后果仅限 clarify 409 误导）。"""
    monkeypatch.setattr(get_settings(), "owner_check_enabled", True)
    tok_a = {"X-Owner-Token": "owner-a-rt4"}
    tok_b = {"X-Owner-Token": "attacker-rt4"}
    tid = f"t-zombie-{uuid.uuid4().hex[:6]}"
    async with client.stream("POST", "/api/ask",
                             json={"question": "最近卖得怎么样", "thread_id": tid},
                             headers=tok_a) as resp:
        await resp.aread()
    assert suspend_env["store"].thread_owner(tid) is not None
    for i in range(10):
        async with client.stream("POST", "/api/ask",
                                 json={"question": "上月销售额", "thread_id": f"t-flood2-{i}"},
                                 headers=tok_a) as resp:
            await resp.aread()
    snap = await suspend_env["graph"].aget_state(_cfg(tid))
    assert snap.next == (), "checkpoint 已淘汰"
    assert suspend_env["store"].thread_owner(tid) is not None, \
        "属主注册表与 checkpointer 生命周期错位：绑定不随 checkpoint 淘汰收缩"
    r_atk = await client.post("/api/ask",
                              json={"question": "上月销售额", "thread_id": tid}, headers=tok_b)
    assert r_atk.status_code == 403, "跨属主写已淘汰 thread 仍 fail-closed 403"
    r_own = await client.post("/api/ask",
                              json={"question": "上月销售额", "thread_id": tid}, headers=tok_a)
    assert r_own.status_code == 200, "属主本人可在僵尸 thread 上开新 run"


async def test_rt4_cpt_evicted_resume_graph_level_semantics_pinned(suspend_env):
    """口径钉（对照证据）：绕过 API 预检直连图 astream(Command(resume)) 于已淘汰
    thread——langgraph 把 resume 值当新图输入、route_node 取 state["question"]
    KeyError。API 层 snap.next 预检（上一用例的 409）是唯一防线；SSE gen 的
    except Exception 会把任何残余此类异常转成 error 帧 + 任务终态（不崩进程）。
    本用例钉该机制存在性——预检一旦被移除，失败模式即为此形态。"""
    graph = suspend_env["graph"]
    victim = f"t-victim2-{uuid.uuid4().hex[:6]}"
    await _drain(graph, {"question": "最近卖得怎么样", "role": "boss", "events": [], "usage": {}},
                 _cfg(victim))
    for i in range(10):
        await _drain(graph, {"question": "上月销售额", "role": "boss", "events": [], "usage": {}},
                     _cfg(f"t-flood3-{i}"))
    with pytest.raises(KeyError):
        async for _chunk in graph.astream(Command(resume="销售额"), config=_cfg(victim),
                                          stream_mode="updates"):
            pass


async def test_rt4_cpt_lru_touch_refresh_protects_polled_thread():
    """LRU 触碰语义（AB-2 的缓解面）：get_tuple/aget_state 触碰把 thread 移到
    LRU 尾部——客户端持续轮询的挂起 thread 不被洪水淘汰。行为断言：触碰过的
    t1 存活，未触碰的 t2 被淘汰。"""
    saver = _LRUSaver(max_threads=3)
    g = build_graph(clarify_suspend=True, checkpointer=saver)
    await _drain(g, {"question": "最近卖得怎么样", "role": "boss", "events": [], "usage": {}},
                 _cfg("t1"))
    await _drain(g, {"question": "上月销售额", "role": "boss", "events": [], "usage": {}}, _cfg("t2"))
    for _ in range(3):
        await g.aget_state(_cfg("t1"))               # 客户端轮询 = LRU 触碰
    await _drain(g, {"question": "上月销售额", "role": "boss", "events": [], "usage": {}}, _cfg("t3"))
    await _drain(g, {"question": "上月销售额", "role": "boss", "events": [], "usage": {}}, _cfg("t4"))
    snap1 = await g.aget_state(_cfg("t1"))
    snap2 = await g.aget_state(_cfg("t2"))
    assert snap1.next == ("clarify_suspend",), "被触碰的挂起 thread 存活"
    assert snap2.next == (), "未触碰的 thread 按 LRU 淘汰"


async def test_rt4_cpt_registry_cap_diverges_from_lru(suspend_env):
    """两套存储容量口径钉：thread_owner 注册表 4096 上限（RT2 已测有界性），
    本用例钉**容量错位**——注册表 300 项远超 checkpointer LRU(8)：注册表项不随
    checkpoint 淘汰收缩（生命周期错位 = AB-2 的结构性成因，属主校验权威源
    优先保 fail-closed 方向）。"""
    store = suspend_env["store"]
    for i in range(300):
        store.bind_thread_owner(f"th-rt4-{i}", "a" * 64)
    assert len(store._thread_owners) == 300          # 未触及 4096 上限
    for i in range(10):
        await _drain(suspend_env["graph"],
                     {"question": "上月销售额", "role": "boss", "events": [], "usage": {}},
                     _cfg(f"t-lru-flood-{i}"))
    assert store.thread_owner("th-rt4-0") == "a" * 64, \
        "注册表项不受 checkpointer LRU 淘汰影响（另一册生命周期）"


# ===========================================================================
# 5. RT4-DISC：真 ASGI 断连三线联动（AB-1/AB-18/ASKBI-1 端到端红测）
# ===========================================================================


async def test_rt4_disc_mid_executor_three_lines_linked(store_env, monkeypatch):
    """攻击假设：SSE 流中途真断连（http.disconnect → starlette 取消 body 流）→
    ①任务滞留 running（ASKBI-1 兜底缺口）/②flight 永久残留（AB-1①缺口）/
    ③resume 标记泄漏（ASKBI-7 缺口）三线任一未联动。端到端断言：断连后任务
    终态 aborted + flight 表清空 + 标记释放 + 同 key 可再竞选/同 thread 可再执行。"""
    s = get_settings()
    monkeypatch.setattr(s, "clarify_suspend_enabled", True)
    # 显式钉住缓存开关：不依赖前置测试的遗留状态（rt2 admin 测试曾泄漏
    # cache_enabled=True——本用例要观测 flight 表语义，必须确定性开缓存）
    monkeypatch.setattr(s, "cache_enabled", True)
    reset_query_cache()
    cache = get_query_cache()
    cache._version_ttl_seconds = 0.0   # 探测不记忆化：flight 观测窗口内不缓存命中
    monkeypatch.setattr(api_main, "_SUSPEND_GRAPH",
                        build_graph(clarify_suspend=True, checkpointer=_LRUSaver(max_threads=16)))
    store = store_env
    started = asyncio.Event()
    block = asyncio.Event()        # 永不置位——取消是 slow_execute 的唯一出口

    async def slow_execute(self, sql):
        started.set()
        await block.wait()
        return SimpleNamespace(columns=["value"], rows=[[1]], row_count=1,
                               truncated=False, elapsed_ms=1.0)

    monkeypatch.setattr(Sandbox, "execute", slow_execute)
    tid = f"t-disc-{uuid.uuid4().hex[:6]}"
    disconnect = asyncio.Event()
    task, _messages = await _asgi_request(
        api_main.app, {"question": "上月销售额", "thread_id": tid}, disconnect)
    try:
        await asyncio.wait_for(started.wait(), timeout=15)
        assert len(cache._flights) == 1, "leader 在飞：flight 已登记"
        flight_key = next(iter(cache._flights))
        assert tid in store._resuming_threads, "在途标记已占"
        task_id = store.list(limit=5)[0]["id"]
        assert store.get(task_id)["status"] == "running"

        disconnect.set()               # 客户端断连（uvicorn → http.disconnect）
        await asyncio.wait_for(task, timeout=10)
        await _apoll(lambda: store.get(task_id)["status"] != "running")
        t = store.get(task_id)
        assert t["status"] == "done" and t["kind"] == "aborted", \
            f"断连必落终态（ASKBI-1 兜底）：{t['status']}/{t['kind']}"
        assert cache._flights == {}, "leader 取消 → executor finally 必拆 flight（AB-1①）"
        assert tid not in store._resuming_threads, "gen finally 必释放在途标记（AB-18）"
        outcome, _flight = cache.register_flight(flight_key)
        assert outcome == "leader", "同 key 后续请求可再竞选（无 flight 残留楔死）"
        cache.fail_flight(flight_key)
    finally:
        block.set()
        reset_query_cache()

    # 同 thread 可再次执行（标记已释放，不误拒后续正常请求）
    task2, messages2 = await _asgi_request(api_main.app, {"question": "上月销售额", "thread_id": tid})
    await asyncio.wait_for(task2, timeout=20)
    frames = _sse_frames(messages2)
    assert frames and frames[-1].get("type") == "final"


async def test_rt4_disc_mid_clarifier_thread_reusable_after_disconnect(store_env, monkeypatch):
    """攻击假设：澄清判定中途断连留下半毁 thread（checkpoint 停在 clarifier 待跑）
    ——后续 resume 是否错误恢复/崩溃。实测语义：任务落 aborted 终态、标记释放；
    resume 时 langgraph 从头重跑待跑节点（arun_with_retry 语义）→ 重新反问
    （clarify_pending）——thread 完整可复用，无错误状态续跑。"""
    monkeypatch.setattr(get_settings(), "clarify_suspend_enabled", True)
    monkeypatch.setattr(get_settings(), "cache_enabled", False)   # 显式关：不依赖环境态
    monkeypatch.setattr(api_main, "_SUSPEND_GRAPH",
                        build_graph(clarify_suspend=True, checkpointer=_LRUSaver(max_threads=16)))
    store = store_env
    block = asyncio.Event()
    entered = asyncio.Event()
    switch = {"slow": True}

    class SwitchLLM:
        async def chat(self, req):
            if switch["slow"]:
                entered.set()
                await block.wait()
                raise AssertionError("测试标记：应被断连取消，不应走到这里")
            return await MockLLM().chat(req)

    monkeypatch.setattr(nodes, "get_llm", lambda: SwitchLLM())
    tid = f"t-clar-disc-{uuid.uuid4().hex[:6]}"
    disconnect = asyncio.Event()
    task, _messages = await _asgi_request(
        api_main.app, {"question": "最近卖得怎么样", "thread_id": tid}, disconnect)
    try:
        # 等 LLM 进入挂起等待（gen 已启动且在节点内）再断连——过早断连会落进
        # AB-18 的"生成器未启动窗口"（finally 不跑，任务滞留 running 直至 TTL 收回）
        await asyncio.wait_for(entered.wait(), timeout=15)
        disconnect.set()
        await asyncio.wait_for(task, timeout=15)
        types = [f.get("type") for f in _sse_frames(_messages)]
        assert "final" not in types and "clarify_pending" not in types
        task_id = store.list(limit=5)[0]["id"]
        t = store.get(task_id)
        assert t["status"] == "done" and t["kind"] == "aborted"
        assert tid not in store._resuming_threads

        switch["slow"] = False          # 恢复正常 LLM 后 resume
        task2, messages2 = await _asgi_request(
            api_main.app, {"question": "看销售额", "thread_id": tid, "resume": True})
        await asyncio.wait_for(task2, timeout=20)
        frames2 = _sse_frames(messages2)
        assert frames2[-1].get("type") == "clarify_pending", \
            "中断的澄清节点重跑后重新挂起（无错误状态续跑、无崩溃）"
        assert frames2[-1].get("thread_id") == tid
    finally:
        block.set()


async def test_rt4_disc_after_clarify_pending_marker_released_and_resumable(client, suspend_env):
    """攻击假设：clarify_pending 帧下发后客户端断连——标记是否泄漏导致 resume
    恒 409（AB-18 修复前的原始缺陷形态）。实测：clarify_pending 早退路径的
    gen finally 释放标记 → 任务终态 clarify_pending → 之后 resume 正常完成出数。"""
    tid = f"t-cp-{uuid.uuid4().hex[:6]}"
    async with client.stream("POST", "/api/ask",
                             json={"question": "最近卖得怎么样", "thread_id": tid}) as resp:
        body = await resp.aread()
    assert '"clarify_pending"' in body.decode()
    assert suspend_env["store"]._resuming_threads == {}, "早退路径标记已释放"
    async with client.stream("POST", "/api/ask",
                             json={"question": "看销售额", "thread_id": tid, "resume": True}) as resp:
        body2 = await resp.aread()
    assert '"final"' in body2.decode(), "clarify_pending 后 resume 正常出数"


async def test_rt4_disc_in_flight_mutex_409_then_release_allows_rerun(store_env, monkeypatch):
    """断连窗口 × per-thread 互斥（AB-6）：第一个 run 在途（slow execute 持有）时
    同 thread 第二个请求 409；断连释放后第三个请求 200——互斥标记与断连清理
    联动正确，无永久 409 楔死。"""
    monkeypatch.setattr(get_settings(), "clarify_suspend_enabled", True)
    monkeypatch.setattr(get_settings(), "cache_enabled", False)   # 显式关：不依赖环境态
    monkeypatch.setattr(api_main, "_SUSPEND_GRAPH",
                        build_graph(clarify_suspend=True, checkpointer=_LRUSaver(max_threads=16)))
    started = asyncio.Event()
    block = asyncio.Event()

    async def slow_execute(self, sql):
        if disconnect.is_set():
            # 断连后的后续 run（r3）快速执行——本桩只模拟"断连前第一个 run 在途"
            return SimpleNamespace(columns=["value"], rows=[[1]], row_count=1,
                                   truncated=False, elapsed_ms=1.0)
        started.set()
        await block.wait()
        return SimpleNamespace(columns=["value"], rows=[[1]], row_count=1,
                               truncated=False, elapsed_ms=1.0)

    monkeypatch.setattr(Sandbox, "execute", slow_execute)
    tid = f"t-mutex-{uuid.uuid4().hex[:6]}"
    disconnect = asyncio.Event()
    task, _messages = await _asgi_request(
        api_main.app, {"question": "上月销售额", "thread_id": tid}, disconnect)
    try:
        await asyncio.wait_for(started.wait(), timeout=15)
        transport = httpx.ASGITransport(app=api_main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r2 = await c.post("/api/ask", json={"question": "上月销售额", "thread_id": tid})
            assert r2.status_code == 409, "在途窗口内同 thread 第二请求被互斥拒绝"

        disconnect.set()
        await asyncio.wait_for(task, timeout=10)
        transport = httpx.ASGITransport(app=api_main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r3 = await c.post("/api/ask", json={"question": "上月销售额", "thread_id": tid})
            assert r3.status_code == 200, "断连释放标记后同 thread 可再次执行"
    finally:
        block.set()


async def test_rt4_disc_storm_four_threads_no_residual(store_env, monkeypatch):
    """断连风暴 × singleflight：4 路同 key 并发（1 leader + 3 等待者）同时断连
    ——leader 取消拆 flight（executor finally）、等待者取消回收名额
    （_wait_flight BaseException 路径）、4 任务全部终态、标记全部释放、flight
    表收敛为空。任何一条残留即 AB-1①/ASKBI-7 回归。"""
    s = get_settings()
    monkeypatch.setattr(s, "clarify_suspend_enabled", True)
    monkeypatch.setattr(s, "cache_enabled", True)
    reset_query_cache()
    cache = get_query_cache()
    monkeypatch.setattr(api_main, "_SUSPEND_GRAPH",
                        build_graph(clarify_suspend=True, checkpointer=_LRUSaver(max_threads=16)))
    started = asyncio.Event()
    block = asyncio.Event()

    async def slow_execute(self, sql):
        started.set()
        await block.wait()
        return SimpleNamespace(columns=["value"], rows=[[1]], row_count=1,
                               truncated=False, elapsed_ms=1.0)

    monkeypatch.setattr(Sandbox, "execute", slow_execute)
    store = store_env
    tids = [f"t-storm-{uuid.uuid4().hex[:6]}-{i}" for i in range(4)]
    disconnects = [asyncio.Event() for _ in range(4)]
    pairs = []
    for tid, disc in zip(tids, disconnects):
        task, msgs = await _asgi_request(
            api_main.app, {"question": "上月销售额", "thread_id": tid}, disc)
        pairs.append((tid, task, disc, msgs))
    try:
        await asyncio.wait_for(started.wait(), timeout=15)
        # 等 4 路的 start 帧都下发（拿到 task_id 映射），再同时断连
        task_ids = {}
        for tid, _t, _d, msgs in pairs:
            await _apoll(lambda m=msgs: bool(_sse_frames(m)))
            frames = _sse_frames(msgs)
            assert frames[0]["type"] == "start"
            task_ids[tid] = frames[0]["task_id"]
        for _tid, _task, disc, _msgs in pairs:
            disc.set()
        await asyncio.gather(*(asyncio.wait_for(t, timeout=15)
                               for _tid, t, _d, _m in pairs))
        await _apoll(lambda: all(store.get(i)["status"] != "running" for i in task_ids.values()))
        statuses = [store.get(i)["kind"] for i in task_ids.values()]
        assert all(k == "aborted" for k in statuses), f"断连风暴全员终态 aborted：{statuses}"
        assert cache._flights == {}, "leader/等待者双取消路径后 flight 表必须为空"
        assert store._resuming_threads == {}, "4 个在途标记全部释放"
    finally:
        block.set()
        reset_query_cache()


# ===========================================================================
# 6. RT4-MCP：MCP 工具面契约
# ===========================================================================


def test_rt4_mcp_filters_over_max_rejected():
    """攻击假设（AB-7 MCP 直达面）：MCP 客户端直传超量 filters 绕过 API 层。
    防御：MAX_FILTERS=16 是 DSL 校验层约束——MCP 面同闸（handle_tool 走
    build_dsl），17 条 pydantic 校验即拒。"""
    filters = [{"field": "region", "op": "=", "value": "华南"} for _ in range(MAX_FILTERS + 1)]
    with pytest.raises(ValidationError) as ei:
        handle_tool("query_metric", {"metric": "order_count", "filters": filters})
    assert f"at most {MAX_FILTERS}" in str(ei.value) or "16" in str(ei.value)


def test_rt4_mcp_filters_16_boundary_accepted():
    """上界边界对照：恰 16 条合法 filters 正常编译执行（无误伤）。"""
    filters = [{"field": "region", "op": "=", "value": "华南"} for _ in range(MAX_FILTERS)]
    out = handle_tool("query_metric", {"metric": "order_count", "dimensions": ["region"],
                                       "filters": filters, "limit": 10})
    d = json.loads(out)
    assert "sql" in d and d["row_count"] >= 0


def test_rt4_mcp_filter_value_injection_escaped_no_extra_rows():
    """攻击假设：MCP filters value 直达 SQL 字符串面（引号逃逸注入）。防御：
    compile_sql 的 _q 统一单引号转义——注入串变普通字符串字面量，匹配不到任何
    行（零数据外泄），guard 二层兜底。"""
    out = handle_tool("query_metric", {"metric": "order_count", "dimensions": ["region"],
                                       "filters": [{"field": "region", "op": "=",
                                                    "value": "华南' OR '1'='1"}],
                                       "limit": 100})
    d = json.loads(out)
    assert "华南'' OR" in d["sql"], "值面统一转义（纵深：_q 转义 + guard）"
    assert d["row_count"] == 0, "注入值匹配不到任何行——无绕过方向"


def test_rt4_mcp_masked_field_filter_rejected():
    """攻击假设：MCP 客户端用 filters 直取掩码列。防御：过滤字段白名单（DSL 层）
    miss 即 ValidationError——掩码列在 MCP 面同样不可达。"""
    with pytest.raises(ValidationError, match="未知过滤字段"):
        handle_tool("query_metric", {"metric": "order_count",
                                     "filters": [{"field": "phone", "op": "=", "value": "1"}]})


def test_rt4_mcp_time_field_injection_rejected():
    """攻击假设：time.start 携带 SQL 片段（ASK-1 修复前的历史直拼面）。防御：
    YYYY-MM fullmatch 白名单——任何注入形态校验即拒。"""
    with pytest.raises(ValidationError, match="YYYY-MM"):
        handle_tool("query_metric", {"metric": "order_count",
                                     "time": {"mode": "between",
                                              "start": "2026-01'; DROP TABLE orders--",
                                              "end": "2026-09"}})


def test_rt4_mcp_limit_clamped_bounded():
    """攻击假设：limit=10^9 撑爆结果面/计费面。防御：_check_limit 钳制 [1,200]，
    编译期 LIMIT 注入——MCP 面与 API 面同口径。"""
    out = handle_tool("query_metric", {"metric": "order_count", "dimensions": ["region"],
                                       "limit": 10**9})
    d = json.loads(out)
    assert d["sql"].rstrip().endswith("LIMIT 200")
    assert d["row_count"] <= 200


def test_rt4_mcp_limit_default_bounded():
    """缺省 limit 也受钳制（handle_tool or-50 兜底 + MAX_LIMIT 钳制）。"""
    out = handle_tool("query_metric", {"metric": "order_count", "dimensions": ["region"]})
    d = json.loads(out)
    assert d["sql"].rstrip().endswith("LIMIT 50")


def test_rt4_mcp_unknown_tool_valueerror():
    """未知工具名：ValueError（MCP 传输层转 tool error），不进入任何查询路径。"""
    with pytest.raises(ValueError, match="未知工具"):
        handle_tool("read_file", {"path": "/etc/passwd"})


def test_rt4_mcp_missing_metric_keyerror_pinned():
    """口径钉：inputSchema required=["metric"] 在传输层校验；core.handle_tool
    直调面缺 metric → KeyError 原样抛出（MCP 库转 tool error）——core 不做
    自由文本错误整形（错误整形在传输层职责边界之外，观察项口径如实留档）。"""
    with pytest.raises(KeyError):
        handle_tool("query_metric", {})


def test_rt4_mcp_execute_sync_no_timeout_pinned(tmp_path, monkeypatch):
    """口径钉（review_report_5 AB-4，观察项未修）：MCP 面走 execute_sync——
    无 timeout 参数、无 wait_for/interrupt：慢查询挂多久算多久，兜底=stdio
    传输的进程级纪律与客户端取消。对照 API 面 Sandbox.execute 有 sql_timeout
    中断——两层资源策略不同源（口径如实留档，不伪装成有超时）。"""
    import app.data.sandbox as sandbox_mod

    hold = threading.Event()

    class StuckConn:
        def execute(self, sql):
            hold.wait(timeout=10)          # 兜底释放防线程泄漏
            raise RuntimeError("released by test")

        def interrupt(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(sandbox_mod.duckdb, "connect", lambda *a, **kw: StuckConn())
    holder: dict = {}

    def run():
        try:
            execute_sync("SELECT 1", db_path=tmp_path / "fake.duckdb")
        except BaseException as e:  # noqa: BLE001
            holder["exc"] = e

    th = threading.Thread(target=run, daemon=True)
    th.start()
    time.sleep(0.5)
    assert th.is_alive(), "execute_sync 若有超时会在 0.5s 内返回/抛错（AB-4 口径=无超时）"
    hold.set()
    th.join(timeout=5)
    assert isinstance(holder.get("exc"), RuntimeError)


def test_rt4_mcp_readonly_tools_no_input_echo():
    """攻击假设：list_metrics/describe_schema 的入参被回显（注入面）。防御：
    两工具为纯字典静态内容、不消费入参——敌意参数输出逐字节与空参一致。"""
    hostile = {"metric": "<script>alert(1)</script>", "x": "\u202e"}
    a = json.loads(handle_tool("list_metrics", hostile))
    b = json.loads(handle_tool("list_metrics", {}))
    assert a == b, "只读工具不回显入参（无注入面）"
    c = json.loads(handle_tool("describe_schema", hostile))
    assert set(c) == {"dimensions", "time_range"}
    assert "<script>" not in json.dumps(c, ensure_ascii=False)


# ===========================================================================
# 7. RT4-FAULT：故障 × 并发风暴叠加
# ===========================================================================


class _AlwaysFailLLM:
    calls = 0

    async def chat(self, req):
        _AlwaysFailLLM.calls += 1
        raise LLMError("注入：连接被重置（模拟服务不可用）")


async def test_rt4_fault_llm_down_samekey_storm_all_refuse(db_path, monkeypatch):
    """fault_inject S2 × 同 key 8 路风暴（cache_enabled）：LLM 故障发生在 executor
    之前——flight 表零登记（无 claim 窗口可楔死）、全部诚实拒答（"查询生成服务
    暂时不可用"）、零异常穿透。fault_inject 串行场景的风暴化红测。"""
    s = get_settings()
    monkeypatch.setattr(s, "cache_enabled", True)
    reset_query_cache()
    cache = get_query_cache()
    monkeypatch.setattr(nodes, "get_llm", lambda: _AlwaysFailLLM())
    try:
        results = await asyncio.gather(
            *(run_ask("上月销售额是多少") for _ in range(8)), return_exceptions=True)
        crashes = [r for r in results if isinstance(r, BaseException)]
        assert crashes == [], f"风暴下零异常穿透：{crashes!r}"
        kinds = [(r.get("answer") or {}).get("kind") for r in results]
        assert kinds == ["refusal"] * 8
        messages = {(r.get("answer") or {}).get("message") for r in results}
        assert any("暂时不可用" in m for m in messages), f"诚实拒答文案：{messages}"
        assert cache._flights == {}, "LLM 面故障不进 executor——flight 零登记零残留"
    finally:
        reset_query_cache()


async def test_rt4_fault_db_down_storm_version_failopen_all_refuse(db_path, monkeypatch):
    """fault_inject S5 × AB-19 × 同 key 8 路风暴：沙箱库不可用（执行失败进修复
    循环）+ 版本探测失败（缺库/文件锁 OSError）→ 哨兵 fail-open：get 命中一律
    作废、set 跳过写入（stores 不增）、version_probe_failures 逐次留痕；flight
    表在失败链反复竞选后收敛为空；8 路全部"查询连续执行失败"诚实拒答，零异常
    穿透、零 duckdb 原文出 SSE（W2 AB-19 修复的风暴化红测）。"""
    s = get_settings()
    monkeypatch.setattr(s, "cache_enabled", True)
    reset_query_cache()
    cache = get_query_cache()
    cache._version_ttl_seconds = 0.0   # 单测语义：探测不记忆化（全局单例在 finally 整体重建）

    async def boom(self, sql):
        raise OSError("注入：库文件缺失/被锁")

    def probe_fail():
        raise OSError("注入：版本探测失败（缺库）")

    try:
        # 预热：健全期先落一条缓存条目（让 get 命中路径存在，探测失败才可观测）
        warm = await run_ask("上月销售额是多少")
        assert (warm.get("answer") or {}).get("kind") == "answer"
        stores_before = cache.stats()["stores"]
        assert stores_before >= 1, "预热 run 必须写入缓存条目（get 命中路径的前提）"

        # 注入故障：沙箱库不可用 + 版本探测失败
        monkeypatch.setattr(Sandbox, "execute", boom)
        cache._version_provider = probe_fail   # 全局单例，测试后整体重建（finally reset）
        results = await asyncio.gather(
            *(run_ask("上月销售额是多少") for _ in range(8)), return_exceptions=True)
        crashes = [r for r in results if isinstance(r, BaseException)]
        assert crashes == [], f"故障×风暴零异常穿透：{crashes!r}"
        kinds = [(r.get("answer") or {}).get("kind") for r in results]
        assert kinds == ["refusal"] * 8
        msgs = {(r.get("answer") or {}).get("message") or "" for r in results}
        assert any("查询连续执行失败" in m for m in msgs)
        assert not any("duckdb" in m.lower() for m in msgs), \
            "duckdb 原文不得穿透应答面（降级矩阵）"
        st = cache.stats()
        assert st["version_probe_failures"] >= 8, f"探测失败逐次留痕：{st}"
        assert st["stores"] == stores_before, "写路径同样跳过缓存（探测失败 → stores 冻结）"
        assert cache._flights == {}, "失败链后 flight 表收敛为空（无残留楔死）"
    finally:
        reset_query_cache()


def test_rt4_fault_probe_failure_storm_cache_level():
    """AB-19 缓存层风暴化：探测中途转失败 + 8 线程 × get/set 交错——零异常穿透、
    命中一律作废（fail-open 误失效方向）、stores 冻结在健全期条目、失败逐次留痕。"""
    state = {"ok": True}

    def provider():
        if not state["ok"]:
            raise OSError("database is locked")
        return "v1"

    c = QueryCache(version_provider=provider, enabled=True, version_ttl_seconds=0.0)
    c.set("seed", dict(_SF_RESULT))            # 健全期：条目已落
    assert c.get("seed") is not None
    state["ok"] = False                        # 注入故障（缺库/文件锁）
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            for _ in range(25):
                assert c.get("seed") is None   # 命中一律作废（误失效方向）
                c.set(f"k{i}-{_}", dict(_SF_RESULT))   # 写路径跳过，不抛
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert errors == [], f"故障风暴零异常穿透：{errors!r}"
    st = c.stats()
    assert st["version_probe_failures"] >= 8 * 25, "每线程每轮 get+set 各留痕一次"
    assert st["stores"] == 1, "故障期 stores 冻结（写路径全部跳过）"
    assert c._flights == {}


def test_rt4_fault_leader_hang_storm_dual_fail_open_accounting():
    """故障（leader 挂死，永不 publish/fail）× 风暴（8 等待者 + 名额上限 3）：
    等待者超时 fail-open（timeouts==3）与名额超限 fail-open（overflow==5）两条
    路径同场留痕，8 路全部拿到 ("miss", None) 自行执行——执行责任不丢；迟到
    publish 照常清理 flight（不楔死）。"""
    c = QueryCache(version_provider=lambda: "v1", enabled=True, version_ttl_seconds=0.0,
                   flight_wait_seconds=0.3, max_flight_waiters=3)
    assert c.claim_flight("k") == ("leader", None)   # leader 挂死（测试内永不收尾）
    outcomes: list[tuple] = []

    def waiter():
        outcomes.append(c.claim_flight("k", timeout=0.3))

    threads = [threading.Thread(target=waiter) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert all(o == ("miss", None) for o in outcomes), f"全员 fail-open：{outcomes}"
    st = c.stats()
    assert st["singleflight_timeouts"] == 3
    assert st["singleflight_overflow"] == 5
    c.publish_flight("k", dict(_SF_RESULT))          # 迟到 publish
    assert c._flights == {}, "迟到 publish 清理 flight（不楔死）"
    assert c.get("k") is not None, "迟到 publish 结果照常入缓存"


# ===========================================================================
# 8. RT4-ESC：掩码列转义书写（AB-8 观察项——负向不变量）
# ===========================================================================

_U_ESC_DEFAULT = r'SELECT U&"p\0068one" FROM customers'   # \0068 = 'h' → 意图解码出 phone


def test_rt4_esc_sqlglot_rewrites_u_ampersand_to_bitwise_and():
    """机制钉：sqlglot 30.33 不建模 duckdb 的 U&"" Unicode 转义标识符——解析产物
    为位与表达式 `U & "p\\0068one"`。转义通道在 AST/标识符层**不存在解码形态**，
    这是下方两层负向不变量的机制基础。"""
    tree = sqlglot.parse_one(_U_ESC_DEFAULT, read="duckdb")
    assert isinstance(tree, exp.Select)
    assert "U & " in tree.sql(), f"sqlglot 归一为位与表达式：{tree.sql()!r}"


def test_rt4_esc_guard_text_scan_blind_spot_acknowledged():
    """AB-8 攻击面如实钉：掩码列守卫是文本扫描——U& 转义书写不产生 'phone' 子串，
    guard 放行（盲区承认，与 review_report_5 AB-8 记录一致）。放行无害的前提由
    下两条用例钉死：解码在任何层都不发生。"""
    safe = guard(_U_ESC_DEFAULT)
    assert "LIMIT" in safe


@pytest.mark.parametrize("payload", [
    r'SELECT U&"p\0068one" FROM customers',        # U& 默认转义符
    "SELECT U&'p\\0068one' FROM customers",        # U& 单引号变体
    r'SELECT "p\0068one" FROM customers',          # 纯引号怪名（无 U&，字面形态）
])
async def test_rt4_esc_engine_refuses_every_escape_spelling(db_path, payload):
    """负向不变量：任何转义书写都不可能解出掩码列 phone——guard 文本层盲区之外，
    引擎层按字面标识符绑定失败（BinderException fail-closed），零数据泄漏。"""
    safe = guard(payload)
    with pytest.raises(duckdb.BinderException, match="Referenced column"):
        execute_sync(safe, db_path=db_path)


def test_rt4_esc_uescape_variant_parse_rejected():
    """UESCAPE 自定义转义符变体：sqlglot 直接解析失败 → 解析层拒绝（比引擎层
    更早收口）。"""
    with pytest.raises(SqlRejected, match="解析层"):
        guard(r'SELECT U&"p&0068one" UESCAPE "&" FROM customers')
