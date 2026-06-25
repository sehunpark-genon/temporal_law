"""
커버리지 검증 테스트.

"본문의 모든 하이퍼링크 주소를 가져왔는가?" 를 확인한다.

방법:
  1) 법제처 본문 페이지를 Chrome 으로 렌더링(= 사용자가 브라우저에서 보는 그대로)
     해서 본문 앵커(하이퍼링크)를 전부 추출한다. 이게 'ground truth'.
  2) 수집 코어가 만든 payload(relations + ordinance_delegations) 와 대조한다.
  3) 카테고리별로 빠진 링크가 있는지 리포트한다.
  4) 캡처한 target_url 들이 실제로 살아있는 주소인지 표본 검증한다.

분류:
  - 외부 법령 인용  「OO법」      -> payload citations
  - 시행령/시행규칙 위임(대통령령) -> payload delegation(법령)
  - 위임행정규칙                  -> payload delegation(행정규칙)
  - 위임자치법규(조례)            -> payload ordinance_delegations
  - 학칙공단(정관 등)             -> payload delegation(학칙공단)
  - 같은/타 법령의 '제N조' 조문링크 -> 문서간 참조가 아니라 조문 네비게이션(별도 집계)
"""

import re
import json

import requests

from collector import render as R
from collector.core import OUTPUT_DIR, build_ref_url


def load_payloads() -> list[dict]:
    with open(f"{OUTPUT_DIR}/laws_payload.json", encoding="utf-8") as f:
        return json.load(f)


def _src_articles(items, category=None, deleg_types=None) -> set[str]:
    out = set()
    for r in items:
        if category and r.get("target_category") != category:
            continue
        if deleg_types and r.get("delegation_type") not in deleg_types:
            continue
        if r.get("source_article_no"):
            out.add(r["source_article_no"])
    return out


def render_ground_truth(law: dict) -> dict:
    """렌더링 DOM 에서 카테고리별 ground-truth 추출."""
    dom = R.render_law_dom(law["mst"], law.get("enforcement_date", ""))
    anchors = R.extract_body_anchors(dom)

    gt = {
        "external_law_names": set(),     # 「OO법」 인용 법령명
        "admrul_anchors": {},            # 출처조문 -> 위임행정규칙 앵커(대표 1개)
        "ordin_anchors": {},             # 출처조문 -> 위임자치법규 앵커(대표 1개)
        "schlpub_titles": set(),         # 학칙공단 대상 제목
        "internal_jo_links": 0,          # 조문 네비게이션 링크 수
    }

    for a in anchors:                            # 렌더링 앵커를 카테고리별로 집계
        cat = a["category"]
        if cat == "법령참조":
            if a.get("ref_kind") == "external":  # 「OO법」 = 외부 법령 인용
                m = re.match(r"「([^」]+)」", a["text"])
                if m:
                    gt["external_law_names"].add(m.group(1).strip())
            else:                                # 제N조만 = 본문 내 조문 네비게이션
                gt["internal_jo_links"] += 1
        elif cat == "행정규칙":                   # 출처조문 단위로 대표 앵커 보관(나중 실조회용)
            if a.get("source_article_no"):
                gt["admrul_anchors"].setdefault(a["source_article_no"], a)
        elif cat == "자치법규":
            if a.get("source_article_no"):
                gt["ordin_anchors"].setdefault(a["source_article_no"], a)
        elif cat == "학칙공단":                   # 정관 등은 팝업 조회로 제목 해석
            title = R.resolve_schlpub_title(a)
            if title:
                gt["schlpub_titles"].add(title)

    gt["_anchor_counts"] = _count_categories(anchors)
    return gt


def _count_categories(anchors) -> dict:
    from collections import Counter
    c = Counter()
    for a in anchors:
        if a["category"] == "법령참조":
            c[f"법령참조({a.get('ref_kind')})"] += 1
        else:
            c[a["category"]] += 1
    return dict(c)


