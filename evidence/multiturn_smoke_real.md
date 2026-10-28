# AskBI 多轮评测报告（eval v2 追加集 · mock）

- 评测集：`eval/dataset_v2_followup.json`（--only clarify_suspend）
- 环境：provider=mock（零 API 花费） · PROFILE=full（cache/followup 开）· 挂起条目走独立 checkpointer 图
- 结果：条目全对 **2/2** · 轮次正确 **4/4**（100.0%）· 缓存命中轮 0
- 延迟 avg 58.4ms · token ≈0 · 缓存计数 {"hits": 0, "misses": 2, "stores": 2}

| 场景 | 轮次通过 |
|---|---|
| clarify_suspend | 4/4 |

| 条目.轮 | 结果 | 场景 | 角色 | 问（消解后） | 期望 | 缓存 | 消解说明/失败详情 |
|---|---|---|---|---|---|---|---|
| fu28.t1 | ✓ | clarify_suspend | boss | 最近卖得怎么样 | None | - |  |
| fu28.t2 | ✓ | clarify_suspend | boss | 销售额 | answer | - |  |
| fu29.t1 | ✓ | clarify_suspend | boss | 上月各渠道表现如何 | None | - |  |
| fu29.t2 | ✓ | clarify_suspend | boss | 销售额 | answer | - |  |