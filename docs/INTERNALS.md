# 수집 내부 동작 상세 (함수별 설명)

> "한 법령이 어떻게 payload 가 되나"를 함수 단위로 쭉 설명. 처음 봐도 이해되게.
> 코드: `collector/core.py`(수집 코어) · `collector/render.py`(Chrome/정관)

---

## 0. 큰 그림 — 한 법령 처리 순서 (`build_payload`)

```
① search_law           → 메타(MST·날짜)              [API: lawSearch]
② fetch_law_body        → 본문 JSON (텍스트만!)        [API: lawService target=law]
③ fetch_delegated       → 위임링크 JSON               [API: lawService target=lsDelegated]
④ build_body_text       → 본문 JSON을 읽을 수 있는 텍스트로 펼침
⑤ build_delegation_relations → ③을 파싱 → 위임 relations
⑥ build_citation_relations   → ④ 텍스트를 정규식 파싱 → 인용·자기참조 relations
⑦ render_law_dom + build_schlpub_relations → Chrome 렌더 → 정관(학칙공단) relations
⑧ dedupe → 조례 그룹핑 → 정렬 → payload 완성
```
**단위는 "법령 1건"** (조문 하나씩이 아님). API 콜 = 법령당 약 3회(①②③), Chrome = 법령당 1회(⑦).

---

## 1. ⚠️ 가장 큰 오해: "본문 호출하면 링크도 같이 오지 않나?"

**안 와.** 본문 API(`②`)는 **조문 텍스트만** 준다.

- `②`가 주는 것: `조문내용`, `항내용`, `호내용`, `목내용` … = **글자만**.
  - 예: `"… 대통령령으로 정한다"` 라는 **글자**는 있지만,
  - 그게 **어느 시행령 몇 조로 연결되는지**(=하이퍼링크)는 **본문 API에 없다.**
- 그 "연결 정보"는 **별도 API `③ lsDelegated`** 가 따로 준다.

그래서 본문(`②`)과 위임링크(`③`)를 **두 번 따로** 호출하는 거야.

| 무엇 | 어디서 | 예 |
|---|---|---|
| 본문 글자 | `② fetch_law_body` | "보건복지부령으로 정하는 …" |
| **위임 연결** (어느 규정 몇 조) | `③ fetch_delegated` | → 시행규칙 제2조의4 |
| 다른 법 인용 / 자기 조문 | 본문 글자에서 파싱(`⑥`) | 「형법」 제250조, 제9조 |
| 정관/공단규정 | Chrome 렌더(`⑦`) | (한국자활복지개발원) 정관 |

---

## 2. 함수별 설명

### ① `search_law(law_name)` — 메타 조회
- 호출: `lawSearch.do?target=law&query=법령명`
- 받는 것: **MST(법령일련번호)**, 법령ID, 시행일·공포일, 법령구분 등.
- 왜: ②③ 호출에 MST 가 필요하고, payload 메타도 채움.

### ② `fetch_law_body(law_meta, law_name)` — 본문
- 호출: `lawService.do?target=law&MST=…&type=JSON`
- 받는 것: 조문 구조 JSON (`조문단위[]` → `항[]` → `호[]` → `목[]`). **텍스트만, 링크 없음.**

### ③ `fetch_delegated(law_meta, law_name)` — 위임링크
- 호출: `lawService.do?target=lsDelegated&MST=…&type=JSON`
- 받는 것: 조문별 **위임 정보** = "이 조문의 '대통령령으로 정하는' 이라는 말이 → 시행령 제N조로 위임됨" 같은 매핑.
- 종류: 시행령 / 시행규칙 / 위임행정규칙 / 위임자치법규(조례). 각 항목에 링크텍스트·문장·대상제목·대상번호 포함.

### ④ `build_body_text(body_json)` + `extract_article_text(jo)` — 본문 텍스트로 펼치기
**이게 뭐냐:** ②의 본문은 **계층 JSON**이라 사람이 읽기 어렵고, 인용 파싱(⑥)도 텍스트가 있어야 가능. 그래서 **조 → 항 → 호 → 목 순서로 순회하며 글자를 이어붙여** 읽을 수 있는 한 덩어리 텍스트로 만든다.

법조문 계층:
```
조(條)  제1조
 └ 항(項)  ①②③
    └ 호(號)  1. 2. 3.
       └ 목(目)  가. 나. 다.
```
- `extract_article_text` = 조 1개를 → `조문내용`(머리글) + 항내용들 + 호내용들 + 목내용들 순으로 합침.
- `build_body_text` = 모든 조를 순서대로 합쳐 **본문 전체 텍스트** 완성.
- (목내용이 가끔 '리스트의 리스트'로 와서 `flatten_text` 로 평탄화 — 예전에 여기서 버그 났던 부분)

