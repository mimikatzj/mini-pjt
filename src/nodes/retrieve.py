"""retrieve_checklist 노드: 기사 요약 → 관련 체크리스트 항목 (contexts).

1. 쿼리 확장 (LLM, 저가 모델): 점검 대상(원인·변경조항·대책)마다 체크리스트 표현의 검색어 2~3개를 만든다.
2. 후보 검색 (코드): 대상마다 [원래 문구 + 확장 검색어]로 하이브리드 검색 후 순위 합산(RRF) → 상위 CANDIDATES_PER_TARGET개.
3. 리랭킹 (LLM, 저가 모델): 후보 × 대상 쌍마다 관련도 0~3점.
4. 최종 선택 (코드): 대상마다 가장 관련 높은 항목을 1개씩 먼저 넣고, 남은 자리를 점수순으로 채운다.
   최대 개수는 config.RETRIEVE_K. 단 대상 수가 더 많으면 대상 수만큼 (대상마다 근거 후보가 하나는 있어야 하므로).
"""

from functools import lru_cache

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from src import config
from src.llm import get_structured_llm
from src.nodes.coverage import coverage_targets
from src.retriever import fused_search, load_checklist, normalize_id, to_document
from src.state import QueryExpansion, QueryState, RerankResult

# 리랭킹 전 대상 1개당 후보 수. 5 → 8 (3-6: 검색어 여러 개 중 하나에서만 1위인 항목이 순위 합산에서 밀려 후보에 못 드는 사례)
CANDIDATES_PER_TARGET = 8
MAX_EXPANDED = 3           # 대상 1개당 확장 검색어 최대 개수
MIN_SCORE = 1              # 이 점수 미만은 최종 결과에 넣지 않는다
GUARANTEE_MIN_SCORE = 2    # 대상마다 보장해서 넣는 항목의 최소 점수
GUARANTEE_PER_TARGET = 2   # 대상마다 보장하는 항목 수 (1위 포함)


# ── 1. 쿼리 확장 ─────────────────────────────────────

@lru_cache(maxsize=1)
def build_expand_chain() -> Runnable:
    system = (config.PROMPTS_DIR / "expand_queries.md").read_text(encoding="utf-8")
    prompt = ChatPromptTemplate.from_messages([
        ("system", system),
        ("human", "점검항목 이름 목록: {item_names}\n\n<targets>\n{targets}\n</targets>"),
    ])
    return prompt | get_structured_llm("fast", QueryExpansion)


def expand_queries(targets: list[str]) -> dict[int, list[str]]:
    """{대상 번호: [원래 문구, 확장 검색어...]}. 확장 결과가 빠진 대상은 원래 문구만 쓴다."""
    names = sorted({row["점검항목"] for row in load_checklist().values()})
    result: QueryExpansion = build_expand_chain().invoke({
        "item_names": ", ".join(names),
        "targets": "\n".join(f"{i}. {t}" for i, t in enumerate(targets, 1)),
    })
    expanded = {item.target_no: item.queries for item in result.items}
    out = {}
    for i, t in enumerate(targets, 1):
        extra = [q.strip() for q in expanded.get(i, []) if q.strip() and q.strip() != t][:MAX_EXPANDED]
        out[i] = [t, *extra]
    return out


# ── 3. 리랭킹 ───────────────────────────────────────

@lru_cache(maxsize=1)
def build_rerank_chain() -> Runnable:
    system = (config.PROMPTS_DIR / "rerank.md").read_text(encoding="utf-8")
    prompt = ChatPromptTemplate.from_messages([
        ("system", system),
        ("human", "<targets>\n{targets}\n</targets>\n\n<candidates>\n{candidates}\n</candidates>"),
    ])
    return prompt | get_structured_llm("fast", RerankResult)


