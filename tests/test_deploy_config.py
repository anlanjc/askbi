"""部署配置回归（ASKBI-3，评审 4.0）：compose environment 白名单与文档清单逐一对齐。

修复前：白名单漏 PROFILE 与 OPENAI 三件套（同族还有 DEEPSEEK_BASE_URL），镜像又不
COPY .env（pydantic-settings env_file 指向的 /app/.env 不存在）——按 DEPLOY.md
「环境变量清单（Docker/平台通用）」部署时配置被静默丢弃：PROFILE=full 演示特性
不开且无任何报错；LLM_PROVIDER=openai_compatible 时凭据三件套收不到，每次请求
LLM 调用必失败。纯静态文本断言，不触 Docker。

ASKBI-9 / ASKBI-10（评审 4.0）扩展：requirements 下限与实际 import 的一致性、
build context 排除表（.dockerignore）与 Dockerfile COPY 白名单的一致性。
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_COMPOSE = _ROOT / "docker-compose.yml"
_DOCKERFILE = _ROOT / "Dockerfile"

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


# ---- ASKBI-9（评审 4.0）：requirements 下限与实际 import 的一致性 ----

def test_mcp_floor_covers_server_import():
    """server.py 用 `from mcp.server.mcpserver import MCPServer`——该模块仅 mcp 2.x
    存在（1.x 只有 mcp/server/fastmcp；mcp 1.2.0 wheel 实测无 mcpserver 模块，
    本机 .venv 实装 2.3.0 可 import）。requirements 曾声明 mcp>=1.2：按声明下限
    解析到 1.x 的环境跑 scripts/mcp_server.py 在 import 处即 ModuleNotFoundError。
    钉住：①模块在本环境可寻位；②声明下限 ≥ 2.0。"""
    import importlib.util

    assert importlib.util.find_spec("mcp.server.mcpserver") is not None, \
        "mcp.server.mcpserver 不可寻位——app/mcp/server.py 的 import 在本环境即失效"

    text = (_ROOT / "requirements.txt").read_text(encoding="utf-8")
    m = re.search(r"^mcp>=([\d.]+)", text, re.M)
    assert m, "requirements.txt 必须声明 mcp>= 下限"
    floor = tuple(int(p) for p in m.group(1).split("."))
    assert floor >= (2, 0), \
        f"mcp 下限 {m.group(1)} 低于 server.py 实际依赖（mcp.server.mcpserver 仅 2.x 存在）"


# ---- ASKBI-10（评审 4.0）：build context 排除表与 COPY 白名单的一致性 ----

def _dockerignore_patterns() -> list[str]:
    text = (_ROOT / ".dockerignore").read_text(encoding="utf-8")
    return [s.strip() for s in text.splitlines()
            if s.strip() and not s.strip().startswith("#")]


def _dockerfile_copy_sources() -> list[str]:
    return re.findall(r"^COPY\s+(\S+)", _DOCKERFILE.read_text(encoding="utf-8"), re.M)


def test_dockerignore_excludes_secret_and_bulk_paths():
    """仓库根曾无 .dockerignore——.venv/.git/data（askbi*.duckdb/tasks.jsonl）与
    带真实 key 的 .env 全进 build context 发给构建 daemon（.gitignore 只约束
    git，不约束 docker build 的上下文上传）。排除表必须覆盖密钥与大体积路径。"""
    patterns = _dockerignore_patterns()
    required = {
        ".env": "真实密钥文件，绝不能进 build context",
        ".env.*": ".env 变体（.env.example 等）同不入上下文",
        ".git": "版本库历史（数百 MB）",
        ".venv": "本地虚拟环境（数百 MB）",
        "data": "*.duckdb/*.jsonl 数据产物（构建期由 gen_data.py 现生成，上下文不需要）",
        "tests": "测试代码与其 _db 产物（镜像不含测试）",
        "evidence": "评审/演示证据产物",
    }
    for p, why in required.items():
        assert p in patterns, f".dockerignore 缺少 {p}（{why}）"


def test_dockerignore_does_not_exclude_copy_sources():
    """Dockerfile COPY 白名单语义不变：requirements.txt/app/scripts/eval/web 五项
    （及 gen_data.py 构建步）必须不被 .dockerignore 排除——排除表只清非白名单
    路径，COPY 进镜像的内容不得因排除表悄悄缺失。"""
    patterns = _dockerignore_patterns()
    for src in _dockerfile_copy_sources():
        assert src not in patterns, \
            f"COPY 源 {src} 被 .dockerignore 排除——镜像内容将缺失（白名单语义被破坏）"
        # 顶级排除目录也不得是 COPY 源的父级（如误排 app 会吞掉 COPY app ./app）
        for p in patterns:
            if p.startswith("**/") or p.startswith("!") or "*" in p:
                continue   # 通配排除只命中原名匹配已单独查过的场景，父级误吞风险极低
            if (p.endswith("/") and src.startswith(p)) or src.startswith(p + "/"):
                raise AssertionError(f"COPY 源 {src} 落在排除目录 {p} 下——镜像内容将缺失")
