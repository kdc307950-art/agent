"""应用层 schema 版本管理与就绪检查。

LangGraph 自己管理 checkpoint/store 的迁移表；本模块管理应用表，
并记录 backend.migrations 实际应用的版本号。
当版本或必需表结构与当前代码不匹配时，服务拒绝就绪（readiness 失败）。
"""

from __future__ import annotations

from typing import Iterable

from psycopg import AsyncConnection


APP_SCHEMA_NAME = "langgraph_agent"
APP_SCHEMA_VERSION = 6
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
