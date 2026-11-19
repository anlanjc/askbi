# AskBI 多轮评测报告（eval/multiturn_cross_d.json · mock）

- 评测集：`eval/multiturn_cross_d.json`
- 环境：provider=mock（零 API 花费） · PROFILE=full（cache/followup 开）· 挂起条目走独立 checkpointer 图
- 结果：条目全对 **7/7** · 轮次正确 **31/31**（100.0%）· 缓存命中轮 1
- 延迟 avg 94.7ms · token ≈5528 · 缓存计数 {"hits": 1, "misses": 27, "stores": 27}

| 场景 | 轮次通过 |
|---|---|
| resolve_chain | 8/8 |
| constraint_keep | 4/4 |
| clarify_then_drill | 4/4 |
| topic_switch | 5/5 |
| topic_switch_cache | 5/5 |
| refusal_mixed | 5/5 |

| 条目.轮 | 结果 | 场景 | 角色 | 问（消解后） | 期望 | 缓存 | 消解说明/失败详情 |
|---|---|---|---|---|---|---|---|
| mcd01.t1 | ✓ | resolve_chain | boss | 上月直销渠道的房费收入 | answer | - |  |
| mcd01.t2 | ✓ | resolve_chain | boss | 上月旅行社渠道的房费收入 | answer | - | 指代消解：'那旅行社呢' → '上月旅行社渠道的房费收入'（替换 channel: 直销→旅行社） |
| mcd01.t3 | ✓ | resolve_chain | boss | 上月协议渠道的房费收入 | answer | - | 指代消解：'那协议呢' → '上月协议渠道的房费收入'（替换 channel: 旅行社→协议） |
| mcd01.t4 | ✓ | resolve_chain | boss | 上月各渠道间夜数 | answer | - |  |
| mcd02.t1 | ✓ | resolve_chain | boss | 近6个月度假店型的平均房价 | answer | - |  |
| mcd02.t2 | ✓ | resolve_chain | boss | 近6个月商务店型的平均房价 | answer | - | 指代消解：'那商务呢' → '近6个月商务店型的平均房价'（替换 hotel_type: 度假→商务） |
| mcd02.t3 | ✓ | resolve_chain | boss | 近6个月精品店型的平均房价 | answer | - | 指代消解：'那精品呢' → '近6个月精品店型的平均房价'（替换 hotel_type: 商务→精品） |
| mcd02.t4 | ✓ | resolve_chain | boss | 近6个月各酒店类型的间夜数 | answer | - |  |
| mcd03.t1 | ✓ | constraint_keep | boss | 华东地区近12个月毛利率 | answer | - |  |
| mcd03.t2 | ✓ | constraint_keep | boss | 华北地区近12个月毛利率 | answer | - | 指代消解：'那华北呢' → '华北地区近12个月毛利率'（替换 region: 华东→华北） |
| mcd03.t3 | ✓ | constraint_keep | boss | 东北地区近12个月毛利率 | answer | - | 指代消解：'那东北呢' → '东北地区近12个月毛利率'（替换 region: 华北→东北） |
| mcd03.t4 | ✓ | constraint_keep | boss | 近12个月各城市毛利额 | answer | - |  |
| mcd04.t1 | ✓ | clarify_then_drill | boss | 最近经营情况怎么样 | None | - |  |
| mcd04.t2 | ✓ | clarify_then_drill | boss | 房费收入 | answer | - |  |
| mcd04.t3 | ✓ | clarify_then_drill | boss | 上月各酒店类型的预订量 | answer | - |  |
| mcd04.t4 | ✓ | clarify_then_drill | boss | 为什么上月大床房型的间夜数下滑了 | answer | - |  |
| mcd05.t1 | ✓ | topic_switch | boss | 上月商务店型的房费收入 | answer | - |  |
| mcd05.t2 | ✓ | topic_switch | boss | 上月各渠道的平均房价 | answer | - |  |
| mcd05.t3 | ✓ | topic_switch | boss | 那度假呢 | clarify | - | 检测到追问但上轮无同类型实体可替换，已按新问题处理 |
| mcd05.t4 | ✓ | topic_switch | boss | 上月度假店型的房费收入 | answer | - |  |
| mcd05.t5 | ✓ | topic_switch | boss | 上月精品店型的房费收入 | answer | - | 指代消解：'那精品呢' → '上月精品店型的房费收入'（替换 hotel_type: 度假→精品） |
| mcd06.t1 | ✓ | topic_switch_cache | boss | 上月华东地区的间夜数 | answer | - |  |
| mcd06.t2 | ✓ | topic_switch_cache | boss | 上月各房型的平均房价 | answer | - |  |
| mcd06.t3 | ✓ | topic_switch_cache | boss | 上月华东地区的间夜数 | answer/cache | ⚡ |  |
| mcd06.t4 | ✓ | topic_switch_cache | boss | 上月华南地区的间夜数 | answer | - | 指代消解：'那华南呢' → '上月华南地区的间夜数'（替换 region: 华东→华南） |
| mcd06.t5 | ✓ | topic_switch_cache | boss | 上月各渠道的入住宾客数 | answer | - |  |
| mcd07.t1 | ✓ | refusal_mixed | boss | 上月直销渠道的房费收入 | answer | - |  |
| mcd07.t2 | ✓ | refusal_mixed | boss | 预测下季度的房费收入 | refusal | - |  |
| mcd07.t3 | ✓ | refusal_mixed | boss | 上月毛利率 | answer | - |  |
| mcd07.t4 | ✓ | refusal_mixed | boss | 上月亲子房型的预订量 | answer | - |  |
| mcd07.t5 | ✓ | refusal_mixed | boss | 上月套房房型的预订量 | answer | - | 指代消解：'那套房呢' → '上月套房房型的预订量'（替换 room_type: 亲子→套房） |