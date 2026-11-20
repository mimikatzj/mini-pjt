"""설정값. 모델, 건수 제한, 경로, 수집 대상 등 바뀔 수 있는 값은 모두 여기에 둔다."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── 경로 ─────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
SAMPLES_DIR = DATA_DIR / "samples"
CHECKLIST_PATH = DATA_DIR / "checklist.csv"
CHROMA_DIR = DATA_DIR / "chroma"
DB_PATH = DATA_DIR / "agent.db"
OUTPUT_DIR = ROOT_DIR / "output"
TMP_DIR = ROOT_DIR / "tmp"
TRACE_DIR = ROOT_DIR / "trace"
PROMPTS_DIR = ROOT_DIR / "src" / "prompts"
TEMPLATES_DIR = ROOT_DIR / "src" / "templates"

# ── 모델 (Amazon Bedrock) ───────────────────────────
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# 용도별 모델 목록. 첫 번째가 1순위, 나머지는 순서대로 대체 모델이다.
# Claude 5.5 모델 ID는 사용자 확인 후 맨 앞에 추가한다 (TASKS.md 0-2). 그 전에는 실습에서 동작이 확인된 4.5로 동작한다.
MODELS = {
    # 선별·요약·검색 보조: triage_news, summarize_news, summarize_document, retrieve_checklist
    "fast": [
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",   # 대체: Haiku가 계속 실패하면 Sonnet
    ],
    # 점검·제안·병합·재확인: coverage_check(ReAct 점검 에이전트 포함), draft_proposals, merge_proposals, recheck_proposals
    "smart": [
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",    # 대체: Sonnet이 계속 실패하면 Haiku (품질 낮음, trace에 기록)
    ],
}
LLM_RETRY_ATTEMPTS = 2  # 모델마다 시도 횟수 (boto3 자체 재시도와 별도). 그래도 실패하면 다음 모델로
# 평가 채점관 모델 (evaluation/run_eval.py). 1차·2차 비교가 의미 있도록 대체 모델 없이 고정한다.
JUDGE_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"

# ── 건수·크기 제한 ──────────────────────────────────
# 선별 묶음 (부문 × 지역). 묶음마다 따로 중복 제거·선별하고 최대 건수만큼 보낸다 (SERVICE.md 7.1, 18장)
# 기사가 어느 묶음에 속하는지는 수집처(SOURCES의 track, region)로 정해진다.
TRACKS = [
    {"track": "사고", "region": "국내", "limit": 5},
    {"track": "사고", "region": "해외", "limit": 3},
    {"track": "정책", "region": "국내", "limit": 3},
]
# 부문별 카테고리 우선순위 (작을수록 먼저). 같은 순위 안에서는 중요도 점수 → 최신순 (사용자 결정)
#   사고: 침해사고 → 그 외        정책: 법·제도(정책 변경, 과징금, 조사) → 그 외
CATEGORY_PRIORITY = {
    "사고": {"침해사고": 1, "취약점": 2, "법·제도": 2, "기타": 2},
    "정책": {"법·제도": 1, "취약점": 2, "침해사고": 2, "기타": 2},
}
DOC_MAX_PAGES = 30            # 문서 요약 시 읽을 최대 쪽수 (P2)
RETRIEVE_K = 5                # 체크리스트 검색 결과 수
# 하이브리드 검색 가중치 (BM25, 벡터). 실습 기본값 0.3/0.7보다 0.5/0.5가 검색 시험 14개에서 더 좋았다 (2-2, evaluation/retrieval_probe.py)
HYBRID_WEIGHTS = (0.5, 0.5)
# 점검 방식 (SERVICE.md 10.5, 사용자 결정 2026-11-20)
#   "fixed": 검색 1회(쿼리 확장 → 하이브리드 → 리랭킹) → 점검. 처리 순서가 고정된다
#   "react": 첫 검색 결과를 받은 점검 에이전트가 도구(재검색·항목 원문 조회)를 스스로 골라 더 조사한 뒤 점검한다
# 배치 모드는 매일 같은 순서·비용으로 돌아야 하므로 fixed를 유지한다.
COVERAGE_MODE = {"query": "react", "batch": "fixed"}
REACT_MAX_TOOL_CALLS = 4      # ReAct 점검에서 기사 1건당 도구 호출 상한. 넘으면 그때까지 모은 항목으로 판정한다
QUERY_MAX_CHARS = 20_000      # 질의 입력 최대 길이
MIN_ARTICLE_CHARS = 100       # 이보다 짧은 본문은 기사가 아닌 입력(인사, 질문)으로 보고 안내한다

BATCH_CONCURRENCY = 3   # 배치 모드에서 동시에 처리하는 기사 수 (모델 호출 한도에 걸리지 않게 제한)
COLLECT_RECENT_DAYS = 2  # 이 기간 안에 올라온 기사만 수집한다 (전날 오후 기사·실행 누락 대비). 이미 보낸 기사는 중복 제거(4-3)에서 뺀다

# ── 수집 대상 (SERVICE.md 7.1, 2026-11-19 접속 확인) ───────────
# 사이트 안의 지정 메뉴만 수집한다. (track, region)이 중복 제거·선별 묶음이다 (TRACKS).
SOURCES: list[dict] = [
    # 사고 · 국내
    {"name": "보안뉴스 사건·사고", "track": "사고", "region": "국내", "type": "rss", "url": "https://www.boannews.com/rss/S1N2.xml"},
    {"name": "데일리시큐 이슈", "track": "사고", "region": "국내", "type": "rss", "url": "https://www.dailysecu.com/rss/S1N2.xml"},
    # 사고 · 해외
    {"name": "The Hacker News Cyber Attack", "track": "사고", "region": "해외", "type": "rss",
     "url": "https://thehackernews.com/feeds/posts/default/-/Cyber%20Attack?alt=rss"},
    # BleepingComputer는 분류별 RSS가 없어 전체 뉴스 피드에서 Security 메뉴(/news/security/) 기사만 남긴다
    {"name": "BleepingComputer Security", "track": "사고", "region": "해외", "type": "rss",
     "url": "https://www.bleepingcomputer.com/feed/", "link_prefix": "https://www.bleepingcomputer.com/news/security/"},
    # 정책 · 국내 (정부 보도자료 원문만. 둘 다 RSS가 없어 목록 페이지를 읽는다 — P1 예외, 사용자 결정)
    {"name": "개인정보보호위원회 보도자료", "track": "정책", "region": "국내", "type": "pipc_board",
     "url": "https://pipc.go.kr/np/cop/bbs/selectBoardList.do?bbsId=BS074&mCode=C020010000"},
    # 목록의 등록일·상세 본문은 HTML에 없어, 글마다 MD 첨부(보도자료 전문)에서 배포 일시와 본문을 읽는다
    {"name": "과학기술정보통신부 보도자료", "track": "정책", "region": "국내", "type": "msit_board",
     "url": "https://www.msit.go.kr/bbs/list.do?sCode=user&mPid=208&mId=307"},
    # 제외: 보안뉴스 공공·정책(사용자 결정, 정부 원문만 수집), 데일리시큐 정책(RSS 없음),
    #       The Hacker News Vulnerability(사용자 결정: 중요한 취약점은 다른 출처에도 실린다)
]

# ── 메일 ────────────────────────────────────────────
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
MAIL_TO = [addr.strip() for addr in os.getenv("MAIL_TO", "").split(",") if addr.strip()]
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465      # SSL
SMTP_TIMEOUT = 30    # 초

# ── DB 보관 기간 (일). None은 영구 보관 ───────────────
RETENTION_DAYS = {
    "seen": 90,
    "items": 365,
    "coverage_log": 365,
    "proposals": None,
    "checklist_snapshot": None,
}
