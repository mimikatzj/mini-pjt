# 미니 PJT: 보안 데일리 에이전트 (보안정보 수집 · 체크리스트 점검)

## 무엇을 푸나

보안 담당자가 매일 여러 사이트의 보안 기사·보도자료·가이드 문서를 직접 확인하고 우리 보안 체크리스트와 일일이 대조하던 일을, **매일 아침 메일 한 통(요약 + 체크리스트 점검 결과 + 수정 제안)**과 **기사 1건 즉시 점검 화면**으로 바꿉니다.

기획 상세는 [SERVICE.md](SERVICE.md)를 참고하세요. 테스트 단계에서는 사내 체크리스트 대신 공개 자료인 **ISMS-P 인증기준**으로 점검합니다.

## 활용한 패턴 (Day 1~7)

- Day 1: LCEL chain (Pydantic 구조화 출력) — 선별·요약·점검·제안 단계를 `프롬프트 | 모델 | Pydantic 출력` 체인으로 구현
- Day 2: RAG (하이브리드·리랭킹·쿼리 확장) — 기사의 원인·대책으로 검색어를 확장하고, BM25(kiwipiepy) + Chroma 벡터 하이브리드 검색 후 LLM 리랭킹으로 관련 체크리스트 항목을 찾음
- Day 5: 가드레일 — 수집 본문과 질의 입력의 인젝션 의심 문구 탐지, 사내망 URL 차단, 개인정보 마스킹, 판정 근거 항목 ID 검증
- Day 5: 미들웨어 — 모든 모델 호출에 재시도, 대체 모델 전환, 개인정보 마스킹 적용
- Day 7: 장기 메모리 — SQLite 누적 기록(제안, 판정)을 다음 점검과 제안 병합에서 참조
- Day 7: Observability · Trace — 로컬 파일 트레이서(JSONL)와 API 응답의 `trace`
- Day 7: 평가 — `test_queries.csv` 기반 LLM-as-Judge 통과율과 RAGAS 4개 지표

## 아키텍처

두 가지 모드가 같은 노드(요약 → 체크리스트 검색 → 점검 → 제안)를 공유합니다.

```
[배치 모드] python main.py (제출 후: 매일 07:00 GitHub Actions)
 수집(RSS. 스크래핑·국내 문서는 제출 후 추가) → 중복 제거 → 입력 가드레일 → 선별(상위 5건)
 → 요약 → 체크리스트 검색(RAG) → 점검 → 제안 작성·병합 → 출력 가드레일
 → 메일 발송 → (발송 성공 시) 누적 기록 저장

[질의 모드] POST /query 또는 웹 페이지
 입력 검사(URL/본문 판별, 보안 검사) → 본문 추출 → 요약 → 체크리스트 검색(RAG)
 → 점검 → 제안 작성 → 출력 가드레일 → 응답 (answer, contexts, trace)
```

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

- **AWS**: Bedrock에서 Claude 모델과 Titan Text Embeddings V2 사용 권한이 필요합니다.
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
  -d '{"question": "○○사가 랜섬웨어 공격을 받아 3일간 서비스가 중단됐다. 공격자는 ..."}'
```

### 5. 매일 자동 실행 (GitHub Actions, 제출 후 구현 예정)

과제 제출 시점에는 `python main.py`를 직접 실행합니다. 자동 실행은 제출 후 아래 방식으로 추가합니다.

1. Settings → Secrets and variables → Actions에 위 환경변수를 같은 이름으로 등록합니다.
2. 워크플로가 매일 **오전 7시(KST)**(UTC 22:00)에 실행됩니다. Actions 탭에서 수동으로 실행할 수도 있습니다.
3. 실행이 끝나면 `data/agent.db`와 `output/*.md`가 저장소에 자동으로 커밋됩니다.

### 결과물

| 결과물 | 위치 | 설명 |
|---|---|---|
| 메일 | 수신자 메일함 | 기사·문서별 요약, 체크리스트 점검 결과, 제안 |
| 제안 목록 | `output/proposals.md` | 누적 제안 (검토대기, 반영완료). 자동 생성, 직접 수정 금지 |
| 판정 기록 | `output/coverage_log.md` | 모든 점검 판정 기록. 자동 생성, 직접 수정 금지 |
| 원본 데이터 | `data/agent.db` | 위 md 파일의 원본 (SQLite) |

## RAGAS 평가 결과

평가 후 기입합니다. 산출 방법은 [SERVICE.md 16장](SERVICE.md#16-평가)을 참고하세요.

- context_recall: 0.XX
- context_precision: 0.XX
- faithfulness: 0.XX
- answer_relevancy: 0.XX

## 인-아웃 세트 통과율 (자체 평가)

- 1차 (Day 9 종료): XX / XX 통과
- 2차 (Day 10 개선 후): XX / XX 통과
- 개선폭: +XX 건 (주요 개선 사항: ...)

상세: [evaluation/round1_report.md](evaluation/round1_report.md), [evaluation/round2_report.md](evaluation/round2_report.md)

## 트라이앤에러 회고

구현하면서 기입합니다.

- 시도했지만 실패한 접근 · 왜 실패했는가
- 최종 채택한 접근 · 왜 그것으로 갔는가
- 남은 한계 · 향후 개선 방향 (2차: 메일 회신으로 판정 수정, 오판 학습, 법령 원문 확인 / 운영 전환: 사내 체크리스트 적용)

## 핵심 코드 위치

구현 후 줄 번호를 기입합니다.

- `src/agent.py` — 배치 모드·질의 모드 그래프
- `src/tools.py` — 수집기, 본문 추출, 메일 발송, DB 접근
- `src/retriever.py` — RAG 인덱스 생성과 체크리스트 검색
- `src/guards.py` — 입력·출력 가드레일
- `src/llm.py` — 모델 호출 래퍼 (재시도·대체 모델·마스킹)
- `src/api.py` — 질의 API와 웹 페이지
- `evaluation/run_eval.py` — 자체 평가

## 관련 문서

| 문서 | 내용 |
|---|---|
| [SERVICE.md](SERVICE.md) | 서비스 스펙과 상세 설계 (무엇을 만드는지의 기준) |
| [TASKS.md](TASKS.md) | 구현 작업 목록 |
| [CLAUDE.md](CLAUDE.md) | Claude Code 개발 규칙 |
