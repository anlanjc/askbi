"""多轮指代消解（E 系原创扩展）：让"那华北呢"这类追问继承上一轮的查询骨架。

设计（对齐 ADR 口径）：
1. 会话上下文只存"上一轮的原始问题 + 上轮 DSL 的轻量影子"（metric/dimensions/
   filters 的字段名），不存对话原文——隐私面最小，也天然适配无状态的图执行。
2. 追问判定是**规则**：短句 + "那/换/呢"模式 + 不含指标关键词 → 追问。
   判不出来就当独立问题走原链路——误判方向被设计成"保守"（宁可当成新问题）。
3. 实体替换用**同维度类型词表**：上轮问华南（region），这轮"那华北呢"→ 把上轮
   问题里的 region 实体替换成华北。词表**收敛到激活 spec 单一来源**
   （registry.get_spec → dimension_values ∩ dimensions，与 schema_prompt 展示的
   取值域同构）：切 DATASET=coffee/saas/hotel 后消解随对应数据集词表走——
   "那外卖呢"在 coffee 消解、在 ecommerce 不命中（跨源隔离是双向的）。
   默认 ecommerce 的推导结果与旧硬编码（schema.DIMENSION_VALUES 四键）逐项相同。
4. 开关 `followup_enabled` 默认 False：关闭时 resolve 直接原样返回，
   API 行为与无此模块完全一致（既有 57 项测试不动）。
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict

# 指标关键词：出现即视为独立问题（不算追问）
_METRIC_WORDS = ("销售额", "毛利率", "毛利额", "订单量", "客单价", "退货率",
                 "销量", "趋势", "多少", "排名", "前5", "前 5", "前3", "前 3")

# 路由概念值："全国"不是过滤取值，换入会产生非法过滤值——不进消解词表
# （历史口径：旧实现仅对 region 剔除；现对全部键统一剔除，四源取值域均无此值，默认源逐项不变）
_ROUTING_VALUES = frozenset({"全国"})

# 通用后缀（AB-2 修复）：换入词根避免"搜索渠道渠道"式重复后缀
_GENERIC_SUFFIX = re.compile(r"(渠道|品类|地区|会员)$")


def _entity_vocab() -> dict[str, tuple[str, ...]]:
    """同维度类型实体词表：**收敛到激活 spec 单一来源**（[1B5] 收口 EVIDENCE §6 边界）。

    推导规则（与 schema_prompt 展示取值域同构）：只取 spec.dimensions 里声明了
    取值域的维度（status 这类非分组维度的取值域不进词表），剔除路由概念值。
    默认 ecommerce 推导结果与旧硬编码（schema.DIMENSION_VALUES 四键词表）逐项
    相同——AB-2 词表收敛语义原样保留，只是取数来源随数据集走（tests/
    test_followup_vocab_spec.py 以改动前快照守门）。

    每次调用现算（≤5 键 × ≤7 值，微秒级，仅请求期/评测轮调用一次）：
    换 DATASET / 测试注册 replace 变体即时生效，不引入缓存失效面。
    """
    # 延迟导入：registry 不反向依赖本模块
    from .data.registry import get_spec

    spec = get_spec()
    return {
        key: tuple(v for v in spec.dimension_values.get(key, ())
                   if v not in _ROUTING_VALUES)
        for key in spec.dimensions
        if spec.dimension_values.get(key)
    }


def _entity_to_type(vocab: dict[str, tuple[str, ...]]) -> dict[str, str]:
    """实体 → 维度类型倒排表（值跨维度重复时后声明维度的键胜出，与旧实现一致）。"""
    return {e: t for t, es in vocab.items() for e in es}


def _entity_type(entity: str, to_type: dict[str, str]) -> str | None:
    """实体 → 维度类型；剥离通用后缀（直播渠道→直播）后再查词表。"""
    if entity in to_type:
        return to_type[entity]
    stripped = _GENERIC_SUFFIX.sub("", entity)
    return to_type.get(stripped)

# 追问句式：那X呢 / 换成X / 改成X / X呢（X 为已登记实体）
# 实体用非贪婪 + 可选语气词后缀（呢/啊/吗），避免"电商呢"整个被吃进实体名
_FOLLOWUP_PATTERNS = (
    re.compile(r"^那(?P<entity>\S{1,8}?)(?:呢)?[?？!！。]?$"),
    re.compile(r"^(?:换成|改成)(?P<entity>\S{1,8}?)(?:呢|啊|吗)?[?？!！。]?$"),
    re.compile(r"^(?P<entity>\S{1,8}?)呢[?？!！。]?$"),
)


class SessionContext:
    """会话级"上一轮"记忆：session_id -> 原始问题。进程内、线程安全、LRU 有界。"""

    MAX_SESSIONS = 256   # 防会话字典无界增长（评审发现：原实现永不回收）

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._last_question: OrderedDict[str, str] = OrderedDict()

    def set(self, session_id: str, question: str) -> None:
        with self._lock:
            if session_id:
                self._last_question[session_id] = question
                self._last_question.move_to_end(session_id)
                while len(self._last_question) > self.MAX_SESSIONS:
                    self._last_question.popitem(last=False)

    def get(self, session_id: str) -> str:
        with self._lock:
            return self._last_question.get(session_id, "")

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._last_question.pop(session_id, None)


_CONTEXT = SessionContext()


def get_session_context() -> SessionContext:
    return _CONTEXT


def is_followup(question: str) -> bool:
    """短句 + 追问句式 + 不含指标关键词 → 追问。"""
    q = question.strip()
    if len(q) > 16 or any(w in q for w in _METRIC_WORDS):
        return False
    to_type = _entity_to_type(_entity_vocab())
    for pat in _FOLLOWUP_PATTERNS:
        m = pat.match(q)
        if m and _entity_type(m.group("entity"), to_type):
            return True
    return False


def resolve_followup(question: str, last_question: str,
                     enabled: bool = True) -> tuple[str, str]:
    """把追问改写成完整问题：用同类型实体替换上轮问题里的实体。

    返回 (改写后的问题, 说明)；无法消解时原样返回问题并给出原因说明
    （评审 D3：把"静默当独立问题"变成可观测的 trace）。
    """
    if not enabled:
        return question, ""
    if not is_followup(question):
        return question, ""
    if not last_question:
        return question, "检测到追问但无上轮上下文，已按新问题处理"
    vocab = _entity_vocab()
    to_type = _entity_to_type(vocab)
    m = next((pm.match(question.strip()) for pm in _FOLLOWUP_PATTERNS
              if pm.match(question.strip())), None)
    if not m:
        return question, "检测到追问但句式未匹配，已按新问题处理"
    entity = m.group("entity")
    etype = _entity_type(entity, to_type)
    if not etype:
        return question, "检测到追问但实体不在词表内，已按新问题处理"
    # 上轮问题里找同类型旧实体
    olds = [e for e in vocab[etype] if e in last_question and e != entity]
    if not olds:
        return question, "检测到追问但上轮无同类型实体可替换，已按新问题处理"
    if len(set(olds)) > 1:
        # 评审 D2：上轮含多个同类型实体 → 用户意图歧义，保守回退独立问题
        return question, f"检测到追问但上轮含多个 {etype} 实体（{olds}），已按新问题处理"
    old = olds[0]
    # AB-2 评审修复：替换值用剥离后缀的词根（"换成搜索渠道"→换入"搜索"而非
    # "搜索渠道"），否则上轮已带后缀时会产生"搜索渠道渠道"重复后缀
    core = _GENERIC_SUFFIX.sub("", entity)
    resolved = last_question.replace(old, core)   # 评审 D2：全部替换，不留混合骨架
    return resolved, f"指代消解：'{question}' → '{resolved}'（替换 {etype}: {old}→{core}）"
