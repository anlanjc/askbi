"""缓存正确性评测：开关缓存各跑一遍全量评测，逐条 diff 结果并对比延迟。

用法：python scripts/eval_cache.py
前提：无（mock 模式确定性，不需要 key）。数据文件缺失时评测脚本会自行生成。

验证两件事：
1. 正确性——缓存开启后每条用例的 ✓/✗ 必须与关闭时完全一致（缓存不得改变答案）；
2. 生效性——命中用例的耗时应显著下降（延迟差是缓存生效的旁证；命中率precision
   的进程内证据见 tests/test_cache.py 的计数器断言）。
"""

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = ROOT / "eval"
RUN_EVAL = ROOT / "scripts" / "run_eval.py"


def run_eval(env_extra: dict[str, str], tag: str) -> Path:
    out = EVAL_DIR / f"report_mock_{tag}.md"
    # 强制 mock：.env 里的 openai_compatible 会让评测烧真模型 key，本脚本只验证缓存正确性
    env = {**os.environ, "LLM_PROVIDER": "mock", **env_extra}
    subprocess.run(
        [sys.executable, str(RUN_EVAL)],
        cwd=ROOT, env=env, check=True, capture_output=True, text=True,
    )
    src = EVAL_DIR / "report_mock_none.md"
    if not src.exists():
        raise SystemExit(f"评测未生成报告：{src}")
    out.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return out


def parse_cases(path: Path) -> dict[str, tuple[str, int]]:
    """解析逐用例表：id -> (✓/✗, 耗时ms)。"""
    cases: dict[str, tuple[str, int]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*(\w+)\s*\|\s*([✓✗])\s*\|[^|]*\|\s*(\d+)\s*(?:\.?\d*)?\s*\|", line)
        if m:
            cases[m.group(1)] = (m.group(2), int(m.group(3)))
    return cases


def main() -> None:
    print("== 第一遍：缓存关闭 ==")
    off_report = run_eval({"CACHE_ENABLED": "0"}, "cache_off")
    print("== 第二遍：缓存开启 ==")
    on_report = run_eval({"CACHE_ENABLED": "1"}, "cache_on")

    off, on = parse_cases(off_report), parse_cases(on_report)
    if set(off) != set(on):
        missing = set(off) ^ set(on)
        raise SystemExit(f"用例集合不一致：{missing}")

    diffs = [cid for cid in off if off[cid][0] != on[cid][0]]
    slower = [cid for cid in off if on[cid][1] > off[cid][1]]
    off_avg = sum(t for _, t in off.values()) / len(off)
    on_avg = sum(t for _, t in on.values()) / len(on)

    print(f"\n用例数：{len(off)}")
    print(f"结果不一致：{len(diffs)} 条 {diffs if diffs else '（缓存未改变任何答案 ✓）'}")
    print(f"平均耗时：关闭 {off_avg:.0f}ms → 开启 {on_avg:.0f}ms（重复查询命中应显著下降）")
    if slower:
        print(f"变慢的用例（首次写入开销，属预期）：{slower}")
    if diffs:
        raise SystemExit("FAIL：缓存改变了评测结果，禁止上线")
    print("\nPASS：缓存正确性验证通过（结果逐条一致）")


if __name__ == "__main__":
    main()
