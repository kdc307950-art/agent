"""应用层 schema 版本管理与就绪检查。

LangGraph 自己管理 checkpoint/store 的迁移表；本模块管理应用表，
并记录 backend.migrations 实际应用的版本号。
当版本或必需表结构与当前代码不匹配时，服务拒绝就绪（readiness 失败）。
"""

from __future__ import annotations

from typing import Iterable

from psycopg import AsyncConnection


APP_SCHEMA_NAME = "langgraph_agent"
APP_SCHEMA_VERSION = 14
MIGRATION_LOCK_KEY = 891274631

# These are the tables created by the pinned LangGraph PostgreSQL adapters and
# by backend.audit.  Keeping the list here makes schema drift visible at
# readiness time instead of on the first user request.
REQUIRED_RELATIONS: tuple[str, ...] = (
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "store_migrations",
    "store",
    "agent_runs",
    "agent_events",
    "agent_schema_version",
    "agent_thread_activity",
    "tickets",
    "ticket_status_events",
    "inbound_events",
    "knowledge_documents",
    "knowledge_chunks",
    "ticket_messages",
    "outbox_events",
    "sla_policies",
    "ticket_sla",
    "satisfaction_surveys",
    "ticket_workflow_runs",
    "support_teams",
    "support_members",
    "support_schedules",
    "routing_rules",
    "ticket_assignments",
    "it_assets",
    "tenant_it_policies",
    "admin_audit_events",
)


