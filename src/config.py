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
    # 점검·제안·병합·재확인: coverage_check, draft_proposals, merge_proposals, recheck_proposals
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
TOP_NEWS = 5                  # 하루에 요약·점검할 기사 수
DOC_MAX_PAGES = 30            # 문서 요약 시 읽을 최대 쪽수 (P2)
RETRIEVE_K = 5                # 체크리스트 검색 결과 수
# 하이브리드 검색 가중치 (BM25, 벡터). 실습 기본값 0.3/0.7보다 0.5/0.5가 검색 시험 14개에서 더 좋았다 (2-2, evaluation/retrieval_probe.py)
HYBRID_WEIGHTS = (0.5, 0.5)
QUERY_MAX_CHARS = 20_000      # 질의 입력 최대 길이
MIN_ARTICLE_CHARS = 100       # 이보다 짧은 본문은 기사가 아닌 입력(인사, 질문)으로 보고 안내한다

# ── 수집 대상 (RSS) ─────────────────────────────────
# 사이트 이름 → RSS 주소. 실제 주소는 TASKS.md 4-1에서 확인해 채운다.
RSS_FEEDS: dict[str, str] = {}

# ── 메일 ────────────────────────────────────────────
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
MAIL_TO = [addr.strip() for addr in os.getenv("MAIL_TO", "").split(",") if addr.strip()]

# ── DB 보관 기간 (일). None은 영구 보관 ───────────────
RETENTION_DAYS = {
    "seen": 90,
    "items": 365,
    "coverage_log": 365,
    "proposals": None,
    "checklist_snapshot": None,
}
