"""
Postgres 저장 계층 (SQLAlchemy 2.0). 모든 키는 law_id 기준.

Spring 으로 치면 Repository 레이어. 엔티티는 models.py(@Entity), 여기는 그걸 쓰는
질의/저장 함수. 공개 함수 시그니처는 activities.py 가 그대로 호출하므로 바꾸지 않는다.

  - engine          : 커넥션 풀 포함(QueuePool). 매 호출 connect 하던 raw 방식 대체
  - SessionLocal    : 세션 팩토리. `with SessionLocal.begin()` = @Transactional
  - init_schema     : models.Base 기준으로 테이블/인덱스 생성(없을 때만)

조례(ordinance_delegations)는 양이 많아 law.payload(JSONB) 안에만 보존한다.
"""

from datetime import datetime, timezone

from sqlalchemy import create_engine, delete, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import sessionmaker

from pipeline.config import DATABASE_URL
from pipeline.models import (
    Base, CollectState, Law, LawCatalog, LawRelation, SyncHistory,
)


def _sa_url(url: str) -> str:
    """psycopg(v3) 드라이버를 쓰도록 SQLAlchemy 방언 접두사를 보정."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


# 커넥션 풀(=HikariCP 역할). pool_pre_ping 으로 끊긴 커넥션 자동 복구.
engine = create_engine(
    _sa_url(DATABASE_URL),
    pool_size=8, max_overflow=4, pool_pre_ping=True, future=True,
)
# expire_on_commit=False: 커밋 후에도 객체 속성 접근 가능(세션 밖에서 dict 조립할 때 안전)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

# law_relation 에 넣을 relation dict 키 (law_id/law_name 제외한 본문 필드)
_RELATION_COLS = [
    "relation_type", "delegation_type", "target_category",
    "source_article_no", "source_clause", "link_text",
    "target_law_name", "target_article_no", "target_mst",
    "target_url", "resolve_method",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def init_schema() -> None:
    """models 에 정의된 테이블/인덱스를 생성(이미 있으면 그대로 둠)."""
    Base.metadata.create_all(engine)


# ── 목록(catalog) ────────────────────────────────────────────────

def upsert_catalog(rows: list[dict]) -> dict:
    """
    목록조회 결과를 law_catalog 에 upsert(지문 포함) + 신규는 collect_state(pending) 생성
    + 이번 목록에 안 보인 기존 법령은 is_active=false(폐지 soft delete).
    반환: {total, new, repealed}
    """
    now = _now()
    new = 0
    with SessionLocal.begin() as s:
        for r in rows:
            cat = pg_insert(LawCatalog).values(
                law_id=r["law_id"], mst=r["mst"], law_name=r["law_name"],
                law_type=r["law_type"], ministry=r["ministry"],
                enforcement_date=r["enforcement_date"], promulgation_date=r["promulgation_date"],
                detail_link=r["detail_link"], version_signature=r["version_signature"],
                is_active=True, discovered_at=now, last_seen_at=now,
            )
            cat = cat.on_conflict_do_update(
                index_elements=[LawCatalog.law_id],
                set_=dict(
                    mst=cat.excluded.mst, law_name=cat.excluded.law_name,
                    law_type=cat.excluded.law_type, ministry=cat.excluded.ministry,
                    enforcement_date=cat.excluded.enforcement_date,
                    promulgation_date=cat.excluded.promulgation_date,
                    detail_link=cat.excluded.detail_link,
                    version_signature=cat.excluded.version_signature,
                    is_active=True, last_seen_at=cat.excluded.last_seen_at,
                ),
            )
            s.execute(cat)

            # 신규 법령만 처리 대기열(pending)에 등록 (기존 상태는 유지 → 충돌 시 무시)
            # ON CONFLICT DO NOTHING 은 스킵 시 rowcount 가 -1 이라 신뢰 불가 →
            # RETURNING 으로 '실제 INSERT 된 행'만 세서 신규 건수를 정확히 집계.
            cs = pg_insert(CollectState).values(
                law_id=r["law_id"], law_name=r["law_name"], status="pending", attempts=0,
            ).on_conflict_do_nothing(index_elements=[CollectState.law_id]).returning(CollectState.law_id)
            new += len(s.execute(cs).fetchall())   # 삽입=1행 반환, 스킵=0행

        # 이번 조회에서 안 보인(=현행 목록에서 사라진) 법령 → 폐지 표시
        rep = s.execute(
            update(LawCatalog)
            .where(LawCatalog.last_seen_at < now, LawCatalog.is_active.is_(True))
            .values(is_active=False)
        )
        repealed = rep.rowcount
    return {"total": len(rows), "new": new, "repealed": repealed}


def list_backfill_targets(limit: int | None = None) -> list[dict]:
    """초기/재처리 대상: 살아있는(active) 법령 중 아직 done 이 아닌 것(pending/failed)."""
    stmt = (
        select(LawCatalog.law_id, LawCatalog.law_name, LawCatalog.version_signature)
        .join(CollectState, CollectState.law_id == LawCatalog.law_id)
        .where(LawCatalog.is_active.is_(True),
               CollectState.status.in_(["pending", "failed"]))
        .order_by(LawCatalog.law_name)
    )
    if limit:
        stmt = stmt.limit(int(limit))
    with SessionLocal() as s:
        return [{"law_id": a, "law_name": b, "signature": c}
                for a, b, c in s.execute(stmt)]


def list_sync_targets() -> list[dict]:
    """
    변경/미완 대상: 살아있는 법령 중
      - 아직 done 이 아니거나(status != done)
      - catalog 최신 지문이 마지막 수집 지문과 다른 것(=개정됨)
    catalog 지문(new)과 collect_state 지문(old)을 함께 돌려준다(이력기록용).
    """
    stmt = (
        select(LawCatalog.law_id, LawCatalog.law_name, LawCatalog.version_signature,
               CollectState.version_signature, CollectState.status)
        .outerjoin(CollectState, CollectState.law_id == LawCatalog.law_id)
        .where(
            LawCatalog.is_active.is_(True),
            or_(
                CollectState.status.is_distinct_from("done"),
                func.coalesce(CollectState.version_signature, "")
                != func.coalesce(LawCatalog.version_signature, ""),
            ),
        )
        .order_by(LawCatalog.law_name)
    )
    with SessionLocal() as s:
        return [{"law_id": r[0], "law_name": r[1], "signature": r[2],
                 "old_signature": r[3], "status": r[4]}
                for r in s.execute(stmt)]


# ── 수집 결과 저장 ───────────────────────────────────────────────

def upsert_law(payload: dict, version_signature: str, content_hash: str) -> None:
    """law 1행 upsert + law_relation 통째 교체 (키: law_id)."""
    law_id = str(payload.get("law_id") or "")
    law_name = payload["law_name"]
    body = payload.get("body", {})
    articles = body.get("articles", [])
    # 조 단위 구조 → 조회용 body_text(전체 본문)·law_relation(평면 relations) 파생
    body_text = "\n\n".join(a.get("content", "") for a in articles)
    rels = [r for a in articles for r in a.get("relations", [])] \
        + body.get("unmatched_relations", [])
    now = _now()
    with SessionLocal.begin() as s:
        stmt = pg_insert(Law).values(
            law_id=law_id, law_name=law_name, mst=payload.get("mst"),
            law_type=payload.get("law_type"),
            enforcement_date=payload.get("enforcement_date"),
            promulgation_date=payload.get("promulgation_date"),
            is_current=payload.get("is_current"),
            article_count=body.get("article_count"), body_text=body_text,
            payload=payload, content_hash=content_hash,
            version_signature=version_signature, synced_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[Law.law_id],
            set_=dict(
                law_name=stmt.excluded.law_name, mst=stmt.excluded.mst,
                law_type=stmt.excluded.law_type,
                enforcement_date=stmt.excluded.enforcement_date,
                promulgation_date=stmt.excluded.promulgation_date,
                is_current=stmt.excluded.is_current,
                article_count=stmt.excluded.article_count,
                body_text=stmt.excluded.body_text, payload=stmt.excluded.payload,
                content_hash=stmt.excluded.content_hash,
                version_signature=stmt.excluded.version_signature,
                synced_at=stmt.excluded.synced_at,
            ),
        )
        s.execute(stmt)

        # 관계 통째 교체 (law_id 기준) — 삭제 후 일괄 INSERT (조별 relations 를 평면화)
        s.execute(delete(LawRelation).where(LawRelation.law_id == law_id))
        if rels:
            s.execute(insert(LawRelation), [
                {"law_id": law_id, "law_name": law_name,
                 **{c: r.get(c) for c in _RELATION_COLS}}
                for r in rels
            ])


# ── 처리 상태(collect_state) ─────────────────────────────────────

def read_catalog(law_id: str) -> dict | None:
    """catalog 단건 조회 — 수집 시 search 재호출 없이 MST·시행일을 재사용하기 위함."""
    with SessionLocal() as s:
        row = s.get(LawCatalog, law_id)
        if not row:
            return None
        return {"law_id": row.law_id, "mst": row.mst, "law_type": row.law_type,
                "ministry": row.ministry, "enforcement_date": row.enforcement_date,
                "promulgation_date": row.promulgation_date}


def read_collect_state(law_id: str) -> dict | None:
    with SessionLocal() as s:
        row = s.get(CollectState, law_id)        # PK 단건 조회(= JPA findById)
        if not row:
            return None
        return {"status": row.status, "attempts": row.attempts,
                "version_signature": row.version_signature,
                "content_hash": row.content_hash}


def mark_attempt(law_id: str, law_name: str) -> None:
    """수집 시작 시 호출 — 시도 횟수 +1 (행 없으면 생성)."""
    with SessionLocal.begin() as s:
        stmt = pg_insert(CollectState).values(
            law_id=law_id, law_name=law_name, status="pending", attempts=1)
        stmt = stmt.on_conflict_do_update(
            index_elements=[CollectState.law_id],
            set_=dict(attempts=CollectState.attempts + 1,
                      law_name=stmt.excluded.law_name),
        )
        s.execute(stmt)


def mark_done(law_id: str, version_signature: str, content_hash: str, changed: bool) -> None:
    now = _now()
    vals = dict(status="done", last_error=None,
                version_signature=version_signature, content_hash=content_hash,
                last_checked_at=now, last_collected_at=now)
    if changed:                                   # 내용이 바뀐 경우에만 변경시각 갱신
        vals["last_changed_at"] = now
    with SessionLocal.begin() as s:
        s.execute(update(CollectState).where(CollectState.law_id == law_id).values(**vals))


def mark_failed(law_id: str, law_name: str, error: str) -> None:
    with SessionLocal.begin() as s:
        stmt = pg_insert(CollectState).values(
            law_id=law_id, law_name=law_name, status="failed", attempts=1,
            last_error=(error or "")[:2000])
        stmt = stmt.on_conflict_do_update(
            index_elements=[CollectState.law_id],
            set_=dict(status="failed", last_error=stmt.excluded.last_error),
        )
        s.execute(stmt)


def touch_checked(law_id: str) -> None:
    with SessionLocal.begin() as s:
        s.execute(update(CollectState).where(CollectState.law_id == law_id)
                  .values(last_checked_at=_now()))


def append_sync_history(law_id: str, law_name: str, old_sig: str | None,
                        new_sig: str, reason: str) -> None:
    with SessionLocal.begin() as s:
        s.add(SyncHistory(law_id=law_id, law_name=law_name, changed_at=_now(),
                          old_signature=old_sig, new_signature=new_sig, reason=reason))
