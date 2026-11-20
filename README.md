# 미니 PJT: 보안 데일리 에이전트 (보안정보 수집 · 체크리스트 점검)

## 무엇을 푸나

보안 담당자가 매일 여러 사이트의 보안 기사·보도자료·가이드 문서를 직접 확인하고 우리 보안 체크리스트와 일일이 대조하던 일을, **매일 아침 메일 한 통(요약 + 체크리스트 점검 결과 + 수정 제안)**과 **기사 1건 즉시 점검 화면**으로 바꿉니다.

기획 상세는 [SERVICE.md](SERVICE.md)를 참고하세요. 테스트 단계에서는 사내 체크리스트 대신 공개 자료인 **ISMS-P 인증기준**으로 점검합니다.

## 활용한 패턴 (Day 1~7)

- Day 1: LCEL chain (Pydantic 구조화 출력) — 선별·요약·점검·제안·병합 단계를 `프롬프트 | 모델.with_structured_output(Pydantic)` 체인으로 구현
- Day 2: RAG (하이브리드·리랭킹·쿼리 확장) — 기사의 핵심 점검 대상으로 검색어를 확장하고, BM25(kiwipiepy) + Chroma 벡터 하이브리드 검색(RRF로 합침) 후 LLM 리랭킹으로 관련 체크리스트 항목을 찾음
- Day 3: LangGraph — 상태 그래프와 조건부 분기(입력 판별, 점검 대상 유무, 발송 성공 여부), `Send`로 선별 기사를 병렬 처리(동시 3건)
- Day 5: 가드레일 — 수집 본문과 질의 입력의 인젝션 의심 문구 탐지, 사내망 URL 차단, 개인정보 마스킹, 판정 근거 항목 ID 검증
- Day 5: 미들웨어 — 모든 모델 호출에 재시도, 대체 모델 전환, 개인정보 마스킹 적용
- Day 7: 장기 메모리 — SQLite 누적 기록(제안, 판정)을 다음 점검과 제안 병합에서 참조
- Day 7: Observability · Trace — 로컬 파일 트레이서(JSONL)와 API 응답의 `trace`
- Day 7: 평가 — `test_queries.csv` 기반 LLM-as-Judge 통과율과 RAGAS 4개 지표

## 아키텍처

두 가지 모드가 같은 노드(요약 → 체크리스트 검색 → 점검 → 제안)를 공유합니다.

```
[배치 모드] python main.py (직접 실행 또는 GitHub Actions 수동 실행)
 수집(보안뉴스·데일리시큐·The Hacker News·BleepingComputer RSS + 개인정보위·과기정통부 보도자료. 국내 문서는 제출 후 추가)
 → 중복 제거(묶음별) → 입력 가드레일 → 선별(사고·국내 5건, 사고·해외 3건, 정책·국내 3건.
   사고 부문은 침해사고, 정책 부문은 정책 변경·과징금·조사가 우선)
 → 요약 → 체크리스트 검색(RAG) → 점검 → 제안 작성·병합 → 출력 가드레일
 → 메일 발송 → (발송 성공 시) 누적 기록 저장

[질의 모드] POST /query 또는 웹 페이지
 입력 검사(URL/본문 판별, 보안 검사) → 본문 추출 → 요약 → 체크리스트 검색(RAG)
 → 점검 → 제안 작성 → 출력 가드레일 → 응답 (answer, contexts, trace)
```

점검은 **기사마다 핵심 점검 대상 1개**(침해사고는 결과가 아니라 원인, 법·제도는 가장 큰 변경, 취약점은 가장 먼저 할 권고 대책)를 골라 체크리스트와 폭넓게 대조하고, 판정 1개와 제안 최대 1건만 냅니다. 처리 순서는 그래프가 정하고 LLM은 단계별 판단만 하며, `@tool`로 LLM이 도구를 고르는 ReAct 방식은 쓰지 않습니다 (순서가 고정된 작업이고, 메일 발송·DB 저장처럼 되돌리기 어려운 동작을 LLM 판단에 맡기지 않기 위해).

