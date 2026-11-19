"""coverage_check 노드: 기사 요약 + 검색된 체크리스트 항목 → 점검 대상별 판정.

- 무엇을 점검할지(점검 대상)는 카테고리에 따라 코드가 정한다. "기타"는 LLM을 부르지 않는다.
- 판정은 LLM(고성능 모델)이 한다: 프롬프트 | 모델.with_structured_output(CoverageResult)
- 판정에 인용된 근거 ID는 코드가 검증한다 (SERVICE.md 11장).
"""

import re
from functools import lru_cache

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from src import config
from src.llm import get_structured_llm
from src.retriever import load_checklist, normalize_id
from src.state import CoverageResult, NewsSummary, QueryState

ID_IN_TEXT = re.compile(r"(?<![\d.])\d+\.\d+\.\d+(?![\d.])")
NOT_TARGET = "점검 대상 아님"
NO_EVIDENCE = "판단 근거 없음"


def coverage_targets(summary: NewsSummary) -> list[str]:
    """카테고리별 점검 대상 (SERVICE.md 11장). 계산 규칙이므로 코드로 정한다."""
    if summary.category == "침해사고":
        return summary.causes
    if summary.category == "법·제도":
        return summary.changed_clauses
    if summary.category == "취약점":
        return summary.countermeasures
    return []


@lru_cache(maxsize=1)
def build_chain() -> Runnable:
    system = (config.PROMPTS_DIR / "coverage_check.md").read_text(encoding="utf-8")
    prompt = ChatPromptTemplate.from_messages([
        ("system", system),
        ("human",
         "<article_summary>\n카테고리: {category}\n제목: {title}\n요약: {summary}\n</article_summary>\n\n"
         "점검 대상:\n{targets}\n\n"
         "<checklist>\n{checklist}\n</checklist>"),
    ])
    return prompt | get_structured_llm("smart", CoverageResult)


def validate(result: CoverageResult, targets: list[str], context_ids: set[str]) -> list[dict]:
    """LLM 판정을 코드로 검증해 최종 판정 목록을 만든다.

    - 근거 ID는 checklist.csv에 있고, 이번에 검색해서 보여 준 항목이어야 한다.
      (모델이 기억하는 다른 판본의 번호를 막는다. 예: 인증기준 전체판 2.9.3 = 백업, 이 체크리스트 2.9.3 = 로그)
    - 유효한 근거가 하나도 남지 않은 "있음"/"수정 필요"는 "판단 근거 없음"으로 바꾼다.
    - 판정이 빠진 점검 대상도 "판단 근거 없음"으로 채운다. 중복 판정은 첫 번째만 쓴다.
    """
    checklist_ids = set(load_checklist())
    by_no = {}
    for j in result.judgments:
        if 1 <= j.target_no <= len(targets) and j.target_no not in by_no:
            by_no[j.target_no] = j

    final = []
    for no, target in enumerate(targets, 1):
        j = by_no.get(no)
        if j is None:
            final.append({"target": target, "verdict": NO_EVIDENCE, "evidence_ids": [], "reason": "판정이 생성되지 않음"})
            continue
        cited = list(dict.fromkeys(normalize_id(i) for i in j.evidence_ids))  # "[2.9.2] 이름" 같은 형태 정리
        if not cited:  # 근거 칸을 비우고 이유에만 ID를 쓴 경우: 이유에서 찾는다 (아래에서 똑같이 검증)
            cited = list(dict.fromkeys(ID_IN_TEXT.findall(j.reason)))
        valid = [i for i in cited if i in checklist_ids and i in context_ids]
        dropped = [i for i in cited if i not in valid]
        verdict, reason = j.verdict, j.reason
        if verdict in ("있음", "수정 필요") and not valid:
            verdict = NO_EVIDENCE
            reason = f"근거 ID 검증 실패 (원래 판정: {j.verdict}, 인용: {j.evidence_ids}) — {j.reason}"
        final.append({"target": target, "verdict": verdict, "evidence_ids": valid, "reason": reason,
                      **({"dropped_ids": dropped} if dropped else {})})
    return final


def format_checklist(contexts: list[dict]) -> str:
    return "\n\n".join(c["text"] for c in contexts)


def coverage_check(state: QueryState) -> dict:
    """그래프 노드. state["summary"], state["contexts"] → state["coverage"]."""
    summary = state["summary"]
    contexts = state.get("contexts", [])
    targets = coverage_targets(summary)

    if not targets:  # 기타 카테고리이거나 점검할 대상이 추출되지 않음
        coverage = [{"target": summary.title, "verdict": NOT_TARGET, "evidence_ids": [], "reason": f"카테고리: {summary.category}"}]
    else:
        result = build_chain().invoke({
            "category": summary.category,
            "title": summary.title,
            "summary": summary.summary,
            "targets": "\n".join(f"{i}. {t}" for i, t in enumerate(targets, 1)),
            "checklist": format_checklist(contexts),
        })
        coverage = validate(result, targets, {c["doc_id"] for c in contexts})

    return {
        "coverage": coverage,
        "trace": [{
            "step": "coverage_check",
            "input": {"targets": targets, "context_ids": [c["doc_id"] for c in contexts]},
            "output": coverage,
        }],
    }
