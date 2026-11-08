"""练习场全局夹具：把 app 包加入 sys.path + mock LLM + 独立测试库。

与 askbi/tests/conftest.py 平行，但只服务 practice/ 目录：
- 不修改既有 tests/ 与 app/ 的任何文件；
- 测试库生成到 practice/_db/（独立目录，缺了自动生成，12 个月固定种子）。
"""

from __future__ import annotations

import os
import pathlib
import sys

PRACTICE_DIR = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = PRACTICE_DIR.parent  # askbi/

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TEST_DB_DIR = PRACTICE_DIR / "_db"
os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("DB_PATH", str(TEST_DB_DIR / "test.duckdb"))
os.environ.setdefault("GENERATOR_MONTHS", "12")
os.environ.setdefault("RAG_BACKEND", "tfidf")

import pytest

from app.config import get_settings


@pytest.fixture(scope="session")
def db_path():
    """练习专用 DuckDB（session 级生成一次，与 tests/_db 互不影响）。"""
    s = get_settings()
    s.db_abs_path.parent.mkdir(parents=True, exist_ok=True)
    if not s.db_abs_path.exists():
        from app.data.generator import generate

        generate(
            s.db_abs_path,
            seed=s.generator_seed,
            months=12,
            end_month=s.generator_end_month,
        )
    return s.db_abs_path