async def ensure_schema_version(connection: AsyncConnection) -> None:
    """Create or validate the application schema version row.

    Future schema changes must add an explicit migration step and increment
    ``APP_SCHEMA_VERSION``.  A running application never upgrades the schema.

    显式开事务：调用方（``backend.migrations``）的连接是 autocommit 的，否则会
    阻塞 ``CREATE INDEX CONCURRENTLY``（见 migrations.py 的注释）。而 v1→v2 的
    「建表 + 回填 + 更新版本号」必须原子完成 —— 逐条提交时中途失败会留下
    「版本号已是 2 但回填只做了一半」的状态，且因为版本号已更新，重跑迁移不会修复它。
    这段事务在 checkpointer.setup() 之后才开始，不会再阻塞索引创建。
    """

    async with connection.transaction(), connection.cursor() as cursor:
        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_schema_version (
                schema_name TEXT PRIMARY KEY,
                version INTEGER NOT NULL CHECK (version >= 1),
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await cursor.execute(
            "SELECT version FROM agent_schema_version WHERE schema_name = %s",
            (APP_SCHEMA_NAME,),
        )
        row = await cursor.fetchone()
        if row is None:
            # audit.setup() creates the v1 application tables before this function.
            # Record that baseline, then run every later migration in order. Writing
            # the latest version here would mark a fresh database ready before its
            # newer relations actually existed.
            current = 1
            await cursor.execute(
                "INSERT INTO agent_schema_version (schema_name, version) VALUES (%s, %s)",
                (APP_SCHEMA_NAME, current),
            )
        else:
            current = int(row[0])
        if current > APP_SCHEMA_VERSION:
            raise RuntimeError(
                f"应用 schema 版本不匹配: database={current}, expected={APP_SCHEMA_VERSION}; "
                "请先运行迁移命令"
            )

        if current < 2:
            await cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_thread_activity (
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    thread_id TEXT PRIMARY KEY,
                    last_started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_finished_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_thread_activity_finished
                ON agent_thread_activity (last_finished_at)
                WHERE last_finished_at IS NOT NULL
                """
            )
            await cursor.execute(
                """
                INSERT INTO agent_thread_activity
                    (tenant_id, user_id, thread_id, last_started_at, last_finished_at, updated_at)
                SELECT tenant_id, user_id, thread_id, max(started_at), max(finished_at), now()
                FROM agent_runs
                GROUP BY tenant_id, user_id, thread_id
                ON CONFLICT (thread_id) DO NOTHING
                """
            )
            current = 2
            await cursor.execute(
                "UPDATE agent_schema_version SET version = %s, applied_at = now() WHERE schema_name = %s",
                (current, APP_SCHEMA_NAME),
            )

        if current < 3:
            # v3: 挂起审批是一次运行的持久状态。旧库的匿名 CHECK 约束由
            # PostgreSQL 自动命名为 agent_runs_status_check；新库从 audit.py
            # 开始显式使用同名约束，保证迁移可重复执行。
            await cursor.execute(
                "ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS agent_runs_status_check"
            )
            await cursor.execute(
                """
                ALTER TABLE agent_runs
                ADD CONSTRAINT agent_runs_status_check CHECK (
                    status IN (
                        'running',
                        'awaiting_approval',
                        'completed',
                        'timeout',
                        'cancelled',
                        'failed',
                        'budget_exceeded'
                    )
                )
                """
            )
            current = 3
            await cursor.execute(
                "UPDATE agent_schema_version SET version = %s, applied_at = now() WHERE schema_name = %s",
                (current, APP_SCHEMA_NAME),
            )

        if current < 4:
            await cursor.execute(
                """
                CREATE TABLE tickets (
                    tenant_id TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    requester_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    external_ticket_id TEXT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    priority TEXT NOT NULL DEFAULT 'normal',
                    category TEXT,
                    assigned_team_id TEXT,
                    assigned_user_id TEXT,
                    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    resolved_at TIMESTAMPTZ,
                    closed_at TIMESTAMPTZ,
                    PRIMARY KEY (tenant_id, ticket_id),
                    CONSTRAINT tickets_status_check CHECK (
                        status IN (
                            'new',
                            'intaking',
                            'awaiting_customer',
                            'classified',
                            'answer_proposed',
                            'awaiting_customer_confirmation',
                            'queued',
                            'assigned',
                            'in_progress',
                            'awaiting_approval',
                            'resolved',
                            'closed',
                            'cancelled'
                        )
                    ),
                    CONSTRAINT tickets_external_id_unique
                        UNIQUE (tenant_id, channel, external_ticket_id)
                )
                """
            )
            await cursor.execute(
                """
                CREATE INDEX idx_tickets_tenant_status_updated
                ON tickets (tenant_id, status, updated_at DESC)
                """
            )
            await cursor.execute(
                """
                CREATE TABLE ticket_status_events (
                    event_id BIGSERIAL PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    ticket_version INTEGER NOT NULL CHECK (ticket_version >= 0),
                    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    CONSTRAINT ticket_status_events_ticket_fk
                        FOREIGN KEY (tenant_id, ticket_id)
                        REFERENCES tickets (tenant_id, ticket_id)
                        ON DELETE CASCADE,
                    CONSTRAINT ticket_status_events_version_unique
                        UNIQUE (tenant_id, ticket_id, ticket_version)
                )
                """
            )
            await cursor.execute(
                """
                CREATE INDEX idx_ticket_status_events_tenant_ticket
                ON ticket_status_events (tenant_id, ticket_id, event_id)
                """
            )
            await cursor.execute(
                """
                CREATE TABLE inbound_events (
                    tenant_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    external_event_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    ticket_id TEXT,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    processed_at TIMESTAMPTZ,
                    PRIMARY KEY (tenant_id, channel, external_event_id),
                    CONSTRAINT inbound_events_ticket_fk
                        FOREIGN KEY (tenant_id, ticket_id)
                        REFERENCES tickets (tenant_id, ticket_id)
                )
                """
            )
            current = 4
            await cursor.execute(
                "UPDATE agent_schema_version SET version = %s, applied_at = now() WHERE schema_name = %s",
                (current, APP_SCHEMA_NAME),
            )

        if current < 5:
            await cursor.execute(
                """
                CREATE TABLE knowledge_documents (
                    tenant_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK (version >= 1),
                    title TEXT NOT NULL,
                    source_uri TEXT,
                    status TEXT NOT NULL DEFAULT 'draft',
                    allowed_departments TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
                    valid_from TIMESTAMPTZ,
                    valid_until TIMESTAMPTZ,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, document_id, version),
                    CONSTRAINT knowledge_documents_status_check
                        CHECK (status IN ('draft', 'published', 'retired')),
                    CONSTRAINT knowledge_documents_validity_check
                        CHECK (valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from)
                )
                """
            )
            await cursor.execute(
                """
                CREATE INDEX idx_knowledge_documents_active
                ON knowledge_documents (tenant_id, document_id, version DESC)
                WHERE status = 'published'
                """
            )
            await cursor.execute(
                """
                CREATE TABLE knowledge_chunks (
                    tenant_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    document_version INTEGER NOT NULL,
                    chunk_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                    content TEXT NOT NULL,
                    embedding_ref TEXT,
                    embedding_model TEXT,
                    search_vector TSVECTOR GENERATED ALWAYS AS (
                        to_tsvector('simple', coalesce(content, ''))
                    ) STORED,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, document_id, document_version, chunk_id),
                    CONSTRAINT knowledge_chunks_document_fk
                        FOREIGN KEY (tenant_id, document_id, document_version)
                        REFERENCES knowledge_documents (tenant_id, document_id, version)
                        ON DELETE CASCADE
                )
                """
            )
            await cursor.execute(
                """
                CREATE INDEX idx_knowledge_chunks_search
                ON knowledge_chunks USING GIN (search_vector)
                """
            )
            current = 5
            await cursor.execute(
                "UPDATE agent_schema_version SET version = %s, applied_at = now() WHERE schema_name = %s",
                (current, APP_SCHEMA_NAME),
            )

        if current < 6:
            await cursor.execute(
                """
                CREATE TABLE ticket_messages (
                    tenant_id TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    actor_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    external_message_id TEXT,
                    content TEXT NOT NULL,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, ticket_id, message_id),
                    CONSTRAINT ticket_messages_ticket_fk
                        FOREIGN KEY (tenant_id, ticket_id)
                        REFERENCES tickets (tenant_id, ticket_id)
                        ON DELETE CASCADE,
                    CONSTRAINT ticket_messages_direction_check
                        CHECK (direction IN ('inbound', 'outbound')),
                    CONSTRAINT ticket_messages_external_unique
                        UNIQUE (tenant_id, channel, external_message_id)
                )
                """
            )
            await cursor.execute(
                """
                CREATE INDEX idx_ticket_messages_tenant_ticket_created
                ON ticket_messages (tenant_id, ticket_id, created_at, message_id)
                """
            )
            await cursor.execute(
                """
                CREATE TABLE outbox_events (
                    tenant_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    aggregate_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    claimed_at TIMESTAMPTZ,
                    delivered_at TIMESTAMPTZ,
                    last_error_code TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, event_id),
                    CONSTRAINT outbox_events_idempotency_unique
                        UNIQUE (tenant_id, idempotency_key),
                    CONSTRAINT outbox_events_status_check
                        CHECK (status IN ('pending', 'processing', 'delivered', 'dead'))
                )
                """
            )
            await cursor.execute(
                """
                CREATE INDEX idx_outbox_events_ready
                ON outbox_events (available_at, created_at)
                WHERE status = 'pending'
                """
            )
            await cursor.execute(
                """
                CREATE TABLE sla_policies (
                    tenant_id TEXT NOT NULL,
                    policy_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    business_days SMALLINT[] NOT NULL,
                    work_start TIME NOT NULL,
                    work_end TIME NOT NULL,
                    holidays DATE[] NOT NULL DEFAULT ARRAY[]::DATE[],
                    first_response_minutes INTEGER NOT NULL CHECK (first_response_minutes > 0),
                    resolution_minutes INTEGER NOT NULL CHECK (resolution_minutes > 0),
                    pause_on_customer_wait BOOLEAN NOT NULL DEFAULT TRUE,
                    reset_on_reassignment BOOLEAN NOT NULL DEFAULT FALSE,
                    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, policy_id),
                    CONSTRAINT sla_policies_hours_check CHECK (work_end > work_start)
                )
                """
            )
            await cursor.execute(
                """
                CREATE TABLE ticket_sla (
                    tenant_id TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    policy_id TEXT NOT NULL,
                    policy_version INTEGER NOT NULL,
                    first_response_due_at TIMESTAMPTZ NOT NULL,
                    resolution_due_at TIMESTAMPTZ NOT NULL,
                    paused_at TIMESTAMPTZ,
                    pause_reason TEXT,
                    total_paused_seconds BIGINT NOT NULL DEFAULT 0 CHECK (total_paused_seconds >= 0),
                    first_responded_at TIMESTAMPTZ,
                    first_response_breached_at TIMESTAMPTZ,
                    resolution_breached_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, ticket_id),
                    CONSTRAINT ticket_sla_ticket_fk
                        FOREIGN KEY (tenant_id, ticket_id)
                        REFERENCES tickets (tenant_id, ticket_id)
                        ON DELETE CASCADE,
                    CONSTRAINT ticket_sla_policy_fk
                        FOREIGN KEY (tenant_id, policy_id)
                        REFERENCES sla_policies (tenant_id, policy_id)
                )
                """
            )
            await cursor.execute(
                """
                CREATE INDEX idx_ticket_sla_resolution_due
                ON ticket_sla (resolution_due_at)
                WHERE resolution_breached_at IS NULL AND paused_at IS NULL
                """
            )
            await cursor.execute(
                """
                CREATE TABLE satisfaction_surveys (
                    tenant_id TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    survey_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    score SMALLINT CHECK (score BETWEEN 1 AND 5),
                    feedback TEXT,
                    sent_at TIMESTAMPTZ,
                    responded_at TIMESTAMPTZ,
                    expires_at TIMESTAMPTZ NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, survey_id),
                    CONSTRAINT satisfaction_surveys_ticket_fk
                        FOREIGN KEY (tenant_id, ticket_id)
                        REFERENCES tickets (tenant_id, ticket_id)
                        ON DELETE CASCADE,
                    CONSTRAINT satisfaction_surveys_ticket_unique
                        UNIQUE (tenant_id, ticket_id),
                    CONSTRAINT satisfaction_surveys_status_check
                        CHECK (status IN ('pending', 'sent', 'responded', 'expired'))
                )
                """
            )
            current = 6
            await cursor.execute(
                "UPDATE agent_schema_version SET version = %s, applied_at = now() WHERE schema_name = %s",
                (current, APP_SCHEMA_NAME),
            )

        if current < 7:
            await cursor.execute(
                """
                CREATE TABLE ticket_workflow_runs (
                    tenant_id TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    command_type TEXT NOT NULL,
                    expected_version INTEGER NOT NULL CHECK (expected_version >= 0),
                    checkpoint_thread_id TEXT NOT NULL,
                    checkpoint_id TEXT,
                    status TEXT NOT NULL DEFAULT 'started',
                    intent JSONB,
                    result_hash TEXT,
                    error_code TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    committed_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, ticket_id, operation_id),
                    CONSTRAINT ticket_workflow_runs_ticket_fk
                        FOREIGN KEY (tenant_id, ticket_id)
                        REFERENCES tickets (tenant_id, ticket_id)
                        ON DELETE CASCADE,
                    CONSTRAINT ticket_workflow_runs_status_check
                        CHECK (status IN ('started', 'intent_recorded', 'committed', 'failed'))
                )
                """
            )
            await cursor.execute(
                """
                CREATE INDEX idx_ticket_workflow_runs_recovery
                ON ticket_workflow_runs (created_at)
                WHERE status IN ('started', 'intent_recorded')
                """
            )
            current = 7
            await cursor.execute(
                "UPDATE agent_schema_version SET version = %s, applied_at = now() WHERE schema_name = %s",
                (current, APP_SCHEMA_NAME),
            )

        if current < 8:
            await cursor.execute(
                "ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS worker_id TEXT"
            )
            await cursor.execute(
                "ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ"
            )
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_outbox_events_reclaimable
                ON outbox_events (lease_expires_at)
                WHERE status = 'processing'
                """
            )
            current = 8
            await cursor.execute(
                "UPDATE agent_schema_version SET version = %s, applied_at = now() WHERE schema_name = %s",
                (current, APP_SCHEMA_NAME),
            )

        if current < 9:
            await cursor.execute("""
                CREATE TABLE support_teams (
                    tenant_id TEXT NOT NULL, team_id TEXT NOT NULL, name TEXT NOT NULL,
                    department_id TEXT, active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, team_id)
                )
            """)
            await cursor.execute("""
                CREATE TABLE support_members (
                    tenant_id TEXT NOT NULL, member_id TEXT NOT NULL, team_id TEXT NOT NULL,
                    skills TEXT[] NOT NULL DEFAULT '{}', capacity INTEGER NOT NULL DEFAULT 10 CHECK (capacity >= 1),
                    active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, member_id),
                    FOREIGN KEY (tenant_id, team_id) REFERENCES support_teams (tenant_id, team_id)
                )
            """)
            await cursor.execute("""
                CREATE TABLE support_schedules (
                    tenant_id TEXT NOT NULL, schedule_id TEXT NOT NULL, member_id TEXT NOT NULL,
                    starts_at TIMESTAMPTZ NOT NULL, ends_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (tenant_id, schedule_id),
                    FOREIGN KEY (tenant_id, member_id) REFERENCES support_members (tenant_id, member_id),
                    CHECK (ends_at > starts_at)
                )
            """)
            await cursor.execute("""
                CREATE TABLE routing_rules (
                    tenant_id TEXT NOT NULL, rule_id TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 100,
                    category TEXT, subcategory TEXT, channel TEXT, department_id TEXT,
                    required_skill TEXT, target_team_id TEXT NOT NULL, active BOOLEAN NOT NULL DEFAULT TRUE,
                    PRIMARY KEY (tenant_id, rule_id),
                    FOREIGN KEY (tenant_id, target_team_id) REFERENCES support_teams (tenant_id, team_id)
                )
            """)
            await cursor.execute("""
                CREATE TABLE ticket_assignments (
                    tenant_id TEXT NOT NULL, assignment_id BIGINT GENERATED ALWAYS AS IDENTITY,
                    ticket_id TEXT NOT NULL, team_id TEXT NOT NULL, member_id TEXT,
                    reason_codes TEXT[] NOT NULL DEFAULT '{}', assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    ended_at TIMESTAMPTZ, PRIMARY KEY (tenant_id, assignment_id),
                    FOREIGN KEY (tenant_id, ticket_id) REFERENCES tickets (tenant_id, ticket_id),
                    FOREIGN KEY (tenant_id, team_id) REFERENCES support_teams (tenant_id, team_id),
                    FOREIGN KEY (tenant_id, member_id) REFERENCES support_members (tenant_id, member_id)
                )
            """)
            current = 9
            await cursor.execute(
                "UPDATE agent_schema_version SET version = %s, applied_at = now() WHERE schema_name = %s",
                (current, APP_SCHEMA_NAME),
            )

        if current < 10:
            # v10: IT 服务台业务配置 —— 租户级分类策略（必填字段、默认优先级、
            # 自动回答与人工审批开关）。category 支持点号子分类（it.vpn / it.account
            # / it.network），required_fields 用数组而非 JSON，便于按列过滤和校验。
            # 时间 SLA（TTO/TTR）不再内联：policy_id 引用 sla_policies，由该表提供
            # first_response_minutes / resolution_minutes，避免两套 SLA 配置漂移。
            await cursor.execute(
                """
                CREATE TABLE tenant_it_policies (
                    tenant_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    policy_id TEXT,
                    required_fields TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
                    default_priority TEXT NOT NULL DEFAULT 'normal',
                    auto_answer_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                    approval_required BOOLEAN NOT NULL DEFAULT FALSE,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, category),
                    CONSTRAINT tenant_it_policies_priority_check
                        CHECK (default_priority IN ('low', 'normal', 'high', 'urgent')),
                    CONSTRAINT tenant_it_policies_sla_fk
                        FOREIGN KEY (tenant_id, policy_id)
                        REFERENCES sla_policies (tenant_id, policy_id)
                )
                """
            )
            # 对早期 v10 草稿库做幂等修正：补 policy_id 引用、移除内联 TTO/TTR。
            await cursor.execute(
                "ALTER TABLE tenant_it_policies ADD COLUMN IF NOT EXISTS policy_id TEXT"
            )
            await cursor.execute(
                "ALTER TABLE tenant_it_policies DROP COLUMN IF EXISTS tto_minutes"
            )
            await cursor.execute(
                "ALTER TABLE tenant_it_policies DROP COLUMN IF EXISTS ttr_minutes"
            )
            await cursor.execute(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'tenant_it_policies_sla_fk'
                    ) THEN
                        ALTER TABLE tenant_it_policies
                        ADD CONSTRAINT tenant_it_policies_sla_fk
                        FOREIGN KEY (tenant_id, policy_id)
                        REFERENCES sla_policies (tenant_id, policy_id);
                    END IF;
                END $$;
                """
            )
            await cursor.execute(
                """
                CREATE INDEX idx_tenant_it_policies_active
                ON tenant_it_policies (tenant_id, active)
                """
            )
            # v10: IT 资产 —— 资产类型用 asset_type 定义表 + 资产实例表分离，
            # 借鉴 GLPI 泛型资产模型：每类资产字段不同，用 custom_fields JSON 承载，
            # 不为一类资产单建表。is_deleted 软删，不做物理删除。
            # asset_no 用「部分唯一索引」而非 UNIQUE 约束：软删后可复用编号，
            # 避免 is_deleted 行永久占用编号导致唯一键异常。
            await cursor.execute(
                """
                CREATE TABLE it_assets (
                    tenant_id TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    asset_no TEXT NOT NULL,
                    asset_type TEXT NOT NULL,
                    name TEXT,
                    hostname TEXT,
                    ip_address TEXT,
                    department TEXT,
                    owner_user_id TEXT,
                    uuid TEXT,
                    serial TEXT,
                    status TEXT NOT NULL DEFAULT 'in_use',
                    purchased_at TIMESTAMPTZ,
                    warranty_expires_at TIMESTAMPTZ,
                    location TEXT,
                    custom_fields JSONB NOT NULL DEFAULT '{}'::jsonb,
                    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, asset_id),
                    CONSTRAINT it_assets_status_check
                        CHECK (status IN ('in_stock', 'in_use', 'repairing', 'retired'))
                )
                """
            )
            # 兼容早期 v10 草稿库：移除整表唯一约束，改用部分唯一索引。
            await cursor.execute(
                "ALTER TABLE it_assets DROP CONSTRAINT IF EXISTS it_assets_asset_no_unique"
            )
            await cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS it_assets_asset_no_active_unique
                ON it_assets (tenant_id, asset_no)
                WHERE is_deleted = FALSE
                """
            )
            await cursor.execute(
                """
                CREATE INDEX idx_it_assets_owner
                ON it_assets (tenant_id, owner_user_id)
                WHERE is_deleted = FALSE
                """
            )
            await cursor.execute(
                """
                CREATE INDEX idx_it_assets_department
                ON it_assets (tenant_id, department)
                WHERE is_deleted = FALSE
                """
            )
            # v10: 工单关联资产 —— 回答"哪台电脑常报修/某员工名下有哪些设备"。
            await cursor.execute(
                """
                ALTER TABLE tickets ADD COLUMN IF NOT EXISTS asset_id TEXT
                """
            )
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tickets_asset
                ON tickets (tenant_id, asset_id)
                WHERE asset_id IS NOT NULL
                """
            )
            # 工单资产必须引用真实存在的租户资产：复合外键 (tenant_id, asset_id)
            # 同时约束租户隔离，NULL 资产不触发检查。
            await cursor.execute(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'tickets_asset_fk'
                    ) THEN
                        ALTER TABLE tickets
                        ADD CONSTRAINT tickets_asset_fk
                        FOREIGN KEY (tenant_id, asset_id)
                        REFERENCES it_assets (tenant_id, asset_id);
                    END IF;
                END $$;
                """
            )
            # v10: 知识库增强 —— 分类、可见性、创建者。
            # visibility 是粗粒度过滤（public/internal/restricted），与已有的
            # allowed_departments 部门 ACL 并存：visibility 先粗筛，ACL 再细控。
            await cursor.execute(
                """
                ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS category TEXT
                """
            )
            await cursor.execute(
                """
                ALTER TABLE knowledge_documents
                ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'internal'
                """
            )
            await cursor.execute(
                """
                ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS created_by TEXT
                """
            )
            await cursor.execute(
                """
                ALTER TABLE knowledge_documents
                DROP CONSTRAINT IF EXISTS knowledge_documents_visibility_check
                """
            )
            await cursor.execute(
                """
                ALTER TABLE knowledge_documents
                ADD CONSTRAINT knowledge_documents_visibility_check
                CHECK (visibility IN ('public', 'internal', 'restricted'))
                """
            )
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_knowledge_documents_category
                ON knowledge_documents (tenant_id, category)
                WHERE category IS NOT NULL
                """
            )
            current = 10
            await cursor.execute(
                "UPDATE agent_schema_version SET version = %s, applied_at = now() WHERE schema_name = %s",
                (current, APP_SCHEMA_NAME),
            )

        if current < 11:
            # v11: 管理操作审计 —— 资产、IT 策略、知识文档的写操作记录在这里。
            # 与 agent_events 分离：agent_events 绑定 agent_runs（run 级审计），
            # 管理操作没有 run_id，放进独立表避免伪造 run 或污染 run 语义。
            await cursor.execute(
                """
                CREATE TABLE admin_audit_events (
                    id BIGSERIAL PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'success',
                    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            await cursor.execute(
                """
                CREATE INDEX idx_admin_audit_events_tenant
                ON admin_audit_events (tenant_id, created_at)
                """
            )
            current = 11
            await cursor.execute(
                "UPDATE agent_schema_version SET version = %s, applied_at = now() WHERE schema_name = %s",
                (current, APP_SCHEMA_NAME),
            )

        if current < 12:
            # v12: 中文分词全文检索 —— knowledge_chunks 增加 search_text（jieba 分词后
            # 的文本）与 embedding 状态列；search_vector 从「content 生成列」改为
            # 基于 search_text 的普通列，由入库/分词路径写入；pg_trgm 提供错别字与
            # 短词兜底。权限/租户过滤仍由 repository 查询强制，不在此放宽。
            await cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            await cursor.execute(
                "ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS search_text TEXT NOT NULL DEFAULT ''"
            )
            await cursor.execute(
                "ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS embedding_status TEXT NOT NULL DEFAULT 'pending'"
            )
            await cursor.execute(
                "ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS embedding_error TEXT"
            )
            await cursor.execute(
                "ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS embedding_updated_at TIMESTAMPTZ"
            )
            # search_vector 改为普通列：DROP 生成列后重建，避免触发器的 content 分词
            # 与 search_text 分词不一致。v5 曾定义为 GENERATED STORED 列。
            await cursor.execute("ALTER TABLE knowledge_chunks DROP COLUMN IF EXISTS search_vector")
            await cursor.execute("ALTER TABLE knowledge_chunks ADD COLUMN search_vector TSVECTOR")
            await cursor.execute("DROP INDEX IF EXISTS idx_knowledge_chunks_search")
            await cursor.execute(
                """
                CREATE INDEX idx_knowledge_chunks_search
                ON knowledge_chunks USING GIN (search_vector)
                """
            )
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_search_text_trgm
                ON knowledge_chunks USING GIN (search_text gin_trgm_ops)
                """
            )
            # 存量回填：旧行按原始 content 生成（行为不退化的兜底），新写入路径会
            # 用 jieba 分词覆盖 search_text / search_vector。
            await cursor.execute(
                """
                UPDATE knowledge_chunks
                SET search_text = content,
                    search_vector = to_tsvector('simple', content)
                WHERE search_text = ''
                """
            )
            current = 12
            await cursor.execute(
                "UPDATE agent_schema_version SET version = %s, applied_at = now() WHERE schema_name = %s",
                (current, APP_SCHEMA_NAME),
            )

        if current < 13:
            # v13: 渠道入站异步化 —— inbound_events 增加处理状态机与租约/重试列。
            # POST 只登记事件（received）立即返回，InboundWorker 领取后建单受理；
            # 幂等由 (tenant_id, channel, external_event_id) 主键与 ticket_id 关联保证。
            await cursor.execute(
                "ALTER TABLE inbound_events ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'received'"
            )
            await cursor.execute(
                "ALTER TABLE inbound_events ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb"
            )
            await cursor.execute(
                "ALTER TABLE inbound_events ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0"
            )
            await cursor.execute(
                "ALTER TABLE inbound_events ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now()"
            )
            await cursor.execute(
                "ALTER TABLE inbound_events ADD COLUMN IF NOT EXISTS error_code TEXT"
            )
            await cursor.execute(
                "ALTER TABLE inbound_events ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ"
            )
            await cursor.execute(
                "ALTER TABLE inbound_events ADD COLUMN IF NOT EXISTS worker_id TEXT"
            )
            await cursor.execute(
                "ALTER TABLE inbound_events ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ"
            )
            await cursor.execute(
                """
                ALTER TABLE inbound_events
                DROP CONSTRAINT IF EXISTS inbound_events_status_check
                """
            )
            await cursor.execute(
                """
                ALTER TABLE inbound_events
                ADD CONSTRAINT inbound_events_status_check
                CHECK (status IN ('received', 'processing', 'committed', 'failed', 'dead'))
                """
            )
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_inbound_events_claim
                ON inbound_events (status, next_attempt_at)
                WHERE status IN ('received', 'failed')
                """
            )
            current = 13
            await cursor.execute(
                "UPDATE agent_schema_version SET version = %s, applied_at = now() WHERE schema_name = %s",
                (current, APP_SCHEMA_NAME),
            )

        if current < 14:
            # v14: 企微追问 Resume 闭环 —— 客户待补全关联表。
            # 渠道工单进入 awaiting_customer 时登记，客户回复时按
            # (tenant, channel, external_user_id) 匹配唯一有效记录恢复原工单，
            # 绝不新建工单。部分唯一索引保证同一客户同时只有一个 awaiting 追问。
            await cursor.execute(
                """
                CREATE TABLE ticket_customer_pending_intake (
                    tenant_id TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    external_user_id TEXT NOT NULL,
                    required_fields TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
                    status TEXT NOT NULL DEFAULT 'awaiting',
                    resume_count INTEGER NOT NULL DEFAULT 0,
                    expires_at TIMESTAMPTZ NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, ticket_id),
                    CONSTRAINT pending_intake_ticket_fk
                        FOREIGN KEY (tenant_id, ticket_id)
                        REFERENCES tickets (tenant_id, ticket_id)
                        ON DELETE CASCADE,
                    CONSTRAINT pending_intake_status_check
                        CHECK (status IN ('awaiting', 'resumed', 'expired', 'cancelled'))
                )
                """
            )
            await cursor.execute(
                """
                CREATE UNIQUE INDEX uq_pending_intake_active
                ON ticket_customer_pending_intake (tenant_id, channel, external_user_id)
                WHERE status = 'awaiting'
                """
            )
            await cursor.execute(
                """
                CREATE INDEX idx_pending_intake_expiry
                ON ticket_customer_pending_intake (expires_at)
                WHERE status = 'awaiting'
                """
            )
            current = 14
            await cursor.execute(
                "UPDATE agent_schema_version SET version = %s, applied_at = now() WHERE schema_name = %s",
                (current, APP_SCHEMA_NAME),
            )


