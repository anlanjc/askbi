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
