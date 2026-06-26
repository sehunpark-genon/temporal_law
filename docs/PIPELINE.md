# 적재 파이프라인 (Temporal + Postgres)

전체 현행법령 목록을 카탈로그로 적재하고, 그 카탈로그를 기준으로 법령별 수집·적재·변경감지를 자동화한다. 결과는 전용 **Postgres `lawdb`** 에 적재 → AI팀이 DB 로 소비.

> 전체 흐름은 [OVERVIEW.md](OVERVIEW.md), 본문·하이퍼링크 추출 로직은 [COLLECTION.md](COLLECTION.md) 참고. 이 문서는 "전체 목록을 받아 어떻게 자동으로 돌리고 적재·갱신하나".

---

## 1. 디렉터리 구조

```
collector/            ← 수집 코어 (pipeline 이 이걸 호출)
  core.py             본문+위임+인용+자기참조+정렬 → payload   (구 main.py)
  render.py           Chrome 렌더링 + 정관 해석
  verify.py           커버리지 검증

pipeline/             ← 적재 자동화 (Temporal). collector 를 사용
  config.py           .env 설정 + 카탈로그 옵션(법률만/배치크기)
  collect.py          어댑터: list_catalog(전체목록)·discover_versions(MST지문)·collect_payload·content_hash
  db.py               Postgres 스키마 + upsert (law_catalog / collect_state / law / law_relation / sync_history)
  activities.py       I/O(목록·네트워크·Chrome·DB)를 Temporal Activity 로 격리
  workflows.py        오케스트레이션 (워크플로 4개)
  worker.py           워커
  starter.py          CLI: discover / backfill / sync-now / schedule / unschedule

docker-compose.yml    temporal-db · temporal · temporal-ui · lawdb
```
**의존 방향**: `pipeline` → `pipeline.collect` → `collector.core` → `collector.render`.

---

## 2. 워크플로 (4개)

```
DiscoverCatalogWorkflow(law_only)     전체 목록 조회 → law_catalog 적재 (신규는 처리대기 등록)
CollectLawWorkflow(law_name, law_id)  공용 단위: 한 법령 [버전조회→수집→저장] (멱등)
├─ BackfillWorkflow(only_unfinished, limit)  카탈로그 기준 배치 수집 (초기적재 & 재처리)
└─ SyncWorkflow()                     (스케줄) 카탈로그 전체 MST 지문 비교 → 바뀐 것만 재수집
```
- 실제 I/O 는 전부 **activity** 로 격리(워크플로는 결정적이어야 함). activity 는 워커가 ThreadPoolExecutor 로 실행.
- **데이터 흐름**: `discover`(목록) → `law_catalog` → `backfill`(법령별 수집) → `law`/`law_relation`, 이후 `sync`(매일 변경분만).

---

## 3. 재처리(이어받기) — 부하·실패에 강하게

- **Temporal 자동 재시도**: 네트워크/일시 오류는 activity RetryPolicy 가 자동 재시도(지수 백오프).
- **법령별 실패 격리**: 한 법령이 끝까지 실패해도 `collect_state.status='failed'` 로 기록하고 **나머지는 계속** 진행.
- **이어받기**: `backfill` 은 `status IN (pending, failed)` 만 대상으로 한다. 중간에 죽거나 일부 실패해도 **다시 `backfill` 하면 안 끝난 것만** 재처리. `attempts` 로 시도 횟수 추적.
- **배치**: `BACKFILL_BATCH`(기본 20) 단위로 병렬 → 동시 부하 / Temporal 히스토리 제어.
- **호출 간격**: `get_json` 이 호출마다 0.2s 슬립 → 과호출 방지. (본문/lsDelegated 는 법령당 1회씩)

---

## 4. 변경 감지 (MST 지문)

- `discover_versions` 가 **법령 + 시행령 + 시행규칙의 MST** 를 `lawSearch` 한 번으로 모아 **지문(signature)** 생성.
- `sync` 가 `collect_state` 의 지문과 비교 → 다르면 재수집, 같으면 `last_checked_at` 만 갱신.
- `content_hash`(payload 내용 SHA-256)는 별개로, 수집 후 **내용이 같으면 DB 쓰기를 스킵**하는 보조 값.

---

## 5. DB 스키마 (`lawdb`)

```mermaid
erDiagram
    law_catalog ||--|| collect_state : law_id
    law_catalog ||--o| law           : law_id
    law         ||--o{ law_relation  : law_id
    law         ||--o{ sync_history  : history
```
| 테이블 | 역할 |
|---|---|
| `law_catalog` | 전체 현행법령 목록 "무엇이 존재하나" — discover 가 채움 |
| `collect_state` | 법령별 처리 상태/재처리 (status·attempts·error·지문) |
| `law` | 수집 결과. payload(JSONB) + 조회용 컬럼 |
| `law_relation` | `payload.relations` 정규화 (SQL 쿼리 편의용) |
| `sync_history` | 지문 변경 이력 (감사용) |
> 조례(`ordinance_delegations`)는 양이 많아 `law.payload` JSONB 안에만 보존.