async def check_required_relations(
    connection: AsyncConnection,
    relations: Iterable[str] = REQUIRED_RELATIONS,
) -> list[str]:
    """Return missing public relations without exposing connection details."""

    names = tuple(relations)
    if not names:
        return []
    async with connection.cursor() as cursor:
        await cursor.execute(
            """
            SELECT relname
            FROM pg_catalog.pg_class
            JOIN pg_catalog.pg_namespace ON pg_namespace.oid = pg_class.relnamespace
            WHERE pg_namespace.nspname = 'public' AND relname = ANY(%s)
            """,
            (list(names),),
        )
        present = {str(row[0]) for row in await cursor.fetchall()}
    return [name for name in names if name not in present]


async def check_schema_ready(connection: AsyncConnection) -> None:
    """Raise a safe, actionable error when schema is absent or out of date."""

    missing = await check_required_relations(connection)
    if missing:
        raise RuntimeError("PostgreSQL schema 未完成迁移")
    async with connection.cursor() as cursor:
        await cursor.execute(
            "SELECT version FROM agent_schema_version WHERE schema_name = %s",
            (APP_SCHEMA_NAME,),
        )
        row = await cursor.fetchone()
    if row is None or int(row[0]) != APP_SCHEMA_VERSION:
        raise RuntimeError("应用 schema 版本不匹配，请先运行迁移命令")
