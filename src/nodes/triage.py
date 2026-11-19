"""triage_news 노드 (배치 모드 4단계): 기사 선별.

선별 묶음 (config.TRACKS): 사고·국내 최대 5건, 사고·해외 최대 3건, 정책·국내 최대 3건. 기사의 묶음은 수집처로 정해진다.
- LLM(저가 모델, 묶음마다 1회 호출): 기사별 카테고리, 중요도 1~5점, 사건 키(event_key), 이유
- 코드: 같은 묶음에서 event_key가 같은 기사 묶기 → 부문별 우선순위(config.CATEGORY_PRIORITY) → 점수 → 최신순 정렬
        → 묶음별 최대 개수로 자르기
  우선순위는 LLM이 아니라 코드가 카테고리로 정한다 (사고 부문은 침해사고, 정책 부문은 법·제도가 항상 앞에 오도록).

확인용 실행: python -m src.nodes.triage   (실제 수집 → 중복 제거 → 입력 가드레일 → 선별)
"""

from functools import lru_cache
from typing import Literal

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field

from src import config
from src.llm import get_structured_llm

SNIPPET_CHARS = 200  # 선별에는 제목과 설명 앞부분만 쓴다 (기사 수가 많아 토큰을 아낀다)


class TriageItem(BaseModel):
    no: int = Field(description="기사 번호 (입력에 주어진 번호 그대로)")
    category: Literal["침해사고", "취약점", "법·제도", "기타"]
    importance: int = Field(ge=1, le=5, description="중요도 1~5")
    event_key: str = Field(description="같은 사건이면 같은 키. 영어 소문자와 하이픈")
    reason: str = Field(description="카테고리와 점수의 이유 한 문장")


class TriageResult(BaseModel):
    items: list[TriageItem]


@lru_cache(maxsize=1)
def build_chain() -> Runnable:
    system = (config.PROMPTS_DIR / "triage_news.md").read_text(encoding="utf-8")
    prompt = ChatPromptTemplate.from_messages([
        ("system", system),
        ("human", "이번 목록의 부문: {track}\n{track_guide}\n\n<articles>\n{articles}\n</articles>"),
    ])
    return prompt | get_structured_llm("fast", TriageResult)


TRACK_GUIDE = {
    "사고": "사고 부문: 특정 조직이 당한 실제 사고 사례(원인·경로·피해)만 침해사고로 분류한다. 같은 침해사고 안에서는 피해 규모가 크고 "
            "국내·업무 영향이 클수록 높게 준다. 범죄자 검거·기소·판결·제재 소식, 사기·피싱 통계, 업체 발표 위협 동향은 기타로 분류한다.",
    "정책": "정책 부문: 보안·개인정보와 관련된 정책 변경(법령·고시·가이드라인·제도 개정), 과징금·처분, 조사(착수·결과 발표)가 가장 중요하며 "
            "모두 법·제도로 분류한다. 보안·개인정보와 관계없는 정책 변경(예: 전파·통신 설비 기준, 산업 지원)은 기타다. "
            "처분·조사 사유(어떤 보호조치 위반인지)나 바뀌는 기준이 구체적일수록 높게 준다. 협약·행사·예산·사업 계획·수상은 기타다.",
}


def format_articles(items: list[dict]) -> str:
    lines = []
    for n, it in enumerate(items, 1):
        line = f"{n}. [{it['source']}] {it['title']}"
        if it.get("snippet"):
            line += f"\n   설명: {it['snippet'][:SNIPPET_CHARS]}"
        if it.get("related"):
            line += "\n   [관련 기사] " + " / ".join(r["title"] for r in it["related"][:5])
        lines.append(line)
    return "\n".join(lines)


def classify(items: list[dict], track: str) -> list[dict]:
    """LLM 분류 결과를 기사에 붙인다. 결과가 빠진 기사는 기타·1점으로 둔다 (코드 검증)."""
    if not items:
        return []
    result: TriageResult = build_chain().invoke(
        {"track": track, "track_guide": TRACK_GUIDE[track], "articles": format_articles(items)})
    by_no = {r.no: r for r in result.items if 1 <= r.no <= len(items)}
    out = []
    for n, it in enumerate(items, 1):
        r = by_no.get(n)
        out.append({**it,
                    "category": r.category if r else "기타",
                    "importance": r.importance if r else 1,
                    "event_key": (r.event_key.strip().lower() if r and r.event_key.strip() else f"item-{it['id']}"),
                    "triage_reason": r.reason if r else "분류 결과 없음 (기본값)"})
    return out


def select(classified: list[dict], limit: int, track: str) -> tuple[list[dict], list[dict]]:
    """같은 사건 묶기 → 정렬 → 자르기 (코드). (선택된 기사, 정렬된 전체 후보)를 돌려준다."""
    priority = config.CATEGORY_PRIORITY[track]

    def key(it):
        return (priority.get(it["category"], 9), -it["importance"], _neg_time(it["published"]))

    groups: dict[str, list[dict]] = {}
    for it in classified:
        groups.setdefault(it["event_key"], []).append(it)
    reps = []
    for members in groups.values():
        members.sort(key=key)
        rep = dict(members[0])  # 묶음에서 우선순위가 가장 높은 기사를 대표로
        rep["related"] = list(rep.get("related", [])) + [
            {"title": m["title"], "url": m["url"], "source": m["source"]} for m in members[1:]]
        reps.append(rep)
    ranked = sorted(reps, key=key)
    return ranked[:limit], ranked


def _neg_time(iso: str) -> float:
    from datetime import datetime
    try:
        return -datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0  # 날짜 없는 기사는 같은 순위·점수 안에서 맨 뒤


def triage_news(state: dict) -> dict:
    items = state.get("new_items", [])
    top, summary = [], {}
    for t in config.TRACKS:
        regional = [it for it in items if it["track"] == t["track"] and it["region"] == t["region"]]
        chosen, ranked = select(classify(regional, t["track"]), t["limit"], t["track"])
        top += chosen
        summary[f"{t['track']}·{t['region']}"] = {
            "candidates": len(regional), "events": len(ranked), "selected": len(chosen),
            "ranking": [{"title": r["title"][:60], "category": r["category"], "importance": r["importance"],
                         "event_key": r["event_key"], "reason": r["triage_reason"], "selected": r in chosen}
                        for r in ranked],
        }
    return {"top_news": top, "trace": [{"step": "triage_news", "input": {"new_items": len(items)}, "output": summary}]}


if __name__ == "__main__":
    import sys

    from src.nodes.collect import collect_news
    from src.nodes.dedupe import dedupe, guard_input

    sys.stdout.reconfigure(encoding="utf-8")
    state = {"records": {"seen_urls": {}}}
    state.update(collect_news(state))
    state.update(dedupe(state))
    state.update(guard_input(state))
    out = triage_news(state)
    for group, s in out["trace"][0]["output"].items():
        print(f"\n===== {group}: 후보 {s['candidates']}건 → 사건 {s['events']}개 → 선택 {s['selected']}건")
        for i, r in enumerate(s["ranking"], 1):
            mark = "★" if r["selected"] else " "
            if i <= s["selected"] + 4:
                print(f" {mark}{i:>2}. [{r['category']} {r['importance']}점] {r['title']}")
                print(f"      {r['event_key']} | {r['reason']}")
