"""多轮指代消解（E 系原创扩展）：让"那华北呢"这类追问继承上一轮的查询骨架。

设计（对齐 ADR 口径）：
1. 会话上下文只存"上一轮的原始问题 + 上轮 DSL 的轻量影子"（metric/dimensions/
   filters 的字段名），不存对话原文——隐私面最小，也天然适配无状态的图执行。
2. 追问判定是**规则**：短句 + "那/换/呢"模式 + 不含指标关键词 → 追问。
   判不出来就当独立问题走原链路——误判方向被设计成"保守"（宁可当成新问题）。
3. 实体替换用**同维度类型词表**：上轮问华南（region），这轮"那华北呢"→ 把上轮
   问题里的 region 实体替换成华北。词表内置评测域（地区/渠道/品类），换域需补表
   ——与 memomind 意图映射表同款的显式维护面（TAKEOVER 已列）。
4. 开关 `followup_enabled` 默认 False：关闭时 resolve 直接原样返回，
   API 行为与无此模块完全一致（既有 57 项测试不动）。
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict

from .data.schema import DIMENSION_VALUES

# 指标关键词：出现即视为独立问题（不算追问）
_METRIC_WORDS = ("销售额", "毛利率", "毛利额", "订单量", "客单价", "退货率",
                 "销量", "趋势", "多少", "排名", "前5", "前 5", "前3", "前 3")

# 同维度类型实体词表：**收敛到 schema.DIMENSION_VALUES 单一来源**（AB-2 评审发现：
# 原硬编码词表与数据集取值交集仅"直播"，"门店/电商/个护"等是假值，真实取值如
# "搜索/推荐/社交/官网"反而不在表内）。"全国"是路由概念不是过滤取值，不进词表。
_ENTITY_TYPES: dict[str, tuple[str, ...]] = {
    "region": tuple(v for v in DIMENSION_VALUES["region"] if v != "全国"),
    "channel": tuple(DIMENSION_VALUES["channel"]),
    "category": tuple(DIMENSION_VALUES["category"]),
    "member_level": tuple(DIMENSION_VALUES["member_level"]),
}
_ENTITY_TO_TYPE = {e: t for t, es in _ENTITY_TYPES.items() for e in es}
_GENERIC_SUFFIX = re.compile(r"(渠道|品类|地区|会员)$")


def _entity_type(entity: str) -> str | None:
    """实体 → 维度类型；剥离通用后缀（直播渠道→直播）后再查词表。"""
    if entity in _ENTITY_TO_TYPE:
        return _ENTITY_TO_TYPE[entity]
    stripped = _GENERIC_SUFFIX.sub("", entity)
    return _ENTITY_TO_TYPE.get(stripped)

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
        self._last_question: "OrderedDict[str, str]" = OrderedDict()

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
    for pat in _FOLLOWUP_PATTERNS:
        m = pat.match(q)
        if m and _entity_type(m.group("entity")):
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
    m = next((pm.match(question.strip()) for pm in _FOLLOWUP_PATTERNS
              if pm.match(question.strip())), None)
    if not m:
        return question, "检测到追问但句式未匹配，已按新问题处理"
    entity = m.group("entity")
    etype = _entity_type(entity)
    if not etype:
        return question, "检测到追问但实体不在词表内，已按新问题处理"
    # 上轮问题里找同类型旧实体
    olds = [e for e in _ENTITY_TYPES[etype] if e in last_question and e != entity]
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
