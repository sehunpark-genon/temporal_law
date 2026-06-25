"""
Postgres 저장 계층 (모든 키는 law_id 기준).

테이블 관계
  law_catalog  (law_id PK)   전체 현행법령 목록. "무엇이 존재하나" — discovery 가 채움
    └ 1:1 collect_state(law_id PK)   처리 상태/재처리용 (status·attempts·error·지문)
    └ 1:1 law(law_id PK)             수집 결과. payload(JSONB) 통째 + 조회용 컬럼
            └ 1:N law_relation(law_id FK)   relations 정규화 (AI팀 쿼리용)
  sync_history(law_id)        지문 변경 이력 (감사용)

조례(ordinance_delegations)는 양이 많아 law.payload JSONB 안에만 보존한다.
"""

import json
from datetime import datetime, timezone

import psycopg

from pipeline.config import DATABASE_URL


SCHEMA_STATEMENTS = [
    # 전체 목록 (현행). discovery 가 페이지네이션으로 채움
    """
    CREATE TABLE IF NOT EXISTS law_catalog (
        law_id            text PRIMARY KEY,   -- 안정적 법령 식별자
        mst               text,               -- 현행 버전 일련번호
        law_name          text,
        law_type          text,               -- 법령구분명 (법률/대통령령/…)
        ministry          text,               -- 소관부처명
        enforcement_date  text,
        promulgation_date text,
        detail_link       text,
        version_signature text,               -- 법률+시행령+시행규칙 MST 지문(최신)
        is_active         boolean DEFAULT true,  -- 폐지되면 false (soft delete)
        discovered_at     timestamptz,        -- 최초 발견
        last_seen_at      timestamptz         -- 마지막 목록조회에서 본 시각
    )
    """,
    # 처리 상태 + 변경감지 (재처리의 핵심)
    """
    CREATE TABLE IF NOT EXISTS collect_state (
        law_id            text PRIMARY KEY,
        law_name          text,
        status            text,               -- pending | done | failed
        attempts          int DEFAULT 0,      -- 수집 시도 횟수
        last_error        text,               -- 마지막 실패 메시지
        version_signature text,               -- 마지막 MST 지문(변경감지)
        content_hash      text,               -- 마지막 내용 해시(저장 스킵)
        last_checked_at   timestamptz,
        last_collected_at timestamptz,
        last_changed_at   timestamptz
    )
    """,
    # 수집 결과 본체
    """
    CREATE TABLE IF NOT EXISTS law (
        law_id            text PRIMARY KEY,
        law_name          text,
        mst               text,
        law_type          text,
        enforcement_date  text,
        promulgation_date text,
        is_current        boolean,
        article_count     int,
        body_text         text,
        payload           jsonb NOT NULL,     -- 전체 payload(문서 그대로)
        content_hash      text,
        version_signature text,
        synced_at         timestamptz
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS law_relation (
        id                bigserial PRIMARY KEY,
        law_id            text NOT NULL REFERENCES law(law_id) ON DELETE CASCADE,
        law_name          text,
        relation_type     text,   -- delegation | citation | internal_ref
        delegation_type   text,
        target_category   text,   -- 법령 | 행정규칙 | 자치법규 | 학칙공단
        source_article_no text,
        source_clause     text,
        link_text         text,
        target_law_name   text,
        target_article_no text,
        target_mst        text,
        target_url        text,
        resolve_method    text
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_relation_law ON law_relation(law_id)",
    "CREATE INDEX IF NOT EXISTS idx_relation_target ON law_relation(target_law_name, target_article_no)",
    "CREATE INDEX IF NOT EXISTS idx_relation_type ON law_relation(relation_type)",
    "CREATE INDEX IF NOT EXISTS idx_state_status ON collect_state(status)",
    """
    CREATE TABLE IF NOT EXISTS sync_history (
        id            bigserial PRIMARY KEY,
        law_id        text,
        law_name      text,
        changed_at    timestamptz,
        old_signature text,
        new_signature text,
        reason        text
    )
    """,
]

# law_relation 에 넣을 relation dict 키 (law_id/law_name 제외한 본문 필드)
_RELATION_COLS = [
    "relation_type", "delegation_type", "target_category",
    "source_article_no", "source_clause", "link_text",
    "target_law_name", "target_article_no", "target_mst",
    "target_url", "resolve_method",
]


def _now():
    return datetime.now(timezone.utc)


def _connect():
    return psycopg.connect(DATABASE_URL, autocommit=True)


def init_schema():
    with _connect() as conn:
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)


# ── 목록(catalog) ────────────────────────────────────────────────

def upsert_catalog(rows: list[dict]) -> dict:
    """
    목록조회 결과를 law_catalog 에 upsert (지문 포함) + 신규는 collect_state(pending) 생성
    + 이번 목록에 안 보인 기존 법령은 is_active=false (폐지 soft delete).
    반환: {total, new, repealed}
    """
    now = _now()
    new = 0
    with _connect() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO law_catalog (law_id, mst, law_name, law_type, ministry,
                    enforcement_date, promulgation_date, detail_link, version_signature,
                    is_active, discovered_at, last_seen_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s,%s)
                ON CONFLICT (law_id) DO UPDATE SET
                    mst=EXCLUDED.mst, law_name=EXCLUDED.law_name, law_type=EXCLUDED.law_type,
                    ministry=EXCLUDED.ministry, enforcement_date=EXCLUDED.enforcement_date,
                    promulgation_date=EXCLUDED.promulgation_date, detail_link=EXCLUDED.detail_link,
                    version_signature=EXCLUDED.version_signature, is_active=true,
                    last_seen_at=EXCLUDED.last_seen_at
                """,
                (r["law_id"], r["mst"], r["law_name"], r["law_type"], r["ministry"],
                 r["enforcement_date"], r["promulgation_date"], r["detail_link"],
                 r["version_signature"], now, now),
            )
            # 신규 법령만 처리 대기열(pending)에 등록 (기존 상태는 유지)
            cur = conn.execute(
                """INSERT INTO collect_state (law_id, law_name, status, attempts)
                   VALUES (%s,%s,'pending',0)
                   ON CONFLICT (law_id) DO NOTHING""",
                (r["law_id"], r["law_name"]),
            )
            new += cur.rowcount
        # 이번 조회에서 안 보인(=현행 목록에서 사라진) 법령 → 폐지 표시
        rep = conn.execute(
            "UPDATE law_catalog SET is_active=false WHERE last_seen_at < %s AND is_active=true",
            (now,),
        )
        repealed = rep.rowcount
    return {"total": len(rows), "new": new, "repealed": repealed}


