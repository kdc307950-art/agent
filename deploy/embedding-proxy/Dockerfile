# 契约适配代理部署镜像 —— 只包含 proxy，不携带主项目依赖
#
# 构建/运行：
#   docker build -f deploy/embedding-proxy/Dockerfile -t embedding-contract-proxy .
#   docker run --rm -p 8100:8100 \
#     -e EMBEDDING_UPSTREAM="https://.../compatible-mode/v1/embeddings" \
#     -e EMBEDDING_MODEL="qwen3.7-text-embedding" \
#     -e EMBEDDING_DIMENSION="1024" \
#     -e OPENAI_API_KEY="<key>" \
#     -e EMBEDDING_PROXY_TOKEN="<random-token>" \
#     -e EMBEDDING_HOST="0.0.0.0" \
#     embedding-contract-proxy
#
# 健康自检（容器内/外部）：
#   curl http://127.0.0.1:8100/healthz   # => {"status":"ok"}
#   curl -X POST http://127.0.0.1:8100/ -H "Content-Type: application/json" \
#     -H "X-Embedding-Proxy-Token: <random-token>" \
#     -d '{"texts":["你好"]}'   # => {"embeddings":[[...]]}

FROM python:3.12-slim

WORKDIR /app

# 仅安装 proxy 运行所需依赖（与 pyproject 版本对齐）
RUN pip install --no-cache-dir \
    "fastapi>=0.141.1" \
    "uvicorn>=0.52.3" \
    "httpx>=0.27" \
    "pydantic>=2"

# proxy 不依赖 backend 包，可单独复制运行
COPY backend/embedding_proxy.py /app/embedding_proxy.py

EXPOSE 8100

# 全部配置走环境变量（EMBEDDING_* / OPENAI_API_KEY / PORT）
CMD ["python", "/app/embedding_proxy.py"]
