import asyncio

from backend.sla_worker import SLAWorker


class Repository:
    def __init__(self):
        self.calls = 0

    async def scan_sla_breaches(self, *, limit):
        self.calls += 1
        return 2 if self.calls == 1 else 0


def test_sla_worker_runs_scan_and_stops():
    repository = Repository()
    stop = asyncio.Event()

    async def run():
        worker = SLAWorker(repository, interval_seconds=0.01)
        await worker.run_once()
        stop.set()
        await worker.run_forever(stop_event=stop)

    asyncio.run(run())
    assert repository.calls == 1