def list_backfill_targets(limit: int | None = None) -> list[dict]:
    """초기/재처리 대상: 살아있는(active) 법령 중 아직 done 이 아닌 것(pending/failed)."""
    sql = """
        SELECT c.law_id, c.law_name, c.version_signature
        FROM law_catalog c JOIN collect_state s ON s.law_id = c.law_id
        WHERE c.is_active AND s.status IN ('pending','failed')
        ORDER BY c.law_name
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    with _connect() as conn:
        return [{"law_id": r[0], "law_name": r[1], "signature": r[2]}
                for r in conn.execute(sql).fetchall()]


def list_sync_targets() -> list[dict]:
    """
    변경/미완 대상: 살아있는 법령 중
      - 아직 done 이 아니거나(status != done)
      - catalog 의 최신 지문이 마지막 수집 지문과 다른 것(=개정됨)
    catalog 지문(new)과 collect_state 지문(old)을 함께 돌려준다(이력기록용).
    """
    sql = """
        SELECT c.law_id, c.law_name, c.version_signature, s.version_signature, s.status
        FROM law_catalog c LEFT JOIN collect_state s ON s.law_id = c.law_id
        WHERE c.is_active
          AND (s.status IS DISTINCT FROM 'done'
               OR COALESCE(s.version_signature,'') <> COALESCE(c.version_signature,''))
        ORDER BY c.law_name
    """
    with _connect() as conn:
        return [{"law_id": r[0], "law_name": r[1], "signature": r[2],
                 "old_signature": r[3], "status": r[4]}
                for r in conn.execute(sql).fetchall()]


# ── 수집 결과 저장 ───────────────────────────────────────────────

def upsert_law(payload: dict, version_signature: str, content_hash: str):
    """law 1행 upsert + law_relation 통째 교체 (키: law_id)."""
    law_id = str(payload.get("law_id") or "")
    law_name = payload["law_name"]
    body = payload.get("body", {})
    with _connect() as conn, conn.transaction():
        conn.execute(
            """
            INSERT INTO law (law_id, law_name, mst, law_type, enforcement_date,
                             promulgation_date, is_current, article_count, body_text,
                             payload, content_hash, version_signature, synced_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (law_id) DO UPDATE SET
                law_name=EXCLUDED.law_name, mst=EXCLUDED.mst, law_type=EXCLUDED.law_type,
                enforcement_date=EXCLUDED.enforcement_date,
                promulgation_date=EXCLUDED.promulgation_date,
                is_current=EXCLUDED.is_current, article_count=EXCLUDED.article_count,
                body_text=EXCLUDED.body_text, payload=EXCLUDED.payload,
                content_hash=EXCLUDED.content_hash,
                version_signature=EXCLUDED.version_signature, synced_at=EXCLUDED.synced_at
            """,
            (law_id, law_name, payload.get("mst"), payload.get("law_type"),
             payload.get("enforcement_date"), payload.get("promulgation_date"),
             payload.get("is_current"), body.get("article_count"), body.get("content"),
             json.dumps(payload, ensure_ascii=False), content_hash, version_signature, _now()),
        )
        # 관계 통째 교체 (law_id 기준)
        conn.execute("DELETE FROM law_relation WHERE law_id = %s", (law_id,))
        rows = [
            tuple([law_id, law_name] + [r.get(c) for c in _RELATION_COLS])
            for r in payload.get("relations", [])
        ]
        if rows:
            ph = ",".join(["%s"] * (len(_RELATION_COLS) + 2))
            conn.cursor().executemany(
                f"INSERT INTO law_relation (law_id, law_name, {','.join(_RELATION_COLS)}) "
                f"VALUES ({ph})",
                rows,
            )


# ── 처리 상태(collect_state) ─────────────────────────────────────

def read_collect_state(law_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT status, attempts, version_signature, content_hash "
            "FROM collect_state WHERE law_id=%s", (law_id,),
        ).fetchone()
    if not row:
        return None
    return {"status": row[0], "attempts": row[1],
            "version_signature": row[2], "content_hash": row[3]}


def mark_attempt(law_id: str, law_name: str):
    """수집 시작 시 호출 — 시도 횟수 +1 (행 없으면 생성)."""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO collect_state (law_id, law_name, status, attempts)
               VALUES (%s,%s,'pending',1)
               ON CONFLICT (law_id) DO UPDATE SET attempts = collect_state.attempts + 1,
                                                   law_name = EXCLUDED.law_name""",
            (law_id, law_name),
        )


