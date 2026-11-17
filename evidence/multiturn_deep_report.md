# AskBI 多轮评测报告（eval/multiturn_deep.jsonl · mock）

- 评测集：`eval/multiturn_deep.jsonl`
- 环境：provider=mock（零 API 花费） · PROFILE=full（cache/followup 开）· 挂起条目走独立 checkpointer 图
- 结果：条目全对 **20/20** · 轮次正确 **95/95**（100.0%）· 缓存命中轮 3
- 延迟 avg 97.9ms · token ≈17982 · 缓存计数 {"hits": 3, "misses": 82, "stores": 82}

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
| mixed_long | 6/6 |

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