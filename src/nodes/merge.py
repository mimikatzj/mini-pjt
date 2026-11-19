"""merge_proposals 노드 (배치 모드 8단계 뒷부분): 오늘의 제안을 누적 제안과 비교해 새 제안과 근거 추가를 결정한다.

SERVICE.md 9장:
- 비교 후보는 코드로 좁힌다: 수정 제안은 대상 항목 ID가 같은 것, 신규 제안은 같은 분야의 신규 제안.
  비교 대상은 DB의 누적 제안(records.proposals)과 오늘 앞서 새로 등록한 제안 모두다 (같은 날 중복도 합친다).
- 후보마다 LLM(고성능 모델)에 "같은 보완 내용인가"를 예/아니오(Pydantic)로만 묻는다. 후보가 없으면 LLM 없이 새 제안.
- 반려 목록(records.rejected, 2차)과 같은 제안은 다시 올리지 않는다.
- DB에는 쓰지 않는다. 결정(proposal_plan)만 상태에 담고, 저장은 commit_records(4-9)가 한 트랜잭션으로 한다.
"""

from datetime import datetime
from functools import lru_cache

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field

from src import config
from src.llm import get_structured_llm
from src.tools import KST


class SameCheck(BaseModel):
    candidate_no: int = Field(description="후보 번호 (입력에 주어진 번호 그대로)")
    same: bool = Field(description="같은 보완 내용이면 true")
    reason: str = Field(description="판정 이유 한 문장")


class MergeResult(BaseModel):
    checks: list[SameCheck]


@lru_cache(maxsize=1)
def build_chain() -> Runnable:
    system = (config.PROMPTS_DIR / "merge_proposals.md").read_text(encoding="utf-8")
    prompt = ChatPromptTemplate.from_messages([
        ("system", system),
        ("human", "<new_proposal>\n{new}\n</new_proposal>\n\n<candidates>\n{candidates}\n</candidates>"),
    ])
    return prompt | get_structured_llm("smart", MergeResult)


def _describe(p: dict) -> str:
    kind = f"수정: {p.get('target_id')}" if p.get("type") == "수정" else f"신규: {p.get('area') or '분야 미정'}"
    return f"({kind}) 점검항목: {p.get('item')} / 점검내용: {p.get('content')}"


SIMILAR_TOP = 3          # 문장이 비슷한 후보를 몇 개까지 더할지
SIMILAR_MIN = 0.2        # 형태소 겹침 비율(자카드) 하한


def _tokens(text: str) -> set[str]:
    from src.retriever import korean_tokenizer  # 검색과 같은 형태소 분석 (kiwipiepy)

    return {t for t in korean_tokenizer(text or "") if len(t) > 1}


def candidates_for(p: dict, pool: list[dict]) -> list[dict]:
    """코드로 비교 후보를 좁힌다 (계산 규칙).

    1) 규칙 후보: 수정 제안은 대상 항목 ID가 같은 수정 제안, 신규 제안은 같은 분야의 신규 제안 (SERVICE.md 9장)
    2) 문장 후보: 점검내용의 형태소 겹침 비율이 높은 제안 상위 3개 (유형·대상이 달라도)
       — 같은 요구사항이라도 LLM이 실행마다 다른 항목에 붙이거나 수정/신규를 바꿔 쓰는 경우가 있다 (4-6 시험에서 확인).
    """
    if p.get("type") == "수정":
        rule = [c for c in pool if c.get("type") == "수정" and c.get("target_id") == p.get("target_id")]
    else:
        rule = [c for c in pool if c.get("type") == "신규" and (c.get("area") or "") == (p.get("area") or "")]
    mine = _tokens(f"{p.get('item', '')} {p.get('content', '')}")
    scored = []
    for c in pool:
        if c in rule:
            continue
        theirs = _tokens(f"{c.get('item', '')} {c.get('content', '')}")
        jac = len(mine & theirs) / len(mine | theirs) if mine and theirs else 0.0
        if jac >= SIMILAR_MIN:
            scored.append((jac, c))
    similar = [c for _, c in sorted(scored, key=lambda x: -x[0])[:SIMILAR_TOP]]
    return rule + similar


