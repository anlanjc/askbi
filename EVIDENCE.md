# AskBI EVIDENCE —— 证据链首页（面试时直接打开本文件）

> 六节结构：我做了什么/没做什么 → 关键决策 → 真实 bug → 评测表 → 复现命令 → 已知边界。
> 所有数字来自实际运行输出（evidence/ 归档），mock 与 real 严格分开标注。更新：2026-10-28/29 跨夜窗口。

## 1. 我做了什么 / 没做什么

**做了**：多 Agent ChatBI 全链路（LangGraph 7 节点状态机、NL→DSL→SQL 白名单编译器、DuckDB 只读沙箱、QA 数值质检闭环、行级权限、MCP stdio server、FastAPI SSE）；2026-10-28/29 补齐工程证据链：可靠性层（LLMError+指数退避+四节点降级）、故障注入 6 场景压测归因、压测矩阵与缓存性能 bug 修复、116 条分层评测集（构建期自检）、多轮对话评测（30 条 64 轮）、PROFILE 功能档位、净销售额口径治理、本证据链；2026-11-05 补齐：**HTTP 全栈压测双口径**（uvicorn 真进程 SSE 全链 vs 进程内，evidence/load_test_http.md）与**管理面审计日志**（谁在何时通过什么改了什么，变更与审计同窗口落盘、审计失败变更回滚，app/audit.py + /api/admin/* 四端点）。

**演示资产（10-29，界面可看可点）**：六帧真实浏览器截图（[docs/screenshots/](docs/screenshots/)，README 顶部嵌入）——首答 Vega 图表+数据表 / 复问 ⚡ 缓存命中徽章 / SQL 全文+口径逐字溯源 / 行级权限三角色 7·1·2 行对比（同题 boss/south/north）；答案卡一键「⬇ 导出 CSV」全链路落地（`app/export.py` 标准库 csv 零依赖：BOM 元信息段+SQL+数据表，Excel 直开；`/api/export?task_id=` 端点 RFC 5987 中文文件名，前端优先服务端/失败降级前端同构格式；9 项单测含注入转义 roundtrip 与 API 集成 404/409，真服务端到端实测 200）；零 key 一键演示复验全通：`scripts/demo.py`（5 问）+ `scripts/demo_full.py`（四场景）输出归档 [evidence/demo_smoke_ab4.txt](evidence/demo_smoke_ab4.txt) / [demo_full_ab4.txt](evidence/demo_full_ab4.txt)，真实答案导出样例 [export_csv_sample_ab4.csv](evidence/export_csv_sample_ab4.csv)；web 深链 `?q=&role=` 支持直达演示帧（截图与分享同一条路）。截图方法注记：SSE 流式页面 Edge `--headless --screenshot` 的 virtual-time-budget/--timeout 均截不到最终异步态，改用浏览器自动化等待答案卡 DOM 出现后截帧，并以服务重启清缓存区分首答与缓存命中帧——两次渲染均为 mock 服务真实响应，无摆拍。

**没做（诚实边界）**：未上生产（无真实 DAU/QPS，压测是 mock+模拟延迟形态）；数据是固定种子合成（10.4 万订单/19.8 万明细/24 个月，注入了可归因的业务故事）——合成是**评测可复现**的设计选择，真实企业接入时换 generator 即可，schema/口径/权限层按真实 BI 场景设计；开发过程 AI 辅助（Claude Code/GLM），所有权策略：理解密度+原创扩展背书，不虚构手写叙事。

## 2. 四个关键决策

1. **为什么不直接 NL2SQL**：LLM 只产出白名单约束的 DSL（枚举校验），物理 SQL 由编译器渲染——注入/幻觉表名在编译期不可达；DSL 可 diff 可单测，行级权限/LIMIT 策略编译期统一注入。
2. **为什么 QA 闭环值得做**：反向消融实证——关掉质检 100%→93.9%（关叙述)/90.9%(关质检)，6.1pp 是每个模块必要性的数字证据。
3. **为什么缓存默认关 + PROFILE 档位**：实验功能保守上线（零行为变化可回滚），PROFILE=full 一键演示——压测同时证明缓存修复后 +12% 吞吐，档位与证据互相背书。
4. **为什么语义层做成可注册数据源（AB-3）**：语义层（维度/指标/取值白名单/NL 词表）从模块级常量升级为 DatasetSpec 注册表——新增物理表结构完全不同的数据集 B（线下连锁咖啡，tickets/ticket_lines/stores/skus），**上层零改动**（图/节点/QA/评测/MCP 无一行 if dataset）；DEFAULT_SPEC 引用原常量对象 + 全部新参数带默认值，95 项存量测试零改动全绿、v1/v2 mock 100% 重跑作回归裁判。"支持多数据源接入"从口号变成可复现实证。ADR：docs/registry_adr.md。

## 3. 三个真实 bug（含复现与修复）

1. **缓存负收益（压测发现，10-28）**：开启缓存吞吐 -30%（69.3 vs 110.8 QPS @50 并发）。定位：`data_version` 每次 get 新开 DuckDB 连接跑 4 个聚合扫描，版本探测比被缓存查询还贵。修复：版本号 30s 记忆化（错误方向仍是误失效）。修复后 124.0 vs 110.8（**+12%**），P50 300 vs 341ms。回归：`tests/test_cache.py::test_version_probe_memoized_within_window`。
2. **LLM 不可用会 500（故障注入发现，10-28）**：任一节点 LLM 调用失败直接炸穿图。修复：客户端指数退避重试（429/5xx/超时×3）+ 四节点各自降级语义（澄清放行/生成拒答/叙述留空/质检回退确定性）。故障注入 6 场景 60 次注入零崩溃（evidence/fault_injection.md）。
3. **系统代理半死导致 API 全断（评测冒烟发现，10-29）**：httpx 连 DashScope 全部 WinError 10054 而 curl 正常。诊断链：手工 TLS 通 → 排除网络/DNS/TLS/ALPN → 跟踪 socket 发现连接目标是 **127.0.0.1:65533（系统代理）** → 代理对该域名转发损坏 → `NO_PROXY=dashscope.aliyuncs.com` 绕过后 401 直达。教训：connect 失败先查代理再查网络。

## 4. 评测表（分层口径）

| 层 | 集合 | 数字 | 说明 |
|---|---|---|---|
| smoke 回归 | v1 33 条 | mock 100% / real 100%（10-26 口径） | 回归集不是 benchmark |
| full 分层 | v2 116 条（80 query/12 归因/12 澄清/12 拒答，13 条 holdout） | mock **100%**（10-28） | 金标由 compile_sql 生成，构建期自检 mock 可答性 |
| full real | v2 116 条 | **94.8%**（110/116，数值一致率 100%）；分类别 query 79/80 · drill 12/12 · clarify 12/12 · refusal 7/12 | 10-29，成本 560,550 token ≈ ¥1.7 |
| 校准迭代 | refusal/clarify 子集 | clarifier prompt 增补 4 类 refuse 规则（**只用 main split 失败信号**）→ 两子集 **12/12 + 12/12** | holdout 不参与调参 |
| holdout 盲测 | 13 条 | run1 84.6% / run2 **100%**（失败均为 drill 类运行间波动，主跑同题全 ✓） | 真实口径 ≈92.3%±波动 |
| **no_qa 消融（real）** | v2 116 条 | **93.1%**：drill **5/12=42%**（vs 主跑 12/12）——**QA 修订循环在 drill 类价值 +58pp**；叙述数值一致率 100%→95.5%；refusal/clarify 校准后无 QA 也 12/12 | 成本 544,462 token；v2-078 失败经 trace 定位=**维度顺序漂移**（非语义错）→ 判分已修为列序无关 |
| 多轮 | 30 条 64 轮 | mock **100%**（AB-2 口径）；real **24/30 条目、57/64 轮=89.1%**（10-29，221,065 token）：**机制场景全绿**——指代消解 16/17+时间追问 6/6+组合 3/3+缓存复问 10/10（命中 4/4）+澄清流 4/4+行级权限多轮 6/6+无上下文 1/1；失败 7 轮全在**边界探针**（fallback/词表边界/多实体守卫——mock 期望编码 mock 的保守规则，真实模型边界判断不同，属预期差异非机制缺陷） | evidence/multiturn_report_real.md |
| **多数据源（AB-3）** | B 集 26 条（18 query/3 归因/2 澄清/3 拒答，咖啡域） | mock **100%**（26/26，DATASET=coffee 全链路落 B 库）；金标由 compile_sql(spec=coffee) 生成+构建期自检 | 语义层解耦实证；同问题双源 SQL/结果不同、缓存互不命中 |
| 故障注入 | 6 场景 ×10 | 零崩溃，全部诚实降级 | evidence/fault_injection.md |
| 压测（进程内） | 12 点位矩阵 | 编排上限 110-124 QPS；300ms 模拟延迟下 P50≈1.2s≈4×LLM（10-28 存档，11-05 同日复测 ±4% 内复现） | evidence/load_test.md |
| **压测（HTTP 全栈双口径）** | 22 点位（进程内 vs uvicorn 真进程 SSE 全链，11-05） | c=1 差值法附加开销 **5.1ms**（框架地板 3.9ms + SSE 净附加 ≈1.2ms）；delay=300ms 下 HTTP≈进程内 **0.92~0.97×**（瓶颈=LLM 串行调用，框架被摊薄）；delay=0 高并发 0.53× 系**共址 CPU 争抢伪影**（诚实标注）；缓存收益随瓶颈位置变化（c=1 +36% / 高并发 +3~4%） | evidence/load_test_http.md |

pytest：**129 项全绿**（11-05 实测；最新增量 = 管理面审计 8 项，tests/test_audit.py；此前基线 121 绿）。金标生成：`scripts/build_dataset_v2.py` / `scripts/build_dataset_b.py`（可重建/自检）。

## 5. 复现命令

```bash
# 安装（无 key 可跑全链路）
pip install -r requirements.txt
python scripts/gen_data.py                 # 种子 42，确定性重建 DuckDB

# 测试与评测
python -m pytest -q                        # 94 项
LLM_PROVIDER=mock python scripts/run_eval.py                          # v1 smoke 33
LLM_PROVIDER=mock python scripts/run_eval.py --dataset eval/dataset_v2.json   # v2 116
LLM_PROVIDER=mock python scripts/run_eval.py --dataset eval/dataset_v2.json --split holdout
python scripts/build_dataset_v2.py --check   # 金标自检（可重建评测集）
python scripts/run_multiturn_eval.py         # 多轮 30 条（PROFILE=full 内部处理）
python scripts/fault_inject.py --n 10        # 故障注入 → evidence/fault_injection.md
python scripts/load_test.py                  # 压测矩阵（进程内）→ evidence/load_test.md
python scripts/load_test_http.py             # HTTP 全栈双口径（uvicorn 真进程）→ evidence/load_test_http.md
python scripts/demo_full.py                  # PROFILE=full 四场景演示

# 管理面（审计日志：谁在何时通过什么改了什么，app/audit.py）
curl -X POST http://127.0.0.1:8000/api/admin/tasks/clear   # 任务清除（JSONL 归档改名可回放）
curl -X POST http://127.0.0.1:8000/api/admin/cache/clear   # 缓存清空
curl -X POST http://127.0.0.1:8000/api/admin/config -H "Content-Type: application/json" -d "{\"cache_enabled\": true}"   # 运行期开关 from→to
curl http://127.0.0.1:8000/api/admin/audit                 # 审计只读查询（最新在前）
# ADMIN_TOKEN 配置后所有 /api/admin/* 须带 X-Admin-Token 头（常量时间比较）

# 第二数据源（AB-3：线下连锁咖啡，DATASET 环境变量切换，默认 ecommerce）
python scripts/gen_data_b.py                 # 生成 B 库（~5 万行，种子 42，双业务故事可归因）
DATASET=coffee LLM_PROVIDER=mock python scripts/run_eval.py --dataset eval/dataset_b.json   # B 集 26 条
python scripts/build_dataset_b.py --check    # B 金标自检（compile_sql(spec=coffee) 同源）

# 真模型（需 DashScope key 于 .env；系统代理半死时先 NO_PROXY）
LLM_PROVIDER=openai_compatible python scripts/run_eval.py --dataset eval/dataset_v2.json
```

## 6. 已知边界与下一步

- 评测是 mock 可判定集（措辞对齐解析器词表），真实模型泛化上限由 real 报告的失败明细界定；holdout 15 条不参与调参。
- 多轮 followup 的渠道词表已收敛到 DIMENSION_VALUES 单一来源；"换成 X 渠道"重复后缀 bug 已修复（AB-2 评审发现）。
- clarify_suspend 需 API 层 thread_id/resume（图内 run_ask 不挂起）——多轮评测脚本按此适配（ADR 见 docs/multiturn_adr.md）。
- 第二数据源适配器已落地（registry_adr.md）：B 数据集共享全局 ROLE_SCOPES（同一大区划分）；RAG 样例库未按数据集隔离（真实 provider 下 B 问题会检索到电商 few-shot，mock 不受影响）；多轮指代消解词表仍取默认数据集（followup 默认关闭，边界如实声明）。
- 管理面审计日志（11-05）："同事务"语义由 JSONL 翻译实现——变更与审计同锁窗口落盘、审计写失败变更回滚（app/audit.py 模块头有完整推导）；任务清除的归档改名假定单 worker 部署口径，多 worker 管理面属 PRODUCTION_GAP；ADMIN_TOKEN 留空=开放但全审计（与 api_key 同哲学）。
- HTTP 全栈压测（11-05）：客户端与 uvicorn 同机共址，delay=0 高并发点位 QPS 受 CPU 争抢压制（0.53×），**不当服务端容量上限使用**；delay=300ms（真实形态近似）下 0.92~0.97× 才是部署参考口径。
- 下一步：200+ 条评测集、RAG 样例库按数据集隔离、真实部署与灰度。
