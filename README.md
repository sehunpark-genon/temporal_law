# 법령 수집·적재 시스템

법제처 현행 법령의 **본문 + 본문 속 모든 하이퍼링크**(다른 법·시행령·시행규칙·행정규칙·조례·정관)를 메타데이터로 정리해 DB에 적재하고, **매일 바뀐 것만 자동 갱신**한다.

**핵심 특징**
- **현행 시행본 수집** — `target=eflaw` 로 "오늘 실제 시행 중"인 본문만 (미래 시행 조문 제외)
- **조 단위 구조** — 본문을 조(條)별로 쪼개고 각 조에 그 조의 relations 를 붙임 (조례는 양이 많아 별도 배열)
- **(MST+시행일) 지문 변경감지** — 개정 *공포* 와 *시행 도래* 를 둘 다 감지
- **Temporal 자동화** — 초기적재(backfill)·매일 동기(sync)·실패 격리·이어받기
- **알림** — 완료/실패 요약을 Slack 또는 macOS 데스크톱으로

## 문서
| 문서 | 내용 |
|---|---|
| **[docs/COLLECTION.md](docs/COLLECTION.md)** | **수집** — 한 법령을 본문+하이퍼링크 payload 로 만드는 방법 + 결과(payload) 형식 |
| **[docs/PIPELINE.md](docs/PIPELINE.md)** | **운영** — Temporal 워크플로(discover·backfill·sync)·재처리·변경감지·검증 |
| **[docs/ERD.md](docs/ERD.md)** | DB 스키마 ERD (테이블·컬럼·관계, mermaid) |

---

## 디렉터리 구조

```
collector/            ← 수집 코어 (pipeline 이 이걸 호출)          → COLLECTION.md
  core.py             본문+위임+인용+자기참조 → 조 단위 payload
  render.py           Chrome 렌더링 + 정관(학칙공단) 해석
  verify.py           커버리지 검증(본문 하이퍼링크 누락/주소 유효성)

pipeline/             ← 적재 자동화 (Temporal). collector 를 사용   → PIPELINE.md
  config.py           .env 설정 (인증키·DB·카탈로그·배치·알림·저장토글)
  collect.py          어댑터: discover_catalog(전체목록+지문)·collect_payload·content_hash
  models.py           ORM 엔티티 (SQLAlchemy 2.0) — 5개 테이블        → ERD.md
  db.py               저장 계층(Repository): 엔진(커넥션 풀) + upsert/조회
  activities.py       I/O(목록·네트워크·Chrome·DB)를 Temporal Activity 로 격리
  notify.py           알림(Slack 웹훅 / macOS 데스크톱, 미설정 시 no-op)
  workflows.py        오케스트레이션 (워크플로 4개)
  worker.py           워커 (워크플로/액티비티 등록·상주)
  starter.py          CLI: discover / backfill [N] / sync-now / schedule / unschedule

pyproject.toml        의존성/프로젝트 정의 (uv) · uv.lock 로 버전 고정
docker-compose.yml    temporal-db · temporal · temporal-ui · lawdb
```

**의존 방향**: `pipeline` → `pipeline.collect` → `collector.core` → `collector.render`
**DB 계층**: `models.py`(@Entity 격) + `db.py`(Repository 격). SQLAlchemy 2.0 ORM + 커넥션 풀. `init_schema()` 가 `models.Base` 기준으로 테이블/인덱스 생성(없을 때만).

---

## 실행

```bash
# 0) 의존성 (uv 프로젝트 — uv.lock 기준 .venv 동기화)
uv sync

# 1) 인프라 (Temporal + UI + 전용 DB lawdb)
docker compose up -d            # 이 PC에 이미 Temporal 이 있으면: docker compose up -d lawdb

# 2) 워커 상주 (정관 렌더용 Chrome 필요) — 터미널 1
uv run python -m pipeline.worker

# 3) 적재/갱신 — 터미널 2
uv run python -m pipeline.starter discover      # 전체 목록 → law_catalog (기본 법률만)
uv run python -m pipeline.starter discover all  #   시행령·시행규칙까지 전체
uv run python -m pipeline.starter backfill       # 본문 수집 (반복 호출 = 안 끝난 것만 이어받기)
uv run python -m pipeline.starter backfill 5     #   테스트: 5건만
uv run python -m pipeline.starter sync-now       # 변경 감지 1회 즉시 실행
uv run python -m pipeline.starter schedule       # 매일 자동 sync 등록 (unschedule 로 해제)

# 커버리지 검증 (payload 는 DB 에서 읽어 Chrome ground-truth 와 대조)
uv run python -m collector.verify --random 5     # 랜덤 5개 스팟체크
uv run python -m collector.verify --changed      # 최근 갱신된 것만
```
> ⚠️ **코드를 바꾸면 워커를 재시작**해야 반영됨 (Temporal 은 워커가 가진 코드로 실행).

---

## 환경변수 (`.env`)

전체 목록·기본값은 **[.env.example](.env.example)** 참고. 요약:

| 변수 | 기본값 | 설명 |
|---|---|---|
| `LAW_API_OC` | (필수) | 법제처 OpenAPI 인증키 |
| `DATABASE_URL` | `postgresql://lawuser:lawpass@localhost:5544/lawdb` | 적재 DB |
| `TEMPORAL_ADDRESS` | `localhost:7233` | Temporal gRPC |
| `LAW_CATALOG_LAW_ONLY` | `true` | catalog 추적 단위(법률만 / false=전체) |
| `LAW_BACKFILL_BATCH` | `20` | backfill 동시 수집 배치 크기 |
| `LAW_SYNC_CRON` | `0 3 * * *` | 매일 sync 스케줄(cron) |
| `SLACK_WEBHOOK_URL` | (없음) | 있으면 Slack, 없으면 macOS 데스크톱 알림 |
| `LAW_SAVE_RAW` | `true` | search/body/위임 원본을 `output/raw/` 에 저장 |
| `LAW_SAVE_PAYLOAD` | `false` | 최종 payload 를 `output/` 에 저장 |
| `LAW_VERIFY_MODE` / `LAW_VERIFY_N` | `random` / `3` | verify 기본 모드·개수(CLI 인자로 덮어씀) |

> 의존성 추가는 `uv add <패키지>` (pyproject.toml/uv.lock 자동 갱신).
