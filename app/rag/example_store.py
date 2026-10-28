"""问题→DSL 样例检索（few-shot RAG）。

选型说明（DESIGN.md「选型判断」章节）：
- 默认 TF-IDF 字符二元组：中文短问句检索效果够用、零模型下载、毫秒级、完全确定性；
- chroma 后端可通过 RAG_BACKEND=chroma 启用（语义检索更强，但引入模型下载与非确定性）。
这是"够用的最简方案优先"的一次显式取舍。
"""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path

from ..config import get_settings

SEED_PATH = Path(__file__).parent / "seed_examples.json"


def _bigrams(text: str) -> Counter:
    t = "".join(text.split()).lower()
    return Counter(t[i : i + 2] for i in range(len(t) - 1)) or Counter({t: 1})


class ExampleStore:
    """样例库接口：add(question, dsl) / search(question, k) -> [(question, dsl, score)]。"""

    def add(self, question: str, dsl: dict) -> None:
        raise NotImplementedError

    def search(self, question: str, k: int = 3) -> list[tuple[str, dict, float]]:
        raise NotImplementedError

    def size(self) -> int:
        raise NotImplementedError


class TfidfExampleStore(ExampleStore):
    def __init__(self):
        self._docs: list[tuple[str, dict]] = []
        self._tf: list[Counter] = []
        self._idf: dict[str, float] = {}

    def add(self, question: str, dsl: dict) -> None:
        self._docs.append((question, dsl))
        self._tf.append(_bigrams(question))

    def _rebuild_idf(self) -> None:
        df: Counter = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = max(1, len(self._tf))
        self._idf = {k: math.log((n + 1) / (v + 1)) + 1.0 for k, v in df.items()}

    def search(self, question: str, k: int = 3) -> list[tuple[str, dict, float]]:
        if not self._docs:
            return []
        self._rebuild_idf()
        q = _bigrams(question)
        scores = []
        for i, tf in enumerate(self._tf):
            common = set(q) & set(tf)
            if not common:
                scores.append(0.0)
                continue
            def weight(c: Counter, term: str) -> float:
                return (1 + math.log(c[term])) * self._idf.get(term, 1.0)
            num = sum(weight(q, t) * weight(tf, t) for t in common)
            den = math.sqrt(sum(weight(q, t) ** 2 for t in q) * sum(weight(tf, t) ** 2 for t in tf))
            scores.append(num / den if den else 0.0)
        ranked = sorted(zip(self._docs, scores), key=lambda x: x[1], reverse=True)[:k]
        return [(q_, d, round(s, 3)) for (q_, d), s in ranked if s > 0.05]

    def size(self) -> int:
        return len(self._docs)


class ChromaExampleStore(TfidfExampleStore):
    """可选向量后端：接口一致，内部用 chroma 默认嵌入。

    语义检索对长问句更稳，但首次运行需下载嵌入模型 —— 演示/CI 场景保持 tfidf。
    """

    def __init__(self):
        super().__init__()
        import chromadb  # noqa: PLC0415

        self._client = chromadb.Client()
        self._col = self._client.get_or_create_collection("askbi_examples")

    def add(self, question: str, dsl: dict) -> None:
        super().add(question, dsl)
        self._col.add(
            ids=[f"ex-{self.size()}"], documents=[question],
            metadatas=[{"dsl": json.dumps(dsl, ensure_ascii=False)}],
        )

    def search(self, question: str, k: int = 3) -> list[tuple[str, dict, float]]:
        res = self._col.query(query_texts=[question], n_results=min(k, max(1, self.size())))
        out = []
        for doc, meta, dist in zip(
            res["documents"][0], res["metadatas"][0], res["distances"][0]
        ):
            out.append((doc, json.loads(meta["dsl"]), round(1 - dist, 3)))
        return out


_STORE = None


def get_example_store() -> ExampleStore:
    global _STORE
    if _STORE is not None:
        return _STORE
    backend = get_settings().rag_backend
    store: ExampleStore = ChromaExampleStore() if backend == "chroma" else TfidfExampleStore()
    if SEED_PATH.exists():
        for item in json.loads(SEED_PATH.read_text(encoding="utf-8")):
            store.add(item["question"], item["dsl"])
    _STORE = store
    return store
