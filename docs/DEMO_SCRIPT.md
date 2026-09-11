# 10 分钟演示脚本：中小企业 IT 服务台闭环

> 适用环境：Windows 下先执行 `./scripts/demo.ps1`。它会启动 `infra/compose.demo.yml`（fake-fmg → migrate → seed → agent → web 自动按依赖顺序执行）、等待就绪并运行控制面八步演练；浏览器访问 http://127.0.0.1:8000 后选择演示身份即可登录。
> 前置：Docker Desktop 已启动，首次构建可访问镜像与 npm 依赖仓库；`DEEPSEEK_API_KEY` 已配置（自动分类 / 知识建议依赖模型；不配置时流程可走到派单，知识建议为空并转人工）。镜像已经构建过时可改用 `./scripts/demo.ps1 -SkipBuild`。
> 产品边界（目标客户 / VPN 受理与建议闭环（主产品 it.vpn）/ 主链路 / 非目标 / 人工介入规则）见 [docs/product/vpn-v1-scope.md](docs/product/vpn-v1-scope.md)。

## 演示账号

| 账号 | 角色 | 令牌命令 |
|---|---|---|
| `demo / customer-1` | 员工（客户） | 登录页选择「员工」 |
| `demo / agent-1` | IT 客服 | 登录页选择「IT 客服」 |
| `demo / admin-1` | IT 管理员 | 登录页选择「IT 管理员」 |

演示登录由后端固定签发短期开发会话，保存在当前标签页的 sessionStorage，关闭标签页即失效；前端不接受自定义身份。生产模式使用 OIDC Authorization Code + PKCE，不复用演示会话。

## 准备（约 2 分钟）

```powershell
./scripts/demo.ps1
```

预期输出包括 `演示环境已就绪` 以及 Fake FMG 演练的 `ALL PASS`。脚本会等待 `/readyz`，超时会直接失败并提示查看 Compose 日志。若只演示服务台闭环，可使用 `./scripts/demo.ps1 -SkipDrill`；仅重跑控制面演练可使用 `./scripts/drill-fmg.ps1`。

## 演示流程（约 8 分钟）

| # | 步骤 | 操作 | 预期结果 |
|---|---|---|---|
| 1 | 打开工作台 | 浏览器访问 http://127.0.0.1:8000 ，选择「员工」 | 进入「工单队列」，左侧导航含 资产 / 知识库 / IT 策略设置 |
| 2 | 员工新建工单 | 点「新建」→ 标题「VPN 无法连接」→ 描述「笔记本连不上公司 VPN，提示错误码 809」→ 关联资产选 `laptop-001` → 提交 | 工单创建成功（status `new`），进入受理 |
| 3 | 自动分类 | 受理图自动执行分类（it + vpn） | 工单 category 显示 `it.vpn`，加载租户 IT 策略 |
| 4 | 必填字段追问 | 策略要求 8 项固定字段：device / operating_system / vpn_client / client_version / error_code / network / multi_user_impacted / recent_change | 工单进入「等待客户」，前端出现补充信息表单 |
| 5 | 员工补充信息 | 填写 8 项固定字段：设备「laptop-001」、系统「Windows 11」、VPN 客户端「公司客户端」、客户端版本「3.4.2」、错误码「809」、网络「办公网」、多人受影响「否」、最近变更「升级客户端」→ 提交 | 8 项必填字段补齐，受理继续 |
| 6 | SLA 与派单 | 分类 `it.vpn` 命中 `sla-vpn`（首响 15 分钟 / 解决 2 小时）；路由规则派给 `team-it` | 工单 `queued`；详情页 SLA 显示首次响应/解决时限，处理团队 `team-it` |
| 7 | 知识建议 | RAG 检索 `vpn-001`，生成建议回复并带引用 | 详情页「知识引用」出现《VPN 配置指南》（document_id vpn-001） |
| 8 | 切换客服 | 打开侧栏退出登录，选择「IT 客服」 | 队列中出现该工单，分类 it.vpn、优先级 normal、SLA 倒计时可见 |
| 9 | 客服接单 | 点「接单」 | 工单 `assigned`，指派给 agent-1 |
| 10 | 开始处理 | 点「开始处理」 | 工单 `in_progress`，SLA 开始计时（首响已标记） |
| 11 | 处理并解决 | 参考知识引用给出的排查步骤，点「标记解决」 | 工单 `resolved`，记录解决时间 |
| 12 | 发起回访 | 客服点「发起回访」 | 生成满意度回访（`satisfaction_surveys` + Outbox 事件） |
| 13 | 员工确认 | 退出登录，选择「员工」，在工单详情确认问题已解决 | 状态流转正常，客户视角只看到自己的工单 |
| 14 | 提交满意度 | 员工提交 5 分 + 反馈 | 回访状态 `responded`，客服端可见评分 |
| 15 | 关闭工单 | 切回「IT 客服」，点「关闭工单」 | 工单 `closed`，闭环完成 |
| 16 | 收尾检查 | `GET /tickets` 过滤、资产台账查看 laptop-001 的历史工单 | 资产页可看到该资产关联工单；全部操作已写入审计 |

## 控制面关联入口（可选，约 1 分钟）

客服将 VPN 工单推进为 `in_progress` 后，可调用 `POST /tickets/{ticket_id}/vpn/redeploy-request`，请求体仅允许可选的 `reason_codes`。该接口不接收目标设备、用户、资产或调用方幂等键；服务端从工单和租户上下文推导这些值，先生成 preview 和待审批记录，不会直接执行 install。审批、`diff_hash` 校验、异步 task 轮询和人工对账仍使用现有 `/vpn/reissue/*` 链路。

该入口只适用于本租户的 `it.vpn`、`in_progress` 工单，且需要 `ticket:agent`；跨租户工单返回 `404`，其他类别或状态返回 `409`。这条演示证明服务台与控制面之间的受控衔接，不证明真实 FMG 或 FortiGate 已下发。

> 账号/权限、网络问题在 V1 演示中降为**旁路或转人工**，不作为演示主线（见 [docs/product/vpn-v1-scope.md](docs/product/vpn-v1-scope.md)）。

## 验收检查点

- 分类准确：VPN 工单自动识别为 `it.vpn` + `vpn_fault`（如 `connection_failed`，而非只到 `it`）。
- SLA 正确：详情页策略 ID 为 `sla-vpn`，与默认 SLA 时限不同。
- 字段补全：缺少 8 项固定字段（如 device / error_code）时会追问，补齐后继续。
- 知识引用：建议回复带文档 ID 与标题，无证据时不自动发送、转人工。
- 权限边界：customer-1 看不到他人资产与他人工单；客服可处理全部队列。
- 幂等建单：重复提交相同渠道事件不会重复建单（企业微信演示用 `/integrations/wecom/events`）。

## 清理

```powershell
./scripts/demo.ps1 -Down
```
