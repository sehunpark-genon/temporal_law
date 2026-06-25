"""
파이프라인 조작 CLI. (워커가 떠 있는 상태에서 실행)

  python -m pipeline.starter discover         # 전체 목록 조회 → law_catalog 적재 (법률만; 'all' 이면 전체)
  python -m pipeline.starter discover all
  python -m pipeline.starter backfill         # 카탈로그의 미처리/실패 법령 수집 (재처리 안전)
  python -m pipeline.starter backfill 5        # 테스트: 5건만
  python -m pipeline.starter sync-now          # 변경 감지 1회 즉시
  python -m pipeline.starter schedule          # 매일 자동 sync 스케줄 생성/갱신
  python -m pipeline.starter unschedule        # 스케줄 삭제

일반 순서: discover → backfill → schedule
"""

import sys
import asyncio

from temporalio.client import (
    Client, Schedule, ScheduleActionStartWorkflow,
    ScheduleSpec, SchedulePolicy, ScheduleOverlapPolicy,
)
from temporalio.common import WorkflowIDConflictPolicy

# 같은 id 가 이미 실행 중이면 그것을 종료하고 새로 시작 (멈춘 실행에 막히지 않게).
# backfill 은 멱등(미완만 처리)이라 재시작해도 안전.
_REPLACE = WorkflowIDConflictPolicy.TERMINATE_EXISTING

from pipeline.config import (
    TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE, TASK_QUEUE, SYNC_CRON,
)
from pipeline.workflows import DiscoverCatalogWorkflow, BackfillWorkflow, SyncWorkflow

SCHEDULE_ID = "law-sync-daily"


async def _client():
    return await Client.connect(TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE)


async def discover(law_only: bool):
    client = await _client()
    handle = await client.start_workflow(
        DiscoverCatalogWorkflow.run, law_only,
        id="law-discover", task_queue=TASK_QUEUE)
    print(f"[discover] 목록 조회 중 (law_only={law_only})…")
    print("  결과:", await handle.result())          # {total, new}


async def backfill(limit: int | None):
    client = await _client()
    # 카탈로그의 미처리/실패(active)만 수집. 반복 호출해도 안 끝난 것만 이어받음.
    handle = await client.start_workflow(
        BackfillWorkflow.run, limit,
        id="law-backfill", task_queue=TASK_QUEUE,
        id_conflict_policy=_REPLACE)
    print(f"[backfill] 시작 (limit={limit}). 결과 대기…")
    print("  결과:", await handle.result())          # {targets, done, failed}


async def sync_now():
    client = await _client()
    handle = await client.start_workflow(
        SyncWorkflow.run, id="law-sync-now", task_queue=TASK_QUEUE,
        id_conflict_policy=_REPLACE)
    print("[sync-now] 시작. 결과 대기…")
    print("  결과:", await handle.result())


async def schedule():
    client = await _client()
    sched = Schedule(
        action=ScheduleActionStartWorkflow(
            SyncWorkflow.run, id="law-sync-scheduled", task_queue=TASK_QUEUE),
        spec=ScheduleSpec(cron_expressions=[SYNC_CRON]),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),  # 겹치면 건너뜀
    )
    try:
        await client.create_schedule(SCHEDULE_ID, sched)
        print(f"[schedule] 생성됨 id={SCHEDULE_ID} cron='{SYNC_CRON}'")
    except Exception:
        await client.get_schedule_handle(SCHEDULE_ID).delete()   # 이미 있으면 재생성
        await client.create_schedule(SCHEDULE_ID, sched)
        print(f"[schedule] 재생성됨 id={SCHEDULE_ID} cron='{SYNC_CRON}'")


async def unschedule():
    client = await _client()
    await client.get_schedule_handle(SCHEDULE_ID).delete()
    print(f"[schedule] 삭제됨 id={SCHEDULE_ID}")


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else ""

    if cmd == "discover":
        law_only = not (len(args) > 1 and args[1] == "all")   # 'all' 이면 전체
        asyncio.run(discover(law_only))
    elif cmd == "backfill":
        limit = int(args[1]) if len(args) > 1 else None
        asyncio.run(backfill(limit))
    elif cmd == "sync-now":
        asyncio.run(sync_now())
    elif cmd == "schedule":
        asyncio.run(schedule())
    elif cmd == "unschedule":
        asyncio.run(unschedule())
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
