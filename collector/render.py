"""
법령 본문 렌더링 & 하이퍼링크 추출 모듈.

법제처 본문 페이지(lsInfoP.do)는 EDotXPress 가 클라이언트(JS)에서 조문을
렌더링하면서 하이퍼링크를 생성한다. 그래서 서버 HTML 에는 <a> 가 없고,
실제 브라우저 DOM 에만 링크가 존재한다.

이 모듈은 설치되어 있는 Chrome 을 headless 로 띄워 최종 DOM 을 덤프하고,
본문 앵커를 전부 추출/분류한다.

분류(본문 링크 onclick 기준):
  fncLsLawPop(lsId, scope, ...)                      -> 법령참조 (다른 법령/자기 조문 인용)
  fncLsPttnLinkPop(pttnId)                           -> 시행령/시행규칙 위임(대통령령·부령)
  joDelegatePop(lsiSeq, joNo, joBrNo, '010102', ...) -> 위임행정규칙
  joDelegatePop(lsiSeq, joNo, joBrNo, '010113', ...) -> 학칙공단(정관 등)
  joDelegateOrdinPop(lsiSeq, lsId, joNo, joBrNo, '010103', ...) -> 위임자치법규(조례)

lsDelegated API 가 시행령/시행규칙/행정규칙/자치법규를 모두 커버하므로,
main 파이프라인에서 이 모듈은 'API 로 못 받는' 학칙공단(정관) 보강 + 커버리지
검증의 ground-truth 용도로 쓴다.
"""

import re
import shutil
import subprocess
from urllib.parse import quote

import requests


LSW = "http://www.law.go.kr/LSW"

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]

# joDelegate* 의 datClsCd -> (카테고리, 팝업 엔드포인트)
DATCLS_MAP = {
    "010102": ("행정규칙", "conAdmrulByLsPop.do"),
    "010113": ("학칙공단", "conSchlPubRulByLsPop.do"),
    "010103": ("자치법규", "ordinJoDelegatedListPop.do"),
}


def find_chrome() -> str | None:
    """설치된 Chrome/Chromium/Edge 실행 경로를 찾는다. 없으면 None."""
    for path in CHROME_CANDIDATES:                # 맥 앱 기본 경로 우선
        if path and shutil.which(path) or (path and _exists(path)):
            return path
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)                # PATH 에서 탐색(리눅스 등)
        if found:
            return found
    return None


def _exists(path: str) -> bool:
    import os
    return os.path.exists(path)


def render_law_dom(mst: str, ef_yd: str = "", virtual_time_ms: int = 20000,
                   timeout: int = 90) -> str:
    """
    Chrome headless 로 법령 본문 DOM 을 렌더링해서 반환.
    Chrome 이 없거나 실패하면 빈 문자열.
    """
    chrome = find_chrome()
    if not chrome:
        print("[WARN] Chrome 을 찾지 못해 본문 렌더링(정관/커버리지)을 건너뜀.")
        return ""

    # 본문 페이지 URL (이 페이지가 JS 로 조문/링크를 그린다)
    url = (
        f"{LSW}/lsInfoP.do?lsiSeq={mst}"
        f"&efYd={ef_yd}&ancYnChk=1&urlMode=lsInfoP&gubun=api"
    )

    cmd = [
        chrome,
        "--headless=new",                                   # 화면 없이 실행
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--disable-dev-shm-usage",
        f"--virtual-time-budget={virtual_time_ms}",         # JS 실행을 이 시간만큼 기다림
        "--run-all-compositor-stages-before-draw",
        "--dump-dom",                                       # JS 실행 끝난 최종 DOM 을 stdout 으로
        url,
    ]

    try:
        out = subprocess.run(
            cmd, capture_output=True, timeout=timeout, text=True, encoding="utf-8"
        )
        dom = out.stdout or ""
        if len(dom) < 5000:
            print(f"[WARN] 렌더링 DOM 이 비정상적으로 짧음(len={len(dom)}). mst={mst}")
        return dom
    except subprocess.TimeoutExpired:
        print(f"[WARN] 렌더링 타임아웃 mst={mst}")
        return ""
    except Exception as e:
        print(f"[WARN] 렌더링 실패 mst={mst} err={e}")
        return ""


# =========================
# 앵커 추출
# =========================

