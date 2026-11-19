"""입력·출력 가드레일. 보안 규칙 판정은 LLM이 아니라 코드로 한다 (CLAUDE.md 구현 원칙).

- 개인정보 마스킹 (mask_pii): 모든 LLM 호출 직전에 적용 (llm.py)
- 질의 입력 검사 (check_query): 질의 모드 입력 판별과 거절 규칙 (SERVICE.md 10.2)
- 인젝션 의심 문구 탐지 (find_injection): 질의 모드와 배치 모드(guard_input)가 함께 쓴다
- 출력 검사 (check_output): 판정·제안의 항목 ID, 형식, 개인정보, 요약 숫자를 검사한다 (질의·배치 공통)
"""

import re

from src import config

# (이름, 패턴). 위에서부터 차례로 적용한다. 긴 숫자 패턴(주민번호, 카드번호)을 전화번호보다 먼저 둔다.
# 보안 기사에 자주 나오는 CVE 번호(CVE-2024-1709), 날짜(2027-03-01), IP 주소는 개인정보가 아니므로 걸리지 않게 좁게 잡는다.
PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    # 주민등록번호·외국인등록번호: 생년월일 6자리 + 성별 자리(1~8) + 6자리
    ("주민등록번호", re.compile(r"(?<!\d)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\s?-\s?[1-8]\d{6}(?!\d)")),
    # 카드번호: 4자리씩 4묶음
    ("카드번호", re.compile(r"(?<!\d)\d{4}[- ]\d{4}[- ]\d{4}[- ]\d{4}(?!\d)")),
    # 휴대전화: 010-1234-5678, 01012345678
    ("전화번호", re.compile(r"(?<!\d)01[016789][- .]?\d{3,4}[- .]?\d{4}(?!\d)")),
    # 일반전화: 02-123-4567, 031-1234-5678 (구분자 필수)
    ("전화번호", re.compile(r"(?<!\d)0(?:2|[3-6][1-5])[- .]\d{3,4}[- .]\d{4}(?!\d)")),
    # 이메일
    ("이메일", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
]


def mask_pii(text: str) -> tuple[str, dict[str, int]]:
    """개인정보를 [종류]로 바꾼다. (마스킹된 텍스트, {종류: 개수})를 돌려준다."""
    counts: dict[str, int] = {}
    for name, pattern in PII_PATTERNS:
        text, n = pattern.subn(f"[{name}]", text)
        if n:
            counts[name] = counts.get(name, 0) + n
    return text, counts


# ── 인젝션 의심 문구 ──────────────────────────────────
# 실습 day7_practice/guards_input.py의 규칙에 한국어 변형과 역할 탈취 표현을 더했다.
INJECTION_PATTERNS: list[re.Pattern] = [re.compile(p, re.IGNORECASE) for p in [
    r"ignore (the |all |any )?(previous|above|prior|earlier) (instructions?|prompts?|rules?)",
    r"disregard (the |all )?(previous|above|prior) (instructions?|prompts?)",
    r"you are now (a |an )?(different|new|unrestricted)",
    r"(reveal|show|print|repeat) (your |the )?(system prompt|hidden instructions?)",
    r"(위의?|이전|기존|지금까지|앞의?) ?(모든 )?(지시|명령|규칙|프롬프트|설정)[은는를이가]? ?.{0,8}(무시|잊|버려|따르지 ?마)",
    r"(시스템 ?프롬프트|숨겨진 (지시|프롬프트)|첫 ?(번째)? ?지시문?|내부 (지시|설정)).{0,10}(공개|출력|알려|보여)",
    r"규칙 ?(이|가) ?없는 (AI|인공지능|모드)",
    r"(개발자|관리자|탈옥) ?모드",
    r"(너는|당신은) ?(이제|지금부터) ?.{0,20}(이다|야|입니다|역할)",
    r"</?(system|admin|root|instructions?)>",
    r"^\s*system\s*:",
]]


def find_injection(text: str) -> list[str]:
    """인젝션 의심 문구를 찾아 일치한 부분 목록을 돌려준다 (없으면 빈 목록)."""
    hits = []
    for p in INJECTION_PATTERNS:
        m = p.search(text)
        if m:
            hits.append(m.group().strip())
    return hits


# ── 질의 입력 검사 (질의 모드) ───────────────────────────
URL_LINE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://\S+$")  # 공백 없는 한 줄 주소 (스킴 무관)

MSG_NOT_ARTICLE = "기사 URL이나 본문을 입력해 주세요."
MSG_TOO_LONG = f"입력이 너무 깁니다. 본문은 {config.QUERY_MAX_CHARS:,}자 이하로 입력해 주세요."
MSG_BAD_SCHEME = "http 또는 https로 시작하는 기사 주소만 입력할 수 있습니다."
MSG_INJECTION = "처리할 수 없는 요청입니다. 점검할 기사 URL이나 본문을 입력해 주세요."


