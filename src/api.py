"""질의 모드 API (SERVICE.md 10.4, 과제 표준 규약).

실행: uvicorn src.api:app
  POST /query   {"question": "기사 URL 또는 본문"} → {"answer", "contexts", "trace"}
  GET  /docs    API 문서·시험 화면 (FastAPI 자동 생성)
  GET  /health  서버 상태 확인
  GET  /        점검 웹 페이지 (입력창 + 결과 화면)
  POST /ui/check  웹 페이지가 부르는 내부 경로. 결과를 Jinja2로 그린 HTML 조각을 돌려준다

질의 모드는 DB에 쓰지 않는다 (CLAUDE.md 구현 원칙).
"""

import json
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from src import config
from src.agent import run_query
from src.retriever import load_checklist

log = logging.getLogger("uvicorn.error")

# 2만 자 제한은 guard_query가 안내 메시지로 처리한다. 여기서는 그보다 훨씬 큰 요청만 형식 단계에서 거절한다.
REQUEST_MAX_CHARS = config.QUERY_MAX_CHARS * 5

app = FastAPI(
    title="보안 데일리 에이전트 — 기사 즉시 점검",
    description="보안 기사 URL 또는 본문을 넣으면 요약, ISMS-P 체크리스트 점검 결과, 체크리스트 수정 제안을 돌려줍니다.",
    version="1.0",
)


class QueryRequest(BaseModel):
    question: str = Field(
        max_length=REQUEST_MAX_CHARS,
        description="기사 URL(http/https 한 줄) 또는 기사 본문",
        examples=["https://www.boannews.com/news/articleView.html?idxno=140597"],
    )


class Context(BaseModel):
    doc_id: str = Field(description="체크리스트 항목 ID (예: 2.9.2)")
    text: str = Field(description="항목 내용 (점검항목, 점검내용, 확인사항)")


class TraceStep(BaseModel):
    step: str
    input: object = None
    output: object = None


class QueryResponse(BaseModel):
    answer: str = Field(description="요약, 체크리스트 점검 결과, 제안 (메일과 같은 형식의 텍스트). 거절·안내 시 안내 문구")
    contexts: list[Context] = Field(description="점검 근거로 쓴 체크리스트 항목 (거절·기타 기사는 빈 목록)")
    trace: list[TraceStep] = Field(description="처리 단계 기록 {step, input, output}")


@app.post("/query", response_model=QueryResponse, summary="기사 1건 점검")
def query(req: QueryRequest) -> QueryResponse:
    """기사 URL 또는 본문 1건을 점검한다. 처리에 20~40초 걸린다.

    일반 함수(def)로 정의해 FastAPI가 별도 스레드에서 실행한다 (처리 중에도 다른 요청을 받는다).
    """
    try:
        result = run_query(req.question)
    except Exception:
        log.exception("질의 처리 실패")  # 자세한 내용은 서버 로그에만 남긴다
        raise HTTPException(status_code=500, detail="처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.")
    return QueryResponse(
        answer=result["answer"],
        contexts=[Context(**c) for c in result["contexts"]],
        trace=[TraceStep(**t) for t in jsonable_encoder(result["trace"])],
    )


@app.get("/health", summary="서버 상태")
def health() -> dict:
    return {"status": "ok"}


# ── 점검 웹 페이지 (2-12) ─────────────────────────────
# Jinja2Templates는 .html 템플릿에 자동 이스케이프를 켠다: 기사 본문·모델 출력 안의 HTML·스크립트는 글자로만 표시된다.
templates = Jinja2Templates(directory=str(config.TEMPLATES_DIR))
# trace 표시용: 한글을 그대로 보여 주는 JSON (기본 tojson은 한글을 \uXXXX로 바꾼다). 결과는 자동 이스케이프된다.
templates.env.filters["pretty"] = lambda v: json.dumps(v, ensure_ascii=False, indent=2, default=str)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"max_chars": config.QUERY_MAX_CHARS})


@app.post("/ui/check", response_class=HTMLResponse, include_in_schema=False)
def ui_check(request: Request, req: QueryRequest):
    try:
        result = run_query(req.question)
    except Exception:
        log.exception("질의 처리 실패 (웹 페이지)")
        raise HTTPException(status_code=500, detail="처리 중 오류가 발생했습니다.")
    result["trace"] = jsonable_encoder(result["trace"])
    names = {k: v["점검항목"] for k, v in load_checklist().items()}
    return templates.TemplateResponse(request, "_result.html", {"result": result, "names": names})
