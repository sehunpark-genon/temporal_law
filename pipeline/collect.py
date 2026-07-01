"""
수집 어댑터 — 수집 코어(collector.core)를 파이프라인이 쓰기 좋게 감싼다.
순수 함수만 두며 Temporal 에 의존하지 않는다.

핵심:
- discover_catalog : 전체 목록을 1회 받아 법률별 'MST 3개 지문'까지 계산해 돌려준다.
                     (이 지문을 catalog 에 저장 → sync 는 API 없이 catalog 만 비교)
- collect_payload  : 전체 payload 생성 (= core.build_payload)
- content_hash     : payload 내용 해시 (저장 스킵 판정용 보조 값)
"""

import json
import hashlib

from collector import core


def _signature_for(law_name: str, name_to_meta: dict) -> str:
    """
    법령 + 그 시행령 + 시행규칙의 (MST + 시행일) 묶음으로 버전 지문(signature)을 만든다.
    name_to_meta: {법령명: "MST:시행일"} 전체 맵에서 '본 법령 + 시행령/규칙' 만 골라 해시.

    시행일을 함께 해시하는 이유: 개정이 '공포'되면 MST 가 바뀌어 잡히지만, 이미 공포된
    개정이 나중에 '시행'되는 전환(같은 MST, 시행일만 advance)은 MST 만으론 못 잡는다.
    eflaw nw=3 의 시행일은 그 전환 때 advance 하므로, 시행일까지 넣어야 시행 도래도 감지된다.
    (discover_catalog/단건 어디서 호출하든 같은 값이 나오도록 한 곳에 모음)
    """
    target = core.compact(law_name)
    docs = {
        nm: meta for nm, meta in name_to_meta.items()
        if core.compact(nm) == target or core.compact(nm).startswith(target + "시행")
    }
    src = json.dumps({k: docs[k] for k in sorted(docs)}, ensure_ascii=False)
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]


def _fetch_all_laws(page_size: int = 100) -> list[dict]:
    """전체 '현행 시행 중' 목록(법률+시행령+시행규칙+…)을 페이지네이션으로 모은다.
    target=eflaw + nw=3 = 오늘 시행 중 버전(법-단위 시행일 정확)."""
    out, page = [], 1
    while True:
        data = core.get_json(f"{core.BASE_URL}/lawSearch.do", {
            "OC": core.OC, "target": "eflaw", "type": "JSON", "nw": "3",
            "display": str(page_size), "page": str(page),
        })
        if not data:
            break
        search = data.get("LawSearch", {})
        laws = core.listify(search.get("law") or [])
        for it in laws:
            out.append({
                "law_id": str(it.get("법령ID", "") or ""),
                "mst": str(it.get("법령일련번호", "") or ""),
                "law_name": (it.get("법령명한글") or "").strip(),
                "law_type": (it.get("법령구분명") or "").strip(),
                "ministry": (it.get("소관부처명") or "").strip(),
                "enforcement_date": str(it.get("시행일자", "") or ""),
                "promulgation_date": str(it.get("공포일자", "") or ""),
                "detail_link": (it.get("법령상세링크") or "").strip(),
            })
        total = int(search.get("totalCnt", 0) or 0)
        if page * page_size >= total or not laws:
            break
        page += 1
    return out


def discover_catalog(law_only: bool = True) -> list[dict]:
    """
    전체 목록 1회 조회 → 추적 단위(법률)만 추리되, 각 법률의 version_signature
    (법률+시행령+시행규칙 MST 지문)를 같은 목록에서 계산해 함께 반환.
    """
    rows_all = _fetch_all_laws()
    # 지문 계산용 전체 {법령명: "MST:시행일"} 맵 (시행령/규칙 포함해야 묶을 수 있음)
    name_to_meta = {r["law_name"]: f'{r["mst"]}:{r["enforcement_date"]}'
                    for r in rows_all if r["law_name"]}

    out = []
    for r in rows_all:
        if law_only and r["law_type"] != "법률":     # 추적 단위는 법률 (확장 시 False)
            continue
        entry = dict(r)
        entry["version_signature"] = _signature_for(r["law_name"], name_to_meta)
        out.append(entry)
    return out


def discover_versions(law_name: str) -> dict:
    """(단건용) 한 법령의 시행일 지문만 eflaw 로 조회. 카탈로그 흐름에선 안 씀."""
    data = core.get_json(f"{core.BASE_URL}/lawSearch.do", {
        "OC": core.OC, "target": "eflaw", "type": "JSON", "nw": "3", "query": law_name,
    })
    laws = core.listify((data or {}).get("LawSearch", {}).get("law") or [])
    name_to_meta = {(it.get("법령명한글") or "").strip():
                    f'{it.get("법령일련번호", "") or ""}:{it.get("시행일자", "") or ""}'
                    for it in laws}
    return {"signature": _signature_for(law_name, name_to_meta)}


def collect_payload(law_name: str, law_meta: dict | None = None) -> dict:
    """전체 payload 생성 (본문+위임+인용+자기참조+조례+정관).
    law_meta 가 주어지면 단건 search 를 생략하고 catalog 메타를 재사용한다."""
    return core.build_payload(law_name, law_meta)


def catalog_meta(row: dict) -> dict:
    """catalog row(db.read_catalog 결과) → core.build_payload 가 쓰는 메타 dict 형태로 변환."""
    return {
        "법령일련번호": row.get("mst", ""),
        "법령ID": row.get("law_id", ""),
        "시행일자": row.get("enforcement_date", ""),
        "공포일자": row.get("promulgation_date", ""),
        "법령구분명": row.get("law_type", ""),
    }


def content_hash(payload: dict) -> str:
    """타임스탬프를 제외한 '내용'만 해시 (DB 저장 스킵 판정용)."""
    body = payload.get("body", {})
    content = {
        # 조 단위 본문 + 각 조의 relations (조문순서·내용·관계가 바뀌면 해시 변화)
        "articles": [
            {"no": a.get("article_no"), "content": a.get("content"),
             "relations": a.get("relations", [])}
            for a in body.get("articles", [])
        ],
        "unmatched": body.get("unmatched_relations", []),
        "ordinance_delegations": payload.get("ordinance_delegations", []),
    }
    blob = json.dumps(content, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