def check_law(law: dict) -> bool:
    print("\n" + "=" * 72)
    print(f"[{law['law_name']}]  (mst={law['mst']})")
    print("=" * 72)

    gt = render_ground_truth(law)
    print("  렌더링 앵커 분포:", gt["_anchor_counts"])

    relations = law["relations"]
    ordinances = law.get("ordinance_delegations", [])

    # payload 측 집합
    pay_citation_names = {
        r["target_law_name"] for r in relations if r["relation_type"] == "citation"
    }
    pay_admrul_src = _src_articles(relations, category="행정규칙")
    pay_ordin_src = {g["source_article_no"] for g in ordinances if g.get("source_article_no")}
    pay_schlpub_titles = {
        r["target_law_name"] for r in relations if r["target_category"] == "학칙공단"
    }

    ok = True

    # 1) 외부 법령 인용
    # payload citation 은 법령명에서 시행령/시행규칙 등 접미사가 붙기도 하므로 부분일치 허용
    def cited(name):
        return any(name == n or name in n or n in name for n in pay_citation_names)

    miss_cite = {n for n in gt["external_law_names"] if not cited(n)}
    ok &= _report("외부 법령 인용 「」", gt["external_law_names"], miss_cite)

    # 2) 위임행정규칙 — payload 에 없는 출처조문은 '빈 위임링크(미등록)' 인지 실조회로 확인
    ok &= _report_delegation("위임행정규칙", gt["admrul_anchors"], pay_admrul_src)

    # 3) 위임자치법규/조례 — 동일하게 실조회로 빈 링크 필터
    ok &= _report_delegation("위임자치법규/조례", gt["ordin_anchors"], pay_ordin_src)

    # 4) 학칙공단/정관 (대상 제목 기준)
    miss_schl = gt["schlpub_titles"] - pay_schlpub_titles
    ok &= _report("학칙공단/정관 (대상제목)", gt["schlpub_titles"], miss_schl)

    # 참고: 조문 네비게이션 링크(문서간 참조 아님)
    print(f"  · 참고) 조문 네비게이션 링크(자기/타법 제N조): {gt['internal_jo_links']}건 "
          f"— 문서간 참조가 아니라 본문 내 이동 링크라 relations 비대상")

    return ok


def _report_delegation(label: str, gt_anchors: dict, pay_src: set) -> bool:
    """
    렌더링에 보이는 위임 출처조문 vs payload.
    payload 에 없는 조문은 실제 위임문서 수를 조회해서, 0건이면 '빈 트리거 링크'로
    간주(누락 아님). 0건이 아니면 진짜 누락.
    """
    gt_src = set(gt_anchors)
    if not gt_src:
        print(f"  ○ {label}: 해당 없음")
        return True

    only_render = gt_src - pay_src
    empty, real_miss = [], []
    for art in sorted(only_render):
        n = R.count_delegated_docs(gt_anchors[art])
        (empty if n == 0 else real_miss).append((art, n))

    captured = len(gt_src & pay_src)
    note = ""
    if empty:
        note = f"  (+{len(empty)}개 조문은 본문에 트리거 링크만 있고 등록문서 0건 → 빈 링크)"

    if real_miss:
        print(f"  ❌ {label}: 실제 누락 {len(real_miss)}건 -> {real_miss}{note}")
        return False
    print(f"  ✅ {label}: 등록된 위임 {captured}개 조문 전부 캡처{note}")
    return True


def _report(label: str, expected: set, missing: set) -> bool:
    total = len(expected)
    got = total - len(missing)
    if total == 0:
        print(f"  ○ {label}: 해당 없음")
        return True
    if missing:
        print(f"  ❌ {label}: {got}/{total} 캡처, 누락 {len(missing)}건 -> {sorted(missing)[:8]}")
        return False
    print(f"  ✅ {label}: {got}/{total} 전부 캡처")
    return True


def spot_check_addresses(payloads, sample_per_category=2) -> bool:
    """캡처한 target_url 표본이 실제로 살아있는(오류페이지 아님) 주소인지 확인."""
    print("\n" + "=" * 72)
    print("[주소 유효성 표본 검증]")
    print("=" * 72)

    by_cat: dict[str, list] = {}
    for law in payloads:
        for r in law["relations"]:
            by_cat.setdefault(r["target_category"], []).append(r["target_url"])
        for g in law.get("ordinance_delegations", []):
            for o in g.get("ordinances", [])[:1]:
                by_cat.setdefault("자치법규", []).append(o["target_url"])
            if g.get("lookup_url"):
                by_cat.setdefault("자치법규", []).append(g["lookup_url"])

    hdr = {"User-Agent": "Mozilla/5.0"}
    all_ok = True
    for cat, urls in by_cat.items():
        for url in urls[:sample_per_category]:
            try:
                r = requests.get(url, headers=hdr, timeout=20)
                bad = ("오류" in r.text[:3000]) or ("죄송합니다" in r.text[:3000]) or r.status_code != 200
                mark = "❌" if bad else "✅"
                if bad:
                    all_ok = False
                print(f"  {mark} [{cat}] {url}")
            except Exception as e:
                all_ok = False
                print(f"  ❌ [{cat}] {url}  ERR={e}")
    return all_ok


def main():
    payloads = load_payloads()

    results = []
    for law in payloads:
        results.append(check_law(law))

    addr_ok = spot_check_addresses(payloads)

    print("\n" + "=" * 72)
    print("최종 결과")
    print("=" * 72)
    for law, ok in zip(payloads, results):
        print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {law['law_name']}")
    print(f"  {'✅ PASS' if addr_ok else '❌ FAIL'}  주소 유효성 표본")

    if all(results) and addr_ok:
        print("\n🎉 모든 본문 하이퍼링크(외부참조)가 payload 에 캡처되었고 주소도 유효합니다.")
    else:
        print("\n⚠️  누락 또는 무효 주소가 있습니다. 위 리포트를 확인하세요.")


if __name__ == "__main__":
    main()
