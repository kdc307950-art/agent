# 10 分钟演示脚本：中小企业 IT 服务台闭环

> 适用环境：`infra/compose.demo.yml` 一键启动 + `backend.seed_demo` 种子数据。
> 前置：`DEEPSEEK_API_KEY` 已配置（自动分类 / 知识建议依赖模型；不配置时流程可走到派单，知识建议为空并转人工）。

## 演示账号

| 账号 | 角色 | 令牌命令 |
|---|---|---|
| `demo / customer-1` | 员工（客户） | `uv run python -m backend.issue_dev_token demo customer-1 --role helpdesk-customer` |
| `demo / agent-1` | IT 客服 | `uv run python -m backend.issue_dev_token demo agent-1 --role helpdesk-agent` |
| `demo / admin-1` | IT 管理员 | `uv run python -m backend.issue_dev_token demo admin-1 --role helpdesk-it-admin` |

三个令牌分别粘贴到前端页面顶部的令牌输入框（或 Vite 代理的 `DEV_TENANT_TOKEN`）。

## 准备（约 2 分钟）

```powershell
docker compose -f infra/compose.demo.yml up --build -d
uv run python -m backend.seed_demo
# 迁移已由 compose 的 migrate 服务完成；seed 幂等，可重复执行
```

预期输出：`✅ 演示种子完成（租户 demo）`，包含 SLA ×4、IT 策略 ×2、团队/成员/排班/路由、知识文档 ×8、资产 ×5。

## 演示流程（约 8 分钟）

| # | 步骤 | 操作 | 预期结果 |
|---|---|---|---|
| 1 | 打开工作台 | 浏览器访问 http://127.0.0.1:8000 ，粘贴 **customer-1** 令牌 | 进入「工单队列」，左侧导航含 资产 / 知识库 / IT 策略设置 |
| 2 | 员工新建工单 | 点「新建」→ 标题「VPN 无法连接」→ 描述「笔记本连不上公司 VPN，提示错误码 809」→ 关联资产选 `laptop-001` → 提交 | 工单创建成功（status `new`），进入受理 |
| 3 | 自动分类 | 受理图自动执行分类（it + vpn） | 工单 category 显示 `it.vpn`，加载租户 IT 策略 |
| 4 | 必填字段追问 | 策略要求 device / operating_system / error_message / network | 工单进入「等待客户」，前端出现补充信息表单 |
| 5 | 员工补充信息 | 填写：设备「laptop-001」、系统「Windows 11」、错误信息「809」、网络「办公网」→ 提交 | 缺失字段补齐，受理继续 |
| 6 | SLA 与派单 | 分类 `it.vpn` 命中 `sla-vpn`（首响 15 分钟 / 解决 2 小时）；路由规则派给 `team-it` | 工单 `queued`；详情页 SLA 显示首次响应/解决时限，处理团队 `team-it` |
| 7 | 知识建议 | RAG 检索 `vpn-001`，生成建议回复并带引用 | 详情页「知识引用」出现《VPN 配置指南》（document_id vpn-001） |
| 8 | 切换客服 | 粘贴 **agent-1** 令牌，刷新 | 队列中出现该工单，分类 it.vpn、优先级 normal、SLA 倒计时可见 |
| 9 | 客服接单 | 点「接单」 | 工单 `assigned`，指派给 agent-1 |
| 10 | 开始处理 | 点「开始处理」 | 工单 `in_progress`，SLA 开始计时（首响已标记） |
| 11 | 处理并解决 | 参考知识引用给出的排查步骤，点「标记解决」 | 工单 `resolved`，记录解决时间 |
| 12 | 发起回访 | 客服点「发起回访」 | 生成满意度回访（`satisfaction_surveys` + Outbox 事件） |
| 13 | 员工确认 | 切换回 **customer-1** 令牌，在工单详情确认问题已解决 | 状态流转正常，客户视角只看到自己的工单 |
| 14 | 提交满意度 | 员工提交 5 分 + 反馈 | 回访状态 `responded`，客服端可见评分 |
| 15 | 关闭工单 | 客服切回 agent-1，点「关闭工单」 | 工单 `closed`，闭环完成 |
| 16 | 收尾检查 | `GET /tickets` 过滤、资产台账查看 laptop-001 的历史工单 | 资产页可看到该资产关联工单；全部操作已写入审计 |

## 验收检查点

- 分类准确：VPN 工单自动识别为 `it.vpn`（而非只到 `it`）。
- SLA 正确：详情页策略 ID 为 `sla-vpn`，与默认 SLA 时限不同。
- 字段补全：缺少 device / error_message 时会追问，补齐后继续。
- 知识引用：建议回复带文档 ID 与标题，无证据时不自动发送、转人工。
- 权限边界：customer-1 看不到他人资产与他人工单；客服可处理全部队列。
- 幂等建单：重复提交相同渠道事件不会重复建单（企业微信演示用 `/integrations/wecom/events`）。

## 清理

```powershell
docker compose -f infra/compose.demo.yml down -v
```
