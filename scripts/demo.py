"""无 API key 的命令行演示（mock 模式）：一条龙跑通 澄清/查询/归因/拒答。

用法：
    python scripts/demo.py          # 前置：python scripts/gen_data.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.graph import run_ask  # noqa: E402
from app.config import get_settings  # noqa: E402

QUESTIONS = [
    "上个月的销售额是多少",
    "为什么上月美妆品类毛利率下滑了",
    "最近卖得怎么样",           # → 澄清
    "上月客服满意度是多少",       # → 拒答（指标不存在）
    "各渠道最近3个月的客单价",
]


async def main() -> None:
    s = get_settings()
    print(f"provider={s.llm_provider}  db={s.db_abs_path}\n" + "=" * 60)
    for q in QUESTIONS:
        print(f"\n❓ {q}")
        state = await run_ask(q, role="boss")
        a = state.get("answer", {})
        if a.get("kind") == "clarify":
            print(f"🤔 需要澄清：{a['message']}  选项：{a.get('options')}")
        elif a.get("kind") == "refusal":
            print(f"🚫 拒答：{a['message']}")
        else:
            print(f"💬 {a.get('summary')}")
            if a.get("narrative"):
                print(f"📖 {a['narrative'][:300]}")
            qa = a.get("qa")
            if qa:
                print(f"🔬 QA：passed={qa['passed']} 核验数字 {qa['numbers_checked']} 个")
            for ev in state.get("events", [])[-3:]:
                print(f"   · [{ev['node']}] {ev['type']}")
        usage = state.get("usage", {})
        tokens = sum(u["prompt_tokens"] + u["completion_tokens"] for u in usage.values())
        print(f"⚙️ nodes={len(usage)} tokens≈{tokens}")


if __name__ == "__main__":
    asyncio.run(main())
