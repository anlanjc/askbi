"""PROFILE 功能档位：conservative（默认）零行为变化；full 一键开实验功能；显式配置永远优先。"""

from app.config import Settings


def test_conservative_default_all_off(monkeypatch):
    monkeypatch.delenv("PROFILE", raising=False)
    s = Settings(llm_provider="mock")
    assert s.profile == "conservative"
    assert not s.cache_enabled
    assert not s.followup_enabled
    assert not s.clarify_suspend_enabled


def test_full_profile_turns_on_experimental_flags(monkeypatch):
    monkeypatch.delenv("PROFILE", raising=False)
    s = Settings(profile="full", llm_provider="mock")
    assert s.cache_enabled
    assert s.followup_enabled
    assert s.clarify_suspend_enabled


def test_explicit_flag_beats_profile(monkeypatch):
    monkeypatch.delenv("PROFILE", raising=False)
    s = Settings(profile="full", cache_enabled=False, llm_provider="mock")
    assert s.cache_enabled is False  # 显式关闭优先于档位
    assert s.followup_enabled
    assert s.clarify_suspend_enabled


def test_profile_from_env(monkeypatch):
    monkeypatch.setenv("PROFILE", "full")
    s = Settings(llm_provider="mock")
    assert s.followup_enabled


def test_settings_has_no_dead_config_fields():
    """ASKBI-4（评审 4.0）：Settings 曾声明并文档化 ROLE/DATA_DIR 但全仓零消费——
    部署者设了毫无效果却以为存在环境级权限/数据目录控制。死配置必须删。"""
    s = Settings()
    assert not hasattr(s, "role"), "ROLE 是死配置：实际角色来自请求体（schemas 默认 boss）"
    assert not hasattr(s, "data_dir"), "DATA_DIR 是死配置：落盘路径只由 db_path 推导"