def find_same(p: dict, cands: list[dict]) -> tuple[dict | None, list[dict]]:
    """후보 중 같은 보완 내용인 첫 후보와 판정 기록을 돌려준다. 후보가 없으면 LLM을 부르지 않는다."""
    if not cands:
        return None, []
    result: MergeResult = build_chain().invoke({
        "new": _describe(p),
        "candidates": "\n".join(f"{i}. {_describe(c)}" for i, c in enumerate(cands, 1)),
    })
    checks = [c.model_dump() for c in result.checks if 1 <= c.candidate_no <= len(cands)]
    for c in checks:
        if c["same"]:
            return cands[c["candidate_no"] - 1], checks
    return None, checks


def next_id(existing_ids: list[str], today: str) -> str:
    """P-YYYYMMDD-NN. 같은 날짜의 가장 큰 번호 다음 번호 (코드)."""
    prefix = f"P-{today}-"
    nums = [int(i[len(prefix):]) for i in existing_ids if i.startswith(prefix) and i[len(prefix):].isdigit()]
    return f"{prefix}{max(nums, default=0) + 1:02d}"


def merge_proposals(state: dict) -> dict:
    records = state.get("records") or {}
    existing = [dict(p, origin="db") for p in records.get("proposals", [])]
    rejected = [dict(r, origin="rejected", type=r.get("type")) for r in records.get("rejected", [])]
    today = datetime.now(KST).strftime("%Y%m%d")
    ids = [p["id"] for p in existing]

    plan: list[dict] = []          # 결정: new / attach / rejected
    created: list[dict] = []       # 오늘 새로 등록한 제안 (뒤의 제안과 비교 대상)
    articles = []
    for a in state.get("articles", []):
        a = dict(a)
        decided = []
        for p in a.get("proposals", []):
            evidence = {"item_id": a["item"]["id"], "url": a["item"]["url"], "title": a["item"]["title"]}
            # 1) 반려 목록과 같으면 다시 올리지 않는다 (2차. 지금은 반려 목록이 비어 있다)
            hit, checks = find_same(p, candidates_for(p, rejected))
            if hit:
                plan.append({"action": "rejected", "proposal": p, "matched": hit.get("proposal_id"), "checks": checks, "evidence": evidence})
                decided.append({**p, "proposal_id": None, "merge": "반려 목록과 같아 제외"})
                continue
            # 2) 누적 제안 + 오늘 앞서 만든 제안과 비교
            hit, checks = find_same(p, candidates_for(p, existing + created))
            if hit:
                plan.append({"action": "attach", "proposal_id": hit["id"], "proposal": p, "checks": checks, "evidence": evidence,
                             "from_today": hit.get("origin") == "today"})
                decided.append({**p, "proposal_id": hit["id"], "merge": f"기존 제안 {hit['id']}에 근거 추가"})
                continue
            # 3) 새 제안
            new_id = next_id(ids, today)
            ids.append(new_id)
            created.append({**p, "id": new_id, "origin": "today"})
            plan.append({"action": "new", "proposal_id": new_id, "proposal": p, "checks": checks, "evidence": evidence})
            decided.append({**p, "proposal_id": new_id, "merge": "새 제안"})
        a["proposals"] = decided
        articles.append(a)

    counts = {k: sum(1 for x in plan if x["action"] == k) for k in ("new", "attach", "rejected")}
    return {
        "articles_merged": articles,
        "proposal_plan": plan,
        "trace": [{"step": "merge_proposals", "input": {"proposals": len(plan), "existing": len(existing)},
                   "output": {"counts": counts, "decisions": [
                       {"action": x["action"], "id": x.get("proposal_id"), "target": x["proposal"].get("target_id") or x["proposal"].get("area"),
                        "title": x["proposal"].get("title", "")[:50], "from": x["evidence"]["title"][:40]} for x in plan]}}],
    }
