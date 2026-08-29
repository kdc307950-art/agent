"""幂等演示数据种子命令 —— 空数据库一条命令生成可演示的 IT 服务台闭环数据。

用法（先启动 PostgreSQL/Redis，或直接用 infra/compose.demo.yml）：

    uv run python -m backend.seed_demo
    uv run python -m backend.seed_demo --tenant demo
    uv run python -m backend.seed_demo --database-url postgresql://langgraph:demo_only_not_a_secret@127.0.0.1:5432/langgraph

生成内容（租户默认 `demo`）：
    - SLA 策略：sla-vpn / sla-account / sla-network / sla-default（Asia/Shanghai 工作日历）
    - IT 策略：it.vpn -> sla-vpn（必填字段 device / operating_system / error_message / network）、it -> sla-default
    - 路由规则：it -> team-it，派单给排班中且有空闲容量的成员
    - 客服团队 team-it、成员 agent-1、全年排班
    - 8 篇已发布知识文档（visibility=public，客户建单的 RAG 建议可召回）
    - 5 台 IT 资产（归属 customer-1 / customer-2，共享设备无使用人）

幂等：重复执行不会重复插入；知识文档与排班窗口每次刷新。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta

from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .knowledge import KnowledgeChunkInput, KnowledgeDocumentInput, KnowledgeRepository
from .migrations import setup_postgres
from .tickets import ItPolicyRepository, UpsertItPolicy

# Windows 控制台默认 GBK 编码，强制 UTF-8 输出，避免 emoji/中文报错。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_TENANT = "demo"

SLA_POLICIES = [
    # policy_id, name, first_response_minutes, resolution_minutes
    ("sla-vpn", "VPN 支持 SLA", 15, 120),
    ("sla-account", "账号支持 SLA", 30, 180),
    ("sla-network", "网络支持 SLA", 30, 180),
    ("sla-default", "默认支持 SLA", 60, 480),
]

IT_POLICIES = [
    ("it.vpn", "sla-vpn", ("device", "operating_system", "error_message", "network")),
    ("it", "sla-default", ()),
]

MEMBER_SKILLS = [
    "vpn",
    "account",
    "network",
    "email",
    "hardware",
    "software",
    "printer",
    "permission",
]

ASSETS = [
    # asset_id, asset_no, asset_type, name, hostname, ip, department, owner, location
    (
        "laptop-001",
        "DEMO-NB-001",
        "laptop",
        "办公笔记本 001",
        "nb-001",
        "10.0.0.11",
        "it",
        "customer-1",
        "3F 开放区",
    ),
    (
        "laptop-002",
        "DEMO-NB-002",
        "laptop",
        "办公笔记本 002",
        "nb-002",
        "10.0.0.12",
        "it",
        "customer-2",
        "3F 开放区",
    ),
    (
        "desktop-001",
        "DEMO-DT-001",
        "desktop",
        "办公台式机 001",
        "dt-001",
        "10.0.0.21",
        "it",
        "customer-1",
        "4F 财务区",
    ),
    (
        "printer-001",
        "DEMO-PR-001",
        "printer",
        "共享打印机 001",
        "pr-001",
        "10.0.0.31",
        "it",
        None,
        "3F 打印区",
    ),
    (
        "monitor-001",
        "DEMO-MN-001",
        "monitor",
        "显示器 001",
        None,
        None,
        "it",
        "customer-1",
        "3F 开放区",
    ),
]

KNOWLEDGE_DOCUMENTS = [
    {
        "document_id": "vpn-001",
        "title": "VPN 配置指南",
        "category": "it.vpn",
        "chunks": [
            "公司 VPN 无法连接、一直转圈或提示错误码 769 / 809 时，按顺序排查：1. 确认已连接外网；2. 检查 VPN 客户端是否为最新版本（769 通常表示目标不可达，809 表示连接被服务端拒绝）；3. 确认账号未被锁定、密码是否过期。远程办公连不上内网多与以上三项有关。仍无法连接请记录错误码并提交工单，附上设备型号、操作系统和网络环境。",
            "Windows 配置 VPN 步骤：设置 -> 网络和 Internet -> VPN -> 添加 VPN 连接，服务器地址填 vpn.company.com，类型选 L2TP/IPsec，用户名密码使用企业账号。Mac 用户在系统设置 -> 网络 -> VPN 中添加相同配置。配置完成后如仍登不进去，删除旧配置重新添加，或联系 IT 服务台解锁账号。",
        ],
    },
    {
        "document_id": "email-001",
        "title": "企业邮箱配置指南",
        "category": "it.email",
        "chunks": [
            "企业邮箱支持 Outlook 与手机自带邮件客户端。IMAP 服务器 imap.company.com，SMTP 服务器 smtp.company.com，均使用 SSL，端口 993 / 465。首次登录需要输入完整企业邮箱地址与统一身份认证密码。",
            "收不到邮件时先检查：1. 是否被自动归档或垃圾邮件拦截；2. 客户端是否处于离线模式；3. 邮箱容量是否已满（超过 90% 建议归档历史邮件）。Outlook 突然收不到邮件多为容量或归档问题。",
        ],
    },
    {
        "document_id": "password-001",
        "title": "账号密码重置指南",
        "category": "it.account",
        "chunks": [
            "忘记密码可在登录页点击「忘记密码」，通过绑定手机号或企业微信扫码自助重置。SSO 账号锁定后需等待 15 分钟自动解锁，或联系 IT 服务台人工解锁。重置后仍登不进去，请确认新密码未违反策略并检查 Caps Lock。",
            "密码策略：至少 10 位，包含大小写字母、数字与特殊字符，90 天强制更换，不可与最近 5 次密码相同。登录提示密码已过期时，按提示完成改密即可。",
        ],
    },
    {
        "document_id": "printer-001",
        "title": "打印机安装与驱动指南",
        "category": "it.printer",
        "chunks": [
            "安装共享打印机：开始菜单 -> 设置 -> 蓝牙和其他设备 -> 打印机 -> 添加设备，选择 \\\\print-server\\printer-001。驱动缺失时从驱动站下载对应型号驱动，安装后打印测试页验证。打印机显示离线时先检查电源、网线和驱动状态。",
            "打印卡纸或报错时：1. 打开前盖检查卡纸位置并清理；2. 检查硒鼓余量；3. 重启打印机电源；4. 仍失败请在工单中注明打印机型号与报错代码。",
        ],
    },
    {
        "document_id": "software-001",
        "title": "软件安装与授权指南",
        "category": "it.software",
        "chunks": [
            "办公软件通过软件中心自助安装，覆盖 Office、浏览器、压缩工具与会议客户端。会议客户端安装失败时先卸载旧版本、清理缓存后重试。安装企业软件需要管理员权限，请联系 IT 管理员开通。",
            "禁止安装未授权软件。许可证管理：部门许可证池不足时，先回收离职员工授权再申请新授权。许可证不够用请提交权限申请工单说明原因。",
        ],
    },
    {
        "document_id": "network-001",
        "title": "办公网络排查指南",
        "category": "it.network",
        "chunks": [
            "办公区断网、无线信号弱或网速慢时排查顺序：1. 检查网线/无线连接状态；2. 查看其他设备是否同样断网（判断是否区域故障）；3. ipconfig /release 后 /renew 重新获取 IP；4. ping 网关与 8.8.8.8 判断内网/外网问题。",
            "Wi-Fi 连不上、提示密码错误时，先忘记网络重新连接，确认选择了正确的 SSID（公司 5G 频段）并输入正确密码。办公区无线信号弱可切换到有线网口，或联系 IT 排查该区域接入点。",
        ],
    },
    {
        "document_id": "hardware-001",
        "title": "电脑故障报修指南",
        "category": "it.hardware",
        "chunks": [
            "电脑开不了机、蓝屏或重启频繁时先尝试：1. 强制重启；2. 外接显示器判断是否为屏幕故障；3. 检查电源与散热。报修工单需提供资产编号、故障现象、发生时间与紧急程度。",
            "硬件维修周期：笔记本 3 个工作日，台式机 1 个工作日，显示器 3 个工作日。维修期间可申请备用机。屏幕损坏、键盘失灵等硬件问题直接走报修流程。",
        ],
    },
    {
        "document_id": "permission-001",
        "title": "系统权限申请指南",
        "category": "it.permission",
        "chunks": [
            "系统权限申请流程：1. 填写权限申请表（系统名称、所需权限、事由、期限）；2. 直属上级审批；3. IT 管理员开通并审计留痕。离职员工的权限应在离职流程中及时回收。",
            "高危权限（管理员、批量导出、财务审批）需要部门负责人与 IT 管理员双重审批，权限默认最小化授予并定期复核。申请开通新系统权限请走审批工单。",
        ],
    },
]


def _utc(offset_days: int = 0) -> datetime:
    return datetime.now(UTC) + timedelta(days=offset_days)


def _document_input(tenant_id: str, item: dict) -> KnowledgeDocumentInput:
    return KnowledgeDocumentInput(
        document_id=item["document_id"],
        version=1,
        title=item["title"],
        source_uri=f"/knowledge/{item['document_id']}",
        status="published",
        category=item["category"],
        visibility="public",
        created_by="seed-demo",
        valid_from=_utc(-1),
        valid_until=_utc(365),
        metadata={"version": "v1", "source": "seed-demo"},
    )


async def _seed(tenant_id: str, conninfo: str) -> dict[str, int]:
    os.environ["DATABASE_URL"] = conninfo
    await setup_postgres()

    pool = AsyncConnectionPool(conninfo, min_size=1, max_size=3, open=False, name="seed-demo")
    await pool.open(wait=True)
    counts: dict[str, int] = {}
    try:
        async with pool.connection() as connection:
            async with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
                for policy_id, name, first_response, resolution in SLA_POLICIES:
                    await cursor.execute(
                        """
                        INSERT INTO sla_policies (
                            tenant_id, policy_id, name, timezone, business_days,
                            work_start, work_end, holidays,
                            first_response_minutes, resolution_minutes
                        ) VALUES (%s, %s, %s, 'Asia/Shanghai', %s, %s, %s, ARRAY[]::DATE[], %s, %s)
                        ON CONFLICT (tenant_id, policy_id) DO NOTHING
                        """,
                        (
                            tenant_id,
                            policy_id,
                            name,
                            [0, 1, 2, 3, 4],
                            "09:00",
                            "18:00",
                            first_response,
                            resolution,
                        ),
                    )
                counts["sla"] = len(SLA_POLICIES)

                await cursor.execute(
                    """
                    INSERT INTO support_teams (tenant_id, team_id, name, department_id)
                    VALUES (%s, 'team-it', 'IT 服务台', 'it')
                    ON CONFLICT (tenant_id, team_id) DO NOTHING
                    """,
                    (tenant_id,),
                )
                await cursor.execute(
                    """
                    INSERT INTO support_members (tenant_id, member_id, team_id, skills, capacity)
                    VALUES (%s, 'agent-1', 'team-it', %s, 10)
                    ON CONFLICT (tenant_id, member_id) DO NOTHING
                    """,
                    (tenant_id, MEMBER_SKILLS),
                )
                # 排班窗口每次种子执行都刷新，保证任意时刻派单都能命中在岗成员。
                await cursor.execute(
                    """
                    INSERT INTO support_schedules (tenant_id, schedule_id, member_id, starts_at, ends_at)
                    VALUES (%s, 'demo-schedule', 'agent-1', %s, %s)
                    ON CONFLICT (tenant_id, schedule_id) DO UPDATE SET
                        member_id = EXCLUDED.member_id,
                        starts_at = EXCLUDED.starts_at,
                        ends_at = EXCLUDED.ends_at
                    """,
                    (tenant_id, _utc(-1), _utc(364)),
                )
                await cursor.execute(
                    """
                    INSERT INTO routing_rules (
                        tenant_id, rule_id, priority, category, subcategory,
                        channel, department_id, required_skill, target_team_id, active
                    ) VALUES (%s, 'routing-it', 100, 'it', NULL, NULL, NULL, NULL, 'team-it', TRUE)
                    ON CONFLICT (tenant_id, rule_id) DO NOTHING
                    """,
                    (tenant_id,),
                )
                counts["team"] = 1
                counts["member"] = 1
                counts["schedule"] = 1
                counts["routing_rule"] = 1

                for (
                    asset_id,
                    asset_no,
                    asset_type,
                    name,
                    hostname,
                    ip,
                    department,
                    owner,
                    location,
                ) in ASSETS:
                    await cursor.execute(
                        """
                        INSERT INTO it_assets (
                            tenant_id, asset_id, asset_no, asset_type, name, hostname,
                            ip_address, department, owner_user_id, status,
                            purchased_at, warranty_expires_at, location, custom_fields
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'in_use', %s, %s, %s, '{}'::jsonb)
                        ON CONFLICT (tenant_id, asset_id) DO NOTHING
                        """,
                        (
                            tenant_id,
                            asset_id,
                            asset_no,
                            asset_type,
                            name,
                            hostname,
                            ip,
                            department,
                            owner,
                            _utc(-365),
                            _utc(365),
                            location,
                        ),
                    )
                counts["asset"] = len(ASSETS)

        it_policies = ItPolicyRepository(pool)
        for category, policy_id, required_fields in IT_POLICIES:
            await it_policies.upsert(
                tenant_id,
                UpsertItPolicy(
                    category=category,
                    policy_id=policy_id,
                    required_fields=required_fields,
                    default_priority="normal",
                    auto_answer_enabled=False,
                    approval_required=False,
                ),
            )
        counts["it_policy"] = len(IT_POLICIES)

        knowledge = KnowledgeRepository(pool)
        for item in KNOWLEDGE_DOCUMENTS:
            await knowledge.put_document(
                tenant_id,
                _document_input(tenant_id, item),
                [
                    KnowledgeChunkInput(chunk_id=f"c{index}", ordinal=index, content=content)
                    for index, content in enumerate(item["chunks"])
                ],
            )
        counts["knowledge"] = len(KNOWLEDGE_DOCUMENTS)
        return counts
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="生成幂等的 IT 服务台演示种子数据")
    parser.add_argument("--tenant", default=DEFAULT_TENANT, help="演示租户 ID（默认 demo）")
    parser.add_argument(
        "--database-url", default=None, help="PostgreSQL 连接串（默认读 DATABASE_URL / .env）"
    )
    args = parser.parse_args()

    load_dotenv()
    conninfo = args.database_url or os.getenv("DATABASE_URL", "").strip()
    if not conninfo:
        raise SystemExit("缺少 DATABASE_URL：请设置环境变量或使用 --database-url")
    counts = asyncio.run(_seed(args.tenant, conninfo))

    print(f"✅ 演示种子完成（租户 {args.tenant}）：")
    for key, value in counts.items():
        print(f"   - {key}: {value}")
    print()
    print("演示账号（开发令牌，AUTH_MODE=dev，租户与种子一致）：")
    print(
        f"  uv run python -m backend.issue_dev_token {args.tenant} customer-1 --role helpdesk-customer"
    )
    print(f"  uv run python -m backend.issue_dev_token {args.tenant} agent-1 --role helpdesk-agent")
    print(
        f"  uv run python -m backend.issue_dev_token {args.tenant} admin-1 --role helpdesk-it-admin"
    )
    print()
    print("客户 customer-1 的资产：laptop-001（笔记本）、desktop-001、monitor-001。")
    print("演示入口：frontend 登录后选择「新建」，提交「VPN 无法连接」工单并绑定 laptop-001。")


if __name__ == "__main__":
    main()
