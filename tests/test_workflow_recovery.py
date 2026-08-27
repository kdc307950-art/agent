import asyncio

from backend.workflow_recovery import WorkflowRecoveryWorker


class Repository:
    def __init__(self):
        self.replayed = []
        self.failed = []

    async def list_recoverable_workflow_operations(self, *, older_than, limit=100):
        return [
            {
                "tenant_id": "tenant-a",
                "ticket_id": "ticket-1",
                "operation_id": "op-intent",
                "intent": {
                    "commands": [
                        {
                            "ticket_id": "ticket-1",
                            "action": "start_intake",
                            "actor_type": "system",
                            "actor_id": "worker",
                            "expected_version": 0,
                            "payload": {},
                        }
                    ]
                },
            },
            {
                "tenant_id": "tenant-a",
                "ticket_id": "ticket-2",
                "operation_id": "op-started",
                "intent": None,
            },
            {
                "tenant_id": "tenant-a",
                "ticket_id": "ticket-3",
                "operation_id": "op-broken",
                "intent": {"commands": [{"bad": True}]},
            },
        ]

    async def transition_many(self, tenant_id, commands, *, scopes, operation_id):
        if operation_id == "op-broken":
            raise ValueError("broken command")
        self.replayed.append((tenant_id, commands, scopes, operation_id))

    async def mark_workflow_operation_failed(self, *, tenant_id, ticket_id, operation_id, error_code):
        self.failed.append((tenant_id, ticket_id, operation_id, error_code))


def test_recovery_replays_intent_isolates_bad_records_and_alerts_started_run():
    repository = Repository()
    replayed, alerts, failed = asyncio.run(
        WorkflowRecoveryWorker(repository, grace_seconds=1).run_once()
    )

    assert (replayed, alerts, failed) == (1, 1, 1)
    assert repository.replayed[0][3] == "op-intent"
    assert repository.replayed[0][1][0].action.value == "start_intake"
    assert repository.failed == [
        ("tenant-a", "ticket-2", "op-started", "missing_intent"),
        ("tenant-a", "ticket-3", "op-broken", "ValidationError"),
    ]
