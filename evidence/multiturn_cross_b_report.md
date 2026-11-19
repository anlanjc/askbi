# AskBI 多轮评测报告（eval/multiturn_cross_b.json · mock）

- 评测集：`eval/multiturn_cross_b.json`
- 环境：provider=mock（零 API 花费） · PROFILE=full（cache/followup 开）· 挂起条目走独立 checkpointer 图
- 结果：条目全对 **8/8** · 轮次正确 **36/36**（100.0%）· 缓存命中轮 1
- 延迟 avg 114.6ms · token ≈6778 · 缓存计数 {"hits": 1, "misses": 32, "stores": 32}

| 场景 | 轮次通过 |
|---|---|
| resolve_chain | 13/13 |
| constraint_keep | 4/4 |
| clarify_then_drill | 4/4 |
| topic_switch | 5/5 |
| mixed_long | 5/5 |
| refusal_mixed | 5/5 |

| 条目.轮 | 结果 | 场景 | 角色 | 问（消解后） | 期望 | 缓存 | 消解说明/失败详情 |
|---|---|---|---|---|---|---|---|
| mcb01.t1 | ✓ | resolve_chain | boss | 上月堂食渠道的销售额 | answer | - |  |
| mcb01.t2 | ✓ | resolve_chain | boss | 上月外卖渠道的销售额 | answer | - | 指代消解：'那外卖呢' → '上月外卖渠道的销售额'（替换 channel: 堂食→外卖） |
| mcb01.t3 | ✓ | resolve_chain | boss | 上月外带渠道的销售额 | answer | - | 指代消解：'那外带呢' → '上月外带渠道的销售额'（替换 channel: 外卖→外带） |
| mcb01.t4 | ✓ | resolve_chain | boss | 上月各渠道销售额 | answer | - |  |
| mcb02.t1 | ✓ | resolve_chain | boss | 上月旗舰店店型的客单价 | answer | - |  |
| mcb02.t2 | ✓ | resolve_chain | boss | 上月标准店店型的客单价 | answer | - | 指代消解：'那标准店呢' → '上月标准店店型的客单价'（替换 store_type: 旗舰店→标准店） |
| mcb02.t3 | ✓ | resolve_chain | boss | 上月旗舰店店型的客单价 | answer/cache | ⚡ |  |
| mcb02.t4 | ✓ | resolve_chain | boss | 上月各店型客单价 | answer | - |  |
| mcb03.t1 | ✓ | resolve_chain | boss | 近6个月烘焙品类的毛利率 | answer | - |  |
| mcb03.t2 | ✓ | resolve_chain | boss | 近6个月饮品品类的毛利率 | answer | - | 指代消解：'那饮品呢' → '近6个月饮品品类的毛利率'（替换 category: 烘焙→饮品） |
| mcb03.t3 | ✓ | resolve_chain | boss | 近6个月咖啡子类的销量 | answer | - |  |
| mcb03.t4 | ✓ | resolve_chain | boss | 近6个月茶饮子类的销量 | answer | - | 指代消解：'那茶饮呢' → '近6个月茶饮子类的销量'（替换 subcategory: 咖啡→茶饮） |
| mcb03.t5 | ✓ | resolve_chain | boss | 上月各品类毛利率 | answer | - |  |
| mcb04.t1 | ✓ | constraint_keep | boss | 华南地区近12个月毛利额 | answer | - |  |
| mcb04.t2 | ✓ | constraint_keep | boss | 华北地区近12个月毛利额 | answer | - | 指代消解：'那华北呢' → '华北地区近12个月毛利额'（替换 region: 华南→华北） |
| mcb04.t3 | ✓ | constraint_keep | boss | 西南地区近12个月毛利额 | answer | - | 指代消解：'那西南呢' → '西南地区近12个月毛利额'（替换 region: 华北→西南） |
| mcb04.t4 | ✓ | constraint_keep | boss | 近12个月各城市毛利额 | answer | - |  |
| mcb05.t1 | ✓ | clarify_then_drill | boss | 最近卖得怎么样 | None | - |  |
| mcb05.t2 | ✓ | clarify_then_drill | boss | 销售额 | answer | - |  |
| mcb05.t3 | ✓ | clarify_then_drill | boss | 上月各渠道订单量 | answer | - |  |
| mcb05.t4 | ✓ | clarify_then_drill | boss | 为什么上月烘焙品类的客单价下滑了 | answer | - |  |
| mcb06.t1 | ✓ | topic_switch | boss | 上月旗舰店店型的销售额 | answer | - |  |
| mcb06.t2 | ✓ | topic_switch | boss | 上月各品类的销量 | answer | - |  |
| mcb06.t3 | ✓ | topic_switch | boss | 那标准店呢 | clarify | - | 检测到追问但上轮无同类型实体可替换，已按新问题处理 |
| mcb06.t4 | ✓ | topic_switch | boss | 上月标准店店型的销售额 | answer | - |  |
| mcb06.t5 | ✓ | topic_switch | boss | 上月店中店店型的销售额 | answer | - | 指代消解：'那店中店呢' → '上月店中店店型的销售额'（替换 store_type: 标准店→店中店） |
| mcb07.t1 | ✓ | mixed_long | boss | 近12个月销售额趋势 | answer | - |  |
| mcb07.t2 | ✓ | mixed_long | boss | 上月外卖渠道的客单价 | answer | - |  |
| mcb07.t3 | ✓ | mixed_long | boss | 上月堂食渠道的客单价 | answer | - | 指代消解：'那堂食呢' → '上月堂食渠道的客单价'（替换 channel: 外卖→堂食） |
| mcb07.t4 | ✓ | mixed_long | boss | 近6个月各店型销售额趋势 | answer | - |  |
| mcb07.t5 | ✓ | mixed_long | boss | 2025年1月到6月的毛利额 | answer | - |  |
| mcb08.t1 | ✓ | refusal_mixed | boss | 上月堂食渠道的销量 | answer | - |  |
| mcb08.t2 | ✓ | refusal_mixed | boss | 预测下季度的销售额 | refusal | - |  |
| mcb08.t3 | ✓ | refusal_mixed | boss | 上月各店型的客单价 | answer | - |  |
| mcb08.t4 | ✓ | refusal_mixed | boss | 上月烘焙品类的订单量 | answer | - |  |
| mcb08.t5 | ✓ | refusal_mixed | boss | 上月饮品品类的订单量 | answer | - | 指代消解：'那饮品呢' → '上月饮品品类的订单量'（替换 category: 烘焙→饮品） |