"""에이전트 그래프.

질의 모드 (SERVICE.md 10장):
  START → guard_query ─┬─(본문)──────────────────────→ summarize_news ─┬─(점검 대상 있음)→ retrieve_checklist → coverage_check ─┬─(없음·수정 필요 있음)→ draft_proposals → guard_output → format_answer → END
                       ├─(URL)→ fetch_article ─(성공)─┘                 └─(기타 등 점검 대상 없음)──────────────→ coverage_check ─┴─(모두 있음 등)─────────────────────→ guard_output → format_answer → END
                       │                      └─(차단·실패)→ refuse → END
                       └─(거절·안내)→ refuse → END
배치 모드 그래프는 M4에서 추가한다.

실행:
  python -m src.agent data/samples/<파일>      파일 내용을 입력으로
  python -m src.agent --text "입력 문자열"     문자열을 그대로 입력으로 (URL, 짧은 입력 시험용)
"""

import time
import uuid
from typing import Literal

from langgraph.graph import END, START, StateGraph

from src.llm import pop_fallback_events, pop_mask_events
from src.nodes.answer import format_answer
from src.nodes.coverage import coverage_check, coverage_targets
from src.nodes.fetch import fetch_article
from src.nodes.guard import guard_query, refuse
from src.nodes.guard_output import guard_output
from src.nodes.proposals import draft_proposals, needs_proposal
from src.nodes.retrieve import retrieve_checklist
from src.nodes.summarize import summarize_news
from src.state import QueryState
from src.tracer import tracer


def route_after_guard(state: QueryState) -> Literal["summarize_news", "fetch_article", "refuse"]:
    """본문이면 점검을 시작하고, URL이면 본문을 가져오고, 그 밖에는 안내한다."""
    return {"article": "summarize_news", "url": "fetch_article"}.get(state["input_kind"], "refuse")


def route_after_fetch(state: QueryState) -> Literal["summarize_news", "refuse"]:
    return "summarize_news" if state["input_kind"] == "article" else "refuse"


def route_after_summary(state: QueryState) -> Literal["retrieve_checklist", "coverage_check"]:
    """점검 대상이 없으면(기타 카테고리 등) 검색을 건너뛴다. 규칙 판정이므로 코드로 한다."""
    return "retrieve_checklist" if coverage_targets(state["summary"]) else "coverage_check"


def route_after_coverage(state: QueryState) -> Literal["draft_proposals", "guard_output"]:
    """"없음"·"수정 필요" 판정이 있을 때만 제안을 쓴다."""
    return "draft_proposals" if needs_proposal(state["coverage"]) else "guard_output"


def build_query_graph():
    builder = StateGraph(QueryState)
    builder.add_node("guard_query", guard_query)
    builder.add_node("refuse", refuse)
    builder.add_node("fetch_article", fetch_article)
    builder.add_node("summarize_news", summarize_news)
    builder.add_node("retrieve_checklist", retrieve_checklist)
    builder.add_node("coverage_check", coverage_check)
    builder.add_node("draft_proposals", draft_proposals)
    builder.add_node("guard_output", guard_output)
    builder.add_node("format_answer", format_answer)

    builder.add_edge(START, "guard_query")
    builder.add_conditional_edges("guard_query", route_after_guard)
    builder.add_conditional_edges("fetch_article", route_after_fetch)
    builder.add_edge("refuse", END)
    builder.add_conditional_edges("summarize_news", route_after_summary)
    builder.add_edge("retrieve_checklist", "coverage_check")
    builder.add_conditional_edges("coverage_check", route_after_coverage)
    builder.add_edge("draft_proposals", "guard_output")
    builder.add_edge("guard_output", "format_answer")
    builder.add_edge("format_answer", END)
    return builder.compile()


query_graph = build_query_graph()


def run_query(question: str) -> dict:
    """질의 1건(기사 URL 또는 본문)을 처리한다. API 응답 형식(SERVICE.md 10장)의 answer, contexts, trace를 돌려준다."""
    pop_fallback_events(), pop_mask_events()  # 이전 실행의 기록이 섞이지 않게 비운다
    query_id = uuid.uuid4().hex[:12]
    started = time.time()
    result = query_graph.invoke(
        {"question": question, "trace": []},
        config={"callbacks": [tracer], "metadata": {"query_id": query_id, "mode": "query"}},
    )
    trace = list(result["trace"])
    masked = pop_mask_events()
    if masked:  # 모델에 보내기 전에 가린 개인정보 개수 (호출마다 센 값의 합)
        trace.append({"step": "pii_masking", "input": None, "output": masked})
    fallbacks = pop_fallback_events()
    if fallbacks:  # 대체 모델로 판정했다는 사실을 응답에서 알 수 있게 남긴다
        trace.append({"step": "llm_fallback", "input": None, "output": fallbacks})
    # trace/날짜.jsonl에 질의 요약 한 줄 (LLM 호출 수·토큰·시간). 같은 값을 응답 trace에도 남긴다.
    totals = tracer.finish(query_id, mode="query", steps=[t["step"] for t in trace], total_seconds=round(time.time() - started, 1))
    trace.append({"step": "usage", "input": {"query_id": query_id}, "output": totals})
    summary = result.get("summary")
    return {
        # API 규약 필드 (POST /query 응답)
        "answer": result["answer"],
        "contexts": result.get("contexts", []),
        "trace": trace,
        # 화면 표시용 필드 (웹 페이지·메일 템플릿이 쓴다. API 응답에는 나가지 않는다)
        "refusal": result["answer"] if "refuse" in [t["step"] for t in trace] else "",
        "source_url": result.get("url", ""),
        "summary": summary.model_dump() if summary else None,
        "coverage": result.get("coverage", []),
        "proposals": result.get("proposals", []),
        "warnings": result.get("output_warnings", []),
    }


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) == 3 and sys.argv[1] == "--text":
        question = sys.argv[2]
    elif len(sys.argv) == 2:
        question = Path(sys.argv[1]).read_text(encoding="utf-8")
    else:
        sys.exit('사용법: python -m src.agent <기사 본문 파일>  또는  python -m src.agent --text "입력"')
    out = run_query(question)
    print(out["answer"])
    print(f"\n[contexts] {[c['doc_id'] for c in out['contexts']]}")
    print(f"[trace] {' → '.join(t['step'] for t in out['trace'])}")
