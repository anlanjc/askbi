"""文档一致性回归（ASKBI-6，评审 4.0）：断链与宣称缺口漂移的守门。

修复前三处漂移：①DEPLOY.md 部署证据动作链接指向不存在的 PILOT_REPORT.md
（仓库根只有 PILOT_TEMPLATE.md）；②EVIDENCE §6 与 registry_adr §7 仍宣称
「RAG 样例库未按数据集隔离」为开放待办，而 3b2becd（S9-OPT2）已落地隔离并有
7 项回归守门；③EVIDENCE §2 把 v1 33 条消融数字标注为 v2 116 条口径。
本测试钉住：md 相对链接目标必须存在、已收口缺口不得残留"未做"措辞。
纯静态文本断言。
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MD_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def test_markdown_relative_links_point_to_existing_files():
    """全仓 markdown 的相对 .md 链接目标必须存在（曾 PILOT_REPORT.md 断链）。"""
    broken: list[str] = []
    for md in _ROOT.rglob("*.md"):
        rel_parent = md.parent
        for target in _MD_LINK.findall(md.read_text(encoding="utf-8")):
            t = target.strip()
            if t.startswith(("http://", "https://", "#", "mailto:")):
                continue
            path_part = t.split("#", 1)[0]
            if not path_part.endswith(".md"):
                continue   # 截图/目录锚点不在本断言范围
            if not (rel_parent / path_part).exists():
                broken.append(f"{md.relative_to(_ROOT)} -> {t}")
    assert not broken, f"断链（相对 .md 链接目标不存在）：{broken}"


def test_rag_isolation_gap_claim_is_closed():
    """RAG 样例库隔离已由 3b2becd（S9-OPT2）落地——文档不得残留「未按数据集隔离」开放缺口措辞。"""
    for name in ("EVIDENCE.md", "docs/registry_adr.md"):
        text = (_ROOT / name).read_text(encoding="utf-8")
        assert "未按数据集隔离" not in text, \
            f"{name} 仍宣称 RAG 样例库未按数据集隔离——该缺口已收口（tests/test_rag_isolation.py 9 项守门）"


def test_ablation_label_matches_archive_sample_size():
    """EVIDENCE §2 消融数字必须与归档报告样本量同口径：100%→93.9%/90.9% 是 v1 33 条，
    v2 116 条消融为 94.8%→93.1%/95.5%——不得混标（曾把 v1 数字标成 v2 116 条）。"""
    text = (_ROOT / "EVIDENCE.md").read_text(encoding="utf-8")
    if "93.9%" in text:   # v1 33 条数字在场的同一句必须标 v1 口径
        sentence = next(s for s in text.split("\n") if "93.9%" in s)
        assert "v1 33 条" in sentence or "33 条" in sentence, \
            f"93.9%/90.9% 属 v1 33 条口径，句子必须标注样本量：{sentence}"
    assert "v2 116 条）——准确率 100%→93.9%" not in text, \
        "v1 33 条消融数字不得标注为 v2 116 条口径"
