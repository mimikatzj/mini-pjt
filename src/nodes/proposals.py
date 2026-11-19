"""draft_proposals 노드: "없음"·"수정 필요" 판정 → 체크리스트 변경 제안 (점검항목·점검내용).

체인: 프롬프트 | 고성능 모델.with_structured_output(ProposalResult)
제안 대상 판정을 고르는 일과 결과 검증은 코드가 한다.
질의 모드에서는 제안을 저장하지 않는다. 기존 제안과의 병합·저장은 배치 모드(M4)에서 한다.
"""

from functools import lru_cache

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from src import config
from src.llm import get_structured_llm
from src.retriever import load_checklist, normalize_id
from src.state import ProposalResult, QueryState

NEEDS_PROPOSAL = ("없음", "수정 필요")


def needs_proposal(coverage: list[dict]) -> bool:
    """제안을 써야 하는 판정이 하나라도 있는지 (그래프 분기용)."""
    return any(c["verdict"] in NEEDS_PROPOSAL for c in coverage)


@lru_cache(maxsize=1)
def build_chain() -> Runnable:
    system = (config.PROMPTS_DIR / "draft_proposals.md").read_text(encoding="utf-8")
    prompt = ChatPromptTemplate.from_messages([
        ("system", system),
        ("human",
         "<article_summary>\n제목: {title}\n요약: {summary}\n</article_summary>\n\n"
         "<judgments>\n{judgments}\n</judgments>\n\n"
         "<checklist>\n{checklist}\n</checklist>\n\n"
         "분야 목록: {areas}"),
    ])
    return prompt | get_structured_llm("smart", ProposalResult)


def validate(result: ProposalResult, targets: dict[int, dict]) -> tuple[list[dict], list[dict]]:
    """LLM 제안을 코드로 검증한다. (통과한 제안, 버린 제안과 이유)를 돌려준다.

    - 제안 대상 판정 번호여야 하고, 판정 하나에 제안 하나만 받는다.
    - 수정 제안의 대상 ID는 checklist.csv에 있고 그 판정의 근거 항목이어야 한다.
    - 신규 제안의 분야가 체크리스트에 없는 이름이면 빈 칸으로 둔다.
    """
    checklist = load_checklist()
    areas = {row["분야"] for row in checklist.values()}
    accepted, dropped, used = [], [], set()
    for p in result.proposals:
        if p.target_id:
            p = p.model_copy(update={"target_id": normalize_id(p.target_id)})  # "[2.9.2] 이름" 같은 형태 정리
        judgment = targets.get(p.judgment_no)
        if judgment is None or p.judgment_no in used:
            dropped.append({"proposal": p.model_dump(), "why": "제안 대상이 아닌 판정 번호이거나 중복"})
            continue
        if p.type == "수정" and (p.target_id not in checklist or p.target_id not in judgment["evidence_ids"]):
            dropped.append({"proposal": p.model_dump(), "why": f"수정 대상 ID 검증 실패: {p.target_id!r}"})
            continue
        used.add(p.judgment_no)
        data = p.model_dump()
        if p.type == "수정":
            data["area"] = checklist[p.target_id]["분야"]
            data["item"] = checklist[p.target_id]["점검항목"]  # 기존 항목명은 원본 그대로 쓴다
        else:
            data["target_id"] = ""
            if data["area"] not in areas:
                data["area"] = ""
        data["source_target"] = judgment["target"]
        accepted.append(data)
    return accepted, dropped


def draft_proposals(state: QueryState) -> dict:
    """그래프 노드. state["coverage"] 중 제안이 필요한 판정 → state["proposals"]."""
    summary = state["summary"]
    coverage = state["coverage"]
    checklist = load_checklist()

    targets = {i: c for i, c in enumerate(coverage, 1) if c["verdict"] in NEEDS_PROPOSAL}
    cited = sorted({e for c in targets.values() for e in c["evidence_ids"]})
    result = build_chain().invoke({
        "title": summary.title,
        "summary": summary.summary,
        "judgments": "\n".join(
            f"{i}. 대상: {c['target']} / 판정: {c['verdict']} / 근거 항목: {', '.join(c['evidence_ids']) or '없음'}\n   이유: {c['reason']}"
            for i, c in targets.items()
        ),
        "checklist": "\n\n".join(
            f"[{i}] {checklist[i]['점검항목']} ({checklist[i]['분야']})\n점검내용: {checklist[i]['점검내용']}\n확인사항: {checklist[i]['확인사항']}"
            for i in cited
        ) or "(근거 항목 없음)",
        "areas": ", ".join(sorted({row["분야"] for row in checklist.values()})),
    })
    proposals, dropped = validate(result, targets)
    return {
        "proposals": proposals,
        "trace": [{
            "step": "draft_proposals",
            "input": {"judgment_nos": list(targets)},
            "output": {"proposals": proposals, **({"dropped": dropped} if dropped else {})},
        }],
    }