# 본문의 onclick 링크 하나하나: group(1)=onclick 본문, group(2)=화면에 보이는 텍스트
_ANCHOR_RE = re.compile(
    r'<a\b[^>]*onclick="javascript:([^"]*)"[^>]*>(.*?)</a>', re.S
)

# onclick 함수별 인자 파서 (어떤 종류의 링크인지 + 파라미터 추출)
_FN_RES = {
    "fncLsLawPop": re.compile(r"fncLsLawPop\('([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\)"),
    "fncLsPttnLinkPop": re.compile(r"fncLsPttnLinkPop\('([^']*)'\)"),
    "joDelegatePop": re.compile(
        r"joDelegatePop\('([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'"
        r"(?:\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)')?\)"
    ),
    "joDelegateOrdinPop": re.compile(
        r"joDelegateOrdinPop\('([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'"
        r"\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\)"
    ),
}


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).replace("\xa0", " ").strip()


def _article_no(jo_no, jo_br_no) -> str:
    """joNo='0015', joBrNo='04' -> 제15조의4 / joNo='0006' -> 제6조"""
    try:
        base = int(jo_no)
    except (TypeError, ValueError):
        return ""
    branch = str(jo_br_no or "").strip()
    if branch and branch not in ("0", "00"):
        try:
            return f"제{base}조의{int(branch)}"
        except ValueError:
            return f"제{base}조"
    return f"제{base}조"


def extract_body_anchors(dom: str) -> list[dict]:
    """렌더링 DOM 에서 본문 참조 앵커를 분류해서 리스트로 반환."""
    anchors = []
    if not dom:
        return anchors

    for m in _ANCHOR_RE.finditer(dom):           # 본문 모든 onclick 링크 순회
        onclick = m.group(1)
        text = _strip_tags(m.group(2))           # 앵커 표시 텍스트(태그 제거)
        if not text:
            continue

        rec = {"text": text, "onclick": onclick}

        # onclick 함수 종류로 링크 분류 (먼저 매칭되는 것 채택)
        law = _FN_RES["fncLsLawPop"].search(onclick)
        pttn = _FN_RES["fncLsPttnLinkPop"].search(onclick)
        deleg = _FN_RES["joDelegatePop"].search(onclick)
        ordin = _FN_RES["joDelegateOrdinPop"].search(onclick)

        if law:                                  # 법령/조문 참조 링크
            rec.update(fn="fncLsLawPop", category="법령참조",
                       ls_id=law.group(1), scope=law.group(2))
            # 「OO법」 형태면 외부 법령 인용, '제N조/제N항' 이면 자기 조문 내부참조
            rec["ref_kind"] = "external" if text.startswith("「") else "internal"

        elif pttn:
            rec.update(fn="fncLsPttnLinkPop", category="시행령시행규칙",
                       pttn_id=pttn.group(1))

        elif deleg:
            datcls = deleg.group(4)
            cat, endpoint = DATCLS_MAP.get(datcls, ("기타위임", ""))
            rec.update(
                fn="joDelegatePop", category=cat, endpoint=endpoint,
                lsi_seq=deleg.group(1), jo_no=deleg.group(2), jo_br_no=deleg.group(3),
                dat_cls=datcls, dgu_bun=deleg.group(5) or "",
                lnk_text=deleg.group(6) or "", seq=deleg.group(7) or "",
                source_article_no=_article_no(deleg.group(2), deleg.group(3)),
            )

        elif ordin:
            rec.update(
                fn="joDelegateOrdinPop", category="자치법규",
                lsi_seq=ordin.group(1), ls_id=ordin.group(2),
                jo_no=ordin.group(3), jo_br_no=ordin.group(4),
                dat_cls=ordin.group(5), dgu_bun=ordin.group(6),
                lnk_text=ordin.group(7),
                source_article_no=_article_no(ordin.group(3), ordin.group(4)),
            )
        else:
            continue  # 소셜 공유/UI 버튼 등은 제외

        anchors.append(rec)

    return anchors


# =========================
# 학칙공단(정관 등) 제목 해석
# =========================

