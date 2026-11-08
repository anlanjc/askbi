"""估算真实模型跑一遍评测的 token 用量与成本（无需 API key）。

原理：mock 模式下完整跑一遍 31 条评测，逐次记录 LLM 调用的
prompt/completion 字符量（CJK 与 ASCII 分开统计），按比例区间换算 token：

    CJK   ≈ 0.6 ~ 1.0 token/字（DeepSeek/GPT 系分词器对中文的常见区间）
    ASCII ≈ 0.25 ~ 0.33 token/字符（JSON/代码/数字较稠密，约 3~4 字符/token）

用法：
    python scripts/estimate_tokens.py                     # 按默认单价估成本
    python scripts/estimate_tokens.py --price-in 2 --price-out 3
（单价为 每百万 token 人民币，对照所用厂商价格页填入）
"""

import argparse
import asyncio
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents import nodes
from app.llm.mock import MockLLM

records: list[dict] = []


def _cjk(s: str) -> int:
    return sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")


class CountingLLM(MockLLM):
    async def chat(self, req):
        resp = await super().chat(req)
        prompt_text = req.system + req.user
        records.append({
            "node": req.node,
            "p_cjk": _cjk(prompt_text),
            "p_ascii": len(prompt_text) - _cjk(prompt_text),
            "c_cjk": _cjk(resp.content),
            "c_ascii": len(resp.content) - _cjk(resp.content),
        })
        return resp


def _tok(cjk: int, ascii_: int, cjk_ratio: float, ascii_ratio: float) -> int:
    return int(cjk * cjk_ratio + ascii_ * ascii_ratio)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--price-in", type=float, default=2.0, help="每百万输入 token 单价（元）")
    ap.add_argument("--price-out", type=float, default=3.0, help="每百万输出 token 单价（元）")
    args = ap.parse_args()

    nodes.get_llm = lambda: CountingLLM()  # 替换节点层 LLM 工厂为计数版
    from app.eval.runner import run

    await run("token_estimate", "none", None)

    LO = (0.6, 0.25)
    HI = (1.0, 0.33)
    agg = defaultdict(lambda: {"n": 0, "p_cjk": 0, "p_ascii": 0, "c_cjk": 0, "c_ascii": 0})
    for r in records:
        a = agg[r["node"]]
        a["n"] += 1
        for k in ("p_cjk", "p_ascii", "c_cjk", "c_ascii"):
            a[k] += r[k]

    print(f"\n{'节点':<12}{'调用数':>5}{'输入tok(低-高)':>18}{'输出tok(低-高)':>18}")
    tot_in_lo = tot_in_hi = tot_out_lo = tot_out_hi = 0
    for node, a in agg.items():
        in_lo = _tok(a["p_cjk"], a["p_ascii"], *LO)
        in_hi = _tok(a["p_cjk"], a["p_ascii"], *HI)
        out_lo = _tok(a["c_cjk"], a["c_ascii"], *LO)
        out_hi = _tok(a["c_cjk"], a["c_ascii"], *HI)
        tot_in_lo, tot_in_hi = tot_in_lo + in_lo, tot_in_hi + in_hi
        tot_out_lo, tot_out_hi = tot_out_lo + out_lo, tot_out_hi + out_hi
        print(f"{node:<12}{a['n']:>5}{in_lo:>10} - {in_hi:<6}{out_lo:>10} - {out_hi:<6}")

    calls = len(records)
    print("-" * 55)
    print(f"合计调用 {calls} 次｜输入 {tot_in_lo}~{tot_in_hi} tok｜输出 {tot_out_lo}~{tot_out_hi} tok")
    cost_lo = tot_in_lo / 1e6 * args.price_in + tot_out_lo / 1e6 * args.price_out
    cost_hi = tot_in_hi / 1e6 * args.price_in + tot_out_hi / 1e6 * args.price_out
    print(f"成本估算（in ¥{args.price_in}/M, out ¥{args.price_out}/M）：¥{cost_lo:.2f} ~ ¥{cost_hi:.2f} / 遍")
    print("注：DeepSeek 的静态系统提示词（schema+指标字典）跨请求命中上下文缓存后，输入成本还能再降 20~40%。")


if __name__ == "__main__":
    asyncio.run(main())