### `law_catalog` — 명단 ("무슨 법이 있나")
| 필드 | 값 예시 | 용도 |
|---|---|---|
| `law_id` (PK) | `001234` | 법령 고유 식별자(버전 바뀌어도 동일) |
| `mst` | `279697` | 현행 버전 일련번호(개정되면 바뀜) |
| `law_name` / `law_type` / `ministry` | `장애인복지법` / `법률` / `보건복지부` | 메타 |
| `enforcement_date` / `promulgation_date` | `20260512` | 시행일 / 공포일 |
| `detail_link` | `/DRF/lawService.do?...` | 법제처 원본 링크 |
| `version_signature` | `0014023d6e007816` | **법률+시행령+시행규칙 MST 지문**(변경 감지 기준) |
| `is_active` | `true` / `false` | 현행이면 true, **폐지되면 false** |
| `discovered_at` / `last_seen_at` | 시각 | 최초 발견 / 마지막 목록조회에서 본 시각 |

### `collect_state` — 처리 상태 ("어디까지 했나")
| 필드 | 값 예시 | 용도 |
|---|---|---|
| `law_id` (PK) | `001234` | 어떤 법 |
| `status` | `pending`/`done`/`failed` | 수집 상태 |
| `attempts` | `2` | 수집 시도 횟수 |
| `last_error` | `Child Workflow ...` | 마지막 실패 사유 |
| `version_signature` | `0014...` | **마지막 수집 시점의 지문**(catalog 지문과 비교) |
| `content_hash` | `595ccf...` | 마지막 저장 내용 해시(같으면 재저장 스킵) |
| `last_checked_at` / `last_collected_at` / `last_changed_at` | 시각 | 확인 / 수집 / 실제변경 시각 |

### `law` — 수집 결과 본체
| 필드 | 값 예시 | 용도 |
|---|---|---|
| `law_id` (PK), `law_name`, `mst`, `law_type` | … | 식별/메타 |
| `enforcement_date`, `promulgation_date`, `is_current` | … | 메타 |
| `article_count` | `196` | 조문 수(검증용) |
| `body_text` | `"제1조(목적) …"` | 본문 전체 텍스트 |
| **`payload`** (JSONB) | `{ … }` | **전체 결과 문서. AI팀은 이것만 주면 충분** (형식 → COLLECTION.md) |
| `content_hash`, `version_signature`, `synced_at` | … | 변경 판정 값 / 저장 시각 |

### `law_relation` — 관계 정규화
`payload.relations` 를 행으로 펼친 사본. `relation_type`·`target_category`·`source_article_no`·`target_law_name`·`target_url` 등 컬럼 → **"이 법이 인용/위임하는 것만 골라 SQL 조회"** 용. (payload 안에도 같은 내용 존재)

### `sync_history` — 변경 이력
`law_id`·`changed_at`·`old_signature`·`new_signature`·`reason`(initial/mst_changed).

```sql
SELECT status, count(*) FROM collect_state GROUP BY status;        -- 진행 현황
SELECT * FROM law_relation WHERE law_name='장애인복지법' AND relation_type='delegation';
SELECT * FROM collect_state WHERE status='failed';                 -- 실패(재처리 대상)
SELECT law_name FROM law_catalog WHERE NOT is_active;              -- 폐지된 법
```

---

## 6. 실행

### 6-1. `.env`
```ini
LAW_API_OC=발급받은_OC_키                                       # 필수
TEMPORAL_ADDRESS=localhost:7233
DATABASE_URL=postgresql://lawuser:lawpass@localhost:5544/lawdb
# 선택: 전체(시행령·규칙 포함) 목록까지 받으려면
# LAW_CATALOG_LAW_ONLY=false
```

### 6-2. 순서
```bash
# 인프라
docker compose up -d            # 새 머신은 전체 / 이 PC는: docker compose up -d lawdb

# 워커 상주 (터미널 1, 정관 Chrome 사용)
python3 -m pipeline.worker

# 적재 (터미널 2)
python3 -m pipeline.starter discover    # ① 전체 목록 → law_catalog (법률 1,714건; 'discover all' = 전체)
python3 -m pipeline.starter backfill     # ② 미처리/실패 법령 수집 (몇 번 돌려도 안 끝난 것만 이어받음)
python3 -m pipeline.starter backfill 5   #    테스트: 5건만
python3 -m pipeline.starter schedule     # ③ 매일 자동 sync 등록 (sync-now / unschedule 도 있음)
```

--