def resolve_schlpub_title(anchor: dict) -> str:
    """
    joDelegatePop(...,'010113',...) 앵커 -> conSchlPubRulByLsPop.do 호출해서
    대상 문서(정관/학칙)의 제목을 추출.
    """
    try:
        r = requests.get(
            f"{LSW}/conSchlPubRulByLsPop.do",
            params={
                "lsiSeq": anchor.get("lsi_seq", ""),
                "joNo": anchor.get("jo_no", ""),
                "joBrNo": anchor.get("jo_br_no", ""),
                "datClsCd": anchor.get("dat_cls", "010113"),
                "dguBun": anchor.get("dgu_bun", "DEG"),
                "lnkText": anchor.get("lnk_text", ""),
                "joSeq": anchor.get("seq", ""),
            },
            timeout=20,
        )
        r.encoding = "utf-8"
        body = r.text
    except Exception as e:
        print(f"[WARN] 학칙공단 제목 해석 실패: {e}")
        return ""

    # 좌측 목록 첫 항목 <span class="tx">1.  (제목)</span>
    m = re.search(r'<span class="tx">\s*(.*?)\s*</span>', body, re.S)
    if m:
        title = _strip_tags(m.group(1))
        return re.sub(r"^\d+\.\s*", "", title).strip()

    return ""


def count_delegated_docs(anchor: dict) -> int:
    """
    행정규칙/자치법규 위임 앵커가 실제로 가리키는 문서 수를 센다.

    본문에는 위임 '문구'만 있으면 링크가 걸리지만, 정작 등록된 위임문서가
    0건인 '빈 링크'가 많다(예: '보건복지부장관이 정하는' 인데 고시 미등록).
    커버리지 검증에서 빈 링크를 진짜 누락으로 오판하지 않으려고 사용.
    """
    cat = anchor.get("category")

    # 행정규칙: 정적 HTML 로 목록이 바로 옴 (<span class="tx"> 항목)
    if cat == "행정규칙":
        try:
            r = requests.get(f"{LSW}/conAdmrulByLsPop.do", params={
                "lsiSeq": anchor.get("lsi_seq", ""),
                "joNo": anchor.get("jo_no", ""),
                "joBrNo": anchor.get("jo_br_no", ""),
                "datClsCd": "010102",
                "lnkText": anchor.get("lnk_text", ""),
            }, timeout=20)
            if r.status_code != 200:
                return 0
            r.encoding = "utf-8"
            return len(re.findall(r'<span class="tx">', r.text))
        except Exception:
            return 0

    # 자치법규(조례): 목록이 JS(AJAX)로 로드돼서 렌더링 후 title 속성으로 카운트
    if cat == "자치법규":
        lnk = anchor.get("lnk_text", "") or "조례로"
        enc = quote(quote(lnk, safe=""), safe="")  # JS: encodeURI(encodeURIComponent())
        url = (
            f"{LSW}/lumThdCmpJo.do?lsiSeq={anchor.get('lsi_seq','')}"
            f"&joNo={anchor.get('jo_no','')}&joBrNo={anchor.get('jo_br_no','')}"
            f"&datClsCd=010103&dguBun=DEG&lsId={anchor.get('ls_id','')}"
            f"&chrClsCd=010202&gubun=STD&lnkText={enc}"
        )
        chrome = find_chrome()
        if not chrome:
            return 0
        try:
            out = subprocess.run(
                [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--virtual-time-budget=9000", "--dump-dom", url],
                capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
            return len(re.findall(r'title="[^"]*조례[^"]*"', out.stdout or ""))
        except Exception:
            return 0

    return 0


def build_schlpub_relations(dom: str, build_ref_url) -> list[dict]:
    """
    렌더링 DOM 에서 학칙공단(정관) 앵커를 찾아 relations 로 변환.
    build_ref_url 은 main 의 URL 생성기를 주입받는다(중복 구현 방지).
    """
    relations = []
    seen = set()

    for a in extract_body_anchors(dom):
        if a.get("category") != "학칙공단":
            continue

        title = resolve_schlpub_title(a)
        if not title:
            continue

        key = (a.get("source_article_no", ""), title)
        if key in seen:
            continue
        seen.add(key)

        relations.append({
            "relation_type": "delegation",
            "delegation_type": "위임학칙공단",
            "target_category": "학칙공단",
            "source_article_no": a.get("source_article_no", ""),
            "source_clause": "",
            "link_text": a.get("lnk_text", "") or a.get("text", ""),
            "line_text": "",
            "target_law_name": title,
            "target_article_no": "",
            "target_article_title": "",
            "target_mst": "",
            "target_url": build_ref_url("학칙공단", title),
            "resolve_method": "rendered_dom",
        })

    return relations
