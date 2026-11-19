"""그래프 상태와 LLM 출력 스키마.

LLM 출력은 모두 Pydantic 모델로 받고, 코드에서 한 번 더 검증한다 (CLAUDE.md 구현 원칙).
"""

import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field

Category = Literal["침해사고", "취약점", "법·제도", "기타"]


class NewsSummary(BaseModel):
    """기사 1건 요약 (summarize_news 출력). 카테고리에 맞는 핵심 정보 필드만 채우고 나머지는 비운다."""

    category: Category = Field(
        description="침해사고: 특정 조직이 실제로 당한 공격·유출 사고 보도 / "
        "취약점: 제품 취약점 공지, 기관의 보안 권고문·주의보(공격 그룹의 수법과 권고 대책을 알리는 문서 포함) / "
        "법·제도: 보안·개인정보 관련 법령·고시·가이드라인 제정·개정, 과징금·처분, 감독기관 조사 / "
        "기타: 위에 해당하지 않는 소식(제품 출시, 행사, 인사, 실적, 예산, 보안과 무관한 규제 변경 등)"
    )
    title: str = Field(description="기사 내용을 나타내는 한국어 제목 한 줄")
    summary: str = Field(description="기사 핵심을 한국어 3~5문장으로 요약")
    core_issue: str = Field(
        default="",
        description="체크리스트로 점검할 핵심 대상 1개 (기타면 빈 칸). 침해사고: 사고를 일으킨 가장 결정적인 원인 / "
        "법·제도: 가장 중요한 변경 내용 / 취약점: 가장 중요한 권고 대책. 한 문장으로 구체적으로. "
        "침해사고는 결과(유출됐다)가 아니라 원인(어떻게 뚫렸나)을 쓴다",
    )

    # 침해사고
    causes: list[str] = Field(default_factory=list, description="[침해사고] 사고 원인. 원인이 여러 개면 나눠서 적는다")
    attack_path: str = Field(default="", description="[침해사고] 공격·유출 경로 (최초 침투부터 피해까지)")
    countermeasures: list[str] = Field(
        default_factory=list, description="[침해사고·취약점] 기사에 나온 대응책이나 권고된 보안 대책"
    )

    # 법·제도
    changed_clauses: list[str] = Field(default_factory=list, description="[법·제도] 바뀌는 조항과 바뀌는 내용")
    effective_date: str = Field(default="", description="[법·제도] 시행일 또는 시행 예정일 (기사에 없으면 빈 칸)")
    stage: str = Field(default="", description="[법·제도] 진행 단계: 입법예고, 국회 통과, 국무회의 의결, 공포, 시행 중 하나 (기사에 없으면 빈 칸)")
    targets: str = Field(default="", description="[법·제도] 적용 대상")


class ExpandedQuery(BaseModel):
    """점검 대상 1개의 확장 검색어."""

    target_no: int = Field(description="점검 대상 번호 (입력에 주어진 번호 그대로)")
    queries: list[str] = Field(description="체크리스트 표현으로 바꾼 검색어 2~3개")


class QueryExpansion(BaseModel):
    """쿼리 확장 체인 출력 (retrieve_checklist)."""

    items: list[ExpandedQuery]


class RelevanceScore(BaseModel):
    """후보 항목 1개 × 점검 대상 1개의 관련도."""

    doc_id: str = Field(description="후보 체크리스트 항목 ID만 (예: 2.9.2). 대괄호나 항목 이름은 붙이지 않는다")
    target_no: int = Field(description="점검 대상 번호")
    score: int = Field(ge=0, le=3, description="3: 이 대상을 직접 다룸 / 2: 관련 통제를 다룸 / 1: 일부만 관련 / 0: 무관")


class RerankResult(BaseModel):
    """리랭킹 체인 출력. 관련도 1점 이상인 쌍만 적는다."""

    scores: list[RelevanceScore]


Verdict = Literal["있음", "없음", "수정 필요"]


