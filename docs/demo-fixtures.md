# 唯一演示数据清单（V1，租户 `demo`）

> 与 `backend/seed_demo.py` 的常量一一对应；任何演示账号、资产、工单路径都可追溯。
> 种子命令：`docker compose -f infra/compose.demo.yml exec agent python -m backend.seed_demo`（幂等）。

## 三角色演示账号

| 角色 | 用户 ID | 令牌命令（容器内） | 说明 |
| --- | --- | --- | --- |
| 员工/客户 | `customer-1` | `docker compose -f infra/compose.demo.yml exec agent python -m backend.issue_dev_token demo customer-1 --role helpdesk-customer` | 拥有 laptop-001 / desktop-001 / monitor-001 |
| 员工/客户 | `customer-2` | 同上（`customer-2`） | 拥有 laptop-002 |
| IT 客服 | `agent-1` | `... --role helpdesk-agent` | team-it 成员，全年排班 |
| IT 管理员 | `admin-1` | `... --role helpdesk-it-admin` | 管理资产/策略/知识 |

## 三类演示工单（固定样例）

| 工单 | 输入文本 | 预期分类 | 必填字段 | SLA | 目标团队 |
| --- | --- | --- | --- | --- | --- |
| VPN | `VPN 无法连接，错误码 809` | `it.vpn` | device / operating_system / error_message / network | `sla-vpn`（15 分钟首响 / 2 小时解决） | team-it |
| 账号 | `SSO 登录失败` | `it.account` | 设备/影响范围（内置字段） | `sla-account`（30/180） | team-it |
| 网络 | `办公室断网了` | `it.network` | 设备/影响范围（内置字段） | `sla-network`（30/180） | team-it |

## 部门与 ACL

| 部门 | 用户 | 资产 | 可见知识 |
| --- | --- | --- | --- |
| `it` | customer-1 / customer-2 / agent-1 | IT 资产（5 台） | public 文档（vpn/account/network/... 8 篇） |
| `finance` | （渠道身份映射示例）ext-user-1 | 财务资产（映射后） | finance-001（restricted） |

## SLA 规则

| policy_id | 名称 | 首响（分钟） | 解决（分钟） | 工作日历 |
| --- | --- | --- | --- | --- |
| sla-vpn | VPN 支持 SLA | 15 | 120 | Asia/Shanghai，周一至周五 9:00-18:00 |
| sla-account | 账号支持 SLA | 30 | 180 | 同上 |
| sla-network | 网络支持 SLA | 30 | 180 | 同上 |
| sla-default | 默认支持 SLA | 60 | 480 | 同上 |

## 资产编号

| asset_id | asset_no | 名称 | 部门 | 归属 |
| --- | --- | --- | --- | --- |
| laptop-001 | DEMO-NB-001 | 办公笔记本 001 | it | customer-1 |
| laptop-002 | DEMO-NB-002 | 办公笔记本 002 | it | customer-2 |
| desktop-001 | DEMO-DT-001 | 办公台式机 001 | it | customer-1 |
| printer-001 | DEMO-PR-001 | 共享打印机 001 | it | 共享 |
| monitor-001 | DEMO-MN-001 | 显示器 001 | it | customer-1 |

## 演示路径的可追溯性

`Web 建单 → 点击「新建」→ 选择 laptop-001 → 提交「VPN 无法连接，错误码 809」→ 缺字段追问 → 补充 device/operating_system/error_message/network → 自动分类 it.vpn → SLA sla-vpn → 派单 team-it → agent-1 接单 → 处理 → 解决 → 回访 → 关闭`。
