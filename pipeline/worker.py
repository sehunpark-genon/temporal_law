"""
Temporal 워커 — 워크플로/액티비티를 등록하고 task queue 를 폴링한다.
호스트에서 실행: `python -m pipeline.worker`  (정관 렌더용 Chrome 필요)
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from pipeline import activities
from pipeline.config import TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE, TASK_QUEUE
from pipeline.workflows import (
    DiscoverCatalogWorkflow, BackfillWorkflow, CollectLawWorkflow, SyncWorkflow,
)


async def main():
    logging.basicConfig(level=logging.INFO)
    client = await Client.connect(TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE)

    # 동기 activity(requests/psycopg/Chrome) 는 스레드풀에서 실행
    with ThreadPoolExecutor(max_workers=8) as executor:
        worker = Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[DiscoverCatalogWorkflow, BackfillWorkflow,
                       CollectLawWorkflow, SyncWorkflow],
            activities=[
                activities.ensure_schema,
                activities.refresh_catalog,
                activities.list_backfill_targets,
                activities.list_sync_targets,
                activities.mark_attempt,
                activities.collect_and_store,
                activities.mark_failed,
                activities.record_change,
            ],
            activity_executor=executor,
        )
        print(f"[worker] task_queue={TASK_QUEUE} @ {TEMPORAL_ADDRESS} 대기 중…")
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
