"""图节点实现。

分工原则（DESIGN.md 的确定性/不确定性决策表）：
- router / executor / finalizer / QA数值核验 / 归因计算 = 纯代码
- clarifier / sql_agent(DSL生成) / insight(叙述) / qa(语义判断) = LLM
每个 LLM 调用都记 token 用量；每个节点都产出 trace 事件。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from ..cache import align_columns, fingerprint, get_query_cache
from ..config import get_settings
from ..data.analytics import drill_down
from ..data.dsl import (
    CompileError,
    build_dsl,
    compile_sql,
    validate_filter_values,
)
from ..data.registry import get_spec
from ..data.schema import schema_prompt
from ..llm.base import ChatRequest, LLMError, LLMResponse
from ..llm.factory import get_llm
from ..rag import get_example_store
from .state import GraphState

# ---------------- 工具函数 ----------------


def _event(state: GraphState, node: str, type_: str, data: Any = None) -> dict:
    return {"ts": round(time.time(), 3), "node": node, "type": type_, "data": data}


def _record_usage(state: GraphState, node: str, resp: LLMResponse) -> dict:
    usage = dict(state.get("usage") or {})
    cur = usage.get(node, {"prompt_tokens": 0, "completion_tokens": 0})
    usage[node] = {
        "prompt_tokens": cur["prompt_tokens"] + resp.usage.prompt_tokens,
        "completion_tokens": cur["completion_tokens"] + resp.usage.completion_tokens,
    }
    return usage


def _merge_events(state: GraphState, *events: dict) -> list[dict]:
    return list(state.get("events") or []) + list(events)


async def _call_llm(node: str, system: str, payload: dict, json_mode: bool = True) -> LLMResponse:
    req = ChatRequest(
        node=node, system=system,
        user=json.dumps(payload, ensure_ascii=False, default=str),
        json_mode=json_mode,
    )
    return await get_llm().chat(req)


def _loose_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1].rsplit("```", 1)[0]
    return json.loads(t)


# ---------------- 1. router（纯代码：规则优先，省一次 LLM 调用） ----------------

_WHY_WORDS = ("为什么", "原因", "为何", "下滑", "增长", "怎么回事", "怎么回事", "分析下", "分析一下")


def route_node(state: GraphState) -> dict:
    q = state["question"]
    # ASK-10 修复（评审 2.0）：payload.depth 曾从未接线——显式传值优先于关键词推断（评测用）
    explicit = state.get("depth")
    if explicit in ("light", "full"):
        return {
            "depth": explicit,
            "route_reason": f"请求显式指定 depth={explicit}（跳过自动路由）",
            "status": "routed",
            "events": _merge_events(state, _event(state, "router", "route",
                                                  {"depth": explicit, "reason": "explicit"})),
        }
    heavy = bool(re.search(r"(为什么|原因|为何|趋势|走势|对比|vs|各|每个|按)", q)) or any(
        w in q for w in _WHY_WORDS
    )
    depth = "full" if heavy else "light"
    reason = (
        "包含归因/趋势/对比语义，走完整图（含下钻与质检修订）"
        if heavy else "简单聚合查询，走轻量链路（跳过下钻叙述）"
    )
    return {
        "depth": depth,
        "route_reason": reason,
        "status": "routed",
        "events": _merge_events(state, _event(state, "router", "route", {"depth": depth, "reason": reason})),
    }


# ---------------- 2. clarifier（LLM：歧义澄清 / 越界拒答） ----------------


async def clarifier_node(state: GraphState) -> dict:
    spec = get_spec(get_settings().dataset)
    system = (
        "你是数据分析助手的意图澄清器。对用户问题判断：\n"
        "1) clear：指标与时间范围都可确定，可直接查询；\n"
        "2) clarify：关键信息歧义（如指标未指明），需反问，给出 2-4 个候选项；\n"
        "3) refuse：属于以下任一情形——\n"
        "   a) 预测未来（预测/预计/未来/下个月/下季度/明年 等）；\n"
        "   b) 所问指标在数据集中不存在（满意度/复购率/留存/评分/投诉/物流/库存 等）"
        "——宁可拒答也不编造或反问；\n"
        "   c) 不是数据查询的动作类请求（写/生成/翻译/推荐/评价/总结报告 等）；\n"
        "   d) 涉及数据集完全不存在的业务域（竞品/员工/财务凭证 等）。\n"
        "判定顺序：先过 refuse 四类，再判 clarify，最后 clear；介于两者之间时倾向 refuse（宁缺毋滥）。\n"
        "示例：「预计最近的销售趋势」→refuse(a)；「客户满意度怎么样」→refuse(b)；"
        "「写一段上月销售总结」→refuse(c)；「哪个渠道比较好」→clarify；「上月华南毛利率」→clear。\n"
        "重要约定：相对时间（上月/本月/最近N个月）以数据截止月 2026-09 为基准，"
        "属于 clear，绝不为相对时间反问；只对『指标不明』反问。\n"
        '严格返回 JSON: {"verdict":"clear|clarify|refuse","reason":"...","clarify_question":"...","options":["..."]}'
    )
    payload = {"question": state["question"], "schema": schema_prompt(spec), "dataset": spec.name}
    try:
        resp = await _call_llm("clarifier", system, payload)
    except LLMError as e:
        # 澄清服务不可用 → 默认放行走主链路（宁可下游多一次校验，不阻塞用户）
        return {
            "status": "clarified",
            "events": _merge_events(
                state, _event(state, "clarifier", "llm_unavailable", {"error": str(e), "fallback": "clear"})
            ),
        }
    try:
        verdict = _loose_json(resp.content)
    except Exception:  # noqa: BLE001  LLM 输出形态不可枚举，解析失败按设计降级为放行
        verdict = {"verdict": "clear", "reason": "解析失败默认放行"}
    v = verdict.get("verdict", "clear")
    update: dict = {"usage": _record_usage(state, "clarifier", resp)}
    if v == "clarify":
        update.update(
            clarify={
                "question": verdict.get("clarify_question") or "能补充一下你想看的具体指标或时间吗？",
                "options": verdict.get("options") or [],
            },
            clarify_reason=verdict.get("reason", ""),
            status="needs_clarification",
            events=_merge_events(state, _event(state, "clarifier", "clarify", verdict)),
        )
    elif v == "refuse":
        update.update(
            refusal=verdict.get("reason") or "该问题超出数据集能力范围",
            status="refused",
            events=_merge_events(state, _event(state, "clarifier", "refuse", verdict)),
        )
    else:
        update.update(
            status="clarified",
            events=_merge_events(state, _event(state, "clarifier", "clear", {"reason": verdict.get("reason", "")})),
        )
    return update


# ---------------- 3. sql_agent（LLM 产 DSL → 编译器产 SQL） ----------------


async def sql_agent_node(state: GraphState) -> dict:
    from ..data.metrics import metrics_prompt

    spec = get_spec(get_settings().dataset)
    examples = get_example_store().search(state["question"], get_settings().rag_topk)
    system = (
        "你是 NL2DSL 引擎：把业务问题转为查询 DSL（JSON）。只允许使用 schema 中的维度与"
        "指标字典中的指标 id，禁止编造。\n"
        "规则：\n"
        "- 时间默认 all；『上月』= last_n_months n=1；『最近N个月』= last_n_months n=N。\n"
        "- limit 约定：仅在用户明确说『前N/TopN』时设置；『哪个X最…』『最高的X』『排名』"
        "都不设 limit——返回全部成员并按 order_by 排序，由排序表达意图。\n"
        "- 『哪个X最…』为 topn 且 dimensions 含 X；『趋势』dimensions 含 month。\n"
        "- 非分析请求/数据集不存在的维度值，返回 {\"error\": \"...\"} 说明原因。\n"
        f"{schema_prompt(spec)}\n{metrics_prompt(spec)}\n"
        '返回 DSL JSON：{"intent":"aggregate|breakdown|topn|trend|compare","metric":"id",'
        '"dimensions":[],"filters":[{"field":"","op":"=","value":""}],'
        '"time":{"mode":"all|last_n_months|between","n":1,"start":"","end":""},'
        '"order_by":{"metric":"id","direction":"desc"},"limit":5}'
    )
    payload = {
        "question": state["question"],
        "mode": "repair" if state.get("sql_error") else "generate",
        "examples": [{"question": q, "dsl": d} for q, d, _s in examples],
        "error": state.get("sql_error"),
        "dataset": spec.name,
    }
    try:
        resp = await _call_llm("sql_agent", system, payload)
    except LLMError as e:
        # 生成服务不可用 → 诚实拒答（不带出编造 DSL，也不 500）
        return {
            "refusal": "查询生成服务暂时不可用，请稍后重试",
            "status": "refused",
            "events": _merge_events(
                state, _event(state, "sql_agent", "llm_unavailable", {"error": str(e)})
            ),
        }
    usage = _record_usage(state, "sql_agent", resp)
    try:
        raw = _loose_json(resp.content)
    except Exception:  # noqa: BLE001  LLM 输出形态不可枚举，解析失败转结构化报错走拒答
        raw = {"error": "DSL 输出无法解析"}

    if "error" in raw:
        return {
            "usage": usage,
            "refusal": f"无法将问题转为合规查询：{raw['error']}",
            "status": "refused",
            "events": _merge_events(state, _event(state, "sql_agent", "refuse", raw)),
        }
    try:
        dsl = build_dsl(raw, spec)
    except Exception as e:  # noqa: BLE001
        # 未知指标/维度/字段 → 拒答（数据集不支持）；其余校验错 → 带错误重试一次
        if "未知指标" in str(e) or "未知维度" in str(e) or "未知过滤字段" in str(e):
            return {
                "usage": usage,
                "refusal": f"数据集不支持该查询：{e}",
                "status": "refused",
                "events": _merge_events(state, _event(state, "sql_agent", "refuse", {"error": str(e)})),
            }
        retry_payload = dict(payload)
        retry_payload["error"] = f"上一次输出未通过校验: {e}"
        try:
            resp2 = await _call_llm("sql_agent", system, retry_payload)
        except LLMError as e2:
            return {
                "usage": usage,
                "refusal": "查询生成服务暂时不可用，请稍后重试",
                "status": "refused",
                "events": _merge_events(
                    state, _event(state, "sql_agent", "llm_unavailable", {"error": str(e2)})
                ),
            }
        usage = _record_usage({**state, "usage": usage}, "sql_agent", resp2)
        try:
            dsl = build_dsl(_loose_json(resp2.content), spec)
        except Exception as e2:  # noqa: BLE001
            return {
                "usage": usage,
                "refusal": f"DSL 连续两次未通过校验：{e2}",
                "status": "refused",
                "events": _merge_events(state, _event(state, "sql_agent", "refuse", {"error": str(e2)})),
            }

    fv_problems = validate_filter_values(dsl, spec)
    if fv_problems:
        return {
            "usage": usage,
            "refusal": "；".join(fv_problems) + "（拒答：宁缺毋滥，不用编造数据回答）",
            "status": "refused",
            "events": _merge_events(state, _event(state, "sql_agent", "refuse", {"problems": fv_problems})),
        }

    try:
        sql, caliber = compile_sql(dsl, role=state.get("role", "boss"), spec=spec)
    except CompileError as e:
        return {
            "usage": usage, "refusal": f"编译失败：{e}", "status": "refused",
            "events": _merge_events(state, _event(state, "sql_agent", "compile_error", {"error": str(e)})),
        }
    return {
        "usage": usage,
        "dsl": dsl.model_dump(),
        "sql": sql,
        "caliber": caliber,
        "sql_error": None,
        "status": "sql_ready",
        "events": _merge_events(
            state,
            _event(state, "sql_agent", "dsl", dsl.model_dump()),
            _event(state, "sql_agent", "sql", {"sql": sql}),
        ),
    }


# ---------------- 4. executor（纯代码：沙箱执行） ----------------


async def executor_node(state: GraphState) -> dict:
    from ..data.sandbox import Sandbox

    spec = get_spec(get_settings().dataset)
    # 查询缓存（E4③，cache_enabled 默认 False）：同 DSL 指纹命中直接回放，不落沙箱。
    # 键含 role（行级权限改变结果集）与数据集标识（同 DSL 不同源不可跨缓存）；
    # 关闭时此分支整体跳过，行为与无缓存完全一致。
    cache = get_query_cache()
    cache_key = None
    if cache.enabled and state.get("dsl") and state.get("sql"):
        cache_key = fingerprint(state["dsl"], state.get("role", "boss"), spec.name)
        # ASK-4 修复（评审 2.0）：缓存 get/set 曾在事件循环线程同步跑 DuckDB——
        # data_version 探测为四聚合全表扫描，每 30s 窗口到期后冻结全部在途请求。
        # 挪 to_thread（与同库 IO 的 Sandbox 惯例对齐）。
        cached = await asyncio.to_thread(cache.get, cache_key)
        if cached is not None:
            result = {**align_columns(cached, state["dsl"].get("dimensions") or []), "from_cache": True}
            return {
                "result": result,
                "sql_error": None,
                "status": "executed",
                "events": _merge_events(
                    state,
                    _event(state, "executor", "cache_hit", {
                        "key": cache_key[:12], "row_count": result.get("row_count"),
                    }),
                ),
            }
    try:
        res = await Sandbox(db_path=spec.db_abs_path).execute(state["sql"])
    except Exception as e:  # noqa: BLE001
        rc = state.get("repair_count", 0) + 1
        over = rc > get_settings().max_sql_repairs
        return {
            "sql_error": str(e),
            "repair_count": rc,
            "refusal": f"查询连续执行失败：{e}" if over else None,
            "status": "sql_failed" if not over else "refused",
            "events": _merge_events(state, _event(state, "executor", "error", {"error": str(e), "repair": rc})),
        }
    result = {
        "columns": res.columns,
        "rows": res.rows,
        "row_count": res.row_count,
        "truncated": res.truncated,
        "elapsed_ms": res.elapsed_ms,
    }
    # 聚合函数在空集上返回一行 NULL：规范化为"无数据"（权限范围外/无符合条件数据）
    if (
        len(res.columns) == 1
        and len(res.rows) == 1
        and res.rows[0][0] is None
    ):
        result.update(rows=[], row_count=0, note="空结果（可能为权限范围外或无符合条件的数据）")
    if cache_key is not None:
        await asyncio.to_thread(cache.set, cache_key, result)  # 空结果同样缓存：权限范围外的问题重复问不必反复执行
    return {
        "result": result,
        "sql_error": None,
        "status": "executed",
        "events": _merge_events(
            state,
            _event(state, "executor", "result", {
                "row_count": res.row_count, "elapsed_ms": res.elapsed_ms,
                "preview": res.rows[:5], "truncated": res.truncated,
            }),
            *(
                [_event(state, "executor", "cache_store", {"key": cache_key[:12]})]
                if cache_key else []
            ),
        ),
    }


# ---------------- 5. insight（代码算事实 + LLM 解读 + 图表） ----------------

_WHY_RE = re.compile(r"(为什么|原因|为何|下滑|增长|怎么回事|分析)")


async def insight_node(state: GraphState) -> dict:
    spec = get_spec(get_settings().dataset)
    dsl = build_dsl(state["dsl"], spec)
    needs_drill = bool(_WHY_RE.search(state["question"])) or dsl.intent == "trend"

    if needs_drill and state.get("depth") == "full":
        facts = (await drill_down(dsl, state.get("role", "boss"), spec)).as_context()
    else:
        rows = state["result"]["rows"]
        facts = {
            "metric": spec.metrics[dsl.metric].name,
            "top_rows": [
                dict(zip(state["result"]["columns"], r)) for r in rows[:5]
            ],
            "total_rows": state["result"]["row_count"],
        }

    system = (
        "你是数据解读器。你只会拿到『代码已计算好的事实』，任务是把事实讲成人话。\n"
        "铁律：禁止出现事实之外的任何数字；禁止自己计算新数字（增长率/倍数/差值等一律不许，"
        "只引用给定事实中已有的数值）；禁止使用『万/亿』等单位改写数字；"
        "禁止给出投资/经营建议，只描述数据。输出 3-6 句中文。"
    )
    try:
        resp = await _call_llm(
            "insight", system,
            {
                "question": state["question"],
                "facts": facts,
                "revision_feedback": state.get("revision_feedback"),  # QA 打回时的整改要求
            },
            json_mode=False,
        )
    except LLMError as e:
        # 解读服务不可用 → 保留数据表与图表，叙述留空（诚实降级，不编解读）
        chart = _build_chart(state)
        return {
            "facts": facts,
            "narrative": "",
            "chart": chart,
            "status": "insight_degraded",
            "events": _merge_events(
                state,
                _event(state, "insight", "llm_unavailable", {"error": str(e), "fallback": "table_only"}),
                _event(state, "insight", "facts_ready", {
                    "drill_down": needs_drill and state.get("depth") == "full",
                }),
            ),
        }
    chart = _build_chart(state)
    return {
        "facts": facts,
        "narrative": resp.content.strip(),
        "chart": chart,
        "usage": _record_usage(state, "insight", resp),
        "status": "insighted",
        "events": _merge_events(
            state,
            _event(state, "insight", "facts_ready", {
                "drill_down": needs_drill and state.get("depth") == "full",
            }),
            _event(state, "insight", "narrative", {"text": resp.content.strip()[:200]}),
        ),
    }


def _build_chart(state: GraphState) -> dict | None:
    result = state.get("result") or {}
    cols, rows = result.get("columns", []), result.get("rows", [])
    if not cols or not rows:
        return None
    dims = [c for c in cols[:-1]] or []
    if not dims:
        return None
    dim = dims[0]
    if dim == "month":
        return _vega("line", {"field": dim, "type": "temporal", "timeUnit": "yearmonth"}, rows, dim)
    top = rows[:15][::-1]
    return _vega("bar", {"field": dim, "type": "nominal"}, top, dim)


def _vega(mark: str, x_enc: dict, rows: list, dim: str) -> dict:
    data = [{dim: r[0], "value": r[-1]} for r in rows]
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "mark": mark, "data": {"values": data},
        "encoding": {
            "x": x_enc,
            "y": {"field": "value", "type": "quantitative"},
            "tooltip": [{"field": dim}, {"field": "value"}],
        },
    }


# ---------------- 6. qa（代码数值核验 + LLM 语义核验 → 修订循环） ----------------

_NUM_RE = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)\s*(%?)")
# 日期（2026-09 / 2026年9月）与时长（近12个月 / 6个月）不参与数值核验：
# 它们是时间坐标/量词，不是业务数值
_DATE_RE = re.compile(r"\d{4}[-/年]\d{1,2}(?:[-/月]\d{1,2}日?)?")
_DUR_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:个?月|年|天|日|小时|分钟|周|季度)")


def _collect_numbers(value: Any, out: set[float]) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        out.add(round(float(value), 4))
        out.add(round(float(value), 2))
    elif isinstance(value, dict):
        for v in value.values():
            _collect_numbers(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _collect_numbers(v, out)


def _match_allowed(val: float, allowed: set[float], is_pct: bool) -> bool:
    """数值匹配：带 % 的 token 只对百分比域，否则只对原值域，杜绝跨域误配。

    - 百分比域容差 max(0.061, |c|×1.1%)：放过 0.3%↔0.35% 这类 1 位小数舍入；
    - 原值域容差 max(0.005, |c|×1.1%)：足以放过千分位/4 位小数表达，
      拦得住 8% 量级的数值篡改（如 0.5006 → 0.5406）。
    """
    floor = 0.061 if is_pct else 0.005
    for a in allowed:
        cands = (a * 100, -a * 100) if is_pct else (a, -a)
        for c in cands:
            if abs(val - c) <= max(floor, abs(c) * 0.011):
                return True
    return False


def check_numbers(narrative: str, result: dict, facts: dict) -> tuple[list[str], int]:
    """确定性核验：叙述中的每个数字必须能在结果集/事实中找到（含百分比换算）。"""
    allowed: set[float] = set()
    _collect_numbers(result.get("rows", []), allowed)
    _collect_numbers(facts, allowed)
    if result.get("row_count") is not None:
        _collect_numbers(result["row_count"], allowed)

    issues: list[str] = []
    checked = 0
    clean = _DUR_RE.sub(" ", _DATE_RE.sub(" ", narrative))
    for tok, pct in _NUM_RE.findall(clean):
        val = float(tok.replace(",", ""))
        checked += 1
        if not _match_allowed(val, allowed, bool(pct)):
            issues.append(f"叙述中的数字 {tok}{'%' if pct else ''} 未出现在查询结果或计算事实中（疑似编造）")
    return issues, checked


async def qa_node(state: GraphState) -> dict:
    det_issues, checked = check_numbers(
        state.get("narrative", ""), state.get("result", {}), state.get("facts", {})
    )
    system = (
        "你是质检员。判断叙述是否：(1)与数据事实一致；(2)未夹带数据外的结论或建议；"
        "(3)口径表述正确。只回答 JSON：{\"passed\":bool,\"issues\":[\"...\"]}"
    )
    try:
        resp = await _call_llm(
            "qa", system,
            {
                "question": state["question"],
                "narrative": state.get("narrative", ""),
                "deterministic_issues": det_issues,
            },
        )
        try:
            verdict = _loose_json(resp.content)
        except Exception:  # noqa: BLE001  LLM 输出形态不可枚举，解析失败退回确定性结论
            verdict = {"passed": not det_issues, "issues": det_issues}
    except LLMError:
        # 质检 LLM 不可用 → 退回纯确定性结论（数值核验仍然生效，语义判断缺席要如实呈现）
        resp = None
        verdict = {"passed": not det_issues, "issues": det_issues}

    passed = (not det_issues) and bool(verdict.get("passed", True))
    report = {
        "passed": passed,
        "deterministic_issues": det_issues,
        "llm_issues": verdict.get("issues", []),
        "numbers_checked": checked,
        "revision": state.get("revision_count", 0) + (0 if passed else 1),
    }
    update = {
        "qa_report": report,
        "usage": _record_usage(state, "qa", resp) if resp is not None else dict(state.get("usage") or {}),
        "status": "qa_passed" if passed else "qa_failed",
        "events": _merge_events(state, _event(state, "qa", "verdict", report)),
    }
    if not passed:
        update["revision_count"] = state.get("revision_count", 0) + 1
        update["revision_feedback"] = "; ".join(det_issues + verdict.get("issues", []))
    return update


# ---------------- 7. finalizer（纯代码：组装 + 降级徽章） ----------------


def finalizer_node(state: GraphState) -> dict:
    spec = get_spec(get_settings().dataset)
    if state.get("refusal"):
        answer = {"kind": "refusal", "message": state["refusal"]}
    elif state.get("clarify"):
        answer = {"kind": "clarify", "message": state["clarify"]["question"], "options": state["clarify"]["options"]}
    else:
        result = state.get("result", {})
        dsl = state.get("dsl", {})
        metric = spec.metrics.get(dsl.get("metric", "revenue"))
        single = None
        if not dsl.get("dimensions") and result.get("rows"):
            single = metric.fmt.format(result["rows"][0][-1]) if metric else str(result["rows"][0][-1])
        answer = {
            "kind": "answer",
            "summary": _summary(state, metric, single),
            "narrative": state.get("narrative", ""),
            "table": {"columns": result.get("columns", []), "rows": result.get("rows", [])},
            "chart": state.get("chart"),
            "sql": state.get("sql", ""),
            "caliber": state.get("caliber", ""),
            "qa": state.get("qa_report"),
            "data_badge": {
                "row_count": result.get("row_count", 0),
                "truncated": result.get("truncated", False),
                "elapsed_ms": result.get("elapsed_ms"),
                "from_cache": bool(result.get("from_cache", False)),
                "degraded": bool(state.get("sql_error")),
            },
        }
    return {
        "answer": answer,
        "status": "done",
        "events": _merge_events(state, _event(state, "finalizer", "answer_kind", {"kind": answer["kind"]})),
    }


def _summary(state: GraphState, metric, single: str | None) -> str:
    if state.get("refusal"):
        return state["refusal"]
    if single is not None and metric:
        unit = {"yuan": "元", "count": "", "percent": ""}.get(metric.unit, "")
        return f"{metric.name}为 {single}{unit}（{state.get('caliber', '')}）"
    dsl = state.get("dsl", {})
    dims = "×".join(dsl.get("dimensions", [])) or "整体"
    metric_name = metric.name if metric else "指标"
    return f"已按 {dims} 输出{metric_name}明细，共 {state.get('result', {}).get('row_count', 0)} 行"

# ---------------- 2.5 clarify_suspend（E4②：澄清挂起/恢复，interrupt 语义） ----------------


async def clarify_suspend_node(state: GraphState) -> dict:
    """把"反问用户"从终结态升级为**挂起点**：interrupt 暂停图，用户答案恢复后续跑。

    首次执行：interrupt(payload) 抛出中断，图在 checkpointer 落盘挂起；
    恢复执行：本节点重入，interrupt() 直接返回用户答案（不重跑 LLM），
    把澄清内容并入问题、清空 clarify → 静态边直达 sql_agent。
    （依赖 clarify_suspend_enabled 开关与图装配时的 checkpointer。）
    """
    from langgraph.types import interrupt

    clarify = state.get("clarify") or {}
    payload = {
        "question": clarify.get("question") or "能补充一下你想看的具体指标或时间吗？",
        "options": clarify.get("options") or [],
        "reason": state.get("clarify_reason", ""),
    }
    answer = interrupt(payload)
    merged_question = f'{state["question"]}（用户澄清：{answer}）'
    return {
        "clarify": None,
        "clarify_reason": "",
        "question": merged_question,
        "status": "clarify_resolved",
        "events": _merge_events(
            state, _event(state, "clarify_suspend", "clarify_resolved", {"answer": answer})
        ),
    }

