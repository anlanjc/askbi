# AskBI 多轮评测报告（eval v2 追加集 · openai_compatible）

- 评测集：`eval/dataset_v2_followup.json`
- 环境：provider=openai_compatible · PROFILE=full（cache/followup 开）· 挂起条目走独立 checkpointer 图
- 结果：条目全对 **24/30** · 轮次正确 **57/64**（89.1%）· 缓存命中轮 4
- 延迟 avg 22325.7ms · token ≈221065 · 缓存计数 {"hits": 4, "misses": 47, "stores": 47}

| 场景 | 轮次通过 |
|---|---|
| entity_switch | 16/17 |
| entity_fallback | 1/2 |
| entity_boundary_refusal | 1/2 |
| guard_multi_entity | 0/2 |
| time_followup | 6/6 |
| entity_then_time | 3/3 |
| cache_repeat | 10/10 |
| clarify_flow | 4/4 |
| role_scope | 4/4 |
| role_followup | 2/2 |
| no_context | 1/1 |
| refusal_then_metric | 1/2 |
| entity_type_guard | 2/2 |
| clarify_suspend | 3/4 |
| cache_role_isolation | 3/3 |

| 条目.轮 | 结果 | 场景 | 角色 | 问（消解后） | 期望 | 缓存 | 消解说明/失败详情 |
|---|---|---|---|---|---|---|---|
| fu01.t1 | ✓ | entity_switch | boss | 上月华南地区的销售额 | answer | - |  |
| fu01.t2 | ✓ | entity_switch | boss | 上月华北地区的销售额 | answer | - | 指代消解：'那华北呢' → '上月华北地区的销售额'（替换 region: 华南→华北） |
| fu02.t1 | ✓ | entity_switch | boss | 上月食品品类的毛利率 | answer | - |  |
| fu02.t2 | ✗ | entity_switch | boss | 上月美妆品类的毛利率 | refusal | - | 期望 answer，得到 refusal note=指代消解：'那美妆呢' → '上月美妆品类的毛利率'（替换 category: 食品→美妆） |
| fu03.t1 | ✓ | entity_switch | boss | 华东地区近3个月的订单量 | answer | - |  |
| fu03.t2 | ✓ | entity_switch | boss | 东北地区近3个月的订单量 | answer | - | 指代消解：'那东北呢' → '东北地区近3个月的订单量'（替换 region: 华东→东北） |
| fu04.t1 | ✓ | entity_fallback | boss | 直播渠道近3个月的销售额 | answer | - |  |
| fu04.t2 | ✗ | entity_fallback | boss | 搜索渠道近3个月的销售额 | answer | - | 期望 clarify，得到 answer note=指代消解：'换成搜索呢' → '搜索渠道近3个月的销售额'（替换 channel: 直播→搜索） |
| fu05.t1 | ✓ | entity_boundary_refusal | boss | 直播渠道近3个月的销售额 | answer | - |  |
| fu05.t2 | ✗ | entity_boundary_refusal | boss | 换成电商呢 | clarify | - | 期望 refusal，得到 clarify note= |
| fu06.t1 | ✗ | guard_multi_entity | boss | 上月华南地区和全国的销售额 | answer | - | 列数不符：2 vs 1 note= |
| fu06.t2 | ✗ | guard_multi_entity | boss | 上月华北地区和全国的销售额 | answer | - | 期望 clarify，得到 answer note=指代消解：'那华北呢' → '上月华北地区和全国的销售额'（替换 region: 华南→华北） |
| fu07.t1 | ✓ | time_followup | boss | 最近6个月各品类销售额 | answer | - |  |
| fu07.t2 | ✓ | time_followup | boss | 那上个月呢 | clarify | - |  |
| fu08.t1 | ✓ | time_followup | boss | 上月华东地区销售额 | answer | - |  |
| fu08.t2 | ✓ | time_followup | boss | 那本月销售额呢 | answer | - |  |
| fu09.t1 | ✓ | time_followup | boss | 近12个月每月毛利额 | answer | - |  |
| fu09.t2 | ✓ | time_followup | boss | 上月毛利额呢 | answer | - |  |
| fu10.t1 | ✓ | entity_then_time | boss | 最近3个月华南地区的毛利率 | answer | - |  |
| fu10.t2 | ✓ | entity_then_time | boss | 最近3个月华北地区的毛利率 | answer | - | 指代消解：'那华北呢' → '最近3个月华北地区的毛利率'（替换 region: 华南→华北） |
| fu10.t3 | ✓ | entity_then_time | boss | 上月销售额呢 | answer | - |  |
| fu11.t1 | ✓ | cache_repeat | boss | 上月美妆品类的销售额 | answer | - |  |
| fu11.t2 | ✓ | cache_repeat | boss | 上月美妆品类的销售额 | answer/cache | ⚡ |  |
| fu12.t1 | ✓ | cache_repeat | boss | 上月华南地区毛利率 | answer | - |  |
| fu12.t2 | ✓ | cache_repeat | boss | 上月华北地区毛利率 | answer | - | 指代消解：'那华北呢' → '上月华北地区毛利率'（替换 region: 华南→华北） |
| fu12.t3 | ✓ | cache_repeat | boss | 上月华南地区毛利率 | answer/cache | ⚡ | 指代消解：'那华南呢' → '上月华南地区毛利率'（替换 region: 华北→华南） |
| fu13.t1 | ✓ | cache_repeat | boss | 上月华北地区销售额 | answer | - |  |
| fu13.t2 | ✓ | cache_repeat | north | 上月华北地区销售额 | answer | - |  |
| fu14.t1 | ✓ | cache_repeat | boss | 近3个月华南地区的销售额 | answer | - |  |
| fu14.t2 | ✓ | cache_repeat | boss | 近6个月华南地区的销售额 | answer | - |  |
| fu14.t3 | ✓ | cache_repeat | boss | 近3个月华南地区的销售额 | answer/cache | ⚡ |  |
| fu15.t1 | ✓ | clarify_flow | boss | 最近卖得怎么样 | clarify | - |  |
| fu15.t2 | ✓ | clarify_flow | boss | 销售额 | answer | - |  |
| fu16.t1 | ✓ | clarify_flow | boss | 上月各渠道表现如何 | clarify | - |  |
| fu16.t2 | ✓ | clarify_flow | boss | 上月直播渠道的销售额 | answer | - |  |
| fu17.t1 | ✓ | role_scope | south | 上月各区域销售额 | answer | - |  |
| fu17.t2 | ✓ | role_scope | south | 那华北呢 | clarify | - | 检测到追问但上轮无同类型实体可替换，已按新问题处理 |
| fu18.t1 | ✓ | role_scope | north | 上月各区域销售额 | answer | - |  |
| fu18.t2 | ✓ | role_scope | north | 那华东呢 | clarify | - | 检测到追问但上轮无同类型实体可替换，已按新问题处理 |
| fu19.t1 | ✓ | role_followup | south | 上月美妆品类的毛利率 | answer | - |  |
| fu19.t2 | ✓ | role_followup | south | 上月食品品类的毛利率 | answer | - | 指代消解：'那食品呢' → '上月食品品类的毛利率'（替换 category: 美妆→食品） |
| fu20.t1 | ✓ | no_context | boss | 那华北呢 | clarify | - | 检测到追问但无上轮上下文，已按新问题处理 |
| fu21.t1 | ✓ | entity_switch | boss | 上月西南地区的订单量 | answer | - |  |
| fu21.t2 | ✓ | entity_switch | boss | 上月华东地区的订单量 | answer | - | 指代消解：'那华东呢' → '上月华东地区的订单量'（替换 region: 西南→华东） |
| fu22.t1 | ✓ | entity_switch | boss | 近6个月美妆品类的订单量 | answer | - |  |
| fu22.t2 | ✓ | entity_switch | boss | 近6个月食品品类的订单量 | answer | - | 指代消解：'那食品呢' → '近6个月食品品类的订单量'（替换 category: 美妆→食品） |
| fu23.t1 | ✓ | entity_switch | boss | 上月华东地区销售额 | answer | - |  |
| fu23.t2 | ✓ | entity_switch | boss | 上月华南地区销售额 | answer | - | 指代消解：'那华南呢' → '上月华南地区销售额'（替换 region: 华东→华南） |
| fu23.t3 | ✓ | entity_switch | boss | 上月华北地区销售额 | answer | - | 指代消解：'那华北呢' → '上月华北地区销售额'（替换 region: 华南→华北） |
| fu24.t1 | ✓ | entity_switch | boss | 近3个月美妆品类的毛利率 | answer | - |  |
| fu24.t2 | ✓ | entity_switch | boss | 近3个月食品品类的毛利率 | answer | - | 指代消解：'换成食品呢' → '近3个月食品品类的毛利率'（替换 category: 美妆→食品） |
| fu25.t1 | ✓ | refusal_then_metric | boss | 上月客服满意度是多少 | refusal | - |  |
| fu25.t2 | ✗ | refusal_then_metric | boss | 那退货率呢 | refusal | - | 期望 answer，得到 refusal note= |
| fu26.t1 | ✓ | entity_type_guard | boss | 上月直播渠道的毛利率 | answer | - |  |
| fu26.t2 | ✓ | entity_type_guard | boss | 那食品呢 | clarify | - | 检测到追问但上轮无同类型实体可替换，已按新问题处理 |
| fu27.t1 | ✓ | entity_switch | boss | 食品品类近3个月的毛利额 | answer | - |  |
| fu27.t2 | ✓ | entity_switch | boss | 美妆品类近3个月的毛利额 | answer | - | 指代消解：'那美妆呢' → '美妆品类近3个月的毛利额'（替换 category: 食品→美妆） |
| fu28.t1 | ✓ | clarify_suspend | boss | 最近卖得怎么样 | None | - |  |
| fu28.t2 | ✗ | clarify_suspend | boss | 销售额 | answer | - | 结果不一致：1行 vs gold 1行 note= |
| fu29.t1 | ✓ | clarify_suspend | boss | 上月各渠道表现如何 | None | - |  |
| fu29.t2 | ✓ | clarify_suspend | boss | 销售额 | answer | - |  |
| fu30.t1 | ✓ | cache_role_isolation | boss | 上月各区域销售额 | answer | - |  |
| fu30.t2 | ✓ | cache_role_isolation | south | 上月各区域销售额 | answer | - |  |
| fu30.t3 | ✓ | cache_role_isolation | boss | 上月各区域销售额 | answer/cache | ⚡ |  |