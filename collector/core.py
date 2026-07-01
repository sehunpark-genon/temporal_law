import os
import re
import json
import time
import logging
from datetime import datetime, timezone, timedelta

import requests

from collector import render

logger = logging.getLogger("collector.core")


# =========================
# 0. 설정
# =========================

def load_dotenv(path: str = ".env"):
    """의존성 없이 .env(KEY=VALUE) 를 os.environ 으로 로드."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            # 따옴표로 감싸지 않은 값의 인라인 주석( ' #' 뒤)은 제거
            if value[:1] not in ("'", '"'):
                value = re.split(r"\s+#", value, 1)[0].strip()
            os.environ.setdefault(key.strip(), value.strip("\"'"))


load_dotenv()

# 법제처 OpenAPI 인증키(OC). 코드에 하드코딩하지 않고 .env 로만 주입한다.
# (.env.example 참고)
OC = os.environ.get("LAW_API_OC", "")
BASE_URL = "http://www.law.go.kr/DRF"

# 사용자 이동용 캐노니컬 URL (한글 그대로, 퍼센트 인코딩 X)
LAWGO = "https://www.law.go.kr"


class CollectError(Exception):
    """수집 실패(검색·본문 조회 실패 등). 빈 payload 를 조용히 저장하지 않고
    명확히 실패시켜 파이프라인이 재시도/실패기록을 하도록 한다."""

# PoC니까 3개만
LAW_NAMES = [
    "자동차관리법",
    "보험업법",
    "국민기초생활 보장법",
    "장애인복지법",
]

OUTPUT_DIR = "output"
RAW_DIR = os.path.join(OUTPUT_DIR, "raw")

KST = timezone(timedelta(hours=9))


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() not in ("false", "0", "no", "")


# 로컬 파일 저장 토글 (env 로 on/off)
#  - LAW_SAVE_RAW     : search/body/delegated 원본 응답을 output/raw/ 에 저장 (기본 on)
#  - LAW_SAVE_PAYLOAD : 최종 payload 를 output/ 에 저장 (기본 off — 파이프라인은 DB 적재라 불필요)
SAVE_RAW = _env_bool("LAW_SAVE_RAW", True)
SAVE_PAYLOAD = _env_bool("LAW_SAVE_PAYLOAD", False)


# =========================
# 1. 공통 유틸
# =========================

def now_kst() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def ensure_dirs():
    os.makedirs(RAW_DIR, exist_ok=True)


def save_json(path: str, data):
    """JSON 저장. 상위 폴더가 없으면 만든다(ensure_dirs 호출 여부와 무관하게 안전)."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _safe_params(params: dict) -> dict:
    """로그에 OC(인증키)가 찍히지 않도록 마스킹."""
    return {k: ("***" if k == "OC" else v) for k, v in (params or {}).items()}


def get_json(url: str, params: dict, sleep_sec: float = 0.2, retries: int = 3):
    """
    법제처 API 호출 공통 함수.

    - 일시 오류(네트워크/5xx/타임아웃)는 지수 백오프로 재시도(1s→2s→4s, 상한 5s)
    - 4xx(잘못된 요청)는 재시도해도 무의미하므로 즉시 중단
    - 호출 사이 sleep_sec 간격 유지(과호출 방지)
    - 최종 실패 시 None 반환(상위에서 빈 데이터를 'done' 으로 저장하지 않도록 CollectError 로 승격)
    - 로그에는 OC(인증키)를 절대 남기지 않는다(_safe_params)
    """
    last_err = None
    for attempt in range(retries):
        try:
            res = requests.get(url, params=params, timeout=30)
            res.raise_for_status()
            text = res.text.strip()
            if not text:                          # 빈 응답
                return None
            return res.json()

        except requests.HTTPError as e:
            last_err = e
            status = getattr(e.response, "status_code", 0)
            if 400 <= status < 500:               # 잘못된 요청 → 재시도 무의미
                logger.warning("API %s (재시도 안 함): %s params=%s",
                               status, url, _safe_params(params))
                return None
        except Exception as e:                    # 네트워크/타임아웃/JSON 등
            last_err = e

        if attempt < retries - 1:                 # 마지막 시도가 아니면 백오프 후 재시도
            time.sleep(min(2 ** attempt, 5))
        time.sleep(sleep_sec)                     # 호출 간 최소 간격

    logger.error("API 실패(%d회 시도): %s params=%s err=%s",
                 retries, url, _safe_params(params), last_err)
    return None


def normalize_text(text: str) -> str:
    if not text:
        return ""

    return (
        str(text)
        .replace("　", " ")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .strip()
    )


def compact(text: str) -> str:
    """
    비교용 정규화.
    공백, 특수 공백 제거.
    """
    if not text:
        return ""

    return (
        str(text)
        .replace(" ", "")
        .replace("　", "")
        .replace("\n", "")
        .replace("\t", "")
        .strip()
    )


def first_value_recursive(obj, candidate_keys):
    """
    중첩 JSON에서 특정 key 후보 중 첫 값을 찾음.
    법제처 API 응답 필드명이 조금씩 달라질 수 있어서 방어적으로 처리.
    """
    if isinstance(obj, dict):
        for key in candidate_keys:
            if key in obj and obj[key] not in (None, ""):
                return obj[key]

        for value in obj.values():
            found = first_value_recursive(value, candidate_keys)
            if found not in (None, ""):
                return found

    elif isinstance(obj, list):
        for item in obj:
            found = first_value_recursive(item, candidate_keys)
            if found not in (None, ""):
                return found

    return ""


