# AskBI 多轮评测报告（eval/multiturn_deep_v2.jsonl · mock）

- 评测集：`eval/multiturn_deep_v2.jsonl`
- 环境：provider=mock（零 API 花费） · PROFILE=full（cache/followup 开）· 挂起条目走独立 checkpointer 图
- 结果：条目全对 **60/60** · 轮次正确 **307/307**（100.0%）· 缓存命中轮 12
- 延迟 avg 109.9ms · token ≈59294 · 缓存计数 {"hits": 12, "misses": 267, "stores": 267}

| 场景 | 轮次通过 |
|---|---|
| resolve_chain | 24/24 |
| constraint_keep | 17/17 |
| clarify_then_drill | 12/12 |
| topic_switch | 15/15 |
| topic_switch_cache | 6/6 |
| role_mixed | 5/5 |
| refusal_mixed | 5/5 |
| clarify_mixed | 5/5 |
| mixed_long | 51/51 |
| role_x_chain | 15/15 |
| between_window | 16/16 |
| double_suspend | 17/17 |
| cache_boundary | 16/16 |
| refusal_downgrade | 12/12 |
| constraint_deep | 27/27 |
| chain_deep | 48/48 |
| multi_entity_guard | 16/16 |

| 条目.轮 | 结果 | 场景 | 角色 | 问（消解后） | 期望 | 缓存 | 消解说明/失败详情 |
|---|---|---|---|---|---|---|---|
| md01.t1 | ✓ | resolve_chain | boss | 上月华南地区的销售额 | answer | - |  |
| md01.t2 | ✓ | resolve_chain | boss | 上月华北地区的销售额 | answer | - | 指代消解：'那华北呢' → '上月华北地区的销售额'（替换 region: 华南→华北） |
| md01.t3 | ✓ | resolve_chain | boss | 上月华东地区的销售额 | answer | - | 指代消解：'换成华东呢' → '上月华东地区的销售额'（替换 region: 华北→华东） |
| md01.t4 | ✓ | resolve_chain | boss | 上月华中地区的销售额 | answer | - | 指代消解：'那华中地区呢' → '上月华中地区的销售额'（替换 region: 华东→华中） |
| md01.t5 | ✓ | resolve_chain | boss | 上月各品类销售额 | answer | - |  |
| md02.t1 | ✓ | resolve_chain | boss | 直播渠道近3个月的销售额 | answer | - |  |
| md02.t2 | ✓ | resolve_chain | boss | 搜索渠道近3个月的销售额 | answer | - | 指代消解：'那搜索呢' → '搜索渠道近3个月的销售额'（替换 channel: 直播→搜索） |
| md02.t3 | ✓ | resolve_chain | boss | 官网渠道近3个月的销售额 | answer | - | 指代消解：'换成官网呢' → '官网渠道近3个月的销售额'（替换 channel: 搜索→官网） |
| md02.t4 | ✓ | resolve_chain | boss | 各渠道销售额 | answer | - |  |
| md03.t1 | ✓ | resolve_chain | boss | 金卡会员上月客单价 | answer | - |  |
| md03.t2 | ✓ | resolve_chain | boss | 银卡会员上月客单价 | answer | - | 指代消解：'那银卡呢' → '银卡会员上月客单价'（替换 member_level: 金卡→银卡） |
| md03.t3 | ✓ | resolve_chain | boss | 黑卡会员上月客单价 | answer | - | 指代消解：'那黑卡呢' → '黑卡会员上月客单价'（替换 member_level: 银卡→黑卡） |
| md03.t4 | ✓ | resolve_chain | boss | 各会员等级的客单价 | answer | - |  |
| md04.t1 | ✓ | resolve_chain | boss | 近6个月美妆品类的毛利率 | answer | - |  |
| md04.t2 | ✓ | resolve_chain | boss | 近6个月数码品类的毛利率 | answer | - | 指代消解：'那数码呢' → '近6个月数码品类的毛利率'（替换 category: 美妆→数码） |
| md04.t3 | ✓ | resolve_chain | boss | 近6个月食品品类的毛利率 | answer | - | 指代消解：'换成食品呢' → '近6个月食品品类的毛利率'（替换 category: 数码→食品） |
| md04.t4 | ✓ | resolve_chain | boss | 近6个月家居品类的毛利率 | answer | - | 指代消解：'那家居呢' → '近6个月家居品类的毛利率'（替换 category: 食品→家居） |
| md04.t5 | ✓ | resolve_chain | boss | 上月各品类退货率 | answer | - |  |
| md05.t1 | ✓ | resolve_chain | boss | 华南地区上月销售额 | answer | - |  |
| md05.t2 | ✓ | resolve_chain | boss | 华北地区上月销售额 | answer | - | 指代消解：'那华北呢' → '华北地区上月销售额'（替换 region: 华南→华北） |
| md05.t3 | ✓ | resolve_chain | boss | 美妆品类上月毛利率 | answer | - |  |
| md05.t4 | ✓ | resolve_chain | boss | 数码品类上月毛利率 | answer | - | 指代消解：'那数码呢' → '数码品类上月毛利率'（替换 category: 美妆→数码） |
| md05.t5 | ✓ | resolve_chain | boss | 上月各渠道净销售额 | answer | - |  |
| md05.t6 | ✓ | resolve_chain | boss | 那官网呢 | clarify | - | 检测到追问但上轮无同类型实体可替换，已按新问题处理 |
| md06.t1 | ✓ | constraint_keep | boss | 华南地区近6个月毛利率 | answer | - |  |
| md06.t2 | ✓ | constraint_keep | boss | 华北地区近6个月毛利率 | answer | - | 指代消解：'那华北呢' → '华北地区近6个月毛利率'（替换 region: 华南→华北） |
| md06.t3 | ✓ | constraint_keep | boss | 东北地区近6个月毛利率 | answer | - | 指代消解：'那东北呢' → '东北地区近6个月毛利率'（替换 region: 华北→东北） |
| md06.t4 | ✓ | constraint_keep | boss | 上月华南地区毛利率 | answer | - |  |
| md07.t1 | ✓ | constraint_keep | boss | 官网渠道上月净销售额 | answer | - |  |
| md07.t2 | ✓ | constraint_keep | boss | 社交渠道上月净销售额 | answer | - | 指代消解：'那社交呢' → '社交渠道上月净销售额'（替换 channel: 官网→社交） |
| md07.t3 | ✓ | constraint_keep | boss | 直播渠道上月净销售额 | answer | - | 指代消解：'那直播呢' → '直播渠道上月净销售额'（替换 channel: 社交→直播） |
| md07.t4 | ✓ | constraint_keep | boss | 上月净销售额 | answer | - |  |
| md08.t1 | ✓ | constraint_keep | boss | 黑卡会员近12个月的购买客户数 | answer | - |  |
| md08.t2 | ✓ | constraint_keep | boss | 普通会员近12个月的购买客户数 | answer | - | 指代消解：'那普通呢' → '普通会员近12个月的购买客户数'（替换 member_level: 黑卡→普通） |
| md08.t3 | ✓ | constraint_keep | boss | 银卡会员近12个月的购买客户数 | answer | - | 指代消解：'那银卡呢' → '银卡会员近12个月的购买客户数'（替换 member_level: 普通→银卡） |
| md08.t4 | ✓ | constraint_keep | boss | 金卡会员近12个月的购买客户数 | answer | - | 指代消解：'那金卡呢' → '金卡会员近12个月的购买客户数'（替换 member_level: 银卡→金卡） |
| md08.t5 | ✓ | constraint_keep | boss | 近12个月购买客户数 | answer | - |  |
| md09.t1 | ✓ | constraint_keep | boss | 食品品类近3个月销量 | answer | - |  |
| md09.t2 | ✓ | constraint_keep | boss | 家居品类近3个月销量 | answer | - | 指代消解：'那家居呢' → '家居品类近3个月销量'（替换 category: 食品→家居） |
| md09.t3 | ✓ | constraint_keep | boss | 运动品类近3个月销量 | answer | - | 指代消解：'那运动呢' → '运动品类近3个月销量'（替换 category: 家居→运动） |
| md09.t4 | ✓ | constraint_keep | boss | 近3个月各品类销量 | answer | - |  |
| md10.t1 | ✓ | clarify_then_drill | boss | 最近卖得怎么样 | None | - |  |
| md10.t2 | ✓ | clarify_then_drill | boss | 销售额 | answer | - |  |
| md10.t3 | ✓ | clarify_then_drill | boss | 上月各品类销售额 | answer | - |  |
| md10.t4 | ✓ | clarify_then_drill | boss | 为什么上月家居品类的销售额下滑了 | answer | - |  |
| md11.t1 | ✓ | clarify_then_drill | boss | 上月各渠道表现如何 | None | - |  |
| md11.t2 | ✓ | clarify_then_drill | boss | 毛利率 | answer | - |  |
| md11.t3 | ✓ | clarify_then_drill | boss | 对比上月各渠道的毛利率差异 | answer/cache | ⚡ |  |
| md11.t4 | ✓ | clarify_then_drill | boss | 上月净销售额 | answer | - |  |
| md12.t1 | ✓ | clarify_then_drill | boss | 最近表现怎么样 | clarify | - |  |
| md12.t2 | ✓ | clarify_then_drill | boss | 上月各品类毛利额 | answer | - |  |
| md12.t3 | ✓ | clarify_then_drill | boss | 为什么上月食品品类的客单价下滑了 | answer | - |  |
| md12.t4 | ✓ | clarify_then_drill | boss | 上月总毛利额 | answer | - |  |
| md13.t1 | ✓ | topic_switch | boss | 华南地区上月销售额 | answer | - |  |
| md13.t2 | ✓ | topic_switch | boss | 上月各渠道毛利率 | answer | - |  |
| md13.t3 | ✓ | topic_switch | boss | 那华北呢 | clarify | - | 检测到追问但上轮无同类型实体可替换，已按新问题处理 |
| md13.t4 | ✓ | topic_switch | boss | 华北地区上月订单量 | answer | - |  |
| md13.t5 | ✓ | topic_switch | boss | 西北地区上月订单量 | answer | - | 指代消解：'那西北呢' → '西北地区上月订单量'（替换 region: 华北→西北） |
| md14.t1 | ✓ | topic_switch | boss | 美妆品类近6个月销售额 | answer | - |  |
| md14.t2 | ✓ | topic_switch | boss | 上月各会员等级客单价 | answer | - |  |
| md14.t3 | ✓ | topic_switch | boss | 那数码呢 | clarify | - | 检测到追问但上轮无同类型实体可替换，已按新问题处理 |
| md14.t4 | ✓ | topic_switch | boss | 数码品类近6个月销售额 | answer | - |  |
| md14.t5 | ✓ | topic_switch | boss | 食品品类近6个月销售额 | answer | - | 指代消解：'那食品呢' → '食品品类近6个月销售额'（替换 category: 数码→食品） |
| md15.t1 | ✓ | topic_switch | boss | 黑卡会员上月订单量 | answer | - |  |
| md15.t2 | ✓ | topic_switch | boss | 上月各品类退货率 | answer | - |  |
| md15.t3 | ✓ | topic_switch | boss | 那金卡呢 | clarify | - | 检测到追问但上轮无同类型实体可替换，已按新问题处理 |
| md15.t4 | ✓ | topic_switch | boss | 金卡会员上月订单量 | answer | - |  |
| md15.t5 | ✓ | topic_switch | boss | 银卡会员上月订单量 | answer | - | 指代消解：'那银卡呢' → '银卡会员上月订单量'（替换 member_level: 金卡→银卡） |
| md16.t1 | ✓ | topic_switch_cache | boss | 上月华南地区的销售额 | answer | - |  |
| md16.t2 | ✓ | topic_switch_cache | boss | 上月各品类销售额 | answer | - |  |
| md16.t3 | ✓ | topic_switch_cache | boss | 上月华南地区的销售额 | answer/cache | ⚡ |  |
| md16.t4 | ✓ | topic_switch_cache | boss | 上月华北地区的销售额 | answer | - | 指代消解：'那华北呢' → '上月华北地区的销售额'（替换 region: 华南→华北） |
| md16.t5 | ✓ | topic_switch_cache | boss | 上月各渠道净销售额 | answer | - |  |
| md16.t6 | ✓ | topic_switch_cache | boss | 那直播呢 | clarify | - | 检测到追问但上轮无同类型实体可替换，已按新问题处理 |
| md17.t1 | ✓ | role_mixed | south | 上月各渠道销售额 | answer | - |  |
| md17.t2 | ✓ | role_mixed | south | 上月各品类毛利率 | answer | - |  |
| md17.t3 | ✓ | role_mixed | south | 上月净销售额 | answer | - |  |
| md17.t4 | ✓ | role_mixed | south | 上月各细分品类销量 | answer | - |  |
| md17.t5 | ✓ | role_mixed | south | 上月购买客户数 | answer | - |  |
| md18.t1 | ✓ | refusal_mixed | boss | 上月销售额 | answer | - |  |
| md18.t2 | ✓ | refusal_mixed | boss | 预测下季度的销售额 | refusal | - |  |
| md18.t3 | ✓ | refusal_mixed | boss | 上月毛利率 | answer | - |  |
| md18.t4 | ✓ | refusal_mixed | boss | 直播渠道上月净销售额 | answer | - |  |
| md18.t5 | ✓ | refusal_mixed | boss | 搜索渠道上月净销售额 | answer | - | 指代消解：'那搜索呢' → '搜索渠道上月净销售额'（替换 channel: 直播→搜索） |
| md19.t1 | ✓ | clarify_mixed | boss | 上月销售额 | answer | - |  |
| md19.t2 | ✓ | clarify_mixed | boss | 整体怎么样 | clarify | - |  |
| md19.t3 | ✓ | clarify_mixed | boss | 上月各品类毛利额 | answer | - |  |
| md19.t4 | ✓ | clarify_mixed | boss | 上月各渠道毛利额 | answer | - |  |
| md19.t5 | ✓ | clarify_mixed | boss | 对比上月各品类的毛利额差异 | answer/cache | ⚡ |  |
| md20.t1 | ✓ | mixed_long | boss | 近12个月销售额趋势 | answer | - |  |
| md20.t2 | ✓ | mixed_long | boss | 黑卡会员上月客单价 | answer | - |  |
| md20.t3 | ✓ | mixed_long | boss | 普通会员上月客单价 | answer | - | 指代消解：'那普通呢' → '普通会员上月客单价'（替换 member_level: 黑卡→普通） |
| md20.t4 | ✓ | mixed_long | boss | 近6个月各渠道客单价趋势 | answer | - |  |
| md20.t5 | ✓ | mixed_long | boss | 上月净销售额 | answer | - |  |
| md20.t6 | ✓ | mixed_long | boss | 2025年1月到6月的毛利额 | answer | - |  |
| md21.t1 | ✓ | role_x_chain | south | 近6个月直播渠道的销售额 | answer | - |  |
| md21.t2 | ✓ | role_x_chain | south | 近6个月搜索渠道的销售额 | answer | - | 指代消解：'那搜索呢' → '近6个月搜索渠道的销售额'（替换 channel: 直播→搜索） |
| md21.t3 | ✓ | role_x_chain | south | 近6个月官网渠道的销售额 | answer | - | 指代消解：'换成官网呢' → '近6个月官网渠道的销售额'（替换 channel: 搜索→官网） |
| md21.t4 | ✓ | role_x_chain | south | 近6个月社交渠道的销售额 | answer | - | 指代消解：'那社交呢' → '近6个月社交渠道的销售额'（替换 channel: 官网→社交） |
| md21.t5 | ✓ | role_x_chain | south | 近6个月各渠道销售额 | answer | - |  |
| md22.t1 | ✓ | role_x_chain | north | 华北地区上月销售额 | answer | - |  |
| md22.t2 | ✓ | role_x_chain | north | 东北地区上月销售额 | answer | - | 指代消解：'那东北呢' → '东北地区上月销售额'（替换 region: 华北→东北） |
| md22.t3 | ✓ | role_x_chain | north | 华北地区上月销售额 | answer/cache | ⚡ | 指代消解：'换成华北呢' → '华北地区上月销售额'（替换 region: 东北→华北） |
| md22.t4 | ✓ | role_x_chain | north | 上月各区域销售额 | answer | - |  |
| md22.t5 | ✓ | role_x_chain | north | 上月东北地区毛利额 | answer | - |  |
| md23.t1 | ✓ | role_x_chain | south | 黑卡会员近12个月的购买客户数 | answer | - |  |
| md23.t2 | ✓ | role_x_chain | south | 普通会员近12个月的购买客户数 | answer | - | 指代消解：'那普通呢' → '普通会员近12个月的购买客户数'（替换 member_level: 黑卡→普通） |
| md23.t3 | ✓ | role_x_chain | south | 银卡会员近12个月的购买客户数 | answer | - | 指代消解：'那银卡呢' → '银卡会员近12个月的购买客户数'（替换 member_level: 普通→银卡） |
| md23.t4 | ✓ | role_x_chain | south | 金卡会员近12个月的购买客户数 | answer | - | 指代消解：'那金卡呢' → '金卡会员近12个月的购买客户数'（替换 member_level: 银卡→金卡） |
| md23.t5 | ✓ | role_x_chain | south | 近12个月各会员等级的购买客户数 | answer | - |  |
| md24.t1 | ✓ | between_window | boss | 2025年1月到3月华南地区的销售额 | answer | - |  |
| md24.t2 | ✓ | between_window | boss | 2025年1月到3月华北地区的销售额 | answer | - | 指代消解：'那华北呢' → '2025年1月到3月华北地区的销售额'（替换 region: 华南→华北） |
| md24.t3 | ✓ | between_window | boss | 2025年6月到8月华东地区的销售额 | answer | - |  |
| md24.t4 | ✓ | between_window | boss | 2025年6月到8月华中地区的销售额 | answer | - | 指代消解：'那华中呢' → '2025年6月到8月华中地区的销售额'（替换 region: 华东→华中） |
| md25.t1 | ✓ | between_window | boss | 2025年3月到5月食品品类的销量 | answer | - |  |
| md25.t2 | ✓ | between_window | boss | 2025年3月到5月家居品类的销量 | answer | - | 指代消解：'那家居呢' → '2025年3月到5月家居品类的销量'（替换 category: 食品→家居） |
| md25.t3 | ✓ | between_window | boss | 2025年6月到8月家居品类的销量 | answer | - |  |
| md25.t4 | ✓ | between_window | boss | 2025年6月到8月运动品类的销量 | answer | - | 指代消解：'那运动呢' → '2025年6月到8月运动品类的销量'（替换 category: 家居→运动） |
| md26.t1 | ✓ | between_window | boss | 2025年1月到6月各渠道销售额趋势 | answer | - |  |
| md26.t2 | ✓ | between_window | boss | 2025年7月到12月各渠道销售额趋势 | answer | - |  |
| md26.t3 | ✓ | between_window | boss | 2025年7月到12月华南地区各渠道销售额趋势 | answer | - |  |
| md26.t4 | ✓ | between_window | boss | 2025年7月到12月华北地区各渠道销售额趋势 | answer | - | 指代消解：'那华北地区呢' → '2025年7月到12月华北地区各渠道销售额趋势'（替换 region: 华南→华北） |
| md27.t1 | ✓ | between_window | boss | 2025年1月到3月的毛利率 | answer | - |  |
| md27.t2 | ✓ | between_window | boss | 2025年1月到4月的毛利率 | answer | - |  |
| md27.t3 | ✓ | between_window | boss | 2025年1月到3月的毛利率 | answer/cache | ⚡ |  |
| md27.t4 | ✓ | between_window | boss | 2025年1月到3月美妆品类的毛利率 | answer | - |  |
| md28.t1 | ✓ | double_suspend | boss | 最近卖得怎么样 | None | - |  |
| md28.t2 | ✓ | double_suspend | boss | 销售额 | answer | - |  |
| md28.t3 | ✓ | double_suspend | boss | 上月各品类销售额 | answer | - |  |
| md28.t4 | ✓ | double_suspend | boss | 整体表现如何 | None | - |  |
| md28.t5 | ✓ | double_suspend | boss | 毛利率 | answer | - |  |
| md29.t1 | ✓ | double_suspend | boss | 最近经营情况怎么样 | None | - |  |
| md29.t2 | ✓ | double_suspend | boss | 客单价 | answer | - |  |
| md29.t3 | ✓ | double_suspend | boss | 黑卡会员上月客单价 | answer | - |  |
| md29.t4 | ✓ | double_suspend | boss | 普通会员上月客单价 | answer | - | 指代消解：'那普通呢' → '普通会员上月客单价'（替换 member_level: 黑卡→普通） |
| md29.t5 | ✓ | double_suspend | boss | 最近整体表现怎么样 | None | - |  |
| md29.t6 | ✓ | double_suspend | boss | 订单量 | answer | - |  |
| md30.t1 | ✓ | double_suspend | boss | 最近表现怎么样 | None | - |  |
| md30.t2 | ✓ | double_suspend | boss | 毛利额 | answer | - |  |
| md30.t3 | ✓ | double_suspend | boss | 上月各渠道毛利额 | answer | - |  |
| md30.t4 | ✓ | double_suspend | boss | 最近卖得如何 | None | - |  |
| md30.t5 | ✓ | double_suspend | boss | 净销售额 | answer | - |  |
| md30.t6 | ✓ | double_suspend | boss | 上月各品类净销售额 | answer | - |  |
| md31.t1 | ✓ | cache_boundary | boss | 上月各品类销售额 | answer | - |  |
| md31.t2 | ✓ | cache_boundary | boss | 上月各品类的销售额 | answer/cache | ⚡ |  |
| md31.t3 | ✓ | cache_boundary | boss | 上月各品类销售额 | answer/cache | ⚡ |  |
| md31.t4 | ✓ | cache_boundary | boss | 上月各品类毛利额 | answer | - |  |
| md32.t1 | ✓ | cache_boundary | boss | 上月销售额 | answer | - |  |
| md32.t2 | ✓ | cache_boundary | boss | 近3个月销售额 | answer | - |  |
| md32.t3 | ✓ | cache_boundary | boss | 近6个月销售额 | answer | - |  |
| md32.t4 | ✓ | cache_boundary | boss | 上月销售额 | answer/cache | ⚡ |  |
| md33.t1 | ✓ | cache_boundary | boss | 上月销售额 | answer | - |  |
| md33.t2 | ✓ | cache_boundary | south | 上月销售额 | answer | - |  |
| md33.t3 | ✓ | cache_boundary | south | 上月销售额 | answer/cache | ⚡ |  |
| md33.t4 | ✓ | cache_boundary | boss | 上月销售额 | answer/cache | ⚡ |  |
| md34.t1 | ✓ | cache_boundary | boss | 华南地区上月销售额 | answer | - |  |
| md34.t2 | ✓ | cache_boundary | boss | 上月华南地区的销售额 | answer/cache | ⚡ |  |
| md34.t3 | ✓ | cache_boundary | boss | 上月华北地区的销售额 | answer | - | 指代消解：'那华北呢' → '上月华北地区的销售额'（替换 region: 华南→华北） |
| md34.t4 | ✓ | cache_boundary | boss | 上月各品类销售额 | answer | - |  |
| md35.t1 | ✓ | refusal_downgrade | boss | 上个月的复购率是多少 | refusal | - |  |
| md35.t2 | ✓ | refusal_downgrade | boss | 上月各品类退货率 | answer | - |  |
| md35.t3 | ✓ | refusal_downgrade | boss | 为什么上月数码品类的退货率下滑了 | answer | - |  |
| md35.t4 | ✓ | refusal_downgrade | boss | 上月各渠道的退货率 | answer | - |  |
| md36.t1 | ✓ | refusal_downgrade | boss | 客户满意度怎么样 | refusal | - |  |
| md36.t2 | ✓ | refusal_downgrade | boss | 上月销售额 | answer | - |  |
| md36.t3 | ✓ | refusal_downgrade | boss | 直播渠道上月净销售额 | answer | - |  |
| md36.t4 | ✓ | refusal_downgrade | boss | 搜索渠道上月净销售额 | answer | - | 指代消解：'那搜索呢' → '搜索渠道上月净销售额'（替换 channel: 直播→搜索） |
| md37.t1 | ✓ | refusal_downgrade | boss | 上个月的留存率是多少 | refusal | - |  |
| md37.t2 | ✓ | refusal_downgrade | boss | 上月各品类毛利额 | answer | - |  |
| md37.t3 | ✓ | refusal_downgrade | boss | 为什么上月食品品类的毛利额下滑了 | answer | - |  |
| md37.t4 | ✓ | refusal_downgrade | boss | 上月食品品类各渠道的毛利额 | answer | - |  |
| md38.t1 | ✓ | constraint_deep | boss | 华南地区近6个月销售额 | answer | - |  |
| md38.t2 | ✓ | constraint_deep | boss | 华北地区近6个月销售额 | answer | - | 指代消解：'那华北呢' → '华北地区近6个月销售额'（替换 region: 华南→华北） |
| md38.t3 | ✓ | constraint_deep | boss | 近12个月华北地区的销售额 | answer | - |  |
| md38.t4 | ✓ | constraint_deep | boss | 近12个月西北地区的销售额 | answer | - | 指代消解：'那西北呢' → '近12个月西北地区的销售额'（替换 region: 华北→西北） |
| md38.t5 | ✓ | constraint_deep | boss | 上月华北地区的销售额 | answer | - |  |
| md39.t1 | ✓ | constraint_deep | boss | 官网渠道上月净销售额 | answer | - |  |
| md39.t2 | ✓ | constraint_deep | boss | 社交渠道上月净销售额 | answer | - | 指代消解：'那社交呢' → '社交渠道上月净销售额'（替换 channel: 官网→社交） |
| md39.t3 | ✓ | constraint_deep | boss | 直播渠道上月净销售额 | answer | - | 指代消解：'那直播呢' → '直播渠道上月净销售额'（替换 channel: 社交→直播） |
| md39.t4 | ✓ | constraint_deep | boss | 近3个月各渠道净销售额 | answer | - |  |
| md39.t5 | ✓ | constraint_deep | boss | 近3个月美妆品类的净销售额 | answer | - |  |
| md39.t6 | ✓ | constraint_deep | boss | 上月各品类净销售额 | answer | - |  |
| md40.t1 | ✓ | constraint_deep | boss | 华南地区直播渠道上月销售额 | answer | - |  |
| md40.t2 | ✓ | constraint_deep | boss | 华南地区搜索渠道上月销售额 | answer | - | 指代消解：'那搜索呢' → '华南地区搜索渠道上月销售额'（替换 channel: 直播→搜索） |
| md40.t3 | ✓ | constraint_deep | boss | 华北地区搜索渠道上月销售额 | answer | - | 指代消解：'那华北呢' → '华北地区搜索渠道上月销售额'（替换 region: 华南→华北） |
| md40.t4 | ✓ | constraint_deep | boss | 上月总销售额 | answer | - |  |
| md40.t5 | ✓ | constraint_deep | boss | 华南地区上月销售额 | answer | - |  |
| md41.t1 | ✓ | constraint_deep | boss | 黑卡会员近12个月的购买客户数 | answer | - |  |
| md41.t2 | ✓ | constraint_deep | boss | 普通会员近12个月的购买客户数 | answer | - | 指代消解：'那普通呢' → '普通会员近12个月的购买客户数'（替换 member_level: 黑卡→普通） |
| md41.t3 | ✓ | constraint_deep | boss | 银卡会员近12个月的购买客户数 | answer | - | 指代消解：'那银卡呢' → '银卡会员近12个月的购买客户数'（替换 member_level: 普通→银卡） |
| md41.t4 | ✓ | constraint_deep | boss | 近12个月各会员等级的购买客户数 | answer | - |  |
| md41.t5 | ✓ | constraint_deep | boss | 近6个月黑卡会员的购买客户数 | answer | - |  |
| md41.t6 | ✓ | constraint_deep | boss | 近6个月各会员等级的购买客户数 | answer | - |  |
| md42.t1 | ✓ | constraint_deep | boss | 上月各品类毛利额 | answer | - |  |
| md42.t2 | ✓ | constraint_deep | boss | 上月各品类毛利率 | answer | - |  |
| md42.t3 | ✓ | constraint_deep | boss | 上月各品类净销售额 | answer | - |  |
| md42.t4 | ✓ | constraint_deep | boss | 上月各品类客单价 | answer | - |  |
| md42.t5 | ✓ | constraint_deep | boss | 上月各品类退货率 | answer | - |  |
| md43.t1 | ✓ | chain_deep | boss | 华南地区上月销售额 | answer | - |  |
| md43.t2 | ✓ | chain_deep | boss | 华北地区上月销售额 | answer | - | 指代消解：'那华北呢' → '华北地区上月销售额'（替换 region: 华南→华北） |
| md43.t3 | ✓ | chain_deep | boss | 直播渠道上月销售额 | answer | - |  |
| md43.t4 | ✓ | chain_deep | boss | 搜索渠道上月销售额 | answer | - | 指代消解：'那搜索呢' → '搜索渠道上月销售额'（替换 channel: 直播→搜索） |
| md43.t5 | ✓ | chain_deep | boss | 华中地区上月销售额 | answer | - |  |
| md43.t6 | ✓ | chain_deep | boss | 西北地区上月销售额 | answer | - | 指代消解：'那西北呢' → '西北地区上月销售额'（替换 region: 华中→西北） |
| md44.t1 | ✓ | chain_deep | boss | 华北地区上月销售额 | answer | - |  |
| md44.t2 | ✓ | chain_deep | boss | 华东地区上月销售额 | answer | - | 指代消解：'那华东呢' → '华东地区上月销售额'（替换 region: 华北→华东） |
| md44.t3 | ✓ | chain_deep | boss | 搜索渠道上月净销售额 | answer | - |  |
| md44.t4 | ✓ | chain_deep | boss | 社交渠道上月净销售额 | answer | - | 指代消解：'那社交呢' → '社交渠道上月净销售额'（替换 channel: 搜索→社交） |
| md44.t5 | ✓ | chain_deep | boss | 美妆品类上月毛利率 | answer | - |  |
| md44.t6 | ✓ | chain_deep | boss | 数码品类上月毛利率 | answer | - | 指代消解：'那数码呢' → '数码品类上月毛利率'（替换 category: 美妆→数码） |
| md45.t1 | ✓ | chain_deep | boss | 黑卡会员上月订单量 | answer | - |  |
| md45.t2 | ✓ | chain_deep | boss | 银卡会员上月订单量 | answer | - | 指代消解：'那银卡呢' → '银卡会员上月订单量'（替换 member_level: 黑卡→银卡） |
| md45.t3 | ✓ | chain_deep | boss | 数码品类上月毛利额 | answer | - |  |
| md45.t4 | ✓ | chain_deep | boss | 食品品类上月毛利额 | answer | - | 指代消解：'那食品呢' → '食品品类上月毛利额'（替换 category: 数码→食品） |
| md45.t5 | ✓ | chain_deep | boss | 为什么上月食品品类的毛利额下滑了 | answer | - |  |
| md45.t6 | ✓ | chain_deep | boss | 官网渠道上月净销售额 | answer | - |  |
| md46.t1 | ✓ | chain_deep | boss | 华南地区上月销售额 | answer | - |  |
| md46.t2 | ✓ | chain_deep | boss | 华北地区上月销售额 | answer | - | 指代消解：'那华北呢' → '华北地区上月销售额'（替换 region: 华南→华北） |
| md46.t3 | ✓ | chain_deep | boss | 各渠道表现如何 | clarify | - |  |
| md46.t4 | ✓ | chain_deep | boss | 上月各渠道毛利率 | answer | - |  |
| md46.t5 | ✓ | chain_deep | boss | 搜索渠道上月毛利率 | answer | - |  |
| md46.t6 | ✓ | chain_deep | boss | 官网渠道上月毛利率 | answer | - | 指代消解：'那官网呢' → '官网渠道上月毛利率'（替换 channel: 搜索→官网） |
| md47.t1 | ✓ | chain_deep | boss | 美妆品类上月毛利额 | answer | - |  |
| md47.t2 | ✓ | chain_deep | boss | 数码品类上月毛利额 | answer | - | 指代消解：'那数码呢' → '数码品类上月毛利额'（替换 category: 美妆→数码） |
| md47.t3 | ✓ | chain_deep | boss | 华南地区上月销售额 | answer | - |  |
| md47.t4 | ✓ | chain_deep | boss | 华北地区上月销售额 | answer | - | 指代消解：'那华北呢' → '华北地区上月销售额'（替换 region: 华南→华北） |
| md47.t5 | ✓ | chain_deep | boss | 为什么上月华北地区的销售额下滑了 | answer | - |  |
| md47.t6 | ✓ | chain_deep | boss | 为什么上月西北地区的销售额下滑了 | answer | - | 指代消解：'那西北呢' → '为什么上月西北地区的销售额下滑了'（替换 region: 华北→西北） |
| md48.t1 | ✓ | chain_deep | boss | 直播渠道上月客单价 | answer | - |  |
| md48.t2 | ✓ | chain_deep | boss | 搜索渠道上月客单价 | answer | - | 指代消解：'那搜索呢' → '搜索渠道上月客单价'（替换 channel: 直播→搜索） |
| md48.t3 | ✓ | chain_deep | boss | 金卡会员上月购买客户数 | answer | - |  |
| md48.t4 | ✓ | chain_deep | boss | 银卡会员上月购买客户数 | answer | - | 指代消解：'那银卡呢' → '银卡会员上月购买客户数'（替换 member_level: 金卡→银卡） |
| md48.t5 | ✓ | chain_deep | boss | 华东地区上月销售额 | answer | - |  |
| md48.t6 | ✓ | chain_deep | boss | 华中地区上月销售额 | answer | - | 指代消解：'那华中呢' → '华中地区上月销售额'（替换 region: 华东→华中） |
| md49.t1 | ✓ | chain_deep | boss | 华南地区上月销售额 | answer | - |  |
| md49.t2 | ✓ | chain_deep | boss | 西南地区上月销售额 | answer | - | 指代消解：'那西南呢' → '西南地区上月销售额'（替换 region: 华南→西南） |
| md49.t3 | ✓ | chain_deep | boss | 食品品类近3个月销量 | answer | - |  |
| md49.t4 | ✓ | chain_deep | boss | 家居品类近3个月销量 | answer | - | 指代消解：'那家居呢' → '家居品类近3个月销量'（替换 category: 食品→家居） |
| md49.t5 | ✓ | chain_deep | boss | 银卡会员近3个月的订单量 | answer | - |  |
| md49.t6 | ✓ | chain_deep | boss | 金卡会员近3个月的订单量 | answer | - | 指代消解：'那金卡呢' → '金卡会员近3个月的订单量'（替换 member_level: 银卡→金卡） |
| md50.t1 | ✓ | chain_deep | boss | 华南地区上月销售额 | answer | - |  |
| md50.t2 | ✓ | chain_deep | boss | 华北地区上月销售额 | answer | - | 指代消解：'那华北呢' → '华北地区上月销售额'（替换 region: 华南→华北） |
| md50.t3 | ✓ | chain_deep | boss | 华南地区上月销售额 | answer/cache | ⚡ |  |
| md50.t4 | ✓ | chain_deep | boss | 直播渠道上月净销售额 | answer | - |  |
| md50.t5 | ✓ | chain_deep | boss | 社交渠道上月净销售额 | answer | - | 指代消解：'那社交呢' → '社交渠道上月净销售额'（替换 channel: 直播→社交） |
| md50.t6 | ✓ | chain_deep | boss | 上月各品类销售额 | answer | - |  |
| md51.t1 | ✓ | mixed_long | boss | 近12个月销售额趋势 | answer | - |  |
| md51.t2 | ✓ | mixed_long | boss | 黑卡会员上月客单价 | answer | - |  |
| md51.t3 | ✓ | mixed_long | boss | 普通会员上月客单价 | answer | - | 指代消解：'那普通呢' → '普通会员上月客单价'（替换 member_level: 黑卡→普通） |
| md51.t4 | ✓ | mixed_long | boss | 近6个月各渠道客单价趋势 | answer | - |  |
| md51.t5 | ✓ | mixed_long | boss | 上月净销售额 | answer | - |  |
| md51.t6 | ✓ | mixed_long | boss | 2025年1月到6月的毛利额 | answer | - |  |
| md51.t7 | ✓ | mixed_long | boss | 上月各品类退货率 | answer | - |  |
| md51.t8 | ✓ | mixed_long | boss | 整体怎么样 | clarify | - |  |
| md52.t1 | ✓ | mixed_long | boss | 华南地区上月销售额 | answer | - |  |
| md52.t2 | ✓ | mixed_long | boss | 华北地区上月销售额 | answer | - | 指代消解：'那华北呢' → '华北地区上月销售额'（替换 region: 华南→华北） |
| md52.t3 | ✓ | mixed_long | boss | 上月各渠道毛利率 | answer | - |  |
| md52.t4 | ✓ | mixed_long | boss | 那华东呢 | clarify | - | 检测到追问但上轮无同类型实体可替换，已按新问题处理 |
| md52.t5 | ✓ | mixed_long | boss | 华东地区上月订单量 | answer | - |  |
| md52.t6 | ✓ | mixed_long | boss | 西北地区上月订单量 | answer | - | 指代消解：'那西北呢' → '西北地区上月订单量'（替换 region: 华东→西北） |
| md52.t7 | ✓ | mixed_long | boss | 预测下季度的销售额 | refusal | - |  |
| md53.t1 | ✓ | mixed_long | boss | 上月销售额 | answer | - |  |
| md53.t2 | ✓ | mixed_long | boss | 上个月的复购率是多少 | refusal | - |  |
| md53.t3 | ✓ | mixed_long | boss | 上月毛利率 | answer | - |  |
| md53.t4 | ✓ | mixed_long | boss | 官网渠道上月净销售额 | answer | - |  |
| md53.t5 | ✓ | mixed_long | boss | 社交渠道上月净销售额 | answer | - | 指代消解：'那社交呢' → '社交渠道上月净销售额'（替换 channel: 官网→社交） |
| md53.t6 | ✓ | mixed_long | boss | 2025年1月到3月美妆品类的销量 | answer | - |  |
| md53.t7 | ✓ | mixed_long | boss | 上月各会员等级客单价 | answer | - |  |
| md54.t1 | ✓ | mixed_long | boss | 近12个月销售额趋势 | answer | - |  |
| md54.t2 | ✓ | mixed_long | boss | 数码品类上月毛利率 | answer | - |  |
| md54.t3 | ✓ | mixed_long | boss | 食品品类上月毛利率 | answer | - | 指代消解：'那食品呢' → '食品品类上月毛利率'（替换 category: 数码→食品） |
| md54.t4 | ✓ | mixed_long | boss | 上月各品类净销售额 | answer | - |  |
| md54.t5 | ✓ | mixed_long | boss | 黑卡会员上月购买客户数 | answer | - |  |
| md54.t6 | ✓ | mixed_long | boss | 银卡会员上月购买客户数 | answer | - | 指代消解：'那银卡呢' → '银卡会员上月购买客户数'（替换 member_level: 黑卡→银卡） |
| md54.t7 | ✓ | mixed_long | boss | 2025年2月到4月的客单价 | answer | - |  |
| md54.t8 | ✓ | mixed_long | boss | 整体表现如何 | clarify | - |  |
| md55.t1 | ✓ | mixed_long | boss | 官网渠道近3个月销售额 | answer | - |  |
| md55.t2 | ✓ | mixed_long | boss | 直播渠道近3个月销售额 | answer | - | 指代消解：'那直播呢' → '直播渠道近3个月销售额'（替换 channel: 官网→直播） |
| md55.t3 | ✓ | mixed_long | boss | 上月各品类毛利额 | answer | - |  |
| md55.t4 | ✓ | mixed_long | boss | 为什么上月家居品类的毛利额下滑了 | answer | - |  |
| md55.t5 | ✓ | mixed_long | boss | 上月各渠道毛利额 | answer | - |  |
| md55.t6 | ✓ | mixed_long | boss | 华北地区上月销售额 | answer | - |  |
| md55.t7 | ✓ | mixed_long | boss | 东北地区上月销售额 | answer | - | 指代消解：'那东北呢' → '东北地区上月销售额'（替换 region: 华北→东北） |
| md56.t1 | ✓ | mixed_long | boss | 上月销售额 | answer | - |  |
| md56.t2 | ✓ | mixed_long | boss | 美妆品类上月毛利率 | answer | - |  |
| md56.t3 | ✓ | mixed_long | boss | 数码品类上月毛利率 | answer | - | 指代消解：'那数码呢' → '数码品类上月毛利率'（替换 category: 美妆→数码） |
| md56.t4 | ✓ | mixed_long | boss | 上月各渠道净销售额 | answer | - |  |
| md56.t5 | ✓ | mixed_long | boss | 那官网呢 | clarify | - | 检测到追问但上轮无同类型实体可替换，已按新问题处理 |
| md56.t6 | ✓ | mixed_long | boss | 官网渠道上月净销售额 | answer | - |  |
| md56.t7 | ✓ | mixed_long | boss | 近12个月购买客户数 | answer | - |  |
| md56.t8 | ✓ | mixed_long | boss | 2025年1月到6月各渠道销售额趋势 | answer | - |  |
| md57.t1 | ✓ | multi_entity_guard | boss | 对比华南地区和华北地区的销售额 | answer | - |  |
| md57.t2 | ✓ | multi_entity_guard | boss | 那华东呢 | clarify | - | 检测到追问但上轮含多个 region 实体（['华南', '华北']），已按新问题处理 |
| md57.t3 | ✓ | multi_entity_guard | boss | 华东地区上月销售额 | answer | - |  |
| md57.t4 | ✓ | multi_entity_guard | boss | 华中地区上月销售额 | answer | - | 指代消解：'那华中呢' → '华中地区上月销售额'（替换 region: 华东→华中） |
| md58.t1 | ✓ | multi_entity_guard | boss | 对比直播渠道和搜索渠道的净销售额 | answer | - |  |
| md58.t2 | ✓ | multi_entity_guard | boss | 那官网呢 | clarify | - | 检测到追问但上轮含多个 channel 实体（['直播', '搜索']），已按新问题处理 |
| md58.t3 | ✓ | multi_entity_guard | boss | 官网渠道上月净销售额 | answer | - |  |
| md58.t4 | ✓ | multi_entity_guard | boss | 社交渠道上月净销售额 | answer | - | 指代消解：'那社交呢' → '社交渠道上月净销售额'（替换 channel: 官网→社交） |
| md59.t1 | ✓ | multi_entity_guard | boss | 华南地区直播渠道上月销售额 | answer | - |  |
| md59.t2 | ✓ | multi_entity_guard | boss | 华南地区搜索渠道上月销售额 | answer | - | 指代消解：'那搜索呢' → '华南地区搜索渠道上月销售额'（替换 channel: 直播→搜索） |
| md59.t3 | ✓ | multi_entity_guard | boss | 华北地区搜索渠道上月销售额 | answer | - | 指代消解：'那华北呢' → '华北地区搜索渠道上月销售额'（替换 region: 华南→华北） |
| md59.t4 | ✓ | multi_entity_guard | boss | 上月各渠道销售额 | answer | - |  |
| md60.t1 | ✓ | multi_entity_guard | boss | 黑卡会员美妆品类上月客单价 | answer | - |  |
| md60.t2 | ✓ | multi_entity_guard | boss | 黑卡会员数码品类上月客单价 | answer | - | 指代消解：'那数码品类呢' → '黑卡会员数码品类上月客单价'（替换 category: 美妆→数码） |
| md60.t3 | ✓ | multi_entity_guard | boss | 银卡会员数码品类上月客单价 | answer | - | 指代消解：'那银卡会员呢' → '银卡会员数码品类上月客单价'（替换 member_level: 黑卡→银卡） |
| md60.t4 | ✓ | multi_entity_guard | boss | 上月各会员等级客单价 | answer | - |  |