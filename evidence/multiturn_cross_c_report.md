# AskBI 多轮评测报告（eval/multiturn_cross_c.json · mock）

- 评测集：`eval/multiturn_cross_c.json`
- 环境：provider=mock（零 API 花费） · PROFILE=full（cache/followup 开）· 挂起条目走独立 checkpointer 图
- 结果：条目全对 **7/7** · 轮次正确 **32/32**（100.0%）· 缓存命中轮 2
- 延迟 avg 113.8ms · token ≈6512 · 缓存计数 {"hits": 2, "misses": 27, "stores": 27}

| 场景 | 轮次通过 |
|---|---|
| resolve_chain | 4/4 |
| clarify_mixed | 5/5 |
| constraint_keep | 5/5 |
| clarify_then_drill | 4/4 |
| topic_switch | 5/5 |
| role_mixed | 4/4 |
| mixed_long | 5/5 |

| 条目.轮 | 结果 | 场景 | 角色 | 问（消解后） | 期望 | 缓存 | 消解说明/失败详情 |
|---|---|---|---|---|---|---|---|
| mcc01.t1 | ✓ | resolve_chain | boss | 上月旗舰版套餐档位的MRR | answer | - |  |
| mcc01.t2 | ✓ | resolve_chain | boss | 上月专业版套餐档位的MRR | answer | - | 指代消解：'那专业版呢' → '上月专业版套餐档位的MRR'（替换 tier: 旗舰版→专业版） |
| mcc01.t3 | ✓ | resolve_chain | boss | 上月基础版套餐档位的MRR | answer | - | 指代消解：'那基础版呢' → '上月基础版套餐档位的MRR'（替换 tier: 专业版→基础版） |
| mcc01.t4 | ✓ | resolve_chain | boss | 上月各套餐档位的MRR | answer | - |  |
| mcc02.t1 | ✓ | clarify_mixed | boss | 上月MRR | answer | - |  |
| mcc02.t2 | ✓ | clarify_mixed | boss | 整体怎么样 | clarify | - |  |
| mcc02.t3 | ✓ | clarify_mixed | boss | 上月各行业的ARPU | answer | - |  |
| mcc02.t4 | ✓ | clarify_mixed | boss | 上月各套餐档位的毛利额 | answer | - |  |
| mcc02.t5 | ✓ | clarify_mixed | boss | 对比上月各套餐档位的毛利额差异 | answer/cache | ⚡ |  |
| mcc03.t1 | ✓ | constraint_keep | boss | 代理渠道近6个月毛利率 | answer | - |  |
| mcc03.t2 | ✓ | constraint_keep | boss | 直销渠道近6个月毛利率 | answer | - | 指代消解：'那直销呢' → '直销渠道近6个月毛利率'（替换 channel: 代理→直销） |
| mcc03.t3 | ✓ | constraint_keep | boss | 代理渠道近6个月毛利率 | answer/cache | ⚡ |  |
| mcc03.t4 | ✓ | constraint_keep | boss | 自助渠道近6个月毛利率 | answer | - | 指代消解：'那自助呢' → '自助渠道近6个月毛利率'（替换 channel: 代理→自助） |
| mcc03.t5 | ✓ | constraint_keep | boss | 近6个月各渠道毛利率 | answer | - |  |
| mcc04.t1 | ✓ | clarify_then_drill | boss | 最近表现怎么样 | None | - |  |
| mcc04.t2 | ✓ | clarify_then_drill | boss | MRR | answer | - |  |
| mcc04.t3 | ✓ | clarify_then_drill | boss | 上月各套餐档位的席位数 | answer | - |  |
| mcc04.t4 | ✓ | clarify_then_drill | boss | 为什么上月教育行业的ARPU下滑了 | answer | - |  |
| mcc05.t1 | ✓ | topic_switch | boss | 上月制造行业的席位数 | answer | - |  |
| mcc05.t2 | ✓ | topic_switch | boss | 上月各套餐档位的账单数 | answer | - |  |
| mcc05.t3 | ✓ | topic_switch | boss | 那零售呢 | clarify | - | 检测到追问但上轮无同类型实体可替换，已按新问题处理 |
| mcc05.t4 | ✓ | topic_switch | boss | 上月零售行业的账单数 | answer | - |  |
| mcc05.t5 | ✓ | topic_switch | boss | 上月金融行业的账单数 | answer | - | 指代消解：'那金融呢' → '上月金融行业的账单数'（替换 industry: 零售→金融） |
| mcc06.t1 | ✓ | role_mixed | south | 上月各套餐档位的MRR | answer | - |  |
| mcc06.t2 | ✓ | role_mixed | south | 上月各渠道毛利率 | answer | - |  |
| mcc06.t3 | ✓ | role_mixed | south | 上月付费客户数 | answer | - |  |
| mcc06.t4 | ✓ | role_mixed | south | 上月各行业席位数 | answer | - |  |
| mcc07.t1 | ✓ | mixed_long | boss | 近12个月MRR趋势 | answer | - |  |
| mcc07.t2 | ✓ | mixed_long | boss | 上月旗舰版套餐档位的席位数 | answer | - |  |
| mcc07.t3 | ✓ | mixed_long | boss | 上月基础版套餐档位的席位数 | answer | - | 指代消解：'那基础版呢' → '上月基础版套餐档位的席位数'（替换 tier: 旗舰版→基础版） |
| mcc07.t4 | ✓ | mixed_long | boss | 近6个月各渠道席位数趋势 | answer | - |  |
| mcc07.t5 | ✓ | mixed_long | boss | 2025年1月到6月的毛利额 | answer | - |  |