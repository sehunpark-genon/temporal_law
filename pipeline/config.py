"""
파이프라인 공통 설정. 모든 접속 정보는 환경변수(.env)로 주입한다.

- TEMPORAL_ADDRESS : Temporal gRPC 주소 (docker compose 기본 localhost:7233)
- DATABASE_URL     : 법령 데이터 Postgres (docker compose 기본 lawdb)
- LAW_TASK_QUEUE   : 워커/워크플로가 공유하는 task queue 이름
- 추적 대상 법령 목록은 main.LAW_NAMES 를 그대로 재사용
"""

import os

# main.py 의 .env 로더를 재사용 (LAW_API_OC 등도 같이 로드됨)
from collector.core import load_dotenv, LAW_NAMES

load_dotenv()

TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
TASK_QUEUE = os.environ.get("LAW_TASK_QUEUE", "law-pipeline")

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://lawuser:lawpass@localhost:5544/lawdb"
)

# 동기 sync 스케줄 (기본: 매일 03:00 KST 대응 — 서버 UTC 기준이면 18:00)
SYNC_CRON = os.environ.get("LAW_SYNC_CRON", "0 3 * * *")

TRACKED_LAWS = list(LAW_NAMES)   # (legacy) 카탈로그 없이 특정 법령만 돌릴 때의 fallback

# ── 카탈로그(전체 목록) 설정 ──
# True  = 법령구분 "법률"만 목록에 담음 (기본)
# False = 시행령/시행규칙 등 전체 (나중에 환경변수로 켜기)
CATALOG_LAW_ONLY = os.environ.get("LAW_CATALOG_LAW_ONLY", "true").lower() != "false"

# backfill 시 한 번에 병렬 수집할 법령 수(부하/Temporal 히스토리 제어)
BACKFILL_BATCH = int(os.environ.get("LAW_BACKFILL_BATCH", "20"))
