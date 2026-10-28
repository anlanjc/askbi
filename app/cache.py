"""查询缓存：同 DSL 指纹回放 + TTL / 数据版本双闸失效（E4 原创①）。

设计要点（完整决策见 docs/cache_adr.md）：
1. 缓存键 = 规范化 DSL 的 SHA256 指纹，而不是 SQL 字符串：
   - 维度顺序、过滤条件顺序、IN 列表顺序、相对时间写法（last_n_months vs between）、
     与 SQL 无关的 intent 字段全部归一——"不同写法的同一查询"命中同一缓存；
   - role 参与指纹：行级权限改变结果集，绝不允许跨角色复用缓存。
2. 失效双闸（缺一不可，ADR 论证）：
   - TTL：限定一切"漏失效"的最大错误窗口（默认 300s，可配）；
   - 数据版本号：读时计算 duckdb 数据集指纹（行数 / 最大日期 / 事实表行数），
     变化即全量失效——数据重生成不必等 TTL 到期。
3. 计数器 + stats()：命中/未命中/TTL 过期/版本失效/写入/淘汰，线程安全（RLock）。
4. cache_enabled 默认 False：关闭时 get/set 直通，执行路径与无缓存时完全一致。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import duckdb

from .config import get_settings
from .data.dsl import QueryDSL, _END_MONTH  # 与编译器共用同一时间锚点，避免两处漂移
from .data.registry import DatasetSpec, get_spec

# 指纹格式版本：规范化规则本身变化时递增，防止新旧语义混存
_FINGERPRINT_VERSION = 1

# semver 注入钩子（test_cache 依赖契约）：非 None 时直接作为编译语义版本参与指纹
_COMPILE_SEMVER_CACHE: str | None = None
# 正常路径的按数据集记忆化：semver = 各数据集指标表达式集合的短哈希
_COMPILE_SEMVER_BY_DS: dict[str, str] = {}


def _compile_semver(spec: DatasetSpec) -> str:
    """编译语义版本：对数据集的指标表达式集合做短哈希（评审 A1）。

    指标口径或编译器语义变更（开发者改 metrics/compile_sql）时，哈希变化
    → 该数据集全部旧指纹失效。按数据集分键惰性计算并缓存；
    _COMPILE_SEMVER_CACHE 非 None 时为测试/运维显式注入，直接生效。
    """
    if _COMPILE_SEMVER_CACHE is not None:
        return _COMPILE_SEMVER_CACHE
    cached = _COMPILE_SEMVER_BY_DS.get(spec.name)
    if cached is None:
        try:
            payload = json.dumps({k: getattr(m, "expr", str(m)) for k, m in spec.metrics.items()},
                                 sort_keys=True, ensure_ascii=False, default=str)
        except Exception:                     # noqa: BLE001 - 版本计算失败不阻断指纹
            payload = "unknown"
        cached = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
        _COMPILE_SEMVER_BY_DS[spec.name] = cached
    return cached


# ---------------- 指纹：规范化 DSL ----------------


def _shift_month(ym: str, k: int) -> str:
    """'2026-09' 平移 k 个月（k 可为负）。"""
    y, m = int(ym[:4]), int(ym[5:7])
    total = y * 12 + (m - 1) + k
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _norm_time(t: dict, end_month: str = _END_MONTH) -> list[str] | None:
    """时间条件归一为绝对月窗口 [start, end]；all（含退化写法）→ None。

    与 TimeRange.to_sql 语义对齐：last_n_months n 缺失/为 0 时编译为 TRUE，即 all。
    last_n_months n=1（上月）与 between('2026-09','2026-09') 归一为同一窗口。
    """
    mode = t.get("mode", "all")
    if mode == "last_n_months" and t.get("n"):
        n = max(1, int(t["n"]))
        return [_shift_month(end_month, -(n - 1)), end_month]
    if mode == "between":
        start, end = t.get("start"), t.get("end")
        if not start and not end:
            return None          # between 全空 ≡ all（评审 A3：归一合流）
        return [start or "", end or ""]
    return None


def _norm_filters(filters: list[dict]) -> list[dict]:
    """过滤条件排序 + IN 列表值排序（SQL 集合语义与书写顺序无关）。"""
    out = []
    for f in filters or []:
        value = f.get("value")
        if isinstance(value, list):
            value = sorted((str(v) for v in value))
        elif value is not None:
            value = str(value)    # 评审 A4：1 与 "1" 同值异键 → 统一字符串化
        out.append({"field": f.get("field"), "op": f.get("op"), "value": value})
    out.sort(key=lambda f: json.dumps(f, sort_keys=True, default=str, ensure_ascii=False))
    return out


def canonical_dsl(dsl: dict | QueryDSL, role: str = "boss", dataset: str = "ecommerce") -> dict:
    """规范化 DSL：只保留影响 SQL 语义的字段（对照 compile_sql 的输入面）。

    丢弃 intent（从不参与编译）；维度/过滤归一；时间解析为绝对窗口。
    role 显式参与——行级权限在编译期注入，同 DSL 不同角色结果不同。
    dataset 显式参与——同 DSL 不同数据源的 SQL 完全不同，绝不跨源命中缓存。
    """
    if isinstance(dsl, QueryDSL):
        dsl = dsl.model_dump()
    d = dict(dsl)
    ob = d.get("order_by")
    spec = get_spec(dataset)
    return {
        "v": _FINGERPRINT_VERSION,
        "ds": spec.name,               # 数据集标识：跨源隔离的第一道闸
        "semver": _compile_semver(spec),   # 评审 A1：指标字典/编译器语义变更 → 全缓存失效
        "role": role,
        "metric": d.get("metric"),
        "dimensions": sorted(d.get("dimensions") or []),
        "filters": _norm_filters(d.get("filters") or []),
        "time_window": _norm_time(d.get("time") or {}, spec.end_month),
        "order_by": (
            {"metric": ob.get("metric"), "direction": ob.get("direction", "desc")}
            if isinstance(ob, dict) else None
        ),
        "limit": d.get("limit"),
    }


def fingerprint(dsl: dict | QueryDSL, role: str = "boss", dataset: str = "ecommerce") -> str:
    """规范化 DSL 的 SHA256 指纹（缓存键）。dataset 缺省为默认数据集。"""
    payload = json.dumps(
        canonical_dsl(dsl, role, dataset), sort_keys=True, ensure_ascii=False, default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------- 数据版本号（失效闸之二） ----------------


def data_version(db_path: str | Path, spec: DatasetSpec | None = None) -> str:
    """数据集指纹：事实行数 + 最大日期 + 明细行数 + 明细去重单数，读时计算。

    探测 SQL 随数据集走（spec 声明，表名不同）。这是启发式指纹
    （内容碰撞理论上可能，ADR 有论证）——TTL 兜底其错误窗口。
    """
    if spec is None:
        from .data.registry import DEFAULT_SPEC  # 延迟导入保持默认路径轻量

        spec = DEFAULT_SPEC
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        row = con.execute(spec.version_probe_sql).fetchone()
    finally:
        con.close()
    return "|".join(str(x) for x in row)


# ---------------- 缓存本体 ----------------


@dataclass
class _Entry:
    result: dict      # 规范化后的结果集（columns/rows/row_count/truncated/elapsed_ms[/note]）
    created_at: float
    version: str


class QueryCache:
    """进程内查询缓存（OrderedDict 近似 LRU + TTL + 版本双闸），线程安全。"""

    def __init__(
        self,
        ttl_seconds: int | None = None,
        max_entries: int | None = None,
        version_provider: Callable[[], str] | None = None,
        db_path: str | Path | None = None,
        enabled: bool | None = None,
        clock: Callable[[], float] = time.time,
        version_clock: Callable[[], float] = time.monotonic,
        version_ttl_seconds: float | None = None,
        spec: DatasetSpec | None = None,
    ):
        s = get_settings()
        self.spec = spec or get_spec(s.dataset)   # 版本探测/库路径随数据集（默认跟随配置）
        self.ttl_seconds = s.cache_ttl_seconds if ttl_seconds is None else ttl_seconds
        self.max_entries = s.cache_max_entries if max_entries is None else max_entries
        self._version_provider = version_provider or (
            lambda: data_version(db_path or self.spec.db_abs_path, self.spec)
        )
        self._enabled_override = enabled
        self._clock = clock
        self._version_clock = version_clock
        # 版本探测记忆化：data_version 是 4 个聚合扫描，本身可能比被缓存的查询还贵
        # （压测实测：每次 get 探测使吞吐 -30%）。30s 窗口内复用，失效延迟上界 = 窗口 + TTL。
        self._version_ttl_seconds = 30.0 if version_ttl_seconds is None else version_ttl_seconds
        self._version_memo: tuple[str, float] | None = None
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._counters = {
            "hits": 0, "misses": 0, "ttl_expired": 0,
            "version_invalidations": 0, "stores": 0, "evictions": 0,
        }

    # ---- 开关：调用时读 settings，测试/评测可在运行期翻转 ----
    @property
    def enabled(self) -> bool:
        if self._enabled_override is not None:
            return self._enabled_override
        return get_settings().cache_enabled

    # ---- 对外接口 ----
    def get(self, key: str) -> dict | None:
        """命中返回结果集副本；未命中/过期/版本变化返回 None。禁用态恒 None 且不计数。"""
        if not self.enabled:
            return None
        now = self._clock()
        entry: _Entry | None
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._counters["misses"] += 1
                return None
            # 闸一：TTL
            if now - entry.created_at >= self.ttl_seconds:
                del self._entries[key]
                self._counters["ttl_expired"] += 1
                self._counters["misses"] += 1
                return None
        # 闸二：数据版本（读时计算、带记忆化窗口；失败方向是"误失效"而非"脏命中"）
        current = self._current_version()
        now2 = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:  # 并发下已被清除
                self._counters["misses"] += 1
                return None
            if now2 - entry.created_at >= self.ttl_seconds:   # 评审 A2：锁外窗口跨过 TTL 的竞态
                del self._entries[key]
                self._counters["ttl_expired"] += 1
                self._counters["misses"] += 1
                return None
            if entry.version != current:
                self._purge_version_locked(entry.version)
                self._counters["version_invalidations"] += 1
                self._counters["misses"] += 1
                return None
            self._entries.move_to_end(key)
            self._counters["hits"] += 1
            return dict(entry.result)

    def set(self, key: str, result: dict) -> None:
        """写入（仅在执行成功后调用）。禁用态为 no-op。"""
        if not self.enabled:
            return
        version = self._current_version()
        with self._lock:
            self._entries[key] = _Entry(result=dict(result), created_at=self._clock(), version=version)
            self._entries.move_to_end(key)
            self._counters["stores"] += 1
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
                self._counters["evictions"] += 1

    def stats(self) -> dict:
        with self._lock:
            hits, misses = self._counters["hits"], self._counters["misses"]
            lookups = hits + misses
            return {
                "enabled": self.enabled,
                "size": len(self._entries),
                "ttl_seconds": self.ttl_seconds,
                "max_entries": self.max_entries,
                "hit_rate": round(hits / lookups, 4) if lookups else 0.0,
                **self._counters,
            }

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    # ---- 内部 ----
    def _current_version(self) -> str:
        """数据版本号（带 30s 记忆化窗口）：窗口内复用上次探测结果。

        错误方向与设计一致：窗口内数据被外部刷新 → 短暂漏检 → 仍被 TTL 兜底，
        绝不会"脏命中"（命中结果的版本一定经过比对）。
        """
        now = self._version_clock()
        memo = self._version_memo
        if memo is not None and now - memo[1] < self._version_ttl_seconds:
            return memo[0]
        version = self._version_provider()
        self._version_memo = (version, now)
        return version

    def _purge_version_locked(self, old_version: str) -> None:
        """数据版本变化 → 全量失效：清掉所有旧版本条目。"""
        stale = [k for k, e in self._entries.items() if e.version == old_version]
        for k in stale:
            del self._entries[k]


# ---------------- 全局单例（与 get_settings/get_graph 同风格） ----------------

_CACHE: QueryCache | None = None
_CACHE_LOCK = threading.Lock()


def get_query_cache() -> QueryCache:
    global _CACHE
    if _CACHE is None:
        with _CACHE_LOCK:
            if _CACHE is None:
                _CACHE = QueryCache()
    return _CACHE


def reset_query_cache() -> None:
    """评测/测试用：丢弃全局实例，下次 get_query_cache() 重建。"""
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None


# ---------------- 结果列对齐（维度顺序归一的配套） ----------------


def align_columns(result: dict, requested_dims: list[str]) -> dict:
    """命中结果按请求方的维度顺序重排列（缓存以首次执行的列序存储）。

    编译器保证列结构恒为 维度... + value，因此可安全重排；
    结构不符预期时防御性原样返回。
    """
    cols = result.get("columns") or []
    if not requested_dims or cols[: len(requested_dims)] == requested_dims:
        return result
    if sorted(cols[: len(requested_dims)]) != sorted(requested_dims) or cols[-1:] != ["value"]:
        return result
    perm = [cols.index(d) for d in requested_dims] + [len(cols) - 1]
    rows = [[r[i] for i in perm] for r in result.get("rows", [])]
    return {**result, "columns": list(requested_dims) + ["value"], "rows": rows}