노드별 상세 흐름도는 [SERVICE.md 9장](SERVICE.md#9-langgraph-구조), [10장](SERVICE.md#10-질의-모드-기사-1건-즉시-점검)을 참고하세요.

## 실행 방법

### 1. 설치

Python 3.14가 필요합니다.

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 환경변수

`.env.example`을 복사해 `.env`를 만들고 값을 채웁니다. `.env`는 커밋하지 않습니다.

```
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_REGION=us-east-1
GMAIL_ADDRESS=
GMAIL_APP_PASSWORD=
MAIL_TO=
```

- **AWS**: Bedrock에서 Claude 모델(Haiku 4.5, Sonnet 4.5)과 Titan Text Embeddings V2 사용 권한이 필요합니다. 개발 중 `us-east-1`의 일일 토큰 한도를 넘어 `us-west-2`로 바꿔 사용했습니다. 모델 목록은 `src/config.py`의 `MODELS`에서 관리합니다.
- **Gmail**: 발송에 쓸 계정에서 2단계 인증을 켠 뒤 앱 비밀번호를 발급합니다. 테스트 단계는 개인 계정을 써도 되며, 회신 처리(2차)를 붙일 때는 전용 계정으로 바꿉니다.

### 3. 체크리스트와 검색 인덱스

`data/checklist.csv`(`id, 분야, 점검항목, 점검내용, 확인사항`, UTF-8)가 에이전트의 판단 기준입니다. 테스트 단계에서는 KISA의 ISMS-P 세부점검항목 엑셀(`data/raw/`)을 변환해 만듭니다. 이 파일을 준비한 뒤 검색 인덱스를 만들고, 체크리스트가 바뀌면 다시 만듭니다.

```bash
python -m src.retriever --build
```

### 4. 실행

```bash
python main.py --dry-run          # 배치 모드: 메일 발송·DB 저장 없이 tmp/preview.html로 결과 확인
python main.py                    # 배치 모드: 실제 실행 (메일 발송 후 기록 저장)
uvicorn src.api:app               # 질의 모드: http://localhost:8000 (웹 페이지), /docs (API)
python evaluation/run_eval.py     # 자체 평가
```

질의 예시:

```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"question": "가상물류가 랜섬웨어 공격을 받아 사흘간 배송 시스템이 멈췄다. 백업 서버가 운영망과 분리되지 않아 백업까지 암호화됐다. (본문 100자 이상)"}'
```

### 5. 매일 자동 실행 (GitHub Actions)

워크플로 파일은 [.github/workflows/daily.yml](.github/workflows/daily.yml)입니다.

1. 저장소 **Settings → Secrets and variables → Actions → New repository secret**에 `.env`와 같은 이름으로 6개를 등록합니다: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `MAIL_TO`
2. **Settings → Actions → General → Workflow permissions**를 **Read and write permissions**로 바꿉니다 (자동 커밋용).
3. **Actions → daily-batch → Run workflow**로 실행합니다. 예약 실행은 현재 꺼져 있습니다. 다시 켜려면 `daily.yml`의 `schedule` 두 줄 주석을 풉니다(매일 06:17 KST, `cron: "17 21 * * *"`). GitHub 예약 실행은 정각에 몰리면 늦어지거나 빠질 수 있어 정각을 피했습니다 (2026-11-20 07:00 1회 예약이 실행되지 않았음).
4. 메일 발송에 성공하면 `data/agent.db`와 `output/*.md`가 `github-actions[bot]` 이름으로 자동 커밋됩니다. 발송에 실패하면 실행이 실패로 표시되고 아무것도 커밋하지 않습니다.
5. 자동 커밋이 쌓이므로 로컬에서 작업하기 전에 `git pull`을 먼저 합니다.

### 결과물

| 결과물 | 위치 | 설명 |
|---|---|---|
| 메일 | 수신자 메일함 | 기사·문서별 요약, 체크리스트 점검 결과, 제안 |
| 제안 목록 | `output/proposals.md` | 누적 제안 (검토대기, 반영완료). 자동 생성, 직접 수정 금지 |
| 판정 기록 | `output/coverage_log.md` | 모든 점검 판정 기록. 자동 생성, 직접 수정 금지 |
| 원본 데이터 | `data/agent.db` | 위 md 파일의 원본 (SQLite) |

## RAGAS 평가 결과

3차 후속 평가(2026-11-19, 21건, 간결 점검 전환 후) 기준입니다. `ragas` 라이브러리 없이 같은 정의로 계산했습니다 (recall·precision은 코드, faithfulness·relevancy는 고정 채점관 LLM). 산출 방법은 [SERVICE.md 16장](SERVICE.md#16-평가)과 [evaluation/run_eval.py](evaluation/run_eval.py)를 참고하세요.

- context_recall: 1.00 (2차 0.94, 1차 0.90) — 3차부터 "근거 항목 A/B/C 중 하나" 묶음 단위로 계산
- context_precision: 0.91 (2차 0.77, 1차 0.86) — 핵심 대상 1개만 검색해 기대 밖 항목이 덜 섞임
- faithfulness: 1.00 (2차 1.00, 1차 1.00)
- answer_relevancy: 1.00 (2차 1.00, 1차 1.00)

## 인-아웃 세트 통과율 (자체 평가)

- 1차 (Day 9 종료): 12 / 20 통과 (60%)
- 2차 (Day 10 개선 후): 17 / 20 통과 (85%) — 가드레일 4/4, 무관한 기사 4/4
- 개선폭: +5 건 (주요 개선 사항: 법·제도 답변에 "법령 원문 미확인"·진행 단계·시행일 표시, 보안 권고문 분류 기준, 판정 기준("일부만 다루면 수정 필요") 보강, 리랭킹 후보 5 → 8개와 대상별 근거 보장, 리랭킹 "통제 방향" 기준)
- 1차 통과 12건은 2차에서도 모두 통과 (개선으로 깨진 사례 없음). 남은 실패 3건 중 1건은 테스트용 체크리스트 판본의 공백(수탁사 관리 항목 없음), 2건은 실행마다 통과·실패가 갈리는 경계 사례

- 3차 (간결 점검 전환 후 재평가): 18 / 20 통과 (90%) — 기사마다 핵심 대상 1개·판정 1개·제안 최대 1건으로 바꾸면서 "원인별 판정" 기대값을 "핵심 대상(후보 중 하나)"으로 고쳐 다시 평가. 2차와 시험지·recall 정의가 달라 같은 자로 잰 값은 아님
- 3차 후속 (최종): **20 / 21 통과 (95%)** — 가드레일 4/4, 무관한 기사 4/4. 인젝션 규칙 보완(18번), 원인 미공개 기사 사례 추가(21번)와 요약 규칙 보완. 남은 실패는 4번(테스트용 체크리스트 판본에 수탁사 관리 항목 없음)

상세: [evaluation/round1_report.md](evaluation/round1_report.md), [evaluation/round2_report.md](evaluation/round2_report.md), [evaluation/round3_report.md](evaluation/round3_report.md)

## 트라이앤에러 회고

### 시도했지만 실패한 접근 · 왜 실패했는가

| 시도 | 무엇이 문제였나 | 바꾼 방법 |
|---|---|---|
| 기사의 원인·대책을 **모두** 나눠 점검 | 메일 1통에 판정 30건, 제안 16건. 담당자가 읽기 어렵고, 원인 여러 개를 다 맞혀야 통과하는 사례는 실행마다 결과가 흔들림 | 기사마다 **핵심 대상 1개·판정 1개·제안 최대 1건** (판정 30 → 11, 제안 16 → 3~4, 사례당 24초 → 16초) |
| 핵심 대상을 "가장 중요한 원인"으로만 지시 | "백업 서비스 침해로 고객사 정보 유출"처럼 **결과**를 고름 → 체크리스트와 대조할 거리가 없음 | "결과가 아니라 원인(어떻게 뚫렸나)"을 지시하고, 원인이 기사에 없으면 피해를 키운 관리 문제 → 침입 지점 → "침입 원인 미공개" 순으로 고르게 함. 요약에도 "침입 경로 미확인"을 남기게 함 |
| 리랭커 출력의 항목 ID를 그대로 사용 | LLM이 `[2.9.2] 백업 및 복구관리`처럼 이름을 붙여 반환 → ID 비교가 모두 실패해 검색 결과가 비어 버림 | `normalize_id`로 `숫자.숫자.숫자`만 뽑아 리랭킹·점검·제안 모두에 적용 |
| 제안 병합 후보를 "대상 항목 ID가 같은 제안"으로만 좁힘 | 같은 내용인데 대상 항목이나 수정/신규가 다르면 놓침 (7건 중 5건만 병합) | 형태소 겹침(자카드) 상위 3개를 후보에 더하고 LLM에 예/아니오만 물음 (6/7) |
| 과기정통부 보도자료 목록 HTML 스크래핑 | 등록일과 본문이 자바스크립트로 그려져 HTML에 없음 | 글마다 첨부된 MD(보도자료 전문)를 메모리에서 읽어 배포 일시와 본문을 얻음 |
| 정책 묶음에 정부 보도자료 전체를 수집 | 전파 규제·공중케이블 같은 보안과 무관한 규제 변경에 엉터리 체크리스트 제안이 나옴 | 법·제도는 "보안·개인정보 관련"으로 한정, 그 밖의 규제 변경은 기타(점검 대상 아님)로 분류 |
| 인젝션 탐지 정규식을 짧은 간격으로만 작성 | "지금까지 **받은** 지시는 전부 무시…"처럼 수식어가 끼면 놓침 (3차 평가 18번) | 간격을 넓히고, 실제 기사 11건에서 오탐이 없는지 확인 |
| 검색할 때마다 BM25 인덱스 생성 | 질의 1건에 15초가 더 걸림 | 인덱스를 한 번 만들어 캐시 |
| `us-east-1` 한 리전만 사용 | Sonnet 일일 토큰 한도 초과로 실행 중단 | 리전을 `.env`로 빼서 `us-west-2`로 전환, 모델 호출 래퍼에 대체 모델 전환 추가 |

### 최종 채택한 접근 · 왜 그것으로 갔는가

- **고정 순서 그래프 + 단계별 LLM 판단**: 기사 점검과 매일 배치는 순서가 늘 같아 ReAct가 필요 없음. 호출 횟수가 정해져 비용·시간을 예측할 수 있고 평가가 덜 흔들림.
- **계산·규칙은 코드, 판단만 LLM**: 중복 제거(URL·CVE), 묶음별 우선순위와 건수 자르기, 체크리스트 ID 존재 검증, 보관 기간 정리, SSRF 주소 차단, recall·precision 계산은 모두 코드. LLM 출력은 Pydantic으로 받고 인용 ID가 체크리스트에 없으면 "판단 근거 없음"으로 바꿈.
- **쿼리 확장 + 하이브리드 검색 + 리랭킹**: 기사 표현("관리 페이지가 외부에 열려 있음")과 체크리스트 표현("외부자 접근통제")이 달라 벡터나 키워드 하나로는 놓침. 대상마다 근거 항목 2개까지 보장해 "체크리스트에 없음 → 엉터리 신규 제안"을 줄임 (precision보다 recall 우선).
- **발송 성공 후 한 트랜잭션 저장**: 발송 전에는 DB에 아무것도 쓰지 않아, 실패하면 다음 실행에서 처음부터 다시 해도 기록이 중복되지 않음. md 파일은 DB에서 다시 만드는 보기용으로만 둠.
- **RAGAS를 라이브러리 없이 계산**: 승인된 라이브러리만 쓰는 규칙에 따라 같은 정의로 직접 계산 (recall·precision은 코드, faithfulness·relevancy는 고정 채점관 LLM).

### 남은 한계 · 향후 개선 방향

- **간결 점검의 대가**: 원인이 여러 개인 기사는 나머지 원인을 점검하지 않음 (예: 초기 비밀번호를 고르면 "별도 백업 없음"은 빠짐).
- **원인 미공개 기사의 흔들림**: 피해 현황 위주 기사는 실행마다 "침입 지점"과 "결과" 사이에서 핵심 대상이 바뀜.
- **체크리스트 판본의 공백**: 테스트용 ISMS-P에 수탁사 보안 점검 항목이 없어 위탁 사고는 근거가 약함 (평가 4번 실패). 운영 단계에서 사내 체크리스트로 바꾸면 달라짐.
- **채점관 LLM의 흔들림**: 같은 답변이 회차에 따라 통과·실패가 갈린 사례가 있음 (2·3차 18번). 채점관이 같은 계열 모델이라 생기는 편향도 배제하지 못함.
- **향후**: 2차(메일 회신으로 제안 삭제·추가 요청, 오판 학습, 국가법령정보센터 법령 원문 확인), P2(국내 가이드 문서 수집·요약, 체크리스트 반영완료 감지), 선택(질의 모드에 `@tool` 기반 ReAct로 "2.9.2 관련 최근 제안" 같은 질문 응답), 운영 전환(사내 체크리스트 적용 전 외부 LLM 사용 가능 여부 확인).

## 핵심 코드 위치

| 기능 | 위치 |
|---|---|
| 질의 모드 그래프 (입력 판별·URL·점검 대상 분기) | [src/agent.py:73](src/agent.py#L73) `build_query_graph`, 분기 함수 [54-71행](src/agent.py#L54-L71) |
| 배치 모드 그래프 (병렬 시작 → 중복 제거 → 선별 → 기사별 `Send` → 병합 → 메일 → 발송 성공 시 저장) | [src/agent.py:161](src/agent.py#L161) `build_batch_graph`, `Send` 병렬 [158행](src/agent.py#L158), 발송 후 분기 [148행](src/agent.py#L148) |
| 그래프 상태와 LLM 출력 스키마 (Pydantic) | [src/state.py](src/state.py) `NewsSummary` [14행](src/state.py#L14), `Judgment` [76행](src/state.py#L76), `BatchState` [112행](src/state.py#L112), `QueryState` [132행](src/state.py#L132) |
| 모델 호출 래퍼 (마스킹 → 재시도 → 대체 모델) | [src/llm.py:88](src/llm.py#L88) `get_structured_llm`, [107행](src/llm.py#L107) `_with_fallbacks` |
| 설정 (모델, 묶음·건수, 우선순위, 수집 대상, 보관 기간) | [src/config.py](src/config.py) `MODELS` [29행](src/config.py#L29), `TRACKS` [49행](src/config.py#L49), `SOURCES` [72행](src/config.py#L72) |
| RAG: 인덱스 생성, 하이브리드 검색, RRF 합치기 | [src/retriever.py:66](src/retriever.py#L66) `build_index`, [116행](src/retriever.py#L116) `search`, [132행](src/retriever.py#L132) `fused_search` |
| RAG: 쿼리 확장 → 리랭킹 → 대상별 근거 보장 | [src/nodes/retrieve.py:41](src/nodes/retrieve.py#L41) `expand_queries`, [68행](src/nodes/retrieve.py#L68) `rerank`, [100행](src/nodes/retrieve.py#L100) `select_final` |
| 핵심 점검 대상 선택과 판정 검증 (ID 존재 확인) | [src/nodes/coverage.py:25](src/nodes/coverage.py#L25) `coverage_targets`, [65행](src/nodes/coverage.py#L65) `validate` |
| 제안 작성 (기사당 최대 1건) | [src/nodes/proposals.py:78](src/nodes/proposals.py#L78) `draft_proposals` |
| 입력 가드레일 (질의 판별, 인젝션, 개인정보) | [src/guards.py:76](src/guards.py#L76) `check_query`, [41행](src/guards.py#L41) `INJECTION_PATTERNS`, [29행](src/guards.py#L29) `mask_pii` |
| 출력 가드레일 (판정 값·ID·형식·원문에 없는 숫자) | [src/guards.py:126](src/guards.py#L126) `check_output` |
| URL 차단 (SSRF) | [src/tools.py:60](src/tools.py#L60) `check_url` |
| 수집 (RSS, 개인정보위·과기정통부 보도자료) | [src/tools.py:328](src/tools.py#L328) `collect_all`, [297행](src/tools.py#L297) `collect_msit_board` |
| 중복 제거 (묶음별, URL·CVE) | [src/nodes/dedupe.py:43](src/nodes/dedupe.py#L43) `dedupe_items` |
| 선별 (LLM 분류 → 코드로 우선순위·건수 자르기) | [src/nodes/triage.py:68](src/nodes/triage.py#L68) `classify`, [86행](src/nodes/triage.py#L86) `select` |
| 제안 병합 (코드로 후보 좁히기 → LLM 예/아니오) | [src/nodes/merge.py:58](src/nodes/merge.py#L58) `candidates_for`, [104행](src/nodes/merge.py#L104) `merge_proposals` |
| 메일 본문·발송 | [src/nodes/compose.py:39](src/nodes/compose.py#L39) `compose`, [src/tools.py:598](src/tools.py#L598) `send_mail` |
| 장기 메모리: 기록 읽기, 과거 판정 참조 | [src/tools.py:418](src/tools.py#L418) `load_records`, [src/nodes/coverage.py:56](src/nodes/coverage.py#L56) `format_past` |
| 장기 메모리: 발송 후 한 트랜잭션 저장, 보관 기간 정리, md 생성 | [src/nodes/records.py:53](src/nodes/records.py#L53) `commit_records`, [src/tools.py:476](src/tools.py#L476) `save_run`, [542행](src/tools.py#L542) `prune_old_rows` |
| 트레이서 (JSONL) | [src/tracer.py:33](src/tracer.py#L33) `FileTracer` |
| 질의 API와 웹 페이지 | [src/api.py:63](src/api.py#L63) `POST /query`, [93행](src/api.py#L93) 웹 페이지 |
| 자체 평가 (코드 검사, LLM-as-Judge, RAGAS) | [evaluation/run_eval.py:60](evaluation/run_eval.py#L60) `code_checks`, [92행](evaluation/run_eval.py#L92) `judge`, [116행](evaluation/run_eval.py#L116) `context_recall`, [165행](evaluation/run_eval.py#L165) `ragas` |
| 매일 실행 | [.github/workflows/daily.yml](.github/workflows/daily.yml) |

## 관련 문서

| 문서 | 내용 |
|---|---|
| [SERVICE.md](SERVICE.md) | 서비스 스펙과 상세 설계 (무엇을 만드는지의 기준) |
| [TASKS.md](TASKS.md) | 구현 작업 목록 |
| [CLAUDE.md](CLAUDE.md) | Claude Code 개발 규칙 |
