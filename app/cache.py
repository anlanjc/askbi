"""查询缓存：同 DSL 指纹回放 + TTL / 数据版本 / 编译 semver 三闸失效（E4 原创①；第三闸为评审 A1 补充）。

设计要点（完整决策见 docs/cache_adr.md）：
1. 缓存键 = 规范化 DSL 的 SHA256 指纹，而不是 SQL 字符串：
   - 维度顺序、过滤条件顺序、IN 列表顺序、相对时间写法（last_n_months vs between）、
     与 SQL 无关的 intent 字段全部归一——"不同写法的同一查询"命中同一缓存；
   - role 参与指纹：行级权限改变结果集，绝不允许跨角色复用缓存。
2. 失效三闸（缺一不可，ADR 论证；第三闸 semver 为对抗性评审 A1 补充）：
   - TTL：限定一切"漏失效"的最大错误窗口（默认 300s，可配）；
   - 数据版本号：读时计算 duckdb 数据集指纹（行数 / 最大日期 / 事实表行数），
     变化即全量失效——数据重生成不必等 TTL 到期。
3. 计数器 + stats()：命中/未命中/TTL 过期/版本失效/写入/淘汰 + 单飞合并/超时，线程安全（RLock）。
4. cache_enabled 默认 False：关闭时 get/set 直通，执行路径与无缓存时完全一致。
5. singleflight 并发击穿防护（AB 车道）：同 key 并发未命中只放一个 leader 执行
   （claim/publish/fail 三原语，执行方保持在事件循环原位——Sandbox 超时 interrupt 语义不迁移）；
   等待者共享 leader 结果；等待超时 fail-open 自行执行，绝不因单飞丢可用性。
   评审 5.0（AB-1）：claim 拆出"登记/等待"两段原语供事件循环调用方使用——等待者
   轮询 flight 终态而非阻塞线程池（40 等待者曾占满默认执行器把 leader 自己反堵死）；
   同 key 等待者名额有上限（超出直接 fail-open）；leader 终态由调用方 try/finally
   保障（fail_flight 幂等，publish 后为 no-op）——取消（CancelledError=BaseException）
   不再永久残留 flight。
6. 版本闸探测异常 fail-open（评审 5.0，AB-19）：_version_provider 抛错（缺库/文件锁
   IOException）时返回 None 哨兵——命中路径一律作废、写路径跳过缓存，失败方向 =
   "误失效"而非"脏命中"（与闸二设计哲学同向）；stats 记 version_probe_failures 留痕。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from .config import get_settings
from .data.dsl import _END_MONTH, QueryDSL  # 与编译器共用同一时间锚点，避免两处漂移
from .data.registry import DatasetSpec, get_spec

# 指纹格式版本：规范化规则本身变化时递增，防止新旧语义混存
_FINGERPRINT_VERSION = 1

# AB-1③：同 key 等待者名额默认上限（构造参数可覆盖；等待者不占线程池，此上限
# 只防极端惊群——超出者直接 fail-open 自行执行，单飞绝不成为可用性风险）
_MAX_FLIGHT_WAITERS = 16

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
            value = sorted(str(v) for v in value)
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


@dataclass
class _Flight:
    """单飞在飞记录：leader 执行期间同 key 的其他调用方挂在此事件上共享结果。"""

    event: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None   # leader 成功后回填；None=失败/被拆除（等待者重新竞选）
    waiters: int = 0             # AB-1③：在册等待者名额数（名额随本对象消亡，无泄漏）


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
        flight_wait_seconds: float | None = None,
        max_flight_waiters: int | None = None,
        spec: DatasetSpec | None = None,
    ):
        s = get_settings()
        self.spec = spec or get_spec(s.dataset)   # 版本探测/库路径随数据集（默认跟随配置）
        self.ttl_seconds = s.cache_ttl_seconds if ttl_seconds is None else ttl_seconds
        self.max_entries = s.cache_max_entries if max_entries is None else max_entries
        # AB-1③：同 key 等待者名额上限——等待者已在事件循环内等待（不占线程池），
        # 上限防的是极端惊群下无界等待队列与等待超时风暴，超出直接 fail-open。
        self._max_flight_waiters = _MAX_FLIGHT_WAITERS if max_flight_waiters is None else max_flight_waiters
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
        # singleflight（并发击穿防护）：同 key 在飞只放一个 leader 执行，等待者共享结果。
        # 等待上界兜底 leader 挂死/极慢：超时 fail-open 自行执行，单飞只是优化不是可用性风险。
        self._flight_wait = (
            s.cache_flight_wait_seconds if flight_wait_seconds is None else flight_wait_seconds
        )
        self._flight_lock = threading.Lock()
        self._flights: dict[str, _Flight] = {}
        self._counters = {
            "hits": 0, "misses": 0, "ttl_expired": 0,
            "version_invalidations": 0, "stores": 0, "evictions": 0,
            "singleflight_coalesced": 0, "singleflight_timeouts": 0,
            "singleflight_overflow": 0,      # AB-1③：等待者超名额上限直接 fail-open 留痕
            "version_probe_failures": 0,     # AB-19：版本闸探测异常 fail-open 留痕
        }

    # ---- 开关：调用时读 settings，测试/评测可在运行期翻转 ----
    @property
    def enabled(self) -> bool:
        if self._enabled_override is not None:
            return self._enabled_override
        return get_settings().cache_enabled

    @property
    def flight_wait_seconds(self) -> float:
        """等待上界（AB-1②：异步等待段在事件循环内等待，上界与 claim_flight 同源）。"""
        return self._flight_wait

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
        if current is None:
            # AB-19：版本探测异常（缺库/文件锁 IOException）→ 命中一律作废（误失效方向），
            # 但不撕毁条目——探测恢复后版本闸照常工作；查询照常执行不打断应答。
            with self._lock:
                self._counters["misses"] += 1
            return None
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
        if version is None:
            # AB-19：版本探测异常 → 写路径跳过缓存（查询结果照常返回，不打断应答）；
            # 不落无版本条目——否则探测恢复后该条目要靠版本比对或 TTL 才能清掉。
            return
        with self._lock:
            self._entries[key] = _Entry(result=dict(result), created_at=self._clock(), version=version)
            self._entries.move_to_end(key)
            self._counters["stores"] += 1
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
                self._counters["evictions"] += 1

    # ---- singleflight（并发击穿防护，AB 车道）：claim/publish/fail 三原语 ----
    # 执行方（executor_node）留在事件循环原位执行——Sandbox 的超时 interrupt 语义不迁移；
    # 缓存只做在飞登记与结果分发。等待超时 fail-open：单飞是优化，绝不成为可用性风险。
    # 评审 5.0（AB-1）：claim 拆出"登记/等待"两段原语（register_flight/flight_state/
    # release_waiter，全部纯内存、事件循环内直调）——异步调用方的等待不再阻塞线程池；
    # claim_flight 保留为同步复合形态（快路径 + 登记 + threading.Event 阻塞等待），
    # 供线程语境的脚本/评测/单测使用，语义与两段式逐字一致。

    def register_flight(self, key: str) -> tuple[str, _Flight | None]:
        """登记段（AB-1②）：快路径 get 由调用方先行，这里只做在飞登记——纯内存、无 IO，
        事件循环内直调不进线程池。

        返回 (outcome, flight)：
        - ("leader", None)  取得执行权（须以 publish_flight / fail_flight 收尾，或由调用方
          try/finally 的终态保障兜底 fail_flight）；
        - ("wait", flight)  等待者名额已占（waiters += 1）——持返回的 flight 引用用
          waiter_state 轮询终态，退出时（无论超时/取消/成功）调 release_waiter 回收名额；
        - ("miss", None)    等待者超出名额上限 → 直接 fail-open 自行执行（计
          singleflight_overflow，不占名额不占线程）。
        禁用态恒 ("leader", None) 且不起 flight——与无缓存行为一致（调用方通常先判 enabled）。
        """
        if not self.enabled:
            return "leader", None
        with self._flight_lock:
            flight = self._flights.get(key)
            if flight is None:
                self._flights[key] = _Flight()
                return "leader", None
            if flight.waiters >= self._max_flight_waiters:
                with self._lock:
                    self._counters["singleflight_overflow"] += 1
                return "miss", None
            flight.waiters += 1
            return "wait", flight

    def waiter_state(self, key: str, flight: _Flight) -> tuple[str, dict | None]:
        """等待段的一步（AB-1②）：持登记返回的 flight 引用读终态，纯内存、事件循环直调。

        关键在"持对象引用"而非查表：publish 是 结果挂对象 → 写缓存 → 拆表 顺序，
        读表会在"结果已挂/表项已拆"的微秒窗口漏看终态，误判 gone 而重新竞选
        （40 路并发曾因此连环重执行）；读对象则结果一经挂上必可读，拆表不影响。
        返回：
        - ("pending", None)  leader 仍在执行，等待者稍后再查；
        - ("flight", result) 共享 leader 结果（计 singleflight_coalesced）——等待结束；
        - ("gone", None)     leader 已拆 flight 且无结果（失败/取消）→ 重新 register 竞选。
        """
        with self._flight_lock:
            if flight.result is not None:
                with self._lock:
                    self._counters["singleflight_coalesced"] += 1
                return "flight", dict(flight.result)
            if self._flights.get(key) is not flight:
                return "gone", None
            return "pending", None

    def release_waiter(self, key: str, flight: _Flight | None = None, timed_out: bool = False) -> None:
        """等待者退出（AB-1②）：回收等待名额；timed_out=True 时计 singleflight_timeouts
        （fail-open 留痕，与 claim_flight 超时路径同口径）。

        按登记对象回收计数（精确）；flight=None（异常早于登记）退化为按键回收——
        flight 已换代时按新代名额回收，只可能少算一名（上限略偏保守），不会多放行。
        名额随 _Flight 对象消亡，无泄漏面。
        """
        if timed_out:
            with self._lock:
                self._counters["singleflight_timeouts"] += 1
        with self._flight_lock:
            target = flight if flight is not None else self._flights.get(key)
            if target is not None and target.waiters > 0:
                target.waiters -= 1

    def claim_flight(self, key: str, timeout: float | None = None) -> tuple[str, dict | None]:
        """单飞认领（同步复合形态：get 快路径 + register_flight 登记 + Event 阻塞等待）。

        返回 (outcome, shared_result)：
        - ("hit", result)   快路径命中，直接回放（与 get() 同语义同计数）；
        - ("leader", None)  取得执行权——调用方执行后必须 publish_flight / fail_flight 二选一收尾；
        - ("flight", result) 等待者共享 leader 结果（计 singleflight_coalesced）；
        - ("miss", None)    等待超时 fail-open（计 singleflight_timeouts）或等待者超名额
                            上限 fail-open（计 singleflight_overflow）——调用方自行执行，
                            结束后仍应 publish_flight（flight 已不在时为纯写缓存）。
        禁用态恒 ("leader", None) 且不起 flight、不计数——与无缓存行为一致。
        """
        hit = self.get(key)
        if hit is not None:
            return "hit", hit
        if not self.enabled:
            return "leader", None
        outcome, flight = self.register_flight(key)
        if outcome != "wait":                     # leader / miss（名额超限 fail-open）
            return outcome, None
        limit = self._flight_wait if timeout is None else timeout
        deadline = time.monotonic() + limit
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not flight.event.wait(remaining):
                self.release_waiter(key, flight, timed_out=True)
                return "miss", None
            if flight.result is not None:
                self.release_waiter(key, flight)
                with self._lock:
                    self._counters["singleflight_coalesced"] += 1
                return "flight", dict(flight.result)
            # event 已置但 result 空 → leader 失败拆 flight → 重新竞选（恰一名接棒）
            outcome, flight = self.register_flight(key)
            if outcome != "wait":
                return outcome, None

    def publish_flight(self, key: str, result: dict) -> None:
        """leader 执行成功后发布：结果挂上 flight 唤醒等待者，再写缓存、拆 flight。

        先分发后写缓存再拆除：等待者读 flight.result 不再探测数据版本；
        self.set 失败也保证 flight 必被拆除（try/finally），不留永久在飞记录。
        """
        with self._flight_lock:
            flight = self._flights.get(key)
            if flight is not None:
                flight.result = result
                flight.event.set()
        try:
            self.set(key, result)
        finally:
            with self._flight_lock:
                if flight is not None and self._flights.get(key) is flight:
                    del self._flights[key]

    def fail_flight(self, key: str) -> None:
        """leader 执行失败：拆 flight 并唤醒等待者（result=None → 等待者重新竞选接棒）。

        fail-open 超时方误调时可能拆到原 leader 的 flight——原 leader 之后 publish 为纯写缓存，
        等待者重新竞选多跑一次，代价有界（fail-open 语义，诚实记录）。
        """
        with self._flight_lock:
            flight = self._flights.pop(key, None)
        if flight is not None:
            flight.event.set()

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

    def clear(self, audit: Callable[[dict], None] | None = None) -> dict:
        """管理面缓存清空：条目清空 + 同窗口审计（"同事务"语义见 app/audit.py 模块头）。

        审计回调写失败 → 条目原样恢复（含 LRU 顺序）并向上抛——缓存丢失本无害可重建，
        但统一"审计失败=变更回滚"的语义，不给例外留口子。
        """
        with self._lock:
            snapshot = list(self._entries.items())
            removed = len(snapshot)
            self._entries.clear()
            info = {"removed": removed}
            if audit is not None:
                try:
                    audit(info)
                except Exception:
                    self._entries.update(snapshot)
                    raise
            return info

    # ---- 内部 ----
    def _current_version(self) -> str | None:
        """数据版本号（带 30s 记忆化窗口）：窗口内复用上次探测结果。

        错误方向与设计一致：窗口内数据被外部刷新 → 短暂漏检 → 仍被 TTL 兜底，
        绝不会"脏命中"（命中结果的版本一定经过比对）。

        AB-19：探测异常（缺库/文件锁 IOException 等）fail-open——返回 None 哨兵，
        绝不向上抛（duckdb 原文曾穿透成 SSE error，绕过降级矩阵）。调用方契约：
        get() 见 None 作废命中（误失效方向），set() 见 None 跳过写入。
        探测失败不进记忆化窗口（下次调用立即重试——缺库/文件锁类故障恢复即时生效，
        每次失败计 version_probe_failures 留痕）。
        """
        now = self._version_clock()
        memo = self._version_memo
        if memo is not None and now - memo[1] < self._version_ttl_seconds:
            return memo[0]
        try:
            version = self._version_provider()
        except Exception:                     # noqa: BLE001 - fail-open，见 docstring（AB-19）
            with self._lock:
                self._counters["version_probe_failures"] += 1
            return None
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