def mark_done(law_id: str, version_signature: str, content_hash: str, changed: bool):
    now = _now()
    with _connect() as conn:
        conn.execute(
            """UPDATE collect_state SET
                 status='done', last_error=NULL,
                 version_signature=%s, content_hash=%s,
                 last_checked_at=%s, last_collected_at=%s,
                 last_changed_at=CASE WHEN %s THEN %s ELSE last_changed_at END
               WHERE law_id=%s""",
            (version_signature, content_hash, now, now, changed, now, law_id),
        )


def mark_failed(law_id: str, law_name: str, error: str):
    with _connect() as conn:
        conn.execute(
            """INSERT INTO collect_state (law_id, law_name, status, attempts, last_error)
               VALUES (%s,%s,'failed',1,%s)
               ON CONFLICT (law_id) DO UPDATE SET status='failed', last_error=EXCLUDED.last_error""",
            (law_id, law_name, (error or "")[:2000]),
        )


def touch_checked(law_id: str):
    with _connect() as conn:
        conn.execute("UPDATE collect_state SET last_checked_at=%s WHERE law_id=%s",
                     (_now(), law_id))


def append_sync_history(law_id: str, law_name: str, old_sig: str | None,
                        new_sig: str, reason: str):
    with _connect() as conn:
        conn.execute(
            """INSERT INTO sync_history (law_id, law_name, changed_at,
                                         old_signature, new_signature, reason)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (law_id, law_name, _now(), old_sig, new_sig, reason),
        )
