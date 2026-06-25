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


def _signature_for(law_name: str, name_to_mst: dict) -> str:
    """
    법령 + 그 시행령 + 시행규칙의 MST 묶음으로 버전 지문(signature)을 만든다.
    name_to_mst: {법령명: MST} 전체 맵에서 '본 법령 + 시행령/규칙' 만 골라 해시.
    (discover_catalog/단건 어디서 호출하든 같은 값이 나오도록 한 곳에 모음)
    """
    target = core.compact(law_name)
    docs = {
        nm: mst for nm, mst in name_to_mst.items()
        if core.compact(nm) == target or core.compact(nm).startswith(target + "시행")
    }
    src = json.dumps({k: docs[k] for k in sorted(docs)}, ensure_ascii=False)
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]


def _fetch_all_laws(page_size: int = 100) -> list[dict]:
    """전체 현행법령 목록(법률+시행령+시행규칙+…)을 페이지네이션으로 모은다."""
    out, page = [], 1
    while True:
        data = core.get_json(f"{core.BASE_URL}/lawSearch.do", {
            "OC": core.OC, "target": "law", "type": "JSON",
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
    # 지문 계산용 전체 {법령명: MST} 맵 (시행령/규칙 포함해야 묶을 수 있음)
    name_to_mst = {r["law_name"]: r["mst"] for r in rows_all if r["law_name"]}

    out = []
    for r in rows_all:
        if law_only and r["law_type"] != "법률":     # 추적 단위는 법률 (확장 시 False)
            continue
        entry = dict(r)
        entry["version_signature"] = _signature_for(r["law_name"], name_to_mst)
        out.append(entry)
    return out


def discover_versions(law_name: str) -> dict:
    """(단건용) 한 법령의 MST 지문만 lawSearch 로 조회. 카탈로그 흐름에선 안 씀."""
    data = core.get_json(f"{core.BASE_URL}/lawSearch.do", {
        "OC": core.OC, "target": "law", "type": "JSON", "query": law_name,
    })
    laws = core.listify((data or {}).get("LawSearch", {}).get("law") or [])
    name_to_mst = {(it.get("법령명한글") or "").strip(): str(it.get("법령일련번호", "") or "")
                   for it in laws}
    return {"signature": _signature_for(law_name, name_to_mst)}


def collect_payload(law_name: str) -> dict:
    """전체 payload 생성 (본문+위임+인용+자기참조+조례+정관)."""
    return core.build_payload(law_name)


def content_hash(payload: dict) -> str:
    """타임스탬프를 제외한 '내용'만 해시 (DB 저장 스킵 판정용)."""
    content = {
        "body": payload.get("body", {}).get("content", ""),
        "relations": payload.get("relations", []),
        "ordinance_delegations": payload.get("ordinance_delegations", []),
    }
    blob = json.dumps(content, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
