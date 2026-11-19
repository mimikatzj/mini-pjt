"""배치 모드 3단계 노드: dedupe(중복 제거), guard_input(입력 가드레일). 모두 규칙 기반 코드, LLM 호출 없음.

중복 제거 (SERVICE.md 8장 3단계, 18장):
- 선별 묶음(부문 × 지역: 사고·국내, 사고·해외, 정책·국내) 안에서만 비교한다.
  (같은 사건을 국내·해외 기사가, 또는 사고·정책 기사가 각각 다루면 둘 다 남긴다)
- 이미 보낸 URL(DB seen)은 묶음과 관계없이 뺀다.
- 같은 URL은 하나로 합치고, 수집된 메뉴(source)를 모두 남긴다.
- 같은 CVE 번호를 다룬 기사는 한 묶음으로 보고 대표 1건만 남긴다. 나머지는 related로 붙인다.

입력 가드레일 (SERVICE.md 8장 3-1단계):
- 제목·설명의 HTML 조각과 제어 문자 정리, 인젝션 의심 문구 표시(버리지 않음), 개인정보 마스킹.
- 선별된 기사의 본문은 4-5에서 가져오며, 그때 같은 검사를 다시 한다.
"""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from src.guards import find_injection, mask_pii

CVE = re.compile(r"CVE-\d{4}-\d{4,7}", re.IGNORECASE)
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$)")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def normalize_url(url: str) -> str:
    """같은 기사를 같은 주소로 본다: 스킴·호스트 소문자, #뒤 제거, 추적용 파라미터(utm_ 등) 제거."""
    p = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)])
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, query, ""))


def cves(item: dict) -> set[str]:
    return {c.upper() for c in CVE.findall(f"{item['title']} {item.get('snippet', '')}")}


def group_of(item: dict) -> str:
    """선별 묶음 이름 (예: 사고·국내)."""
    return f"{item['track']}·{item['region']}"


def dedupe_items(raw_items: list[dict], seen_urls: dict[str, set[str]]) -> tuple[list[dict], list[dict]]:
    """(남은 기사, 뺀 기사와 이유)를 돌려준다."""
    already_sent = {normalize_url(u) for urls in seen_urls.values() for u in urls}
    kept_by_region: dict[str, list[dict]] = {}  # 키: 선별 묶음 (사고·국내 등)
    dropped: list[dict] = []
    for item in raw_items:
        region = group_of(item)
        url = normalize_url(item["url"])
        if url in already_sent:
            dropped.append({"title": item["title"], "region": region, "reason": "이미 보낸 기사"})
            continue
        bucket = kept_by_region.setdefault(region, [])
        same = next((k for k in bucket if k["url_norm"] == url), None)
        if same:  # 같은 URL: 수집 메뉴만 합친다
            if item["source"] not in same["sources"]:
                same["sources"].append(item["source"])
            dropped.append({"title": item["title"], "region": region, "reason": f"같은 URL ({item['source']})"})
            continue
        bucket.append({**item, "url_norm": url, "sources": [item["source"]], "cves": sorted(cves(item)), "related": []})

    # 같은 CVE 묶기 (선별 묶음 안에서만). A-B가 CVE를 공유하고 B-C가 공유하면 A·B·C를 한 묶음으로 본다.
    result: list[dict] = []
    for region, bucket in kept_by_region.items():
        parent = list(range(len(bucket)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        owner: dict[str, int] = {}
        for i, it in enumerate(bucket):
            for c in it["cves"]:
                if c in owner:
                    parent[find(i)] = find(owner[c])
                else:
                    owner[c] = i
        groups: dict[int, list[dict]] = {}
        for i, it in enumerate(bucket):
            groups.setdefault(find(i), []).append(it)
        for members in groups.values():
            # 대표: 설명이 가장 긴 기사 (정보가 많은 쪽), 같으면 먼저 수집된 기사
            rep = max(members, key=lambda m: len(m.get("snippet", "")))
            for m in members:
                if m is not rep:
                    rep["related"].append({"title": m["title"], "url": m["url"], "source": m["source"]})
                    dropped.append({"title": m["title"], "region": region, "reason": f"같은 CVE ({', '.join(sorted(set(m['cves']) & set(rep['cves'])))})"})
            result.append(rep)
    return result, dropped


def dedupe(state: dict) -> dict:
    seen = {} if state.get("ignore_seen") else (state.get("records") or {}).get("seen_urls", {})  # ignore_seen: 시험용
    items, dropped = dedupe_items(state.get("raw_items", []), seen)
    return {
        "new_items": items,
        "trace": [{"step": "dedupe", "input": {"raw": len(state.get("raw_items", []))},
                   "output": {"kept": len(items), "dropped": dropped}}],
    }


def clean_text(s: str) -> str:
    s = BeautifulSoup(s or "", "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", CONTROL.sub(" ", s)).strip()


def guard_input(state: dict) -> dict:
    items, flags = [], []
    for it in state.get("new_items", []):
        it = dict(it)
        masked_total: dict[str, int] = {}
        for field in ("title", "snippet"):
            text, counts = mask_pii(clean_text(it.get(field, "")))
            it[field] = text
            for k, v in counts.items():
                masked_total[k] = masked_total.get(k, 0) + v
        it["injection"] = find_injection(f"{it['title']} {it['snippet']}")  # 표시만 한다. 기사는 데이터로 계속 처리
        if it["injection"] or masked_total:
            flags.append({"title": it["title"][:60], "injection": it["injection"], "masked": masked_total})
        items.append(it)
    return {
        "new_items": items,
        "trace": [{"step": "guard_input", "input": {"items": len(items)}, "output": {"flagged": flags}}],
    }
