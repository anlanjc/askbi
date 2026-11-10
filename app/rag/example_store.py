"""问题→DSL 样例检索（few-shot RAG）。

选型说明（DESIGN.md「选型判断」章节）：
- 默认 TF-IDF 字符二元组：中文短问句检索效果够用、零模型下载、毫秒级、完全确定性；
- chroma 后端可通过 RAG_BACKEND=chroma 启用（语义检索更强，但引入模型下载与非确定性）。
这是"够用的最简方案优先"的一次显式取舍。

样例按数据集隔离（S9-OPT2，AB-3 挂账）：seed 条目带 spec 标签（seed_examples.json
的 "spec" 字段），get_example_store(dataset) 只装载同标签样例——coffee/saas 下
检索不到电商样例、反之亦然，few-shot 不再把生成往别的业务域带偏。默认数据集
（ecommerce）的样例库与改造前全局单库内容逐字节一致（同样 10 条、同算法同 IDF），
检索行为零变化（tests/test_rag_isolation.py 金标 pin 守门）。
"""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path

from ..config import get_settings

SEED_PATH = Path(__file__).parent / "seed_examples.json"

# seed 条目的 spec 标签字段；缺省视为 ecommerce（与打标签前的全局单库行为兼容）
_SPEC_FIELD = "spec"
_DEFAULT_SPEC_TAG = "ecommerce"


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
    collection 按数据集命名（askbi_examples_{dataset}），与 tfidf 路径同一隔离语义。
    """

    def __init__(self, dataset: str = _DEFAULT_SPEC_TAG):
        super().__init__()
        import chromadb

        self._client = chromadb.Client()
        self._col = self._client.get_or_create_collection(f"askbi_examples_{dataset}")

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


_STORES: dict[str, ExampleStore] = {}


def get_example_store(dataset: str | None = None) -> ExampleStore:
    """按数据集取样例库；None = settings.dataset（与 get_spec 同一约定）。

    各数据集的库只装载 seed 中带同 spec 标签的样例（缺省标签 = ecommerce）——
    跨源 few-shot 污染在库层隔离（S9-OPT2）。按 key 缓存：同数据集重复调用
    返回同一实例，ecommerce 库与改造前全局单库装载内容逐字节一致。
    """
    key = dataset if dataset is not None else get_settings().dataset
    if key in _STORES:
        return _STORES[key]
    backend = get_settings().rag_backend
    store: ExampleStore = ChromaExampleStore(key) if backend == "chroma" else TfidfExampleStore()
    if SEED_PATH.exists():
        for item in json.loads(SEED_PATH.read_text(encoding="utf-8")):
            if item.get(_SPEC_FIELD, _DEFAULT_SPEC_TAG) == key:
                store.add(item["question"], item["dsl"])
    _STORES[key] = store
    return store
