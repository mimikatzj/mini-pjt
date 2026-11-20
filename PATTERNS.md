# Day 1~7 패턴 활용 정리

보안 데일리 에이전트가 교육(Day 1~7)에서 배운 패턴 중 **무엇을 어디에 썼고, 무엇을 왜 쓰지 않았는지** 정리한 문서입니다. 2026-11-20 기준(M5 ReAct 점검 반영 후)입니다.

- ✅ 사용: 실제 코드에 들어간 패턴
- ➖ 사용 안 함: 이 서비스에 필요 없거나, 다른 방식으로 대신한 패턴 (이유를 적음)

## 한눈에 보기

| Day | 주제 | 사용 | 대표 위치 |
|---|---|---|---|
| 1 | LCEL 체인, Pydantic 구조화 출력 | ✅ | LLM 단계 8개 전부 `프롬프트 \| 모델.with_structured_output(...)` |
| 2 | RAG (임베딩, 벡터 저장소, 하이브리드 검색, 근거 검증) | ✅ | [src/retriever.py](src/retriever.py), [src/nodes/retrieve.py](src/nodes/retrieve.py) |
| 3 | LangGraph (상태, 조건부 분기, 순환, 리듀서), ReAct | ✅ | [src/agent.py](src/agent.py), [src/nodes/coverage_react.py](src/nodes/coverage_react.py) |
| 4 | 도구 (`@tool`, `ToolNode`, 도구 오류 처리) | ✅ | [src/tools.py](src/tools.py) 체크리스트 도구 2개 |
| 4 | MCP | ➖ | — |
| 5 | 가드레일, 미들웨어(재시도·대체 모델·마스킹·로깅) | ✅ | [src/guards.py](src/guards.py), [src/llm.py](src/llm.py) |
| 5 | 체크포인터, interrupt(HITL), 대화 요약 | ➖ | — |
| 6 | 멀티 에이전트 (라우터, 슈퍼바이저, 전문 에이전트) | ➖ | — |
| 7 | 장기 메모리, 로컬 트레이서, LLM-as-Judge 평가 | ✅ | [src/tools.py](src/tools.py), [src/tracer.py](src/tracer.py), [evaluation/run_eval.py](evaluation/run_eval.py) |
| 7 | Plan-and-Execute | ➖ | — |

---

## Day 1 — LCEL 체인, Pydantic 구조화 출력 ✅

실습: `day1_practice/lcel_basic.py`, `structured_output.py`

모든 LLM 단계를 `프롬프트 | 모델.with_structured_output(Pydantic 모델)` 체인으로 만들었습니다. LLM 출력은 자유 문장이 아니라 Pydantic 객체로 받고, 받은 뒤 코드로 한 번 더 검증합니다.

