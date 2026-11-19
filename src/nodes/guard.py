"""질의 모드 입구 노드: guard_query(입력 판별·보안 검사), refuse(안내 메시지).

판정 규칙은 src/guards.py의 check_query에 있다 (코드 판정, LLM 호출 없음).
"""

from src.guards import check_query
from src.state import QueryState


def guard_query(state: QueryState) -> dict:
    result = check_query(state["question"])
    update: dict = {"input_kind": result["kind"]}
    if result["kind"] == "article":
        update["article"] = result["article"]
    elif result["kind"] == "url":
        update["url"] = result["url"]  # 주소 차단 검사와 본문 추출은 fetch_article에서 한다
    else:
        update["refusal"] = result["message"]
    return {
        **update,
        "trace": [{
            "step": "guard_query",
            "input": {"length": len(state["question"]), "head": state["question"][:100]},
            "output": {k: v for k, v in result.items() if k != "article"},
        }],
    }


def refuse(state: QueryState) -> dict:
    """거절·안내. LLM을 부르지 않고 안내 문구를 그대로 답한다."""
    return {
        "answer": state["refusal"],
        "trace": [{"step": "refuse", "input": state.get("input_kind"), "output": state["refusal"]}],
    }