def check_query(text: str) -> dict:
    """질의 입력을 판별한다. 규칙 판정이므로 코드로 한다 (LLM 호출 없음).

    돌려주는 값:
      {"kind": "url", "url": ...}
      {"kind": "article", "article": ..., "injection": [의심 문구...]}   ← 기사 본문 안의 문구는 표시만 하고 데이터로 처리
      {"kind": "refuse", "reason": 코드, "message": 안내 문구}
    """
    text = (text or "").strip()
    if not text:
        return {"kind": "refuse", "reason": "empty", "message": MSG_NOT_ARTICLE}
    if len(text) > config.QUERY_MAX_CHARS:
        return {"kind": "refuse", "reason": "too_long", "message": MSG_TOO_LONG}

    if URL_LINE.match(text):
        scheme = text.split("://", 1)[0].lower()
        if scheme not in ("http", "https"):
            return {"kind": "refuse", "reason": "bad_scheme", "message": MSG_BAD_SCHEME}
        return {"kind": "url", "url": text}

    injection = find_injection(text)
    if len(text) < config.MIN_ARTICLE_CHARS:
        # 짧은 입력은 기사가 아니다. 인젝션 문구가 있으면 시스템을 향한 명령으로 보고 따로 기록한다.
        if injection:
            return {"kind": "refuse", "reason": "injection", "message": MSG_INJECTION, "injection": injection}
        return {"kind": "refuse", "reason": "not_article", "message": MSG_NOT_ARTICLE}
    return {"kind": "article", "article": text, "injection": injection}


# ── 출력 검사 (질의·배치 공통) ───────────────────────────
VERDICTS = {"있음", "없음", "수정 필요", "판단 근거 없음", "점검 대상 아님"}
NO_EVIDENCE = "판단 근거 없음"
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
MIN_PROPOSAL_CHARS = 15


def _digits(s: str) -> str:
    return s.replace(",", "")


def unsupported_numbers(summary_text: str, article: str) -> list[str]:
    """요약에 나온 숫자 중 원문에 없는 것. (예: 원문 "사흘간" → 요약 "4일간"의 "4")"""
    source = {_digits(n) for n in NUMBER.findall(article)}
    found = []
    for n in NUMBER.findall(summary_text):
        if _digits(n) not in source and n not in found:
            found.append(n)
    return found


def check_output(coverage: list[dict], proposals: list[dict], context_ids: set[str],
                 checklist_ids: set[str]) -> tuple[list[dict], list[dict], list[dict]]:
    """판정·제안을 검사해 (판정, 제안, 문제 목록)을 돌려준다. 고친 내용은 문제 목록에 남긴다.

    - 판정: 값이 정해진 값이 아니거나, 근거 ID가 체크리스트에 없거나 이번 검색 결과에 없으면 정리.
      "있음"·"수정 필요"인데 유효한 근거가 하나도 없으면 "판단 근거 없음"으로 바꾼다.
    - 제안: 근거가 된 판정이 더 이상 "없음"·"수정 필요"가 아니면 제외. 수정 대상 ID가 체크리스트에 없거나
      점검항목·점검내용이 비었거나 너무 짧으면 제외. "~확인"으로 끝나지 않으면 경고만 남긴다.
    - 판정 이유·제안 문장의 개인정보는 마스킹한다.
    """
    issues: list[dict] = []
    clean_cov = []
    for i, c in enumerate(coverage, 1):
        c = dict(c)
        valid = [e for e in c.get("evidence_ids", []) if e in checklist_ids and e in context_ids]
        if valid != c.get("evidence_ids", []):
            issues.append({"where": f"판정 {i}", "issue": "근거 ID 제거", "ids": [e for e in c["evidence_ids"] if e not in valid]})
            c["evidence_ids"] = valid
        if c.get("verdict") not in VERDICTS:
            issues.append({"where": f"판정 {i}", "issue": f"알 수 없는 판정 값: {c.get('verdict')!r} → {NO_EVIDENCE}"})
            c["verdict"], c["evidence_ids"], valid = NO_EVIDENCE, [], []
        if c["verdict"] in ("있음", "수정 필요") and not valid:
            issues.append({"where": f"판정 {i}", "issue": f"유효한 근거 없음 → {NO_EVIDENCE} (원래: {c['verdict']})"})
            c["verdict"] = NO_EVIDENCE
        c["reason"], masked = mask_pii(c.get("reason", ""))
        if masked:
            issues.append({"where": f"판정 {i}", "issue": "개인정보 마스킹", "counts": masked})
        clean_cov.append(c)

    clean_props = []
    for n, p in enumerate(proposals, 1):
        p = dict(p)
        no = p.get("judgment_no")
        judgment = clean_cov[no - 1] if isinstance(no, int) and 1 <= no <= len(clean_cov) else None
        if judgment is None or judgment["verdict"] not in ("없음", "수정 필요"):
            issues.append({"where": f"제안 {n}", "issue": "근거 판정이 없거나 제안 대상이 아님 → 제외"})
            continue
        if p.get("type") == "수정" and p.get("target_id") not in checklist_ids:
            issues.append({"where": f"제안 {n}", "issue": f"수정 대상 ID 없음: {p.get('target_id')!r} → 제외"})
            continue
        if not p.get("item") or len(p.get("content", "")) < MIN_PROPOSAL_CHARS:
            issues.append({"where": f"제안 {n}", "issue": "점검항목·점검내용이 비었거나 너무 짧음 → 제외"})
            continue
        if not p["content"].rstrip(" .").endswith("확인"):
            issues.append({"where": f"제안 {n}", "issue": "점검내용이 '~하는지 확인' 형식이 아님 (경고)"})
        for field in ("title", "item", "content"):
            p[field], masked = mask_pii(p.get(field, ""))
            if masked:
                issues.append({"where": f"제안 {n}", "issue": "개인정보 마스킹", "counts": masked})
        clean_props.append(p)
    return clean_cov, clean_props, issues
