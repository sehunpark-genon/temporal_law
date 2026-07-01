"""
알림 (선택 기능). 우선순위:
  1) SLACK_WEBHOOK_URL 이 있으면 → Slack 전송
  2) 없으면 → macOS 데스크톱 알림(osascript)
  3) 둘 다 불가하면 → 조용히 no-op

알림은 부가 기능이므로 어떤 예외도 밖으로 던지지 않는다(파이프라인을 막지 않음).
외부 의존성 없이 표준 라이브러리(urllib/subprocess)만 사용.
"""

import json
import shutil
import subprocess
import urllib.request

from pipeline.config import SLACK_WEBHOOK_URL

TITLE = "법령 수집 파이프라인"


def _slack(text: str) -> bool:
    try:
        req = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data=json.dumps({"text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _desktop(text: str) -> bool:
    """macOS 데스크톱 알림. osascript 없으면(다른 OS) no-op."""
    if not shutil.which("osascript"):
        return False
    # osascript 문자열 리터럴 안전 처리(따옴표/역슬래시 이스케이프)
    safe = text.replace("\\", "\\\\").replace('"', '\\"')
    title = TITLE.replace('"', '\\"')
    script = f'display notification "{safe}" with title "{title}"'
    try:
        subprocess.run(["osascript", "-e", script],
                       timeout=10, capture_output=True)
        return True
    except Exception:
        return False


def send(text: str) -> bool:
    """알림 전송. Slack(설정 시) → 데스크톱(macOS) → no-op 순으로 시도."""
    if SLACK_WEBHOOK_URL:
        return _slack(text)
    return _desktop(text)
