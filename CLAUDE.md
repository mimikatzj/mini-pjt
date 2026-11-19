# 작업 규칙

## 기준 문서

- 무엇을 만드는지는 SERVICE.md가 기준이다. 애매하면 추측하지 말고 물어본다.
- **P1 범위만 구현한다.** SERVICE.md에서 **(P2)** 로 표시된 부분(스크래핑 수집기 — 단 개인정보위·과기정통부 보도자료 목록은 P1, 국내 문서 수집·요약, 반영완료 감지)과 **(2차)** 로 표시된 부분(회신 메일 처리, 버튼, 반려 목록, 오판 학습, 법령 원문 확인)은 사용자가 요청하기 전까지 만들지 않는다.
- 구현 순서와 작업 단위는 TASKS.md가 기준이다. 묶음(M) 순서대로, 위에서부터 진행한다.
- 한 번에 TASKS.md의 작업 하나만 한다. 작업의 **확인** 줄을 실제로 통과한 뒤에 `[x]`로 완료 표시를 한다.

## 스택

- Python 3.14 (실습 환경과 동일), LangChain(LCEL), LangGraph
- LLM: **Amazon Bedrock**. 실습과 같은 `langchain-aws`의 `ChatBedrockConverse`를 쓴다. 리전은 `.env`의 `AWS_REGION`으로 정한다 (기본 `us-east-1`. 2026-11-19 `us-east-1`의 Sonnet 일일 토큰 한도 초과로 `us-west-2`로 변경해 사용 중). 임베딩은 `BedrockEmbeddings`(`amazon.titan-embed-text-v2:0`)를 쓴다.
- 모델은 코드에 직접 쓰지 않고 설정 파일(`src/config.py`)에서 관리한다.

  | 용도 | 노드 | 1순위 | 대체 모델 (실습에서 동작 확인) |
  |---|---|---|---|
  | 선별·요약·검색 보조 | `triage_news`, `summarize_news`, `summarize_document`, `retrieve_checklist` | Claude Haiku 5.5 | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
  | 점검·제안·병합·재확인 | `coverage_check`, `draft_proposals`, `merge_proposals`, `recheck_proposals` | Claude Sonnet 5.5 | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` |

- Claude 5.5 모델의 Bedrock 모델 ID는 추측해서 쓰지 않는다. 사용자에게 확인받은 ID를 설정 파일에 넣고, 확인 전에는 대체 모델로 동작하게 한다.
- 모든 LLM 호출은 공통 래퍼(`src/llm.py`)를 거친다. 일시적 오류는 재시도하고, 계속 실패하면 대체 모델로 전환해 실행이 중단되지 않게 한다(`with_retry`, `with_fallbacks`). 모델에 보내기 전 개인정보를 마스킹한다.
- LLM 단계는 `프롬프트 | 모델.with_structured_output(Pydantic 모델)` 형태의 LCEL 체인으로 만든다.

## 라이브러리

- 승인된 라이브러리만 사용한다. Day 1~7 실습 구성을 기준으로 한다.

  | 용도 | 라이브러리 |
  |---|---|
  | LLM·체인·그래프 | `langchain`, `langchain-core`, `langchain-aws`, `langgraph`, `pydantic` |
  | RAG | `langchain-text-splitters`, `chromadb`, `langchain-chroma`, `rank-bm25`, `kiwipiepy`, `langchain-community`(`BM25Retriever`), `langchain-classic`(`EnsembleRetriever`) |
  | 환경변수 | `python-dotenv` |
  | 수집 | `feedparser`(RSS), `requests`, `beautifulsoup4`(스크래핑) |
  | 문서 텍스트 추출 | `pypdf`(PDF), `pyhwp`(HWP). HWPX는 기본 라이브러리로 XML 파싱 |
  | 메일·웹 템플릿 | `jinja2` |
  | 질의 API | `fastapi`, `uvicorn` |
  | 기본 라이브러리 | `sqlite3`, `smtplib`, `email`, `csv`, `zipfile`, `xml`, `ipaddress` 등 |

- 위 목록에 없는 프레임워크나 라이브러리는 **절대 사용자의 허락 없이 들이지 않는다.** LangSmith, RAGAS 라이브러리 등 외부 서비스·라이브러리도 마찬가지다.
## 참고 자료

교육 후 실습으로 진행하는 프로젝트이므로, **구현 방식은 교육 자료와 실습 코드를 먼저 따른다.** 같은 기능을 새로운 방식으로 만들기 전에 실습 코드에 같은 패턴이 있는지 먼저 확인한다.

| 자료 | 위치 | 용도 |
|---|---|---|
| 교육 강의 자료 | https://ucanlabs.kr/classroom/sds-ax/ | Day 1~7 패턴 설명, 미니 PJT 산출물 규약(폴더 구조, API 스펙, SERVICE.md·README.md 템플릿, test_queries.csv 스키마, 12개 패턴 빌드 체크리스트) |
| 실습 코드 (GitHub) | https://github.com/mimikatzj/sds-ax-practice | Day 1~7 실습 코드 원본 |
| 실습 코드 (로컬 사본) | `c:\Users\SDS\Desktop\sds-ax-practice` | 위 저장소를 내려받은 사본. 코드를 참고할 때는 로컬 사본을 먼저 읽는다 |

- 강의 자료는 접근 코드가 필요하다. 코드는 이 파일에 적지 않고, 필요하면 사용자에게 받는다. 강의 자료는 외부에 공유하거나 그대로 옮겨 싣지 않는다.
- 패턴별로 참고할 실습 파일

  | 패턴 | 실습 파일 (`sds-ax-practice/`) |
  |---|---|
  | LCEL chain, Pydantic 구조화 출력 | `day1_practice/lcel_basic.py`, `day1_practice/structured_output.py` |
  | RAG (임베딩, 벡터 저장소, 하이브리드 검색) | `day2_practice/embedding.py`, `day2_practice/vectorstore.py`, `day2_practice/retriever.py`, `day2_practice/hybrid.py`, `day2_practice/rag_chain.py` |
  | LangGraph 상태·조건부 분기 | `day3_practice/conditional.py`, `day3_practice/cycle.py` |
  | ReAct, 도구 (2·4번 확장 시) | `day3_practice/react_agent.py`, `day4_practice/toolnode_parallel.py` |
  | 미들웨어, 가드레일 | `day5_practice/` (`guards_output.py`, `logging_middleware.py`, `middleware_order.py` 등), `day7_practice/guards_input.py`, `day7_practice/guards_refusal.py` |
  | 장기 메모리 | `day7_practice/long_term_memory.py` |
  | 트레이스 | `day7_practice/local_tracer.py`, `day7_practice/trace_run.py` |
  | 평가 (LLM-as-Judge) | `day7_practice/llm_judge.py`, `day7_practice/run_eval.py` |

- 실습 코드에 없는 기능(RSS·스크래핑 수집, PDF·HWP 추출, 메일 발송, FastAPI 등)은 깃허브의 괜찮은 레퍼런스 코드를 참고한다.

## 디렉터리 구조

과제 산출물 규약에 맞춘다.

```
mini-pjt/
├─ src/
│  ├─ agent.py        # 배치 모드·질의 모드 그래프
│  ├─ state.py        # 그래프 상태
│  ├─ nodes/          # 노드 함수
│  ├─ tools.py        # 수집기, 본문 추출, 메일 발송, DB 접근
│  ├─ retriever.py    # RAG 인덱스 생성과 체크리스트 검색
│  ├─ guards.py       # 입력·출력 가드레일
│  ├─ llm.py          # 모델 호출 래퍼 (재시도·대체 모델·마스킹)
│  ├─ tracer.py       # 로컬 파일 트레이서
│  ├─ api.py          # FastAPI (POST /query, 웹 페이지)
│  ├─ config.py       # 설정값
│  ├─ prompts/        # 프롬프트
│  └─ templates/      # 메일·웹 페이지 Jinja2 템플릿
├─ scripts/
│  └─ build_checklist.py  # ISMS-P 엑셀 → data/checklist.csv 변환 (1회성)
├─ data/
│  ├─ raw/            # 체크리스트 원본 (ISMS-P 세부점검항목 엑셀)
│  ├─ samples/        # 개발·테스트용 샘플 기사 본문
│  ├─ checklist.csv   # 에이전트의 판단 기준 (원본)
│  ├─ chroma/         # 검색 인덱스 (커밋 안 함, --build로 재생성)
│  └─ agent.db        # 누적 기록 (M4)
├─ evaluation/        # test_queries.csv, run_eval.py, round1·2_report.md
├─ output/            # 보기용 md (자동 생성)
├─ tmp/               # dry-run 미리보기, 첨부 임시 파일 (커밋 안 함)
├─ trace/             # 트레이스 JSONL (커밋 안 함)
├─ main.py            # 배치 모드 진입점
├─ requirements.txt
├─ .env.example       # 환경변수 이름만 (값 없음)
├─ .gitignore
└─ SERVICE.md, README.md, TASKS.md, CLAUDE.md
```

- `Dockerfile`, `run.sh`(과제 규약의 선택 항목)는 만들지 않는다.
- 제출 zip의 최상위 폴더명은 과제 규약대로 `mini-pjt_{본인이름}`으로 한다 (M5).

## 구현 원칙

- **계산과 규칙 판정은 LLM이 아니라 파이썬 코드로 한다.** 필요한 경우에만 LLM 판정을 덧붙인다. (예: URL·CVE 중복 제거, 우선순위 정렬과 묶음별 건수(사고·국내 5, 사고·해외 3, 정책·국내 3) 자르기, 보관 기간 정리, 입력 보안 검사, context_recall·precision 계산은 코드)
- **LLM 출력은 Pydantic으로 받고 코드로 검증한다.** 판정에 인용된 체크리스트 항목 ID가 `data/checklist.csv`에 실제로 있는지 반드시 확인하고, 없으면 "판단 근거 없음"으로 바꾼다.
- **외부 텍스트는 데이터로만 취급한다.** 기사·문서 본문과 질의 입력은 프롬프트에서 구분 표시로 감싸고, 그 안의 지시문을 따르지 않게 한다.
- **저장은 메일 발송 성공 후 한 번만 한다.** 모든 DB 쓰기는 `commit_records` 노드에서 하나의 트랜잭션으로 처리한다. 다른 노드에서는 DB에 쓰지 않는다. 질의 모드는 DB에 쓰지 않는다. (SERVICE.md 8장 10단계, 17장)
- **`output/*.md`는 DB에서 생성하는 보기용 파일이다.** 직접 수정하는 코드를 만들지 않고, 파일 맨 위에 "자동 생성 파일입니다. 직접 수정하지 마세요"를 넣는다.
- **수집기 하나가 실패해도 전체 실행은 계속한다.** 실패한 사이트는 메일 하단에 "수집 실패: ○○"로 표시한다. 문서 한 건의 요약 실패도 마찬가지다.
- **첨부 파일은 임시 폴더에만 내려받고 요약이 끝나면 삭제한다.** 저장소에 커밋되지 않게 `.gitignore`에 넣는다.
- **모든 노드는 상태의 `trace` 목록에 `{"step", "input", "output"}`을 남긴다.** API 응답의 `trace`로 그대로 쓴다.
- **실제 체크리스트는 자동으로 수정하지 않는다.** 에이전트는 제안까지만 한다.
- 다음 값은 설정 파일(`src/config.py`)에 둔다: 모델과 대체 모델 목록, 선별 묶음과 건수(사고·국내 5, 사고·해외 3, 정책·국내 3), 부문별 카테고리 우선순위, 문서 최대 쪽수(30), 검색 결과 수(5), 수집 대상 URL, 메일 수신자, DB 보관 기간, 질의 입력 최대 길이(2만 자).

## 비밀 정보

- API 키와 계정 정보는 `.env`에서 읽는다. GitHub Actions에서는 같은 이름의 Secrets를 환경변수로 넘긴다.
- 환경변수 이름

  | 이름 | 내용 |
  |---|---|
  | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` | Bedrock 호출용 AWS 인증 정보와 리전 (`us-east-1`) |
  | `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` | 메일 발송용 Gmail 계정과 앱 비밀번호 (테스트 단계는 사용자 개인 계정, 2차 회신 처리 전에 전용 계정으로 전환) |
  | `MAIL_TO` | 메일 수신자 |

- `.env`는 커밋하지 않는다. 값 없이 키 이름만 적은 `.env.example`을 저장소에 둔다. 제출 저장소가 public이므로 특히 주의한다.

## 실행과 테스트

- 배치 모드 개발 중에는 `python main.py --dry-run`으로 실행한다. dry-run은 메일을 보내지 않고 DB에도 저장하지 않으며, 메일 본문을 `tmp/preview.html`로 저장한다.
- 실제 발송은 `python main.py`로 한다.
- 질의 모드는 `uvicorn src.api:app`으로 실행한다.
- 평가는 `python evaluation/run_eval.py`로 실행한다.
- `tmp/`, `trace/`는 `.gitignore`에 넣는다.

## 응답

- 설명과 코드 주석은 한국어로 작성한다.
