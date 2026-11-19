"""format_answer 노드: 요약과 판정을 사람이 읽는 답변 텍스트로 만든다 (LLM 호출 없음)."""

from src.retriever import load_checklist
from src.state import QueryState

ICON = {"있음": "✅", "없음": "❌", "수정 필요": "⚠️", "판단 근거 없음": "❔", "점검 대상 아님": "➖"}
TARGET_LABEL = {"침해사고": "사고 원인", "법·제도": "변경 조항", "취약점": "권고 대책"}


def format_answer(state: QueryState) -> dict:
    summary = state["summary"]
    names = {k: v["점검항목"] for k, v in load_checklist().items()}

    lines = [f"■ {summary.title} [{summary.category}]", f"· 요약: {summary.summary}"]
    coverage = state.get("coverage", [])
    if coverage and coverage[0]["verdict"] == "점검 대상 아님":
        lines.append("· 체크리스트 점검: 점검 대상 아님")
    else:
        lines.append(f"· 체크리스트 점검 ({TARGET_LABEL.get(summary.category, '점검 대상')}별)")
        for i, c in enumerate(coverage):
            refs = ", ".join(f"{e} {names.get(e, '')}" for e in c["evidence_ids"])
            lines.append(f"   [{chr(ord('a') + i)}] {c['target']}")
            lines.append(f"       → {ICON.get(c['verdict'], '')} {c['verdict']}" + (f" ({refs})" if refs else ""))
            lines.append(f"         {c['reason']}")

    proposals = state.get("proposals", [])
    for n, p in enumerate(proposals, 1):
        label = chr(ord("a") + p["judgment_no"] - 1)
        kind = f"수정: {p['target_id']}" if p["type"] == "수정" else f"신규: {p['area'] or '분야 미정'}"
        lines.append(f"· 제안 {n} ({kind}) ← [{label}] — {p['title']}")
        lines.append(f"     점검항목: {p['item']}")
        lines.append(f"     점검내용: {p['content']}")

    for w in state.get("output_warnings", []):
        lines.append(f"⚠ {w}")

    answer = "\n".join(lines)
    return {
        "answer": answer,
        "trace": [{"step": "format_answer", "input": {"coverage": len(coverage), "proposals": len(proposals)}, "output": answer[:200]}],
    }