| 단계 | 체인 위치 | 출력 스키마 | 모델 |
|---|---|---|---|
| 선별 (분류) | [src/nodes/triage.py:44](src/nodes/triage.py#L44) | `TriageResult` | fast (Haiku) |
| 요약 | [src/nodes/summarize.py:24](src/nodes/summarize.py#L24) | `NewsSummary` | fast |
| 검색어 확장 | [src/nodes/retrieve.py:38](src/nodes/retrieve.py#L38) | `QueryExpansion` | fast |
| 리랭킹 | [src/nodes/retrieve.py:65](src/nodes/retrieve.py#L65) | `RerankResult` | fast |
| 점검 (고정 순서) | [src/nodes/coverage.py:50](src/nodes/coverage.py#L50) | `CoverageResult` | smart (Sonnet) |
| 점검 판정 (ReAct 뒤) | [src/nodes/coverage_react.py:163](src/nodes/coverage_react.py#L163) | `CoverageResult` | smart |
| 제안 작성 | [src/nodes/proposals.py:38](src/nodes/proposals.py#L38) | `ProposalResult` | smart |
| 제안 병합 | [src/nodes/merge.py:40](src/nodes/merge.py#L40) | `MergeResult` | smart |
| 평가 채점관 2개 | [evaluation/run_eval.py:110](evaluation/run_eval.py#L110), [164행](evaluation/run_eval.py#L164) | `JudgeResult`, `RagasJudge` | 고정 모델 |

- 스키마는 [src/state.py](src/state.py)에 모아 두었습니다.
- 모델은 코드에 직접 쓰지 않고 [src/config.py](src/config.py)의 `MODELS`에서 용도(fast/smart)별로 받습니다.
- 출력 검증 예: 판정에 인용된 체크리스트 ID가 `data/checklist.csv`에 없거나 이번 검색 결과에 없으면 "판단 근거 없음"으로 바꿉니다 ([src/nodes/coverage.py:65](src/nodes/coverage.py#L65) `validate`).

## Day 2 — RAG ✅

실습: `day2_practice/embedding.py`, `vectorstore.py`, `retriever.py`, `hybrid.py`, `rag_chain.py`, `verify.py`

| 패턴 | 사용 | 내용 |
|---|---|---|
| 임베딩 | ✅ | `BedrockEmbeddings`(Titan Text Embeddings V2) |
| 벡터 저장소 | ✅ | Chroma, `data/chroma/` ([src/retriever.py:66](src/retriever.py#L66) `build_index`) |
| 문서 + 메타데이터 | ✅ | 체크리스트 항목 1개 = `Document` 1개, 항목 ID·분야를 메타데이터로 ([src/retriever.py:52](src/retriever.py#L52) `to_document`) |
| 청킹 (TextSplitter) | ➖ | 체크리스트 항목이 이미 짧고 독립된 단위라 자르지 않음. 항목을 자르면 ID와 내용이 갈라짐 |
| 하이브리드 검색 | ✅ | BM25(kiwipiepy 형태소) + 벡터, `EnsembleRetriever`로 합침 ([src/retriever.py:116](src/retriever.py#L116) `search`) |
| 여러 검색어 결과 합치기 | ✅ | 확장 검색어별 결과를 RRF로 합침 ([src/retriever.py:132](src/retriever.py#L132) `fused_search`) |
| 쿼리 확장 · 리랭킹 | ✅ | 기사 표현을 체크리스트 표현으로 바꿔 검색 → LLM 리랭킹 → 대상별 근거 보장 ([src/nodes/retrieve.py](src/nodes/retrieve.py)) |
| 환각 방지 · 근거 검증 | ✅ | 인용 ID 존재 확인, 출력 가드레일이 원문에 없는 숫자·형식 오류를 걸러냄 ([src/guards.py:126](src/guards.py#L126) `check_output`) |

## Day 3 — LangGraph, ReAct ✅

실습: `day3_practice/state_schema.py`, `reducer.py`, `conditional.py`, `cycle.py`, `react_agent.py`

| 패턴 | 사용 | 내용 |
|---|---|---|
| 상태 스키마 | ✅ | `QueryState`, `BatchState` ([src/state.py](src/state.py)) |
| 리듀서 | ✅ | `trace`·`articles`를 `Annotated[list, operator.add]`로 누적 (병렬 기사 결과 모으기), ReAct 대화는 `add_messages` |
| 조건부 분기 | ✅ | 입력 판별(URL/본문/거절), 점검 대상 유무, 발송 성공 여부 ([src/agent.py:61](src/agent.py#L61)~, [172행](src/agent.py#L172)) |
| 병렬 실행 (`Send`) | ✅ | 선별된 기사를 기사별 하위 그래프로 동시 3건 처리 ([src/agent.py:177](src/agent.py#L177)) |
| 순환 (cycle) | ✅ | ReAct 점검: 에이전트 → 도구 → 에이전트 … ([src/nodes/coverage_react.py:82](src/nodes/coverage_react.py#L82)) |
| ReAct | ✅ (질의 모드만) | 첫 검색 뒤 에이전트가 체크리스트를 더 찾을지·원문을 열어 볼지·마칠지 스스로 결정. 호출 상한 4회는 코드가 끊음 ([src/nodes/coverage_react.py](src/nodes/coverage_react.py)) |

**ReAct를 질의 모드에만 쓴 이유**: 배치는 매일 같은 순서·비용으로 돌아야 하고 뒤에 메일 발송·DB 저장이 이어져 고정 순서 그래프가 맞습니다. 질의 모드는 사람이 기사 1건을 맡기는 화면이라 몇 초 더 걸려도 근거를 더 확인하는 편이 낫습니다. 4차 평가에서 품질 지표는 같고 시간은 +36%였습니다 ([evaluation/round4_report.md](evaluation/round4_report.md)). 방식은 [src/config.py:68](src/config.py#L68) `COVERAGE_MODE`로 바꿀 수 있습니다.

## Day 4 — 도구 ✅ / MCP ➖

실습: `day4_practice/tools_basic.py`, `toolnode_parallel.py`, `tool_error.py`, `mcp_*.py`

| 패턴 | 사용 | 내용 |
|---|---|---|
| `@tool` + Pydantic 인자 스키마 | ✅ | `search_checklist(query)`, `get_checklist_item(item_id)` — 읽기 전용 도구 2개 ([src/tools.py:670](src/tools.py#L670), [686행](src/tools.py#L686)) |
| `bind_tools` | ✅ | 재시도·대체 모델·마스킹이 붙은 래퍼로 ([src/llm.py:96](src/llm.py#L96) `get_tool_llm`) |
| `ToolNode` | ✅ | 도구 실행 노드. `handle_tool_errors=True`로 인자 오류를 에이전트에게 돌려줘 스스로 고치게 함 (실습 `tool_error.py`) |
| 도구 결과에 부가 정보 | ✅ | `response_format="content_and_artifact"`: 모델에는 텍스트만, 코드에는 조회한 항목 ID 목록을 넘겨 인용 검증에 씀 |
| MCP 서버·클라이언트 | ➖ | 도구가 2개뿐이고 같은 프로세스 안의 체크리스트만 읽음. 별도 서버로 나눌 이유가 없음 |

메일 발송, DB 쓰기, URL 접속은 **일부러 도구로 주지 않았습니다.** 되돌리기 어려운 동작은 LLM 판단이 아니라 그래프(코드)가 정한 시점에만 실행합니다.

## Day 5 — 가드레일, 미들웨어 ✅ / 체크포인터·HITL ➖

실습: `day5_practice/guards_*.py`, `logging_middleware.py`, `middleware_order.py`, `checkpointer_basic.py`, `interrupt_basic.py`, `hitl_flow.py`, `summarization_demo.py`

| 패턴 | 사용 | 내용 |
|---|---|---|
| 입력 가드레일 | ✅ | 질의 판별, 인젝션 의심 문구 탐지(수집 본문·질의 입력 모두), 길이 제한 ([src/guards.py:76](src/guards.py#L76) `check_query`, [41행](src/guards.py#L41) `INJECTION_PATTERNS`) |
| 거절 응답 | ✅ | 인젝션·기사 아닌 입력에 LLM을 부르지 않고 코드에서 거절 문구 반환 |
| 출력 가드레일 | ✅ | 판정 값·ID·형식, 원문에 없는 숫자 검사 ([src/guards.py:126](src/guards.py#L126)) |
| SSRF 차단 | ✅ | 사내망·사설 IP URL 차단 ([src/tools.py:64](src/tools.py#L64) `check_url`) |
| 미들웨어: 개인정보 마스킹 | ✅ | 모델에 보내기 직전 전화번호·이메일·주민번호 등을 가림 ([src/guards.py:29](src/guards.py#L29) `mask_pii`) |
| 미들웨어: 재시도 · 대체 모델 | ✅ | `with_retry` → `with_fallbacks`, 순서는 마스킹 → 1순위 모델 → 대체 모델 ([src/llm.py:116](src/llm.py#L116)) |
| 미들웨어: 로깅 | ✅ | 대체 모델 사용·마스킹 개수를 기록해 trace에 남김 |
| 체크포인터 | ➖ | 대화형이 아니라 한 번 실행하고 끝나는 구조. 배치는 실패하면 처음부터 다시 돌려도 중복이 없도록 "발송 성공 후 한 번만 저장"으로 설계 |
| interrupt / HITL | ➖ | 사람 검토는 실행 중이 아니라 **메일을 받은 뒤** 담당자가 함 (제안은 초안일 뿐, 체크리스트는 자동 수정하지 않음). 회신 메일로 승인·반려는 2차 범위 |
| 대화 요약 | ➖ | 대화 기록이 쌓이지 않는 구조 |

## Day 6 — 멀티 에이전트 ➖

실습: `day6_practice/router_only.py`, `specialist_agents.py`, `two_agent_supervisor.py`, `supervisor_assembled.py`

**사용하지 않았습니다.** 하는 일이 "기사 요약 → 체크리스트 점검 → 제안" 하나의 흐름이고, 전문 분야가 나뉘지 않습니다. 에이전트 여럿이 서로 넘기면 호출 수와 흔들림만 늘어납니다. 대신 단계별로 **모델을 나눠** 씁니다 (선별·요약·검색 보조는 Haiku, 점검·제안·병합은 Sonnet). 입력 종류에 따른 갈림(URL/본문/거절)은 라우터 에이전트가 아니라 코드 분기로 처리합니다.

## Day 7 — 장기 메모리, 트레이스, 평가 ✅ / Plan-and-Execute ➖

실습: `day7_practice/long_term_memory.py`, `local_tracer.py`, `trace_run.py`, `llm_judge.py`, `run_eval.py`, `guards_input.py`, `guards_refusal.py`, `plan_and_execute.py`

| 패턴 | 사용 | 내용 |
|---|---|---|
| 장기 메모리 | ✅ | SQLite(`data/agent.db`)에 판정·제안 누적. 다음 점검에서 같은 항목의 최근 판정을 참고하고, 제안 병합에서 기존 제안과 합침 ([src/tools.py:422](src/tools.py#L422) `load_records`, [src/nodes/coverage.py:56](src/nodes/coverage.py#L56) `format_past`) |
| 메모리 쓰기 규칙 | ✅ | 쓰기는 메일 발송 성공 후 `commit_records` 한 곳, 한 트랜잭션. 질의 모드는 읽기 전용으로만 엶 ([src/tools.py:460](src/tools.py#L460) `read_recent_coverage`) |
| 로컬 트레이서 | ✅ | 콜백 핸들러로 LLM·노드 실행을 JSONL로 기록 ([src/tracer.py:33](src/tracer.py#L33) `FileTracer`), 노드마다 `{step, input, output}`을 API 응답 `trace`로 반환 |
| LLM-as-Judge | ✅ | 고정 채점관 모델로 기대 특성·금지 항목 판정, 코드 검사와 AND ([evaluation/run_eval.py:99](evaluation/run_eval.py#L99)) |
| RAGAS 지표 | ✅ | 라이브러리 없이 같은 정의로 계산: recall·precision은 코드, faithfulness·relevancy는 채점관 LLM |
| 평가 반복 | ✅ | 1~4차 평가 (60% → 85% → 95% → ReAct 90%, 개선 후 6·13번 재확인) |
| Plan-and-Execute | ➖ | 계획이 필요 없을 만큼 단계가 고정되어 있음. 계획은 그래프 자체가 맡음 |

---

## 정리

- **12개 빌드 체크리스트 관점**: LCEL·구조화 출력, RAG(하이브리드·리랭킹), LangGraph(분기·병렬·순환), ReAct, 도구, 가드레일(입력·출력), 미들웨어, 장기 메모리, 트레이스, 평가를 사용했습니다. 상세 대응표는 [SERVICE.md 6.3](SERVICE.md)을 참고하세요.
- **쓰지 않은 것**(MCP, 체크포인터·HITL, 멀티 에이전트, Plan-and-Execute)은 몰라서 뺀 것이 아니라 **이 서비스의 흐름이 하나로 고정되어 있고, 사람 검토가 실행 밖(메일 수신 후)에 있기 때문**입니다.
- 원칙: **계산·규칙은 코드, 판단만 LLM.** LLM이 도구를 고르는 곳은 질의 모드 점검의 읽기 전용 조사 하나뿐이고, 되돌리기 어려운 동작은 모두 그래프가 정합니다.
