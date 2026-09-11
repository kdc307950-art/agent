# Helpdesk Frontend

LangGraph IT 服务台的 Web 前端：工单队列与处理、资产/知识库/IT 策略管理、基于 SSE 流式的智能助手（含人工审批中断闭环）。

## 技术栈

- React 19 + TypeScript + Vite
- React Router v7（路由化视图）
- eventsource-parser（SSE 流式解析）
- lucide-react（图标）

## 快速开始

```bash
npm install
npm run dev          # 开发服务器（/api 代理到 127.0.0.1:8000 后端）
npm run build        # 类型检查 + 生产构建
npm run typecheck    # 仅类型检查
npm run lint         # oxlint
```

## 测试

```bash
npm run test         # Vitest 单元测试（RTL + jsdom + MSW）
npm run test:e2e     # Playwright E2E（chromium + mobile-chromium）
```

- 单元测试覆盖核心竞态场景：详情快速切换只显示最后选中的工单、建单成功但受理失败时重试不重复建单、SSE 流取消语义。
- E2E 覆盖主流程：智能助手流式渲染与 interrupt 审批闭环、新建工单、工单状态流转。

## 页面与路由

| 路由 | 视图 | 说明 |
|---|---|---|
| `/tickets` | QueueView | 工单队列（全部/我的处理/已解决，`?view=` 切换） |
| `/tickets/:ticketId` | QueueView | 工单详情：状态流转、SLA、消息、回访、补充字段 |
| `/assistant` | AssistantView | SSE 流式智能助手（含 interrupt 人工审批） |
| `/assets` | AssetsView | IT 资产管理与删除 |
| `/knowledge` | KnowledgeView | 知识文档列表与废弃 |
| `/it-policies` | ItPoliciesView | IT 策略与 SLA 关联设置 |

## 认证边界（重要）

- **本地演示**：后端在 `APP_ENV=development`、`AUTH_MODE=dev` 且显式开启
  `DEV_DEMO_LOGIN_ENABLED=true` 时提供固定的 customer / agent / admin 身份选择，
  前端通过 `/api/auth/dev/session` 获取短期会话；不接受自定义租户、用户或角色。
- **企业登录**：`AUTH_MODE=oidc` 时，前端使用 Authorization Code + PKCE，配置
  `OIDC_CLIENT_ID`、`OIDC_REDIRECT_URI`、`OIDC_AUTHORIZATION_ENDPOINT`、
  `OIDC_TOKEN_ENDPOINT` 和 `OIDC_WEB_SCOPES`。后端仍是 Bearer/OIDC JWT 资源服务器，
  `/api/auth/me` 返回服务端校验后的租户与权限主体。
- 访问令牌只保存在当前标签页的 `sessionStorage`，不会写入 `localStorage` 或前端构建产物。
  若需要进一步降低浏览器令牌暴露面，可在后续增加 BFF + HttpOnly Cookie；这不是当前登录流程的前置依赖。

## 已知限制

- OIDC 登录依赖 IdP 允许浏览器完成 PKCE 令牌交换；若部署环境不允许跨域令牌交换，应改为 BFF。
  401 会清除当前会话并回到登录页；401/403/409/429/5xx 仍有统一错误文案（`describeApiError`）。
- 移动端侧栏支持 Escape 关闭与 inert 防聚焦；弹窗具备 `role="dialog"` /
  `aria-modal` / `aria-labelledby`，但焦点锁定与恢复（focus trap）尚未实现。
- 危险操作（删除资产/废弃文档/删除策略）使用 `window.confirm` 基础确认
  （含对象名与影响说明），未实现自定义确认框与逐行独立 busy 状态。
- 知识库/资产/策略页面为列表级能力（搜索/筛选/分页），文档 embedding 状态、
  工单关联关系等增强属后续迭代。

## 本地演示步骤（10 分钟内）

1. `npm run dev` + 后端（PostgreSQL/Redis + `uvicorn backend.app:app`）。
2. 打开 `/tickets`：演示列表搜索、打开工单、状态流转（接单 → 处理 → 解决）。
3. 新建工单：演示客户端幂等（受理失败重试不重复建单）。
4. 快速切换两张工单：演示竞态防护（只显示最后选中的工单）。
5. `/assistant`：SSE 流式回复与 interrupt 审批。
6. `npm run test`：20+ 单元测试（竞态/幂等/SSE 分片与取消/错误文案）。
7. 明确说明：演示使用固定开发身份；OIDC 登录需配置企业 IdP；E2E 覆盖主流程但非生产验证。

## 目录结构

```
src/
  auth/       # 会话存储、Demo 登录、OIDC PKCE 与登录守卫
  api/        # 统一 API 客户端（JSON 封装 + SSE 读取），无 UI 依赖
  components/ # 可复用组件（ApprovalCard、CreateTicketDialog 等）
  views/      # 路由级视图（Queue / Assistant / Assets / Knowledge / Admin）
  lib/        # 通用工具
  test/       # 测试环境配置（setupTests）
e2e/          # Playwright 用例与 fixtures
```

## 关键设计

### SSE 流式对话生命周期（src/api/chat.ts）

- 协议与后端 `_execute_run` 对齐：`text` / `tool` / `interrupt` / `end` / `error` 五类事件。
- `eventsource-parser` 增量解析，正确处理跨 chunk 的事件边界。
- `AbortController` 贯穿 fetch 与读取循环；abort 时主动 `reader.cancel()`，避免 `read()` 永久挂起。
- 中断审批：收到 `interrupt` 后渲染审批卡片，用户决定后调用 `/chat/resume` 以同一 SSE 协议续跑。

### 竞态防护

- **流内状态更新绑定消息 id**：SSE 事件回调通过闭包持有目标消息 id（而非 ref 读取），避免批量状态更新延迟执行时命中失效引用——快流（单 chunk 交付全部事件）下内容会静默丢失，真实网络下也会偶发。
- **请求序号 + AbortController 双保险**：视图层切换/搜索时旧请求的响应一律丢弃，并中断传输。
- **客户端幂等建单**：`ticket_id` / `operation_id` 由前端生成，建单成功但受理（intake）失败时重试只补受理，不会重复建单。

### 列表体验

- 搜索输入 300ms 防抖，分页按游标去重，避免重复条目与竞态覆盖。
