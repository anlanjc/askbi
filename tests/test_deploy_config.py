"""部署配置回归（ASKBI-3，评审 4.0）：compose environment 白名单与文档清单逐一对齐。

修复前：白名单漏 PROFILE 与 OPENAI 三件套（同族还有 DEEPSEEK_BASE_URL），镜像又不
COPY .env（pydantic-settings env_file 指向的 /app/.env 不存在）——按 DEPLOY.md
「环境变量清单（Docker/平台通用）」部署时配置被静默丢弃：PROFILE=full 演示特性
不开且无任何报错；LLM_PROVIDER=openai_compatible 时凭据三件套收不到，每次请求
LLM 调用必失败。纯静态文本断言，不触 Docker。
"""

from __future__ import annotations

from pathlib import Path

_COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"

# DEPLOY.md「环境变量清单」+ .env.example 两条 provider 路线的并集：容器必须能收到
# （ROLE 不在列：ASKBI-4 确认其为死配置已删，角色来自请求体）
_REQUIRED_VARS = {
    "LLM_PROVIDER", "PROFILE", "API_KEY",
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
    "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL",
}


def _compose_env_vars() -> set[str]:
    """解析 docker-compose.yml 的 environment 块，取变量名集合（不引 YAML 依赖）。"""
    text = _COMPOSE.read_text(encoding="utf-8")
    env: set[str] = set()
    in_env = False
    for line in text.splitlines():
        s = line.strip()
        if s == "environment:":
            in_env = True
            continue
        if not in_env or not s or s.startswith("#"):
            continue   # 空行与块内注释行跳过（注释不终止解析）
        if s.startswith("- "):
            env.add(s[2:].split("=", 1)[0].split(":", 1)[0].strip())
        else:          # 非 list 项 = environment 块结束
            break
    return env


def test_compose_environment_covers_documented_vars():
    env = _compose_env_vars()
    missing = _REQUIRED_VARS - env
    assert not missing, f"compose 白名单漏透传变量：{sorted(missing)}（宿主机配置被静默丢弃）"


def test_compose_env_lines_use_host_passthrough_form():
    """每个变量行必须 `VAR=${VAR:-default}` 形式——宿主机环境变量才真正进容器。"""
    text = _COMPOSE.read_text(encoding="utf-8")
    env_block = text.split("environment:", 1)[1].split("volumes:", 1)[0]
    lines = [s.strip() for s in env_block.strip().splitlines()
             if s.strip() and not s.strip().startswith("#")]
    assert lines, "compose 必须有 environment 块"
    for s in lines:
        assert s.startswith("- "), f"environment 项必须是 list 形式：{s}"
        name, _, value = s[2:].partition("=")
        assert value.startswith("${") and name in value, \
            f"变量行必须 ${'{VAR:-default}'} 透传宿主机值：{s}"


def test_compose_does_not_carry_dead_config_vars():
    """ASKBI-4（评审 4.0）：ROLE 曾声明为行级权限角色但全仓零消费（角色来自请求体）——
    compose 白名单不得透传死配置误导部署者。"""
    env = _compose_env_vars()
    assert "ROLE" not in env, "ROLE 是死配置（实际角色来自请求体），compose 不得透传"
