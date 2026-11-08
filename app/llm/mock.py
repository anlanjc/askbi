"""确定性 Mock LLM：按 node 分发到规则处理器，保证全链路无 API key 可运行。

价值定位（写给面试官）：mock 不是玩具——它让 CI 能跑端到端回归，
评测可以先在 mock 下验证流水线正确性，再切真实模型看质量增益。
sql_agent 的规则解析器与 DSL 词表共用一套常量，不会漂移。

多数据源：NL 词表（指标别名/维度关键词/取值域）全部来自当前数据集 spec
（app/data/registry.py），payload 携带 dataset 标识（nodes.py 透传 settings.dataset）；
payload 不带 dataset 时回退默认数据集——历史调用点行为不变。
"""

from __future__ import annotations

import json
import re

from .base import ChatRequest, LLMClient, LLMResponse, TokenUsage


def _payload(req: ChatRequest) -> dict:
    try:
        return json.loads(req.user)
    except Exception:  # noqa: BLE001  payload 非 JSON 时按设计退化为问题文本
        return {"question": req.user}


def _spec_of(p: dict):
    """解析当前数据集 spec：payload.dataset 缺省 = 默认数据集（历史行为不变）。"""
    name = p.get("dataset")
    if not name:
        from ..data.registry import DEFAULT_SPEC

        return DEFAULT_SPEC
    from ..data.registry import get_spec

    return get_spec(name)