def listify(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def flatten_text(value) -> str:
    """문자열/중첩 리스트를 공백으로 이어붙인 한 줄 텍스트로.
    (목내용 등이 법령에 따라 문자열·리스트·리스트의 리스트로 와서 방어 처리)"""
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(flatten_text(v) for v in value)
    return str(value)


# =========================
# 2. 캐노니컬 URL 생성 (한글 그대로)
# =========================
#
# 법제처 단축 URL 규칙:
#   법령      -> https://www.law.go.kr/법령/{법령명}            (+ /제N조)
#   행정규칙  -> https://www.law.go.kr/행정규칙/{행정규칙명}
#   자치법규  -> https://www.law.go.kr/자치법규/{조례명}        (조례/규칙 등 지자체 법규)
#   학칙공단  -> https://www.law.go.kr/학칙공단/{정관명}         (공단 정관/학칙 등)
#
# target_url 은 사람이 읽을 수 있도록 한글을 그대로 저장한다(퍼센트 인코딩 X).

REF_CATEGORIES = {"법령", "행정규칙", "자치법규", "학칙공단"}


def build_ref_url(category: str, title: str, article_no: str | None = None) -> str:
    """
    카테고리 + 제목(+조문)으로 캐노니컬 한글 URL 생성.
    예) build_ref_url("법령", "자동차관리법", "제3조")
        -> https://www.law.go.kr/법령/자동차관리법/제3조
    """
    category = category if category in REF_CATEGORIES else "법령"
    title = normalize_text(title)

    url = f"{LAWGO}/{category}/{title}"

    if article_no:
        url = f"{url}/{article_no}"

    return url


def article_no_from_fields(jo_no, jo_branch) -> str:
    """
    조번호 + 조가지번호를 제N조 또는 제N조의M으로 변환.
    예:
    조번호 1, 조가지번호 2 -> 제1조의2
    조번호 132, 조가지번호 0 -> 제132조
    """
    jo_no = str(jo_no or "").strip()
    jo_branch = str(jo_branch or "").strip()

    if not jo_no:
        return ""

    try:
        base = int(jo_no)
    except ValueError:
        return jo_no

    if base == 0:                                # 조문번호 0 = '모든조문'(법령 전체) → 조 없음(법령단위 링크)
        return ""

    if not jo_branch or jo_branch in ("0", "00"):
        return f"제{base}조"

    try:
        branch = int(jo_branch)
        return f"제{base}조의{branch}"
    except ValueError:
        return f"제{base}조"


def article_sort_key(article_no: str) -> tuple[int, int]:
    """
    '제N조(의M)' 문자열을 정렬용 (N, M) 튜플로.
    제1조 < 제2조 < … < 제15조 < 제15조의2 < 제15조의3 순서.
    조문번호를 못 읽으면 맨 뒤로 보낸다.
    """
    match = re.match(r"제(\d+)조(?:의(\d+))?", article_no or "")
    if not match:
        return (10 ** 9, 0)
    return (int(match.group(1)), int(match.group(2) or 0))


# =========================
# 3. 법령 검색 / 본문 / 위임링크 조회
# =========================

def search_law(law_name: str) -> dict:
    """
    법령명으로 '현행 시행 중' 버전을 검색 (target=eflaw, nw=3).
    여기서 law_id, mst, 공포일자, 시행일자 등을 가져온다.

    target=law(현행법령)는 공포된 최신본(미래 시행 조문 포함)을 주는 반면,
    target=eflaw + nw=3(현행 시행일법령)은 '오늘 실제 시행 중'인 버전을 준다.
    (이 MST·시행일을 fetch_law_body 의 eflaw 조회 키로 그대로 사용)
    """
    url = f"{BASE_URL}/lawSearch.do"
    params = {
        "OC": OC,
        "target": "eflaw",
        "type": "JSON",
        "nw": "3",                 # 현행(오늘 시행 중)만
        "query": law_name,
    }

    data = get_json(url, params)
    if not data:
        return {}

    if SAVE_RAW:
        save_json(os.path.join(RAW_DIR, f"{law_name}_search.json"), data)

    # 응답 구조가 LawSearch.law 형태일 때가 많음
    laws = []
    if isinstance(data, dict):
        law_search = data.get("LawSearch") or data.get("lawSearch") or data
        laws = law_search.get("law") or law_search.get("법령") or []

    laws = listify(laws)

    # 정확히 이름이 맞는 것 우선
    for item in laws:
        item_name = (
            item.get("법령명한글")
            or item.get("법령명")
            or item.get("lawName")
            or item.get("법령명한글내용")
            or ""
        )
        if compact(item_name) == compact(law_name):
            return item

    return laws[0] if laws else {}


def _mst_and_id(law_meta: dict) -> tuple[str, str]:
    mst = (
        law_meta.get("MST")
        or law_meta.get("법령일련번호")
        or law_meta.get("mst")
        or ""
    )
    law_id = (
        law_meta.get("ID")
        or law_meta.get("법령ID")
        or law_meta.get("lawId")
        or ""
    )
    return str(mst), str(law_id)


def _attach_key(params: dict, law_meta: dict, law_name: str) -> dict:
    """MST > ID > 법령명(LM) 순으로 식별자 부착."""
    mst, law_id = _mst_and_id(law_meta)
    if mst:
        params["MST"] = mst
    elif law_id:
        params["ID"] = law_id
    else:
        params["LM"] = law_name
    return params


def fetch_law_body(law_meta: dict, law_name: str) -> dict:
    """
    '오늘 시행 중' 본문 조회 (target=eflaw, 시행일 기준).

    target=eflaw 는 그 시행일(efYd) 시점에 효력 있는 본문만 준다(미래 시행 조문 제외).
    efYd 는 반드시 '그 법의 현행 시행일'(존재하는 시행일)이어야 한다 — 오늘 날짜처럼
    해당 시행일이 없는 값을 주면 빈 결과가 오므로, 매칭 실패 시 efYd 없이 재시도한다.
    """
    url = f"{BASE_URL}/lawService.do"
    ef_yd = str(law_meta.get("시행일자") or law_meta.get("시행일") or "")
    params = _attach_key({"OC": OC, "target": "eflaw", "type": "JSON"}, law_meta, law_name)
    if ef_yd:
        params["efYd"] = ef_yd

    data = get_json(url, params)

    # efYd 가 실제 시행일과 안 맞아 빈 본문이 오면 efYd 없이 재시도(현행 시행본 반환)
    if "efYd" in params and not first_value_recursive(data or {}, ["조문단위"]):
        params.pop("efYd")
        data = get_json(url, params)

    if not data:
        return {}

    if SAVE_RAW:
        save_json(os.path.join(RAW_DIR, f"{law_name}_body.json"), data)
    return data


def fetch_delegated(law_meta: dict, law_name: str) -> dict:
    """
    위임링크 조회 (target=lsDelegated).

    기존 3단비교(thdCmp)는 시행령/시행규칙만 가져왔지만,
    lsDelegated 는 본문의 모든 위임 하이퍼링크를 한 번에 돌려준다.
      - 시행령 / 시행규칙   (대통령령 / 부령)
      - 위임행정규칙        (고시 / 훈령 / 예규 등)
      - 위임자치법규        (지자체 조례 / 규칙)
    각 항목에 링크텍스트(앵커 단어), 라인텍스트(문장), 조항호목(본문 위치),
    대상 제목, 대상 일련번호까지 들어있다.
    """
    url = f"{BASE_URL}/lawService.do"
    params = _attach_key({"OC": OC, "target": "lsDelegated", "type": "JSON"}, law_meta, law_name)

    data = get_json(url, params)
    if not data:
        return {}

    if SAVE_RAW:
        save_json(os.path.join(RAW_DIR, f"{law_name}_delegated.json"), data)
    return data


# =========================
# 4. 본문 텍스트 추출
# =========================

def extract_article_text(jo: dict) -> str:
    """
    조문단위 1개에서 조문내용 -> 항내용 -> 호내용 -> 목내용 순서로 텍스트를 모은다.

    (이전 PoC는 '조내용' 키를 찾았지만 본문 JSON은 '조문내용/항내용/호내용/목내용'
     체계라서 본문이 비어있었음. 여기서 정상화.)
    """
    parts = []                                   # 조문 텍스트 조각 누적

    if not isinstance(jo, dict):
        return ""

    # 조문/항/호/목 '내용' 은 법령에 따라 문자열·리스트·리스트의 리스트로 와서
    # flatten_text 로 평탄화한 뒤 정규화한다(안 하면 "[['...']]" 가 그대로 박힘).
    head = normalize_text(flatten_text(jo.get("조문내용", "")))  # 조 제목/머리글
    if head:
        parts.append(head)

    for hang in listify(jo.get("항")):            # 항(①②…) 순회
        if not isinstance(hang, dict):
            continue

        hang_text = normalize_text(flatten_text(hang.get("항내용", "")))
        if hang_text:
            parts.append(hang_text)

        for ho in listify(hang.get("호")):        # 호(1.2.…) 순회
            if not isinstance(ho, dict):
                continue

            ho_text = normalize_text(flatten_text(ho.get("호내용", "")))
            if ho_text:
                parts.append(ho_text)

            for mok in listify(ho.get("목")):     # 목(가.나.…) 순회
                if not isinstance(mok, dict):
                    continue
                mok_text = normalize_text(flatten_text(mok.get("목내용", "")))
                if mok_text:
                    parts.append(mok_text)

    return "\n".join(parts)                       # 조문 내 줄들을 \n 으로


# 편/장/절/관 표제 (조문여부 != '조문' 인 구조 헤더). 예: "제1장 총칙"
_CHAPTER_RE = re.compile(r"^제\d+\s*[편장절관]")


def build_articles(body_json: dict) -> list[dict]:
    """
    본문 JSON 을 '조 단위' 리스트로 만든다. (search 팀이 본문을 재파싱하지 않도록)
    각 원소 = { article_no, article_title, chapter, content, relations(빈 배열) }.

    - 실제 조문(조문여부=='조문')만 단위로 만든다.
    - 편/장/절/관 표제(조문여부=='전문' 등)는 단위로 만들지 않고, 직전 '장'을
      기억해 뒤따르는 조문의 chapter 로 붙인다(원문 구조 보존).
    - relations 는 호출부에서 source_article_no 기준으로 채워 넣는다.
    """
    units = listify(first_value_recursive(body_json, ["조문단위"]))

    articles = []
    seen = set()                                  # 중복 조문번호 방지(첫 항목 유지)
    current_chapter = ""

    for jo in units:
        if not isinstance(jo, dict):
            continue

        # 조문 여부 판정: '조문' 이거나(표시 없을 때) 조문제목/항이 있으면 조문으로 본다
        is_article = (jo.get("조문여부") == "조문") or (
            not jo.get("조문여부") and (jo.get("조문제목") or jo.get("항"))
        )

        head = normalize_text(flatten_text(jo.get("조문내용", "")))

        if not is_article:                        # 편/장/절/관 표제면 현재 장 갱신만
            if _CHAPTER_RE.match(head):
                current_chapter = head
            continue

        article_no = article_no_from_fields(jo.get("조문번호", ""), jo.get("조문가지번호", ""))
        if not article_no or article_no in seen:
            continue
        content = extract_article_text(jo)
        if not content:
            continue

        seen.add(article_no)
        articles.append({
            "article_no": article_no,
            "article_title": normalize_text(jo.get("조문제목", "")),
            "chapter": current_chapter,
            "content": content,
            "relations": [],                      # 호출부에서 채움
        })

    return articles


# =========================
# 5. 위임 relations 생성 (lsDelegated)
# =========================

def extract_delegated_articles(delegated_json: dict) -> list[dict]:
    """lsDelegated 응답에서 위임조문정보 리스트 추출."""
    if not delegated_json:
        return []

    root = (
        delegated_json.get("lsDelegated", {}).get("법령")
        if isinstance(delegated_json.get("lsDelegated"), dict)
        else None
    )
    if not isinstance(root, dict):
        root = delegated_json.get("법령", {})

    return listify(root.get("위임조문정보")) if isinstance(root, dict) else []


def normalize_delegation_type(raw, link_text: str, target_title: str) -> str:
    """
    위임구분 값 정규화.
    가끔 ["시행령","시행령"] 처럼 리스트로 오거나 아예 빠져 있어서 방어 처리.
    빠진 경우 링크텍스트/제목으로 시행령 vs 시행규칙 추정.
    """
    if isinstance(raw, str) and raw.strip():
        return raw.strip()

    if isinstance(raw, list):
        for x in raw:
            if isinstance(x, str) and x.strip():
                return x.strip()

    title = target_title or ""
    link = link_text or ""

    if title.endswith("시행규칙") or "부령" in link:
        return "시행규칙"
    if title.endswith("시행령") or "대통령령" in link:
        return "시행령"
    return "시행령"


def _source_article_no(article_info: dict) -> str:
    """조정보(이 법령의 조문)에서 제N조(의M) 생성."""
    if not isinstance(article_info, dict):
        return ""
    return article_no_from_fields(
        article_info.get("조문번호", ""),
        article_info.get("조문가지번호", ""),
    )


def _rel_from_law_target(wi: dict, source_article_no: str) -> list[dict]:
    """위임법령(시행령/시행규칙) -> relations.

    한 위임블록(wi)의 '위임법령제목'·'위임법령일련번호'가 한 조문이 여러 규칙에
    위임할 때 **리스트**로 온다(예: 같은 규칙의 현행/구버전, 서로 다른 규칙 여럿).
    그대로 str() 하면 "['규칙A','규칙B']" 로 뭉개지므로, 제목 기준으로 분리해
    (제목, MST) 쌍마다 relation 을 만든다.
    """
    raw_type = wi.get("위임구분")

    # (제목, MST) 쌍 — 제목 기준 중복 제거(같은 규칙이 중복으로 오는 경우), 첫 MST 유지
    titles = listify(wi.get("위임법령제목"))
    msts = listify(wi.get("위임법령일련번호"))
    pairs = []
    seen = set()
    for i, t in enumerate(titles):
        t = normalize_text(t)
        if not t or t in seen:               # 빈 제목/중복 제목은 건너뜀
            continue
        seen.add(t)
        pairs.append((t, str(msts[i]) if i < len(msts) else ""))

    # 대상 규칙이 둘 이상이면 어느 규칙의 몇 조인지 귀속이 모호 → 법령단위 링크로 둔다
    multi = len(pairs) > 1

    out = []
    for jo in listify(wi.get("위임법령조문정보")):
        if not isinstance(jo, dict):
            continue

        link_text = normalize_text(jo.get("링크텍스트", ""))
        article_no = article_no_from_fields(
            jo.get("위임법령조문번호", ""),
            jo.get("위임법령조문가지번호", ""),
        )

        for title, target_mst in pairs:
            target_article_no = "" if multi else article_no
            out.append({
                "relation_type": "delegation",
                "delegation_type": normalize_delegation_type(raw_type, link_text, title),
                "target_category": "법령",
                "source_article_no": source_article_no,
                "source_clause": normalize_text(jo.get("조항호목", "")),
                "link_text": link_text,
                "line_text": normalize_text(jo.get("라인텍스트", "")),
                "target_law_name": title,
                "target_article_no": target_article_no,
                "target_article_title": "" if multi else normalize_text(jo.get("위임법령조문제목", "")),
                "target_mst": target_mst,
                "target_url": build_ref_url("법령", title, target_article_no or None),
                "resolve_method": "lsDelegated",
            })
    return out


def _rel_from_named_target(wi: dict, source_article_no: str,
                           info_key: str, title_key: str, mst_key: str,
                           deleg_type: str, category: str) -> list[dict]:
    """위임행정규칙 / 위임자치법규처럼 '대상 제목 + 일련번호'만 있는 경우."""
    out = []
    for info in listify(wi.get(info_key)):
        if not isinstance(info, dict):
            continue

        title = normalize_text(info.get(title_key, ""))
        if not title:
            continue

        out.append({
            "relation_type": "delegation",
            "delegation_type": deleg_type,
            "target_category": category,
            "source_article_no": source_article_no,
            "source_clause": normalize_text(info.get("조항호목", "")),
            "link_text": normalize_text(info.get("링크텍스트", "")),
            "line_text": normalize_text(info.get("라인텍스트", "")),
            "target_law_name": title,
            "target_article_no": "",  # 대상 문서의 조문번호는 제공되지 않음 -> 문서 단위 링크
            "target_article_title": "",
            "target_mst": str(info.get(mst_key, "") or ""),
            "target_url": build_ref_url(category, title),
            "resolve_method": "lsDelegated",
        })
    return out


def build_delegation_relations(delegated_json: dict) -> list[dict]:
    """lsDelegated 결과 전체를 relations 리스트로 변환."""
    relations = []

    for article in extract_delegated_articles(delegated_json):
        if not isinstance(article, dict):
            continue

        source_article_no = _source_article_no(article.get("조정보", {}))

        for wi in listify(article.get("위임정보")):
            if not isinstance(wi, dict):
                continue

            # 어떤 종류의 위임인지 키 존재로 판별 (위임구분이 없거나 리스트인 경우 대비)
            if "위임법령조문정보" in wi or "위임법령제목" in wi:
                relations.extend(_rel_from_law_target(wi, source_article_no))

            if "위임행정규칙조문정보" in wi:
                relations.extend(_rel_from_named_target(
                    wi, source_article_no,
                    info_key="위임행정규칙조문정보",
                    title_key="위임행정규칙제목",
                    mst_key="위임행정규칙일련번호",
                    deleg_type="위임행정규칙",
                    category="행정규칙",
                ))

            if "위임자치법규조문정보" in wi:
                relations.extend(_rel_from_named_target(
                    wi, source_article_no,
                    info_key="위임자치법규조문정보",
                    title_key="위임자치법규제목",
                    mst_key="위임자치법규일련번호",
                    deleg_type="위임자치법규",
                    category="자치법규",
                ))

    return relations


# =========================
# 6. 인용 relations 생성 (본문 텍스트 파싱)
# =========================

# 「」 안이 법령명 형태인지(별표·서식 등 제외). 숫자로 시작해도 '…법률'이면 법령.
_LAW_NAME_SUFFIX = re.compile(
    r"(법|법률|령|규칙|조례|규정|준칙|협정|특례|헌장|예규|훈령|고시|지침|약관|조약)$"
)

# 본문 토큰: 「법령명」  또는  제N조(의M)
_CITATION_TOKEN = re.compile(r"「([^」]+)」|(제\d+조(?:의\d+)?)")

# 항번호 원문자 ① ~ ⑳ -> 1 ~ 20
_CIRCLED_NUM = {chr(0x2460 + i): i + 1 for i in range(20)}

# 외부 법령 컨텍스트를 '닫는' 표지. 「외부법」 제N조 뒤에 이런 절 경계가 오면
# 그 다음 맨 제N조는 더 이상 외부법 조문이 아니라 자기 법령 참조로 본다.
# (예: "「아동복지법」 제15조…에 따라 … 경우에는 제8조" 의 제8조 = 자기 법령)
# 반면 "「형법」 제2편…살인의 죄 중 제250조, 제252조" 같은 조문 나열은 안 닫음.
_REF_CONTEXT_CLOSER = re.compile(
    r"에\s*따라|에\s*따른|에\s*의하여|에\s*의한|에서|경우에|을\s*준용|를\s*준용|에\s*규정"
)


def _hang_no(hang_label) -> int | None:
    label = str(hang_label or "").strip()
    return _CIRCLED_NUM.get(label[:1]) if label else None


# 호/목 '내용' 맨 앞의 번호 라벨. API의 호번호 필드는 가지번호(1의2,4의2)를 안 주고
# base 만 주는 경우가 많아( '4의2' 도 '4.' 로 옴 ), 실제 가지번호는 내용 앞에서 파싱한다.
_HO_PREFIX = re.compile(r"^\s*(\d+(?:의\d+)?)\s*\.")
_MOK_PREFIX = re.compile(r"^\s*([가-힣])\s*\.")


def _ho_label(ho_no) -> str:
    """호번호 '4.' -> '제4호', '4의2.' -> '제4호의2'."""
    s = str(ho_no or "").strip().rstrip(".").strip()
    if not s:
        return ""
    if "의" in s:
        base, _, branch = s.partition("의")
        return f"제{base.strip()}호의{branch.strip()}"
    return f"제{s}호"


def _ho_label_of(ho: dict) -> str:
    """호 단위의 라벨. 내용 앞('4의2.')을 우선 파싱하고, 없으면 호번호 필드로 폴백."""
    m = _HO_PREFIX.match(normalize_text(flatten_text(ho.get("호내용", ""))))
    return _ho_label(m.group(1) if m else ho.get("호번호", ""))


def _mok_label(mok_no) -> str:
    """목번호 '가.' -> '가목'."""
    s = str(mok_no or "").strip().rstrip(".").strip()
    return f"{s}목" if s else ""


def _mok_label_of(mok: dict) -> str:
    """목 단위의 라벨. 내용 앞('가.')을 우선 파싱하고, 없으면 목번호 필드로 폴백."""
    m = _MOK_PREFIX.match(normalize_text(flatten_text(mok.get("목내용", ""))))
    return _mok_label(m.group(1) if m else mok.get("목번호", ""))


# 조항호목 문자열에서 (항, 호, 호가지, 목) 순번을 뽑아 정렬용 튜플로.
_MOK_ORDER = {chr(c): i for i, c in enumerate(range(ord("가"), ord("힣") + 1))}


def clause_sort_key(article_no: str, rel: dict) -> tuple:
    """
    한 조(條) 안에서 relation 을 '본문 등장 순서(항→호→목)'로 정렬하기 위한 키.
    source_clause(예: '제2조제1항제4호의2', 위임의 '제2조제10호')에서 항/호/목 순번 파싱.
    파싱 안 되는 부분은 0(=앞쪽)으로 둬서 조 머리글·항 없는 항목이 먼저 오게 한다.
    """
    clause = rel.get("source_clause") or article_no or ""
    hang = re.search(r"제(\d+)항", clause)
    ho = re.search(r"제(\d+)호(?:의(\d+))?", clause)
    mok = re.search(r"([가-힣])목", clause)
    return (
        int(hang.group(1)) if hang else 0,
        int(ho.group(1)) if ho else 0,
        int(ho.group(2)) if (ho and ho.group(2)) else 0,
        _MOK_ORDER.get(mok.group(1), 0) if mok else 0,
    )


def iter_citation_lines(body_json: dict):
    """
    본문 JSON 을 (출처조문, 출처조항호목, 라인텍스트) 단위로 펼친다.
    인용이 본문 '어디(몇 조 몇 항 몇 호)'에 있는지 호·목 단위까지 추적한다.
    """
    for jo in listify(first_value_recursive(body_json, ["조문단위"])):
        if not isinstance(jo, dict):
            continue

        art = article_no_from_fields(jo.get("조문번호", ""), jo.get("조문가지번호", ""))

        head = normalize_text(flatten_text(jo.get("조문내용", "")))
        if head:
            yield art, art, head  # 조문 본문(제목+제1항 inline 등)

        for hang in listify(jo.get("항")):
            if not isinstance(hang, dict):
                continue
            hno = _hang_no(hang.get("항번호", ""))
            hang_clause = f"{art}제{hno}항" if (art and hno) else art

            ht = normalize_text(flatten_text(hang.get("항내용", "")))
            if ht:
                yield art, hang_clause, ht

            for ho in listify(hang.get("호")):
                if not isinstance(ho, dict):
                    continue
                ho_clause = hang_clause + _ho_label_of(ho)   # 호 단위(가지번호 포함)
                hot = normalize_text(flatten_text(ho.get("호내용", "")))
                if hot:
                    yield art, ho_clause, hot
                for mok in listify(ho.get("목")):
                    if isinstance(mok, dict):
                        mok_clause = ho_clause + _mok_label_of(mok)
                        mt = normalize_text(flatten_text(mok.get("목내용", "")))
                        if mt:
                            yield art, mok_clause, mt


def build_citation_relations(body_json: dict, self_law_name: str | None = None) -> list[dict]:
    """
    본문에서 다른 법령을 '인용'하는 하이퍼링크 + 자기 법령 내부참조를 추출한다.
    (위임이 아니라 인용/참조. lsDelegated 에는 안 들어옴.)

    - 법령명 컨텍스트 이어받기: "「형법」 … 제250조, 제252조 …" 처럼 「형법」 뒤의
      제N조들도 모두 형법 조문 인용(relation_type="citation")으로 잡는다.
    - 자기참조: 라인에 「」 외부법 맥락이 없는 맨 제N조는 자기 법령(self_law_name)의
      조문 참조(relation_type="internal_ref")로 잡는다. 예) "제11조에 따른 위원회".
      단, 자기 자신 조문(같은 조)을 가리키는 자기루프는 제외.
    - 출처 위치 기록: 참조가 들어있는 조문(source_article_no)·항(source_clause)·
      문장(line_text)을 함께 저장한다.
    """
    relations = []
    seen = set()

    def add(law_name, article_no, link_text, src_art, src_clause, line, rel_type):
        law_name = normalize_text(law_name)
        if not _LAW_NAME_SUFFIX.search(law_name):
            return
        key = (rel_type, law_name, article_no, src_art)
        if key in seen:
            return
        seen.add(key)
        relations.append({
            "relation_type": rel_type,
            "delegation_type": None,
            "target_category": "법령",
            "source_article_no": src_art,
            "source_clause": src_clause,
            "link_text": link_text,
            "line_text": line,
            "target_law_name": law_name,
            "target_article_no": article_no,
            "target_article_title": "",
            "target_mst": "",
            "target_url": build_ref_url("법령", law_name, article_no or None),
            "resolve_method": "text_parse",
        })

    for src_art, src_clause, line in iter_citation_lines(body_json):
        current_law = None
        prev_end = None  # current_law 로 묶이는 직전 토큰의 끝 위치
        matches = list(_CITATION_TOKEN.finditer(line))

        for idx, m in enumerate(matches):
            if m.group(1) is not None:
                # 「법령명」 토큰
                name = m.group(1).strip()
                if not _LAW_NAME_SUFFIX.search(normalize_text(name)):
                    continue  # 별표/서식 등은 컨텍스트로 안 삼음
                current_law = name
                prev_end = m.end()

                # 바로 뒤(공백만 사이)에 제N조가 붙어 있으면 그 조문 핸들러가 처리
                nxt = matches[idx + 1] if idx + 1 < len(matches) else None
                adjacent_article = (
                    nxt is not None
                    and nxt.group(2) is not None
                    and line[m.end():nxt.start()].strip() == ""
                )
                if not adjacent_article:
                    add(name, "", f"「{name}」", src_art, src_clause, line, "citation")
            else:
                # 제N조 토큰
                article_no = m.group(2)

                # 외부 법령명 이후 '에 따라/경우에는' 같은 절 경계가 끼면 컨텍스트 만료.
                # 단, 괄호 안 설명(예: 형법 "(업무상위력 등에 의한 간음)")은 제외하고 판정.
                # 또한 '같은 법'은 직전 외부법을 그대로 가리키므로 만료하지 않는다.
                # (예: "「방송법」 제2조제3호에 따른 … 같은 법 제73조" 의 제73조 = 방송법)
                if current_law and prev_end is not None:
                    gap = re.sub(r"\([^)]*\)", "", line[prev_end:m.start()])
                    if not re.search(r"같은\s*법", gap) and _REF_CONTEXT_CLOSER.search(gap):
                        current_law = None

                if current_law:
                    # 직전에 외부 법령명이 있으면 그 법령의 조문 인용
                    add(current_law, article_no, f"「{current_law}」 {article_no}",
                        src_art, src_clause, line, "citation")
                elif self_law_name and article_no != src_art:
                    # 외부법 맥락이 없으면 자기 법령 내부참조 (자기루프는 제외)
                    add(self_law_name, article_no, article_no,
                        src_art, src_clause, line, "internal_ref")

                prev_end = m.end()

    # 법령단위 인용(조문번호 없음)은, 같은 법령의 조문단위 인용이 이미 있으면 제거.
    # (예: 「형법」 + 형법/제250조 가 같이 있으면 중복이므로 「형법」 단독은 뺀다)
    laws_with_article = {r["target_law_name"] for r in relations if r["target_article_no"]}
    relations = [
        r for r in relations
        if not (r["target_article_no"] == "" and r["target_law_name"] in laws_with_article)
    ]

    return relations


def split_ordinance_groups(relations: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    위임자치법규(조례)를 본문 하이퍼링크 단위(= 출처 조문 단위)로 묶는다.

    본문에는 조문마다 '조례로' 같은 하이퍼링크가 1개 있고, 클릭하면 그 위임을
    이행하는 전국 지자체 조례 수백 건이 팝업으로 뜬다. 이를 수백 개의 flat
    relation 으로 펼치면 노이즈가 커서, 출처 조문별로 묶고 개별 조례는 각자의
    캐노니컬 링크를 가진 리스트로 보존한다.

    반환: (조례를 제외한 flat relations, 조례 그룹 리스트)
    """
    flat = []
    groups: dict[str, dict] = {}

    for rel in relations:
        if rel["delegation_type"] != "위임자치법규":
            flat.append(rel)
            continue

        key = rel["source_article_no"]
        group = groups.setdefault(key, {
            "relation_type": "delegation",
            "delegation_type": "위임자치법규",
            "target_category": "자치법규",
            "source_article_no": rel["source_article_no"],
            "link_text": rel["link_text"],
            "ordinance_count": 0,
            "ordinances": [],
            "resolve_method": "lsDelegated",
        })

        # 그룹 내 조례 제목 중복 방지
        if rel["target_law_name"] not in {o["target_law_name"] for o in group["ordinances"]}:
            group["ordinances"].append({
                "target_law_name": rel["target_law_name"],
                "target_mst": rel["target_mst"],
                "target_url": rel["target_url"],
            })

    for group in groups.values():
        group["ordinance_count"] = len(group["ordinances"])

    return flat, list(groups.values())


def split_ordinance_lookup(relations: list[dict], law_name: str) -> tuple[list[dict], list[dict]]:
    """
    [조례 버전 B - 조회용 링크만]

    개별 지자체 조례를 전부 펼치지 않고, 출처 조문별로 '조회 링크' 1개만 둔다.
    조회 링크는 본문에서 '조례로' 하이퍼링크가 실제로 걸려 있는 그 조문
    (= law.go.kr/법령/{법령}/{제N조}) 으로, 클릭하면 해당 위임 조례 목록 팝업이 뜬다.
    조례 건수(ordinance_count)만 참고용으로 남긴다.

    반환: (조례를 제외한 flat relations, 조례 조회 링크 리스트)
    """
    flat = []
    groups: dict[str, dict] = {}

    for rel in relations:
        if rel["delegation_type"] != "위임자치법규":
            flat.append(rel)
            continue

        key = rel["source_article_no"]
        group = groups.setdefault(key, {
            "relation_type": "delegation",
            "delegation_type": "위임자치법규",
            "target_category": "자치법규",
            "source_article_no": rel["source_article_no"],
            "link_text": rel["link_text"],
            "ordinance_count": 0,
            "_titles": set(),
            "lookup_url": build_ref_url("법령", law_name, rel["source_article_no"] or None),
            "resolve_method": "lsDelegated",
        })
        group["_titles"].add(rel["target_law_name"])

    out_groups = []
    for group in groups.values():
        group["ordinance_count"] = len(group.pop("_titles"))
        out_groups.append(group)

    return flat, out_groups


def dedupe_relations(relations: list[dict]) -> list[dict]:
    """
    같은 '참조 엣지' 중복 제거.

    dedup 기준에서 link_text(앵커 단어)는 제외한다.
    같은 조문이 같은 대상으로 위임/인용하면서 표현만 다른 경우
    (예: '조례로 정한다' vs '조례로 정하는')를 하나로 합치기 위함.
    link_text 자체는 첫 항목 값으로 보존된다.
    대상 URL이 조(條) 단위까지만 구분되므로 항/호 차이도 동일 엣지로 본다.
    """
    out = []
    seen = set()
    for rel in relations:
        key = (
            rel["relation_type"],
            rel["target_category"],
            rel["target_law_name"],
            rel["target_article_no"],
            rel["source_article_no"],
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(rel)
    return out


# =========================
# 7. payload 생성
# =========================

def build_payload(law_name: str, law_meta: dict | None = None) -> dict:
    """
    법령 1건의 전체 payload 조립 (수집 코어의 진입점).

    law_meta:
      - None  → 이름으로 직접 검색(search_law). '검증/단독 실행' 경로.
      - dict  → catalog 가 이미 받아둔 메타(MST·시행일 등)를 재사용. '파이프라인' 경로
                (법마다 search 를 또 호출하는 중복 제거).
    """
    logger.info("[START] %s", law_name)

    if law_meta is None:                          # ① (검증/단독) 이름으로 검색
        law_meta = search_law(law_name)

    body_json = fetch_law_body(law_meta, law_name)        # ② 본문 (JSON)
    delegated_json = fetch_delegated(law_meta, law_name)  # ③ 위임링크 (lsDelegated)

    articles = build_articles(body_json)          # ④ 본문 → 조 단위 리스트

    law_id = (
        law_meta.get("ID")
        or law_meta.get("법령ID")
        or first_value_recursive(body_json, ["법령ID", "ID"])
        or ""
    )

    mst = (
        law_meta.get("MST")
        or law_meta.get("법령일련번호")
        or first_value_recursive(body_json, ["법령일련번호", "MST"])
        or ""
    )

    # ── 수집 실패 가드: 빈 payload 를 조용히 'done' 으로 저장하지 않는다 ──
    # (검색·본문 조회가 일시 실패하면 여기서 raise → 파이프라인이 재시도/실패기록)
    if not str(mst) and not str(law_id):
        raise CollectError(f"법령 메타 조회 실패(검색 결과 없음): {law_name}")
    if not articles:
        raise CollectError(f"본문 조회 실패(조문 0건): {law_name} (mst={mst})")

    law_type = (
        law_meta.get("법령구분명")
        or law_meta.get("법령구분")
        or first_value_recursive(body_json, ["법령구분명", "법령구분"])
        or ""
    )

    promulgation_no = (
        law_meta.get("공포번호")
        or first_value_recursive(body_json, ["공포번호"])
        or ""
    )

    promulgation_date = (
        law_meta.get("공포일자")
        or first_value_recursive(body_json, ["공포일자"])
        or ""
    )

    enforcement_date = (
        law_meta.get("시행일자")
        or first_value_recursive(body_json, ["시행일자"])
        or ""
    )

    revision_date = (
        law_meta.get("개정일자")
        or law_meta.get("공포일자")
        or promulgation_date
        or ""
    )

    # ⑤ 관계 추출: 위임(API) + 인용/자기참조(텍스트) + 정관(렌더링)
    delegation_relations = build_delegation_relations(delegated_json)
    citation_relations = build_citation_relations(body_json, self_law_name=law_name)

    # 학칙공단(정관 등): lsDelegated 에 안 잡히므로 본문 렌더링으로 보강
    dom = render.render_law_dom(str(mst), str(enforcement_date))
    schlpub_relations = render.build_schlpub_relations(dom, build_ref_url)

    # ⑥ 합치고 중복 제거(같은 출처조문→같은 대상조문은 1건)
    all_relations = dedupe_relations(
        delegation_relations + citation_relations + schlpub_relations
    )

    # ── 조례(위임자치법규) 표현 방식: 아래 두 버전 중 하나만 사용 ──────────────
    # [버전 A · 다 보존] 조문별로 묶되 개별 조례를 각자의 캐노니컬 링크로 모두 보존
    relations, ordinance_delegations = split_ordinance_groups(all_relations)

    # [버전 B · 조회용 링크만] 조문별 조회 링크 1개 + 건수만 (개별 조례 미포함)
    # relations, ordinance_delegations = split_ordinance_lookup(all_relations, law_name)
    # ─────────────────────────────────────────────────────────────────────────

    # 출처 조문 오름차순 정렬 (제1조 -> 제2조 -> … -> 제15조의2 …)
    relations.sort(key=lambda r: article_sort_key(r.get("source_article_no", "")))
    ordinance_delegations.sort(key=lambda r: article_sort_key(r.get("source_article_no", "")))

    # ⑦ relations 를 '조 단위'로 그룹핑해 각 조 안에 넣는다.
    #    (조례=위임자치법규는 한 조에 수백 건이라 노이즈 → body 밖 별도 배열로 유지)
    by_src: dict[str, list] = {}
    for rel in relations:
        by_src.setdefault(rel.get("source_article_no", ""), []).append(rel)
    for art in articles:
        rels = by_src.pop(art["article_no"], [])
        # 조 안에서는 '본문 등장 순서(항→호→목)'로 정렬 (타입순 X)
        rels.sort(key=lambda r: clause_sort_key(art["article_no"], r))
        art["relations"] = rels
    # 어느 조에도 매칭되지 않은 relations(출처조문 미상 등) — 보통 비어 있음
    unmatched = [r for rs in by_src.values() for r in rs]

    # 카테고리별 / 관계유형별 카운트 (검증/요약용)
    by_category = {}
    by_relation_type = {}
    for rel in relations:
        by_category[rel["target_category"]] = by_category.get(rel["target_category"], 0) + 1
        by_relation_type[rel["relation_type"]] = by_relation_type.get(rel["relation_type"], 0) + 1

    ordinance_doc_total = sum(g["ordinance_count"] for g in ordinance_delegations)

    body = {
        "format": "articles",                     # 조 단위 구조 (이전: 단일 텍스트)
        "article_count": len(articles),
        "articles": articles,                     # [{article_no, article_title, chapter, content, relations[]}]
    }
    if unmatched:                                 # 매칭 실패분이 있으면 별도 보존
        body["unmatched_relations"] = unmatched

    payload = {
        "law_id": str(law_id),
        "mst": str(mst),
        "law_name": law_name,
        "law_type": law_type,
        "promulgation_no": str(promulgation_no),
        "promulgation_date": str(promulgation_date),
        "enforcement_date": str(enforcement_date),
        "revision_date": str(revision_date),
        "is_current": True,

        "body": body,
        "ordinance_delegations": ordinance_delegations,   # 조례: 조별 묶음(별도 배열)
        "relation_stats": {
            "relations_total": len(relations),
            "by_category": by_category,
            "by_relation_type": by_relation_type,
            "articles_total": len(articles),
            "articles_with_relations": sum(1 for a in articles if a["relations"]),
            "unmatched_relations": len(unmatched),
            "ordinance_groups": len(ordinance_delegations),
            "ordinance_docs_total": ordinance_doc_total,
        },

        "source": {
            "source_url": build_ref_url("법령", law_name),
            "fetched_at": now_kst(),
        },

        "sync": {
            "sync_reason": "INITIAL_LOAD",
            "synced_at": now_kst(),
        },
    }

    logger.info("[DONE] %s | 조문 %d(관계보유 %d) · relations %d %s 미매칭 %d · 조례 %d조문/%d건",
                law_name, len(articles), payload["relation_stats"]["articles_with_relations"],
                len(relations), by_relation_type, len(unmatched),
                len(ordinance_delegations), ordinance_doc_total)

    if SAVE_PAYLOAD:                              # 옵션: 최종 payload 로컬 저장
        save_json(os.path.join(OUTPUT_DIR, f"{law_name}_payload.json"), payload)

    return payload


def main():
    # CLI 단독 실행 시 로그가 보이도록 기본 설정 (워커는 자체 설정 사용)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")

    if not OC:
        raise SystemExit(
            "환경변수 LAW_API_OC 가 비어 있습니다.\n"
            ".env.example 을 복사해 .env 를 만들고 발급받은 법제처 OC 키를 넣어주세요.\n"
            "  cp .env.example .env   # 그리고 LAW_API_OC 값 입력"
        )

    ensure_dirs()

    payloads, failed = [], []
    for law_name in LAW_NAMES:
        try:
            payload = build_payload(law_name)
        except CollectError as e:                  # 한 건 실패해도 나머지는 계속
            logger.warning("[SKIP] %s: %s", law_name, e)
            failed.append(law_name)
            continue
        payloads.append(payload)
        # CLI 는 파일 산출이 목적이라 개별 payload 도 항상 저장
        save_json(os.path.join(OUTPUT_DIR, f"{law_name}_payload.json"), payload)

    save_json(os.path.join(OUTPUT_DIR, "laws_payload.json"), payloads)
    save_json(os.path.join(OUTPUT_DIR, "state.json"), {
        "total": len(payloads),
        "failed": failed,
        "law_names": LAW_NAMES,
        "synced_at": now_kst(),
        "output_file": "output/laws_payload.json",
    })

    logger.info("[SUCCESS] %d건 저장(실패 %d) → output/laws_payload.json",
                len(payloads), len(failed))


if __name__ == "__main__":
    main()
