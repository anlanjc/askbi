# AskBI 部署手册（公开演示版）

> 目标：把 mock 版公开部署为一个可分享的链接（零 API 成本），用于简历/面试"可现场点开"的证据。
> 真实模型版部署方式相同，只需在平台环境变量里配 key（注意费用与限流）。

## 方案 A：Render（推荐，免费档够用）

1. 把仓库推到 GitHub（见下方"前置"）。
2. Render 控制台 → New → Web Service → 连接仓库。
3. 配置：
   - Runtime: **Docker**（仓库根有 Dockerfile，自动识别）
   - Instance Type: Free
   - Environment Variables: `LLM_PROVIDER=mock`（默认即 mock，可不填）
4. 部署后拿到 `https://<app>.onrender.com`，打开即是 Web 工作台。
5. 验证：问"上月各品类销售额"→ 出答案卡 + SQL 溯源；开 PROFILE=full 环境变量可演示缓存徽章/多轮。

**注意**：Free 档 15 分钟无流量会休眠，首次唤醒要 30-60 秒——演示前先自己点开一次。

## 方案 B：Railway / Fly.io

- Railway：New Project → Deploy from Repo → 自动识别 Dockerfile；环境变量同上。
- Fly.io：`fly launch`（检测 Dockerfile）→ `fly deploy`。免费额度需绑卡，介意可略过。

## 前置（一次性）

```bash
cd askbi
git remote add origin git@github.com:<user>/askbi.git
git push -u origin main        # 本地已有 15 笔分模块提交，真实时间线
```

## 部署后的证据动作（很重要，部署本身不是目的）

1. 链接写进简历项目行与 README 顶部（`在线演示（mock）`徽章）。
2. 开 **thumbs up/down 反馈**（web 已有 SSE 通道，加两个按钮+一个 JSONL 落盘即可，约 20 行）——收集到的真实使用记录就是"真实用户"证据链起点。
3. 找 5-10 个同学每人问 10 个真问题，按 [PILOT_REPORT.md](PILOT_REPORT.md) 模板记录成功率/失败/主观分——"没有上线"从此变成"有小规模真实使用数据"。

## 环境变量清单（Docker/平台通用）

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_PROVIDER` | `mock` | mock 零成本；`openai_compatible` 需配 key |
| `PROFILE` | `conservative` | `full` 一键开缓存/多轮/挂起（演示推荐） |
| `API_KEY` | 空 | 配置后所有 /api 请求需 `X-API-Key` |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` | - | 真模型三件套（DashScope 兼容模式已验证） |
| `NO_PROXY` | - | 本机系统代理半死时需 `dashscope.aliyuncs.com`（见 EVIDENCE 真 bug #3） |
