# 명령어 모음 (Cheatsheet)

전제: 프로젝트 루트(`temporal_law/`)에서 실행. 인프라는 docker compose, 앱 DB는 `lawdb`(localhost:5544, user `lawuser` / pw `lawpass`).

---

## 1. 인프라 (Docker)
```bash
docker compose up -d              # 전체 기동 (temporal·UI·temporal-db·lawdb)
docker compose up -d lawdb        # 이 PC처럼 Temporal 이미 떠 있으면 앱 DB만
docker compose ps                 # 상태 확인
docker compose logs -f temporal   # temporal 로그 실시간
docker compose stop               # 정지(데이터 유지)
docker compose down               # 컨테이너 제거(볼륨/데이터는 유지)
docker compose down -v            # ⚠️ 볼륨까지 삭제(데이터 전부 날아감)
```
- **Temporal Web UI**: http://localhost:8080  (워크플로 실행 내역·상태·에러 확인)

## 2. 워커 (작업 처리 — 상주 필요)
```bash
python3 -m pipeline.worker                            # 포그라운드(터미널 점유)
nohup python3 -m pipeline.worker > worker.log 2>&1 &  # 백그라운드 데몬
pkill -f pipeline.worker                              # 정지
tail -f worker.log                                    # 백그라운드 로그 보기
```
> 스케줄(매일 sync)이 실제로 돌려면 그 시각에 **워커가 떠 있어야** 함.
> ⚠️ **코드를 바꾸면 워커를 재시작**해야 적용됨 (Temporal은 워커가 가진 코드로 실행).

## 3. 파이프라인 실행 (starter)
```bash
python3 -m pipeline.starter discover        # 전체 목록 → law_catalog (법률만)
python3 -m pipeline.starter discover all    #   시행령·시행규칙까지 전체
python3 -m pipeline.starter backfill         # 미처리/실패 법령 수집 (반복=이어받기)
python3 -m pipeline.starter backfill 5       #   5건만(테스트)
python3 -m pipeline.starter sync-now         # 변경 감지 1회 즉시 실행
python3 -m pipeline.starter schedule         # 매일 자동 sync 등록(1회)
python3 -m pipeline.starter unschedule       # 스케줄 해제
```

## 4. DB 조회 (lawdb)
psql 접속:
```bash
docker exec -it temporal_law-lawdb-1 psql -U lawuser -d lawdb
```

먼저 단축 **함수** 등록 (**bash·zsh 둘 다 됨**).
> ⚠️ `DB="docker …"; $DB -c …` 식의 **변수 방식은 zsh 에서 안 됨**(zsh는 변수를 단어로 안 쪼갬 → `command not found`). 아래 함수를 쓰세요.
```bash
db()    { docker exec temporal_law-lawdb-1 psql -U lawuser -d lawdb "$@"; }        # 일반 조회
dbraw() { docker exec temporal_law-lawdb-1 psql -U lawuser -d lawdb -t -A "$@"; }  # raw(파일/파이프용)
```
조회 예:
```bash
# 진행 현황
db -c "SELECT status, count(*) FROM collect_state GROUP BY status;"
# 목록/수집 카운트
db -c "SELECT (SELECT count(*) FROM law_catalog) catalog, (SELECT count(*) FROM law) collected;"
# 최근 적재 완료된 법 10개
db -c "SELECT law_name, synced_at::timestamp(0) FROM law ORDER BY synced_at DESC LIMIT 10;"
# (관계 수까지) 최근 적재 10개
db -c "SELECT law_name, article_count, jsonb_array_length(payload->'relations') rels, synced_at::timestamp(0)
       FROM law ORDER BY synced_at DESC LIMIT 10;"
# 실패 목록(재처리 대상)
db -c "SELECT law_name, attempts, left(last_error,80) FROM collect_state WHERE status='failed';"
# 폐지된 법
db -c "SELECT law_name FROM law_catalog WHERE NOT is_active;"
# 이름 모를 때 검색 (정확히 일치해야 payload 조회됨)
db -c "SELECT law_name FROM law WHERE law_name LIKE '%연금%';"
# 특정 법의 관계만
db -c "SELECT relation_type, target_law_name, target_article_no, target_url
       FROM law_relation WHERE law_name='장애인복지법' ORDER BY source_article_no;"
```

## 5. 특정 법 payload 꺼내기
> 함수 `dbraw`(위 4번에서 등록) 사용. `-t -A` = 헤더/정렬 없는 **raw** 출력(파일/파이프용).
```bash
# 콘솔에 보기 좋게 (jq 있으면)
dbraw -c "SELECT payload FROM law WHERE law_name='국민연금법';" | jq .

# 파일로 저장 (예쁘게 들여쓰기 + 한글 그대로) — jsonb_pretty 추천
dbraw -c "SELECT jsonb_pretty(payload) FROM law WHERE law_name='국민연금법';" > 국민연금법.json
# 한 줄 minified 로 충분하면
dbraw -c "SELECT payload FROM law WHERE law_name='국민연금법';" > 국민연금법.min.json
pwd   # '>' 는 호스트 셸 리다이렉트 → '명령 실행한 호스트 현재 폴더'(예: ~/temporal_law)에 저장됨

# payload 안의 일부만 (jsonb 연산자)
dbraw -c "SELECT payload->'body'->>'content' FROM law WHERE law_name='국민연금법';"        # 본문 텍스트
dbraw -c "SELECT payload->'relation_stats' FROM law WHERE law_name='국민연금법';"          # 요약 통계
dbraw -c "SELECT payload->'ordinance_delegations' FROM law WHERE law_name='국민기초생활 보장법';"  # 조례
```

**함수 없이 한 줄로(가장 확실 / 복붙용):**
```bash
docker exec temporal_law-lawdb-1 psql -U lawuser -d lawdb -t -A \
  -c "SELECT jsonb_pretty(payload) FROM law WHERE law_name='국민연금법';" > 국민연금법.json
```

> **`COPY 0` / 빈 파일** 이 나오면 → 그 이름의 법이 **아직 미적재**거나 **이름이 정확히 안 맞는** 것. 위 LIKE 검색으로 실제 이름 확인 후 복붙.
> **인터랙티브 psql(`-it … psql`) 안에서는** `SELECT … > 파일` 이 안 됨(psql 문법 아님). 그땐 `\copy (SELECT …) TO '/tmp/x.json'` 후 `docker cp temporal_law-lawdb-1:/tmp/x.json .`. → 보통은 위 한 줄 방식이 제일 편함.

## 6. 단발 수집(파이프라인 없이, 파일로)
```bash
python3 -m collector.core       # output/*.json 생성
python3 -m collector.verify     # 커버리지 검증
```

---
### 컨테이너 이름 / 접속 정보
| | 값 |
|---|---|
| 앱 DB 컨테이너 | `temporal_law-lawdb-1` |
| 앱 DB 접속 | `localhost:5544` / db `lawdb` / `lawuser` / `lawpass` |
| Temporal gRPC | `localhost:7233` |
| Temporal UI | http://localhost:8080 |
