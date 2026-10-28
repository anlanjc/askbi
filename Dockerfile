FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY eval ./eval
COPY web ./web

# 构建期生成演示数据（固定种子，可复现）；mock 模式开箱即用
RUN python scripts/gen_data.py

EXPOSE 8000

# 默认 mock 模式：无需 API key 即可体验全链路；配 key 时传 -e LLM_PROVIDER=deepseek -e DEEPSEEK_API_KEY=...
CMD ["python", "-m", "uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
