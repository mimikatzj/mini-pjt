"""guard_output 노드: 답변을 내보내기 직전 판정·제안·요약을 코드로 한 번 더 검사한다 (마지막 안전망).

검사 규칙은 src/guards.py의 check_output, unsupported_numbers, mask_pii에 있다. 질의·배치 모드 공통.
"""

from src.guards import check_output, mask_pii, unsupported_numbers
from src.retriever import load_checklist
from src.state import QueryState


def _is_korean(text: str) -> bool:
    """글자 중 한글이 30% 이상이면 한국어 기사로 본다."""
    letters = [ch for ch in text if ch.isalpha()]
    hangul = [ch for ch in letters if "가" <= ch <= "힣"]
    return bool(letters) and len(hangul) / len(letters) >= 0.3


def guard_output(state: QueryState) -> dict:
    summary = state["summary"]
    coverage, proposals, issues = check_output(
        state.get("coverage", []),
        state.get("proposals", []),
        context_ids={c["doc_id"] for c in state.get("contexts", [])},
        checklist_ids=set(load_checklist()),
    )

    # 요약의 개인정보 마스킹 (모델에는 가린 본문만 갔지만, 출력 단계에서 한 번 더 확인)
    fields = {}
    for name in ("title", "summary"):
        fields[name], masked = mask_pii(getattr(summary, name))
        if masked:
            issues.append({"where": f"요약.{name}", "issue": "개인정보 마스킹", "counts": masked})
    summary = summary.model_copy(update=fields)

    # 요약에 나온 숫자가 원문에 있는지 (없으면 답변에 확인 필요 표시).
    # 영어 기사는 번역하며 날짜·단위 표기가 바뀌어(March 12 → 3월 12일) 오탐이 많으므로 한국어 기사에만 적용한다.
    article = state.get("article", "")
    numbers = unsupported_numbers(summary.summary, article) if _is_korean(article) else []
    if numbers:
        issues.append({"where": "요약", "issue": "원문에서 확인되지 않는 숫자", "numbers": numbers})

    return {
        "summary": summary,
        "coverage": coverage,
        "proposals": proposals,
        "output_warnings": [f"요약의 숫자 {', '.join(numbers)}이(가) 원문에서 확인되지 않습니다. 원문을 확인하세요."] if numbers else [],
        "trace": [{"step": "guard_output", "input": {"coverage": len(state.get("coverage", [])), "proposals": len(state.get("proposals", []))},
                   "output": {"issues": issues, "coverage": len(coverage), "proposals": len(proposals)}}],
    }