### ⑤ `build_delegation_relations(delegated_json)` — 위임 → relations
- ③의 JSON을 파싱해 위임 1건 = relation 1개로 변환.
- `위임구분`(시행령/시행규칙/위임행정규칙/위임자치법규)에 따라 `target_category`·`target_url` 결정.
- `_rel_from_law_target`(시행령/규칙) / `_rel_from_named_target`(행정규칙·조례) 가 실제 변환.

### ⑥ `build_citation_relations(body_json, self_law_name)` — 인용/자기참조 (텍스트 파싱)
- ④의 **본문 텍스트**에서 `「OO법」 제N조`(다른 법 **인용**) 와 `제N조`(같은 법 **자기참조**)를 정규식으로 찾음.
- `「형법」` 한 번 나오면 뒤따르는 `제250조·제252조`도 형법 조문으로 이어붙임(컨텍스트 이어받기), `같은 법`·괄호·절경계 보정 포함.
- 위임(⑤)엔 안 나오는 정보(인용·자기참조)라 텍스트에서 직접 뽑는 거.

### ⑦ `render.render_law_dom(...)` + `render.build_schlpub_relations(...)` — 정관(Chrome)
- 정관/공단규정 링크는 ③(lsDelegated)에 **안 나오고** 본문이 JS로 그려져서, **Chrome 으로 본문 페이지를 렌더**해 DOM의 정관 앵커를 찾음.
- 정관 앵커(`joDelegatePop(...,'010113',...)`)를 찾으면 → 그 파라미터로 **`conSchlPubRulByLsPop.do`** 호출 → 정관 제목 추출.
- **`conSchlPubRulByLsPop.do`** = law.go.kr 내부 엔드포인트. "con(조회) + SchlPubRul(학칙·공단규정) + ByLs(법령기준) + Pop(팝업)" = **이 조문이 위임한 학칙·공단규정 목록**을 주는 API. (본문에서 정관 링크 누르면 뜨는 팝업의 데이터)

### ⑧ 마무리 — `dedupe_relations` → `split_ordinance_groups` → 정렬
- 중복 제거(같은 출처조→대상조 1건) → 조례는 조문별로 묶음 → 출처 조문 오름차순 정렬 → payload.

---

## 3. "정관은 무조건 학칙공단?"

**카테고리가 "학칙공단" 이다** 가 맞는 표현. 법제처가 정관·공단규정·학칙 류를 **"학칙·공단"** 분류로 묶어두고 URL 도 `law.go.kr/학칙공단/{제목}`.
- 그래서 정관 relation 은 `target_category="학칙공단"`, `delegation_type="위임학칙공단"`.
- "정관"만 학칙공단인 게 아니라 **공단 지침/규정도 같은 분류** (예: "(한국교통안전공단) 인증검사업무지침"). 정관은 그 분류의 한 예일 뿐.
- 코드상 매핑: `DATCLS_MAP = { '010102': 행정규칙, '010113': 학칙공단, '010103': 자치법규 }` (`render.py`).

---

## 4. MST / 버전지문은 어떻게?

- **MST(법령일련번호)** = 법령 **버전 ID**. 개정되면 **새 MST** 발급. (법령ID는 안 바뀌고, MST만 바뀜)
  - ①(search) / discover 에서 받아옴. ②③ API 호출의 키로 씀.
- **버전지문(`version_signature`)** = "이 법이 바뀌었나" 판정용 값.
  - = **그 법 + 그 시행령 + 그 시행규칙의 MST 들**을 묶어 해시한 짧은 문자열.
  - 만드는 곳: `collect.discover_catalog` 가 전체 목록을 받을 때 이름으로 묶어 `collect._signature_for` 로 계산 → `law_catalog.version_signature` 에 저장.
  - 쓰는 곳: `sync` 가 catalog 의 새 지문 vs `collect_state` 의 마지막 수집 지문을 비교 → 다르면 재수집.
  - 왜 3개(법+령+규칙)? 시행령·시행규칙이 바뀌어도 위임 대상이 달라질 수 있어서 **셋 다** 묶어 추적.

---

## 5. API / 함수 한눈에

| 단계 | 함수 | API/도구 | 결과 |
|---|---|---|---|
| ① 메타 | `search_law` | `lawSearch` | MST·날짜 |
| ② 본문 | `fetch_law_body` | `lawService target=law` | 조문 JSON(텍스트) |
| ③ 위임 | `fetch_delegated` | `lawService target=lsDelegated` | 위임 매핑 JSON |
| ④ 텍스트 | `build_body_text` / `extract_article_text` | — | 본문 텍스트 |
| ⑤ 위임관계 | `build_delegation_relations` | — | delegation relations |
| ⑥ 인용/자기 | `build_citation_relations` | — | citation / internal_ref |
| ⑦ 정관 | `render_law_dom` + `build_schlpub_relations` | Chrome + `conSchlPubRulByLsPop.do` | 학칙공단 relations |
| ⑧ 정리 | `dedupe_relations` · `split_ordinance_groups` | — | 최종 payload |
| 변경감지 | `discover_catalog` · `_signature_for` | `lawSearch`(전체) | version_signature |
