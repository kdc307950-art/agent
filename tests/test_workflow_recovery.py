import asyncio

from backend.workflow_recovery import WorkflowRecoveryWorker


class Repository:
    def __init__(self):
        self.replayed = []

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
        ]

    async def transition_many(self, tenant_id, commands, *, scopes, operation_id):
        self.replayed.append((tenant_id, commands, scopes, operation_id))


def test_recovery_replays_recorded_intent_and_alerts_for_started_run():
    repository = Repository()
    replayed, alerts = asyncio.run(
        WorkflowRecoveryWorker(repository, grace_seconds=1).run_once()
    )

    assert (replayed, alerts) == (1, 1)
    assert repository.replayed[0][3] == "op-intent"
    assert repository.replayed[0][1][0].action.value == "start_intake"