def rerank(targets: list[str], candidates: dict[int, list[str]]) -> dict[int, list[tuple[str, int]]]:
    """대상별 후보에 LLM 관련도를 매겨 {대상 번호: [(항목 ID, 점수), ...]}를 점수 내림차순으로 돌려준다.

    LLM이 점수를 주지 않은 후보는 0점. 같은 점수는 검색 순위(후보 목록 순서)를 따른다.
    """
    checklist = load_checklist()
    all_ids = list(dict.fromkeys(i for ids in candidates.values() for i in ids))
    result: RerankResult = build_rerank_chain().invoke({
        "targets": "\n".join(f"{i}. {t}" for i, t in enumerate(targets, 1)),
        "candidates": "\n\n".join(
            f"[{i}] {checklist[i]['점검항목']}\n점검내용: {checklist[i]['점검내용']}\n확인사항: {checklist[i]['확인사항']}"
            for i in all_ids
        ),
    })
    # 코드 검증: 후보에 있는 ID와 유효한 대상 번호만 받는다
    given = {}
    for s in result.scores:
        doc_id = normalize_id(s.doc_id)
        if doc_id in all_ids and 1 <= s.target_no <= len(targets):
            given[(doc_id, s.target_no)] = s.score
    ranked = {}
    for no, ids in candidates.items():
        scored = [(i, given.get((i, no), 0)) for i in ids]
        # 다른 대상의 후보로 들어왔지만 이 대상과 관련 있다고 평가된 항목도 포함한다
        scored += [(i, s) for (i, n), s in given.items() if n == no and i not in ids]
        order = {i: k for k, (i, _) in enumerate(scored)}
        ranked[no] = sorted(scored, key=lambda x: (-x[1], order[x[0]]))
    return ranked


# ── 4. 최종 선택 ─────────────────────────────────────

def select_final(ranked: dict[int, list[tuple[str, int]]], limit: int) -> list[str]:
    """최종 근거 항목을 고른다.

    1) 대상마다 최고 점수 항목을 넣는다 (점수가 0이어도 검색 1위를 넣어 대상마다 후보가 하나는 있게 한다).
    2) 대상마다 2점 이상인 항목을 GUARANTEE_PER_TARGET개까지 더 보장한다. 이 단계는 limit을 넘을 수 있다.
       (1차 평가: 2점 받은 관련 항목이 다른 대상의 같은 점수 항목에 밀려 잘린 사례가 있었다 → 3-6 개선)
    3) 남은 자리는 limit까지 점수순으로 채운다.
    """
    final: list[str] = []
    for no in sorted(ranked):
        if ranked[no] and ranked[no][0][0] not in final:
            final.append(ranked[no][0][0])
    for no in sorted(ranked):
        strong = [i for i, s in ranked[no] if s >= GUARANTEE_MIN_SCORE][:GUARANTEE_PER_TARGET]
        for i in strong:
            if i not in final:
                final.append(i)
    rest = sorted(
        ((s, i) for items in ranked.values() for i, s in items if s >= MIN_SCORE and i not in final),
        key=lambda x: -x[0],
    )
    for _, i in rest:
        if len(final) >= limit:
            break
        if i not in final:
            final.append(i)
    return final


def retrieve_checklist(state: QueryState) -> dict:
    targets = coverage_targets(state["summary"])
    queries = expand_queries(targets)
    candidates = {no: [d.metadata["id"] for d in fused_search(q, k=CANDIDATES_PER_TARGET)] for no, q in queries.items()}
    ranked = rerank(targets, candidates)
    limit = max(config.RETRIEVE_K, len(targets))
    final_ids = select_final(ranked, limit)

    checklist = load_checklist()
    contexts = [{"doc_id": i, "text": to_document(checklist[i]).page_content} for i in final_ids]  # 검색 청크와 같은 형식
    return {
        "contexts": contexts,
        "trace": [{
            "step": "retrieve_checklist",
            "input": {"targets": targets, "queries": {str(no): q for no, q in queries.items()}},
            "output": {
                "before_rerank": {str(no): ids for no, ids in candidates.items()},
                "after_rerank": {str(no): [f"{i}:{s}" for i, s in r] for no, r in ranked.items()},
                "final": final_ids,
            },
        }],
    }
