"""多轮指代消解测试：追问判定、实体替换、会话隔离、开关关闭零变化。"""

from app.context import (
    SessionContext,
    get_session_context,
    is_followup,
    resolve_followup,
)


def test_followup_pattern_detected():
    assert is_followup("那华北呢")
    assert is_followup("换成直播渠道")
    assert is_followup("华东呢？")
    assert is_followup("那美妆品类呢？")


def test_metric_question_not_followup():
    assert not is_followup("上月华北地区的销售额是多少")
    assert not is_followup("最近6个月各品类的销量")
    assert not is_followup("请给我前5名的销售额排名")


def test_long_or_plain_question_not_followup():
    assert not is_followup("帮我分析一下上个月的整体经营情况怎么样")
    assert not is_followup("你好")


def test_resolve_replaces_same_type_entity():
    resolved, note = resolve_followup(
        "那华北呢", "上月华南地区的毛利率", enabled=True)
    assert resolved == "上月华北地区的毛利率"
    assert "华南→华北" in note


def test_resolve_channel_entity():
    # 词表收敛到 DIMENSION_VALUES 后："搜索"是真实渠道值（原词表用假值"电商"）
    resolved, _ = resolve_followup("换成搜索呢", "直播渠道近3个月的销售额", enabled=True)
    assert resolved == "搜索渠道近3个月的销售额"


def test_resolve_with_suffix_no_duplication():
    """AB-2 评审修复回归：带后缀追问换入词根，杜绝"搜索渠道渠道"重复后缀。"""
    resolved, _ = resolve_followup(
        "换成搜索渠道呢", "直播渠道近3个月的销售额", enabled=True)
    assert resolved == "搜索渠道近3个月的销售额"


def test_channel_values_now_resolve_from_real_lexicon():
    """AB-2 评审修复回归：原硬编码词表缺"社交/官网/推荐"，收敛到 schema 后可消解。"""
    resolved, note = resolve_followup("那社交呢", "上月直播渠道的订单量", enabled=True)
    assert resolved == "上月社交渠道的订单量"
    assert "直播→社交" in note


def test_member_level_followup_resolves():
    resolved, _ = resolve_followup("那黑卡呢", "上月银卡会员的客单价", enabled=True)
    assert resolved == "上月黑卡会员的客单价"


def test_nationwide_not_in_followup_lexicon():
    """"全国"是路由概念不是过滤取值，不进消解词表（换入会产生非法过滤值）。"""
    resolved, _note = resolve_followup("那全国呢", "上月华南地区的销售额", enabled=True)
    assert resolved == "那全国呢"  # 保守回退独立问题


def test_unknown_entity_passthrough():
    resolved, note = resolve_followup("那火星呢", "上月华南地区的毛利率", enabled=True)
    # 词表外实体在 is_followup 阶段即判非追问（视同闲聊）——保守设计的边界：
    # "可观测说明"只给"检测到但消解不了"的真追问，不给随机闲聊
    assert resolved == "那火星呢" and note == ""


def test_no_last_question_passthrough():
    resolved, note = resolve_followup("那华北呢", "", enabled=True)
    # 评审 D3 修订：检测到追问但无上下文 → 静默变可见（原样返回+说明）
    assert resolved == "那华北呢" and "无上轮上下文" in note


def test_disabled_passthrough():
    resolved, note = resolve_followup("那华北呢", "上月华南地区的毛利率", enabled=False)
    assert resolved == "那华北呢" and note == ""


def test_session_context_isolation():
    ctx = SessionContext()
    ctx.set("s1", "上月华南地区的毛利率")
    ctx.set("s2", "各渠道最近3个月的客单价")
    assert ctx.get("s1") == "上月华南地区的毛利率"
    assert ctx.get("s2") == "各渠道最近3个月的客单价"
    ctx.clear("s1")
    assert ctx.get("s1") == ""


def test_global_context_singleton():
    assert get_session_context() is get_session_context()


def test_session_context_lru_eviction():
    """评审修复回归：会话上下文 LRU 有界，超限淘汰最旧。"""
    from app.context import SessionContext
    ctx = SessionContext()
    for i in range(300):
        ctx.set(f"s{i}", f"问题{i}")
    assert ctx.get("s299") == "问题299"   # 新会话在
    assert ctx.get("s0") == ""            # 最旧的被淘汰
    assert len(ctx._last_question) <= SessionContext.MAX_SESSIONS
