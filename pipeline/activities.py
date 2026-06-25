"""
Temporal Activities — '부수효과(I/O)'가 있는 작업은 전부 여기.
(워크플로는 결정적이어야 하므로 네트워크/DB/Chrome 호출은 모두 activity 로 격리.)

모두 동기 함수다. 워커가 ThreadPoolExecutor 로 실행한다(requests·psycopg·Chrome 가 블로킹).
"""

from temporalio import activity

from pipeline import collect, db


@activity.defn
def ensure_schema() -> None:
    db.init_schema()                             # 테이블 없으면 생성


# ── 목록(catalog) ────────────────────────────────────────────────

@activity.defn
def refresh_catalog(law_only: bool) -> dict:
    """전체 목록 조회(지문 계산 포함) → catalog 적재 + 신규 pending 등록 + 폐지 표시."""
    rows = collect.discover_catalog(law_only=law_only)
    result = db.upsert_catalog(rows)             # {total, new, repealed}
    activity.logger.info(f"catalog refresh: {result}")
    return result


@activity.defn
def list_backfill_targets(limit: int | None) -> list[dict]:
    """초기/재처리 대상: active 법령 중 아직 done 아닌 것 [{law_id, law_name, signature}]."""
    return db.list_backfill_targets(limit=limit)


@activity.defn
def list_sync_targets() -> list[dict]:
    """변경/미완 대상: 지문이 바뀌었거나 아직 done 아닌 것 (catalog 비교, API 불필요)."""
    return db.list_sync_targets()


# ── 수집 단계 ────────────────────────────────────────────────────

@activity.defn
def mark_attempt(law_id: str, law_name: str) -> None:
    db.mark_attempt(law_id, law_name)            # 수집 시작 → 시도 횟수 +1


@activity.defn
def collect_and_store(law_name: str, law_id: str, version_signature: str) -> dict:
    """
    수집 + 저장을 한 액티비티에서 처리.
    ★ 큰 payload(수MB)를 워크플로로 되돌리지 않는다 — Temporal payload 크기 한도(2MB)를
      넘기지 않도록 payload 는 워커 안에서만 쓰고 DB 에 바로 저장, 작은 요약만 반환.
    내용 해시가 이전과 같으면 DB 쓰기는 스킵.
    """
    activity.logger.info(f"collect 시작: {law_name}")
    payload = collect.collect_payload(law_name)              # 본문+위임+인용+정관 (메모리)
    payload["law_id"] = law_id or payload.get("law_id")       # catalog law_id 로 통일

    new_hash = collect.content_hash(payload)
    prev = db.read_collect_state(payload["law_id"])
    changed = prev is None or prev.get("content_hash") != new_hash

    if changed:
        db.upsert_law(payload, version_signature, new_hash)   # law + law_relation 저장
    db.mark_done(payload["law_id"], version_signature, new_hash, changed)

    activity.logger.info(
        f"collect 완료: {law_name} relations={len(payload.get('relations', []))} changed={changed}")
    return {"law_id": payload["law_id"], "law_name": law_name,
            "changed": changed, "relations": len(payload.get("relations", []))}


@activity.defn
def mark_failed(law_id: str, law_name: str, error: str) -> None:
    db.mark_failed(law_id, law_name, error)      # 수집 실패 기록(재처리 대상이 됨)


@activity.defn
def record_change(law_id: str, law_name: str, old_sig: str | None,
                  new_sig: str, reason: str) -> None:
    db.append_sync_history(law_id, law_name, old_sig, new_sig, reason)
