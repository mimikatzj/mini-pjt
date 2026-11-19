"""에이전트 그래프.

질의 모드 (SERVICE.md 10장):
  START → guard_query ─┬─(본문)──────────────────────→ summarize_news ─┬─(점검 대상 있음)→ retrieve_checklist → coverage_check ─┬─(없음·수정 필요 있음)→ draft_proposals → guard_output → format_answer → END
                       ├─(URL)→ fetch_article ─(성공)─┘                 └─(기타 등 점검 대상 없음)──────────────→ coverage_check ─┴─(모두 있음 등)─────────────────────→ guard_output → format_answer → END
                       │                      └─(차단·실패)→ refuse → END
                       └─(거절·안내)→ refuse → END

배치 모드 (SERVICE.md 8·9장) — 현재(4-9):
  START ─┬→ load_checklist ─┐
         ├→ load_records ───┼→ dedupe → guard_input → triage_news ─(기사마다 Send, 병렬)→ process_article → END
         └→ collect_news ───┘
  process_article = 본문 확보 → [기사 그래프: summarize_news → retrieve_checklist → coverage_check → draft_proposals
                    → guard_output → format_answer] (질의 모드와 같은 노드)
  process_article(병렬) 모두 끝 → merge_proposals(누적 제안과 비교: 새 제안 / 근거 추가 결정) → compose_email → send_email ─┬─(발송 성공)→ commit_records → END
                                                                                                                   └─(dry-run·발송 실패)→ END (아무것도 저장하지 않음)
  (선별된 기사가 없으면 triage_news → compose_email: "오늘은 새 소식이 없습니다" 메일)

실행:
  python -m src.agent data/samples/<파일>      질의 모드: 파일 내용을 입력으로
  python -m src.agent --text "입력 문자열"     질의 모드: 문자열을 그대로 입력으로 (URL, 짧은 입력 시험용)
  python main.py --dry-run                     배치 모드
"""

import time
import uuid
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from src import config
from src.llm import pop_fallback_events, pop_mask_events
from src.nodes.answer import format_answer
from src.nodes.article import prepare_article
from src.nodes.collect import collect_news
from src.nodes.compose import compose_email
from src.nodes.coverage import coverage_check, coverage_targets
from src.nodes.dedupe import dedupe, guard_input
from src.nodes.fetch import fetch_article
from src.nodes.guard import guard_query, refuse
from src.nodes.guard_output import guard_output
from src.nodes.merge import merge_proposals
from src.nodes.proposals import draft_proposals, needs_proposal
from src.nodes.records import commit_records, load_checklist, load_records
from src.nodes.send import send_email
from src.nodes.retrieve import retrieve_checklist
from src.nodes.summarize import summarize_news
from src.nodes.triage import triage_news
from src.state import BatchState, QueryState
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


def build_article_graph():
    """기사 1건 점검 그래프 (배치 모드용). 질의 모드 그래프에서 입력 검사·URL 처리만 뺀 것으로, 노드는 같다."""
    builder = StateGraph(QueryState)
    builder.add_node("summarize_news", summarize_news)
    builder.add_node("retrieve_checklist", retrieve_checklist)
    builder.add_node("coverage_check", coverage_check)
    builder.add_node("draft_proposals", draft_proposals)
    builder.add_node("guard_output", guard_output)
    builder.add_node("format_answer", format_answer)
    builder.add_edge(START, "summarize_news")
    builder.add_conditional_edges("summarize_news", route_after_summary)
    builder.add_edge("retrieve_checklist", "coverage_check")
    builder.add_conditional_edges("coverage_check", route_after_coverage)
    builder.add_edge("draft_proposals", "guard_output")
    builder.add_edge("guard_output", "format_answer")
    builder.add_edge("format_answer", END)
    return builder.compile()


article_graph = build_article_graph()


