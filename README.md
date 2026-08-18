# LangGraph Agent

## 安全配置

`.env` 只用于本地环境，禁止提交。当前仓库没有 commit 或 remote，但工作区 `.env` 已包含真实的 DeepSeek 和 LangSmith 密钥；这些密钥应立即在对应平台撤销并重新生成。

复制 `.env.example` 为 `.env`，填写：

- `DEEPSEEK_API_KEY`：后端调用模型所需。
- `X_API_KEY`：本地/内网 API 的 Bearer 鉴权密钥。
- `LANGCHAIN_API_KEY`：可选，仅用于 LangSmith 追踪。

启动后，`POST /chat/stream` 必须带 `Authorization: Bearer <X_API_KEY>`。`GET /health` 保持公开，便于健康检查。限流默认为每个客户端和 API key 每分钟 60 次，可通过 `RATE_LIMIT_PER_MINUTE` 调整；当前实现是单进程内存限流，多实例部署应替换为 Redis 等共享限流器。

## 启动

后端：

```powershell
uv run uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

前端：

```powershell
cd frontend
npm run dev
```

Vite 开发代理把 `/api` 转发到后端，并从项目根 `.env` 读取 `X_API_KEY` 注入 Bearer 头；该密钥不会打进 React bundle。公网部署不要把这个本地共享密钥当作最终的用户身份系统，应改为租户级 token、撤销机制和共享限流。
