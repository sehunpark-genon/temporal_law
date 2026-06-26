# 법령 수집·적재 시스템

법제처 현행 법령의 **본문 + 본문 속 모든 하이퍼링크**(다른 법·시행령·시행규칙·행정규칙·조례·정관)를 메타데이터로 정리해 DB에 적재하고, **매일 바뀐 것만 자동 갱신**한다.

## 문서
| 문서 | 내용 |
|---|---|
| **[docs/OVERVIEW.md](docs/OVERVIEW.md)** | 전체 단계별 흐름 (보고용, 그림 위주) — *여기부터* |
| **[docs/COLLECTION.md](docs/COLLECTION.md)** | 본문·하이퍼링크를 어떻게 뽑나 + 결과(payload) 형식 |
| **[docs/PIPELINE.md](docs/PIPELINE.md)** | Temporal 자동화 + DB 스키마 + 실행 방법 |

## 빠른 실행
```bash
# 1) 인프라 (Temporal + UI + 전용 DB lawdb)
docker compose up -d            # 이 PC에 이미 Temporal 이 있으면: docker compose up -d lawdb

# 2) 워커 상주 (정관 렌더용 Chrome 필요)
python3 -m pipeline.worker

# 3) 목록 적재 → 본문 수집 → 매일 자동 갱신
python3 -m pipeline.starter discover    # 전체 법령 목록 → law_catalog
python3 -m pipeline.starter backfill     # 본문 수집 (반복 호출 = 이어받기)
python3 -m pipeline.starter schedule     # 매일 자동 sync(=목록갱신+변경수집)
```
> `.env` 에 법제처 인증키 `LAW_API_OC` 필요. 자세한 건 위 문서들 참고.

## 구성
```
collector/   수집 코어 (본문·하이퍼링크 추출)        → COLLECTION.md
pipeline/    Temporal 적재 자동화                    → PIPELINE.md
docs/        문서
docker-compose.yml
```