def process_article(payload: dict) -> dict:
    """선별된 기사 1건: 본문 확보 → 기사 그래프. 실패해도 그 기사만 "처리 실패"로 남긴다 (CLAUDE.md 구현 원칙)."""
    item = payload["item"]
    started = time.time()
    prep = prepare_article(item)
    base = {"item": item, "body_source": prep["body_source"], "body_error": prep["error"], "injection": prep["injection"]}
    try:
        # 바깥 그래프의 설정(트레이서 콜백, 실행 ID)은 LangGraph가 하위 호출로 넘겨 준다
        r = article_graph.invoke({"article": prep["text"], "url": item["url"], "trace": [],
                                  "past_coverage": payload.get("past", [])})  # 장기 메모리: 최근 판정
    except Exception as e:
        result = {**base, "error": f"{type(e).__name__}: {e}"}
    else:
        summary = r.get("summary")
        result = {**base, "error": "",
                  "summary": summary.model_dump() if summary else None,
                  "contexts": r.get("contexts", []), "coverage": r.get("coverage", []),
                  "proposals": r.get("proposals", []), "warnings": r.get("output_warnings", []),
                  "answer": r.get("answer", ""), "trace": r.get("trace", [])}
    result["seconds"] = round(time.time() - started, 1)
    return {"articles": [result],
            "trace": [{"step": "process_article", "input": item["title"][:60],
                       "output": {"ok": not result.get("error"), "body_source": prep["body_source"], "seconds": result["seconds"]}}]}


def route_after_send(state: BatchState) -> str:
    """발송에 성공했을 때만 기록을 저장한다. dry-run이나 발송 실패면 아무것도 저장하지 않고 끝낸다 (SERVICE.md 9.4)."""
    return "commit_records" if (state.get("sent") or {}).get("status") == "sent" else END


def route_to_articles(state: BatchState) -> list[Send] | str:
    """선별된 기사마다 process_article을 하나씩 병렬로 보낸다 (LangGraph Send).
    선별된 기사가 없어도 "오늘은 새 소식이 없습니다" 메일을 보내도록 메일 작성으로 간다 (SERVICE.md 12.2)."""
    top = state.get("top_news", [])
    past = (state.get("records") or {}).get("recent_coverage", [])
    return [Send("process_article", {"item": it, "past": past}) for it in top] if top else "compose_email"


def build_batch_graph():
    builder = StateGraph(BatchState)
    builder.add_node("load_checklist", load_checklist)
    builder.add_node("load_records", load_records)
    builder.add_node("collect_news", collect_news)
    builder.add_node("dedupe", dedupe)
    builder.add_node("guard_input", guard_input)
    builder.add_node("triage_news", triage_news)
    builder.add_node("process_article", process_article)
    builder.add_node("merge_proposals", merge_proposals)
    builder.add_node("compose_email", compose_email)
    builder.add_node("send_email", send_email)
    builder.add_node("commit_records", commit_records)
    for first in ("load_checklist", "load_records", "collect_news"):  # 서로 기다릴 필요가 없어 병렬로 시작
        builder.add_edge(START, first)
    builder.add_edge(["load_checklist", "load_records", "collect_news"], "dedupe")  # 셋 다 끝나면 중복 제거
    builder.add_edge("dedupe", "guard_input")
    builder.add_edge("guard_input", "triage_news")
    builder.add_conditional_edges("triage_news", route_to_articles, ["process_article", "compose_email"])
    builder.add_edge("process_article", "merge_proposals")  # 병렬로 보낸 기사 처리가 모두 끝나면 한 번 실행
    builder.add_edge("merge_proposals", "compose_email")
    builder.add_edge("compose_email", "send_email")
    builder.add_conditional_edges("send_email", route_after_send, ["commit_records", END])
    builder.add_edge("commit_records", END)
    return builder.compile()


batch_graph = build_batch_graph()


def run_batch(dry_run: bool = True, ignore_seen: bool = False) -> dict:
    """배치 모드 1회 실행. 질의 모드와 같은 트레이서로 LLM 호출을 trace/날짜.jsonl에 남긴다.

    ignore_seen: 시험용. 이미 보낸 URL 제외를 꺼서 같은 기사로 다시 실행한다 (제안 병합 확인).
    """
    pop_fallback_events(), pop_mask_events()
    run_id = "batch-" + time.strftime("%Y%m%d-%H%M%S")
    started = time.time()
    result = batch_graph.invoke(
        {"dry_run": dry_run, "ignore_seen": ignore_seen, "trace": [], "articles": []},
        config={"callbacks": [tracer], "metadata": {"query_id": run_id, "mode": "batch"},
                "max_concurrency": config.BATCH_CONCURRENCY},
    )
    trace = list(result["trace"])
    for step, events in (("pii_masking", pop_mask_events()), ("llm_fallback", pop_fallback_events())):
        if events:
            trace.append({"step": step, "input": None, "output": events})
    totals = tracer.finish(run_id, mode="batch", steps=[t["step"] for t in trace], total_seconds=round(time.time() - started, 1))
    trace.append({"step": "usage", "input": {"run_id": run_id}, "output": totals})
    return {**result, "trace": trace, "run_id": run_id}


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
