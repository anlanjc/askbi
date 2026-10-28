"""测试环境：mock LLM + 独立小库（12 个月，固定种子，session 级生成一次）。"""

from __future__ import annotations

import os
import pathlib

TEST_DB_DIR = pathlib.Path(__file__).parent / "_db"
os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("DB_PATH", str(TEST_DB_DIR / "test.duckdb"))
os.environ.setdefault("GENERATOR_MONTHS", "12")
os.environ.setdefault("RAG_BACKEND", "tfidf")

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402


@pytest.fixture(scope="session")
def db_path():
    s = get_settings()
    s.db_abs_path.parent.mkdir(parents=True, exist_ok=True)
    if not s.db_abs_path.exists():
        from app.data.generator import generate

        generate(s.db_abs_path, seed=s.generator_seed, months=12, end_month=s.generator_end_month)
    return s.db_abs_path