class MockLLM(LLMClient):
    async def chat(self, req: ChatRequest) -> LLMResponse:
        handler = _HANDLERS.get(req.node, _default)
        content = handler(_payload(req))
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        return LLMResponse(
            content=content,
            usage=TokenUsage(prompt_tokens=64, completion_tokens=max(16, len(content) // 8)),
            provider="mock", model="mock-rules",
        )


# ---------------- clarifier ----------------

_VAGUE_METRIC = ["卖得怎么样", "表现怎么样", "情况怎么样", "经营情况", "表现如何", "怎么样"]
_TIME_VAGUE = ["最近", "近期", "这阵子"]


def _clarifier(p: dict) -> dict:
    spec = _spec_of(p)
    q = p.get("question", "")
    has_metric = any(a in q for m_aliases in _metric_aliases(spec) for a in m_aliases)
    # 预测未来：数据不可达 → 拒答而非编造
    if any(w in q for w in ("预测", "未来", "下个月", "下季度", "明年", "预计")):
        return {
            "verdict": "refuse",
            "reason": "数据仅覆盖到 2026-09，无法预测未来。拒答而非编造。",
            "clarify_question": "", "options": [],
        }
    # 常见但未采集的指标：明确拒答（面试高频考点：宁缺毋滥）
    if re.search(r"(满意度|复购率|好评率|留存|评分|投诉)", q):
        return {
            "verdict": "refuse",
            "reason": "数据集中不存在该指标（常见但未采集）。拒答而非编造。",
            "clarify_question": "", "options": [],
        }
    # 非数据请求 → 拒答（只看"有无指标词"：渠道值"推荐"等枚举词不代表这是数据请求）
    not_data_verbs = ("写", "生成", "翻译", "推荐", "评价")
    if not has_metric and any(v in q for v in not_data_verbs):
        return {
            "verdict": "refuse",
            "reason": "这不是经营数据查询类问题（或需要数据集中不存在的信息），拒答以避免编造",
            "clarify_question": "", "options": [],
        }
    if not has_metric or any(v in q for v in _VAGUE_METRIC):
        return {
            "verdict": "clarify",
            "reason": "未指明关注哪个指标" if not has_metric else "指标表述模糊",
            "clarify_question": "你想看哪个指标？",
            "options": ["销售额", "毛利率", "订单量", "客单价"],
        }
    if any(t in q for t in _TIME_VAGUE) and not re.search(r"[最近近]\s*(\d+|一|两|三|六|十二)\s*个?月", q):
        return {
            "verdict": "clarify",
            "reason": "时间范围模糊",
            "clarify_question": "想看哪个时间范围？",
            "options": ["最近1个月", "最近3个月", "最近12个月"],
        }
    return {"verdict": "clear", "reason": "", "clarify_question": "", "options": []}


# ---------------- sql_agent ----------------

_NUM_CN = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "十二": 12}


def _metric_aliases(spec) -> list[list[str]]:
    out = []
    order = spec.metric_parse_order or tuple(spec.metrics)
    for mid in order:
        m = spec.metrics[mid]
        out.append([m.name, *m.aliases])
    return out


def _parser_dim_values(spec) -> dict[str, list[str]]:
    """解析器可提取取值的枚举维度：有 NL 关键词 且 声明了取值域 的维度。"""
    return {d: spec.dimension_values[d] for d in spec.dim_keywords if d in spec.dimension_values}


def _detect_metric(q: str, spec) -> str | None:
    """别名->指标id 映射，长词优先匹配（保证"毛利率"不被"毛利"截胡）。"""
    order = spec.metric_parse_order or tuple(spec.metrics)
    alias2id: dict[str, str] = {}
    for mid in order:
        m = spec.metrics[mid]
        for a in [m.name, *m.aliases]:
            alias2id.setdefault(a, mid)
    ql = q.lower()
    for alias in sorted(alias2id, key=len, reverse=True):
        if alias.lower() in ql:
            return alias2id[alias]
    return None


def _sql_agent(p: dict) -> dict:
    from ..data.dsl import build_dsl, validate_filter_values

    spec = _spec_of(p)
    q = p.get("question", "")
    dsl = _parse_question(q, spec)

    # 规则解析失败 → 结构化报错，节点层会走修复/拒答路径
    try:
        obj = build_dsl(dsl, spec)
    except Exception as e:  # noqa: BLE001
        return {"error": f"DSL 无效: {e}"}
    problems = validate_filter_values(obj, spec)
    if problems:
        return {"error": "; ".join(problems)}
    return obj.model_dump()


def _parse_question(q: str, spec=None) -> dict:
    if spec is None:
        from ..data.registry import DEFAULT_SPEC

        spec = DEFAULT_SPEC
    dim_keywords = spec.dim_keywords
    dim_values = _parser_dim_values(spec)
    ql = q.lower()
    detected = _detect_metric(q, spec)
    metric = detected or "revenue"

    dims: list[str] = []
    matched_kws: list[str] = []
    for d, kws in dim_keywords.items():
        for k in kws:
            if k in q:
                dims.append(d)
                matched_kws.append(k)
                break
    # 关键词包含去重："细分品类"同时命中"品类"与"细分品类"，只保留长词（subcategory）
    if len(dims) > 1:
        keep = [i for i, k in enumerate(matched_kws)
                if not any(k != o and k in o for o in matched_kws)]
        dims = [dims[i] for i in keep]
        matched_kws = [matched_kws[i] for i in keep]
    if metric == "revenue" and not dims and "各" in q:
        dims.append("category")

    # 「值+字段」式表达：华南地区 / 美妆品类 / 直播渠道 / 黑卡会员 —— 提取为过滤条件
    # （取值提取关键词随数据集走：spec.filter_keywords）
    filters: list[dict] = []
    _STOP_PREFIX = {"各", "每", "该", "所有", "全部", "哪个", "哪些", "什么"}
    _OTHER_KWS = {k for kws in dim_keywords.values() for k in kws}
    for fname, kws in spec.filter_keywords.items():
        allowed = dim_values.get(fname)
        if not allowed:
            continue
        for kw in kws:
            for m in re.finditer(r"([\u4e00-\u9fa5]{1,3})" + kw, q):
                raw = m.group(1)
                # 命中片段属于另一个维度关键词（如"细分品类"里的"品类"）时不是取值过滤
                if any(k != kw and (k in raw + kw or raw + kw in k) for k in _OTHER_KWS):
                    continue
                # 候选：完整段优先（未来支持3字值），否则取末2字（本词表取值均为2字）
                cands = [raw] if len(raw) >= 3 else []
                if len(raw) >= 2:
                    cands.append(raw[-2:])

                def _strip(c: str) -> str:
                    changed = True
                    time_prefixes = ("个月", "上月", "本月", "当月", "最近", "近几")
                    while changed and c:
                        changed = False
                        for s in _STOP_PREFIX:
                            if len(c) >= len(s) and c.startswith(s):
                                c = c[len(s):]
                                changed = True
                            if len(c) >= len(s) and c.endswith(s):
                                c = c[: -len(s)]
                                changed = True
                        for t in time_prefixes:
                            if len(c) >= len(t) and c.startswith(t):
                                c = c[len(t):]
                                changed = True
                    return c

                picked_known = None
                picked_unknown = None
                for cand in cands:
                    c = _strip(cand)
                    # 停用词/修饰语（哪个/最高的…）不构成取值
                    if not c or c in _STOP_PREFIX or c.endswith("的") or "最" in c or len(c) < 2:
                        continue
                    if c in allowed:
                        picked_known = c
                        break
                    picked_unknown = c  # 未知取值：短候选优先（宁缺毋滥 → 校验触发拒答）
                picked = picked_known or picked_unknown
                if picked:
                    filters.append({"field": fname, "op": "=", "value": picked})
                    break
    # 值已作为过滤条件时，从分组维度中移除（避免单值分组噪音）
    for f in filters:
        if f["field"] in dims:
            dims.remove(f["field"])

    time = {"mode": "all"}
    m = re.search(
        r"(20\d{2})\s*[-年]\s*(\d{1,2})\s*月?\s*(?:到|至|~|—)\s*(?:(20\d{2})\s*[-年]\s*)?(\d{1,2})\s*月?",
        q,
    )
    if m:  # 显式年月区间：2024年10月到12月 / 2025-01至2025-06（次省年复用首年）
        y1, mo1 = m.group(1), int(m.group(2))
        y2, mo2 = m.group(3) or y1, int(m.group(4))
        time = {"mode": "between", "start": f"{y1}-{mo1:02d}", "end": f"{y2}-{mo2:02d}"}
    m = re.search(r"[最近近](\d+|一|两|二|三|六|十二)个?月", q)
    if m:
        n = _NUM_CN.get(m.group(1), None) or (int(m.group(1)) if m.group(1).isdigit() else 3)
        time = {"mode": "last_n_months", "n": n}
    elif "上月" in q or "上个月" in q:
        time = {"mode": "last_n_months", "n": 1}
    elif "本月" in q or "当月" in q:
        time = {"mode": "last_n_months", "n": 1}  # 数据截止月即"本月"

    intent = "aggregate"
    if any(k in q for k in ["趋势", "走势", "变化", "每月", "逐月"]):
        intent = "trend"
        if "month" not in dims:
            dims = ["month"] + dims  # 趋势必带月份；"各渠道销售额趋势" = month×channel 双维
    elif any(k in q for k in ["哪个", "哪一", "最", "排名", "前"]):
        intent = "topn"
    elif any(k in q for k in ["对比", " vs ", "和", "谁更"]):
        intent = "compare"
    elif any(k in q for k in ["为什么", "原因", "为何", "怎么"]):
        intent = "aggregate"
        if "month" not in dims:
            time = time if time["mode"] != "all" else {"mode": "last_n_months", "n": 1}
    elif dims:
        intent = "breakdown"

    order_dir = "desc"
    if metric in ("refund_rate",) or any(k in q for k in ["最低", "最差", "最少", "下滑"]):
        order_dir = "asc"
    if any(k in q for k in ["最高", "最多", "最好", "最大"]):
        order_dir = "desc"

    limit = None
    lm = re.search(r"前\s*(\d+)|top\s*(\d+)", ql)
    if lm:
        limit = int(lm.group(1) or lm.group(2) or 5)

    out = {
        "intent": intent, "metric": metric, "dimensions": dims,
        "filters": filters, "time": time,
        "order_by": {"metric": metric, "direction": order_dir}, "limit": limit,
    }
    return out


# ---------------- insight / qa ----------------

# 消融实验开关：True 时模拟"LLM 自由心算/编造数字"的退化行为，
# 用于验证 QA 数值核验闭环能否拦截（runner --ablation sloppy 控制）。
SLOPPY = False


def _insight(p: dict) -> str:
    f = p.get("facts", {})
    lines: list[str] = []
    if f.get("变化率") is not None:
        sign = "上升" if f["变化率"] >= 0 else "下降"
        rate, cur, prev = f["变化率"], f.get("当前值"), f.get("上期值")
        if SLOPPY:
            # 故意失真：变化率放大 1.5 倍、当前值加 8%（模拟模型心算/编造）
            rate, cur = rate * 1.5, (cur or 0) * 1.08
        lines.append(
            f"{f.get('metric', '指标')}在{f.get('对比窗口', '')}期间{sign} "
            f"{abs(rate):.1%}（{prev:.4f} → {cur:.4f}）。"
        )
        for b in f.get("维度分解", [])[:2]:
            d = b["dimension"]
            tops = b["top_movers"][:3]
            seg = "；".join(
                f"{t['member']}（{t['上期']:.4f}→{t['本期']:.4f}，变化 {t['变化']}）" for t in tops
            )
            lines.append(f"按{d}分解，变化最大的依次是：{seg}。")
    else:
        for row in f.get("top_rows", [])[:3]:
            lines.append("；".join(f"{k}：{v}" for k, v in row.items()))
        if not lines:
            lines.append("已根据查询结果生成概览，详见数据表与图表。")
    return "\n".join(lines)


def _qa(p: dict) -> dict:
    # 确定性数值核验在节点层完成；LLM 只补充语义判断，mock 下恒过
    return {"passed": not p.get("deterministic_issues"), "issues": p.get("deterministic_issues", [])}


def _default(p: dict) -> str:
    return json.dumps({"ok": True}, ensure_ascii=False)


_HANDLERS = {
    "clarifier": _clarifier,
    "sql_agent": _sql_agent,
    "insight": _insight,
    "qa": _qa,
}
