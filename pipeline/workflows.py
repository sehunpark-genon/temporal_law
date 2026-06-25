"""
Temporal Workflows — '결정적 오케스트레이션'만. 실제 I/O 는 전부 activities 호출.

  DiscoverCatalogWorkflow(law_only)    전체 목록 조회 → catalog 적재(신규/폐지/지문 갱신)
  CollectLawWorkflow(name, id, sig)    공용 단위: 한 법령 수집→저장 (멱등)
   ├─ BackfillWorkflow(limit)          초기/재처리 적재: 아직 done 아닌 것만 배치 수집
   └─ SyncWorkflow()                   (스케줄) discover → 지문 바뀐 것만 재수집

매일 스케줄 = SyncWorkflow:  ① refresh_catalog(전체목록 새로고침) → ② 바뀐 것만 수집.
변경 감지는 catalog 의 최신 지문 vs collect_state 의 마지막 지문 비교(추가 API 없음).
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from pipeline import activities
    from pipeline.config import CATALOG_LAW_ONLY, BACKFILL_BATCH


_SHORT = dict(start_to_close_timeout=timedelta(seconds=30),
              retry_policy=RetryPolicy(maximum_attempts=5))
_CATALOG = dict(start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=3))
_COLLECT = dict(start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=3))
_PERSIST = dict(start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=5))


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


@workflow.defn
class DiscoverCatalogWorkflow:
    """전체 현행법령 목록 조회 → law_catalog 적재 (신규 pending / 폐지 표시 / 지문 갱신)."""

    @workflow.run
    async def run(self, law_only: bool | None = None) -> dict:
        await workflow.execute_activity(activities.ensure_schema, **_SHORT)
        if law_only is None:
            law_only = CATALOG_LAW_ONLY
        return await workflow.execute_activity(
            activities.refresh_catalog, law_only, **_CATALOG)


@workflow.defn
class CollectLawWorkflow:
    """한 법령 수집→저장. 지문(signature)은 호출자가 catalog 에서 받아 넘긴다."""

    @workflow.run
    async def run(self, law_name: str, law_id: str, signature: str) -> dict:
        await workflow.execute_activity(
            activities.mark_attempt, args=[law_id, law_name], **_SHORT)
        # 수집+저장을 한 액티비티에서 (큰 payload 는 워커 안에만, Temporal 경계 안 넘김)
        result = await workflow.execute_activity(
            activities.collect_and_store,
            args=[law_name, law_id, signature], **_COLLECT)
        return {"law_name": law_name, "changed": result["changed"]}


async def _collect_batches(targets, run_id, prefix):
    """targets 를 배치로 나눠 CollectLaw 자식 워크플로 실행. 실패는 격리하고 계속."""
    done, failed = [], []

    async def _one(t):
        try:
            await workflow.execute_child_workflow(
                CollectLawWorkflow.run,
                args=[t["law_name"], t["law_id"], t.get("signature") or ""],
                id=f"{prefix}-{t['law_id']}-{run_id}")
            done.append(t)
        except Exception as e:                       # 자식 실패 → failed 기록 후 진행
            await workflow.execute_activity(
                activities.mark_failed,
                args=[t["law_id"], t["law_name"], str(e)], **_SHORT)
            failed.append(t["law_name"])

    for batch in _chunks(targets, BACKFILL_BATCH):
        await asyncio.gather(*[_one(t) for t in batch])
    return done, failed


@workflow.defn
class BackfillWorkflow:
    """카탈로그의 미처리/실패(active) 법령을 배치 수집. 반복 호출해도 안 끝난 것만 이어받음."""

    @workflow.run
    async def run(self, limit: int | None = None) -> dict:
        await workflow.execute_activity(activities.ensure_schema, **_SHORT)
        targets = await workflow.execute_activity(
            activities.list_backfill_targets, limit, **_SHORT)

        run_id = workflow.info().run_id[:8]
        done, failed = await _collect_batches(targets, run_id, "collect")
        return {"targets": len(targets), "done": len(done), "failed": failed}


@workflow.defn
class SyncWorkflow:
    """(스케줄) ① 전체목록 새로고침 → ② 지문이 바뀐(또는 미완) 법령만 재수집."""

    @workflow.run
    async def run(self) -> dict:
        await workflow.execute_activity(activities.ensure_schema, **_SHORT)

        # ① discover: 신규/폐지/지문 갱신 (전체목록 1회)
        cat = await workflow.execute_activity(
            activities.refresh_catalog, CATALOG_LAW_ONLY, **_CATALOG)

        # ② 변경/미완 대상만 (catalog 지문 vs 마지막 수집 지문 비교 — API 불필요)
        targets = await workflow.execute_activity(activities.list_sync_targets, **_SHORT)

        run_id = workflow.info().run_id[:8]
        done, failed = await _collect_batches(targets, run_id, "sync-collect")

        # 변경 이력 기록 (성공분)
        done_ids = {t["law_id"] for t in done}
        for t in targets:
            if t["law_id"] in done_ids:
                reason = "mst_changed" if t.get("status") == "done" else "initial_or_retry"
                await workflow.execute_activity(
                    activities.record_change,
                    args=[t["law_id"], t["law_name"], t.get("old_signature"),
                          t.get("signature"), reason], **_SHORT)

        return {"catalog": cat, "to_collect": len(targets),
                "done": len(done), "failed": failed}
