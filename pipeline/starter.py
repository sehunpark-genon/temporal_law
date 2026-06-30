"""
파이프라인 조작 CLI (= 워크플로 트리거/스케줄 등록). 워커가 떠 있는 상태에서 실행.

  python -m pipeline.starter discover          # 전체 현행법령 목록 → law_catalog 적재
  python -m pipeline.starter backfill [N]       # 초기/이어받기 적재 (N=최대 건수, 생략=전체)
  python -m pipeline.starter sync-now           # 변경 감지 1회 즉시 실행
  python -m pipeline.starter schedule           # 매일 자동 sync 스케줄 생성/갱신
  python -m pipeline.starter unschedule         # 스케줄 삭제

워커(실행자)와 분리된 '시작 버튼'. 한 번 실행하고 결과를 받아 출력한 뒤 종료한다.
"""

import sys
import asyncio

from temporalio.client import (
    Client, Schedule, ScheduleActionStartWorkflow,
    ScheduleSpec, SchedulePolicy, ScheduleOverlapPolicy,
)
from temporalio.common import WorkflowIDConflictPolicy

from pipeline.config import (
    TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE, TASK_QUEUE, CATALOG_LAW_ONLY, SYNC_CRON,
)
from pipeline.workflows import (
    DiscoverCatalogWorkflow, BackfillWorkflow, SyncWorkflow,
)

SCHEDULE_ID = "law-sync-daily"

# 같은 id 로 다시 돌리면 기존 실행을 종료하고 새로 시작 (중복 실행 에러 방지)
_REPLACE = WorkflowIDConflictPolicy.TERMINATE_EXISTING


async def _client() -> Client:
    return await Client.connect(TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE)


async def discover():
    """전체 목록 조회 → law_catalog 적재(신규 pending / 폐지 표시 / 지문 갱신).
    인자 'all' 이면 시행령·시행규칙까지 전체, 생략하면 CATALOG_LAW_ONLY(기본=법률만)."""
    law_only = False if (len(sys.argv) > 2 and sys.argv[2] == "all") else CATALOG_LAW_ONLY
    client = await _client()
    handle = await client.start_workflow(
        DiscoverCatalogWorkflow.run, law_only,
        id="law-discover", task_queue=TASK_QUEUE, id_conflict_policy=_REPLACE)
    print(f"[discover] 시작 (workflow_id={handle.id}). 결과 대기…")
    print("  결과:", await handle.result())


async def backfill():
    """초기/이어받기 적재. 인자 N 주면 최대 N건만(테스트용), 생략하면 전체."""
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
    client = await _client()
    handle = await client.start_workflow(
        BackfillWorkflow.run, limit,
        id="law-backfill", task_queue=TASK_QUEUE, id_conflict_policy=_REPLACE)
    print(f"[backfill] 시작 (workflow_id={handle.id}, limit={limit}). 결과 대기…")
    print("  결과:", await handle.result())


async def sync_now():
    """변경 감지 1회: discover → 지문 바뀐(또는 미완) 법령만 재수집."""
    client = await _client()
    handle = await client.start_workflow(
        SyncWorkflow.run,
        id="law-sync-now", task_queue=TASK_QUEUE, id_conflict_policy=_REPLACE)
    print(f"[sync-now] 시작 (workflow_id={handle.id}). 결과 대기…")
    print("  결과:", await handle.result())


async def schedule():
    """매일 자동 sync 스케줄 생성(있으면 재생성)."""
    client = await _client()
    spec = ScheduleSpec(cron_expressions=[SYNC_CRON])
    action = ScheduleActionStartWorkflow(
        SyncWorkflow.run, args=[],
        id="law-sync-scheduled", task_queue=TASK_QUEUE)
    sched = Schedule(
        action=action, spec=spec,
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP))
    try:
        await client.create_schedule(SCHEDULE_ID, sched)
        print(f"[schedule] 생성됨 id={SCHEDULE_ID} cron='{SYNC_CRON}'")
    except Exception:
        await client.get_schedule_handle(SCHEDULE_ID).delete()
        await client.create_schedule(SCHEDULE_ID, sched)
        print(f"[schedule] 재생성됨 id={SCHEDULE_ID} cron='{SYNC_CRON}'")


async def unschedule():
    client = await _client()
    await client.get_schedule_handle(SCHEDULE_ID).delete()
    print(f"[schedule] 삭제됨 id={SCHEDULE_ID}")


COMMANDS = {
    "discover": discover, "backfill": backfill, "sync-now": sync_now,
    "schedule": schedule, "unschedule": unschedule,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    asyncio.run(COMMANDS[sys.argv[1]]())


if __name__ == "__main__":
    main()