class Judgment(BaseModel):
    """점검 대상 1개에 대한 판정 (LLM 출력)."""

    target_no: int = Field(description="점검 대상 번호 (입력에 주어진 번호 그대로)")
    verdict: Verdict = Field(description="있음 / 없음 / 수정 필요")
    evidence_ids: list[str] = Field(
        description="근거 체크리스트 항목 ID 목록 (예: [\"2.9.2\"]). reason에서 언급한 항목 ID를 반드시 여기에도 적는다. "
        "대괄호나 항목 이름은 붙이지 않는다. 근거 항목이 정말 없을 때만 빈 목록"
    )
    reason: str = Field(description="판정 이유. 항목의 어느 문장이 대상을 다루는지 또는 무엇이 빠졌는지")


class CoverageResult(BaseModel):
    """coverage_check 체인 출력."""

    judgments: list[Judgment]


class ProposalDraft(BaseModel):
    """체크리스트 변경 제안 1건 (LLM 출력). 형식은 SERVICE.md 14장 proposals.md 항목과 같다."""

    judgment_no: int = Field(description="이 제안의 근거가 된 판정 번호 (입력에 주어진 번호 그대로)")
    type: Literal["수정", "신규"] = Field(description="수정: 기존 항목 보완 / 신규: 새 항목 추가")
    target_id: str = Field(default="", description="[수정] 보완할 기존 항목 ID (판정의 근거 항목 중 하나). 신규면 빈 칸")
    area: str = Field(default="", description="[신규] 새 항목이 들어갈 분야 (주어진 분야 목록 중 하나). 수정이면 빈 칸")
    title: str = Field(description="제안을 한 줄로 나타내는 제목 (예: 랜섬웨어 대비 백업 네트워크 분리)")
    item: str = Field(description="점검항목. 수정이면 기존 항목명 그대로, 신규면 새 항목명")
    content: str = Field(description="점검내용. 체크리스트에 그대로 붙여 쓸 수 있는 문장으로, '~하는지 확인'으로 끝낸다")


class ProposalResult(BaseModel):
    """draft_proposals 체인 출력."""

    proposals: list[ProposalDraft]


class BatchState(TypedDict, total=False):
    """배치 모드 그래프 상태 (SERVICE.md 8·9장)."""

    dry_run: bool
    ignore_seen: bool                # 시험용: 이미 보낸 URL 제외를 끈다 (같은 기사로 두 번 실행해 제안 병합 확인)
    checklist_ids: list[str]         # load_checklist
    changed_items: list[str]         # load_checklist (P2 반영완료 감지용)
    records: dict                    # load_records: seen_urls, proposals, rejected, recent_coverage
    raw_items: list[dict]            # collect_news
    collect_failures: list[dict]     # collect_news: 메일 하단 "수집 실패" 표시용
    new_items: list[dict]            # dedupe → guard_input
    top_news: list[dict]             # triage_news: 선별된 기사
    articles: Annotated[list[dict], operator.add]  # process_article: 기사별 결과 (병렬 실행 결과를 모은다)
    articles_merged: list[dict]      # merge_proposals: 제안마다 제안 번호(P-...)와 병합 결과를 붙인 기사별 결과
    proposal_plan: list[dict]        # merge_proposals: new(새 제안) / attach(근거 추가) / rejected(반려 목록과 같음) 결정
    mail: dict                       # compose_email: subject, html, text, counts
    sent: dict                       # send_email: {"status": sent / failed / skipped(dry-run), "to", "error"}
    trace: Annotated[list[dict], operator.add]


class QueryState(TypedDict, total=False):
    """질의 모드 그래프 상태 (SERVICE.md 10장)."""

    question: str                    # 사용자 입력 (기사 URL 또는 본문)
    input_kind: str                  # guard_query 판별 결과: article / url / refuse
    url: str                         # 입력이 URL일 때
    refusal: str                     # 거절·안내 메시지
    article: str                     # 점검할 기사 본문
    summary: NewsSummary             # summarize_news 결과
    contexts: list[dict]             # retrieve_checklist 결과: [{"doc_id", "text"}]
    past_coverage: list[dict]        # 배치 모드만: 최근 30일 판정 기록 (장기 메모리, coverage_check가 참고)
    coverage: list[dict]             # coverage_check 결과
    proposals: list[dict]            # draft_proposals 결과 (질의 모드에서는 저장하지 않음)
    output_warnings: list[str]       # guard_output이 답변에 붙이는 확인 필요 안내
    answer: str                      # format_answer 결과
    trace: Annotated[list[dict], operator.add]  # 노드마다 {"step", "input", "output"}를 덧붙인다
