"""compose_email 노드 (배치 모드 9단계 앞부분): 메일 제목과 HTML·텍스트 본문을 만든다. LLM 호출 없음.

SERVICE.md 12장 형식. 1차 범위라 "어제 요청 처리 결과", [삭제 요청]·[추가 요청] 버튼, 회신 안내는 넣지 않는다 (2차).
- 묶음(사고·국내 → 사고·해외 → 정책·국내) 순서로 기사 번호를 붙인다. 판정 줄은 [번호-a], 제안은 P-날짜-NN
- 표시 카테고리는 선별 단계(triage) 값으로 통일한다
- 법·제도는 "법령 원문 미확인", 처리 실패 기사는 "요약 실패", 하단에 "수집 실패: ○○"
- HTML 템플릿은 웹 페이지와 같은 조각(_report.html)을 쓴다. Jinja2 자동 이스케이프로 기사·모델 출력의 HTML은 실행되지 않는다
"""

from datetime import datetime

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src import config
from src.retriever import load_checklist
from src.tools import KST

_env = Environment(loader=FileSystemLoader(str(config.TEMPLATES_DIR)),
                   autoescape=select_autoescape(["html"]),  # .html만 이스케이프, .txt는 글자 그대로
                   trim_blocks=False, lstrip_blocks=False)


def build_sections(articles: list[dict]) -> list[dict]:
    """설정의 묶음 순서(config.TRACKS)대로 기사를 나누고 번호를 붙인다. 묶음 안에서는 선별 순서를 지킨다."""
    sections, n = [], 0
    for t in config.TRACKS:
        group = [a for a in articles if a["item"]["track"] == t["track"] and a["item"]["region"] == t["region"]]
        if not group:
            continue
        numbered = []
        for a in group:
            n += 1
            numbered.append({**a, "number": str(n), "warnings": a.get("warnings", []), "proposals": a.get("proposals", []),
                             "coverage": a.get("coverage", [])})
        sections.append({"name": f"{t['track']} · {t['region']}", "articles": numbered})
    return sections


def compose(state: dict) -> dict:
    articles = state.get("articles_merged") or state.get("articles") or []
    # 선별 순서를 유지한다 (병렬 처리라 도착 순서가 섞이므로 top_news 순서로 다시 맞춘다)
    order = {it["id"]: i for i, it in enumerate(state.get("top_news", []))}
    articles = sorted(articles, key=lambda a: order.get(a["item"]["id"], 999))
    plan = state.get("proposal_plan", [])
    pending_before = sum(1 for p in (state.get("records") or {}).get("proposals", []) if p.get("status", "검토대기") == "검토대기")
    counts = {
        "articles": len(articles),
        "new": sum(1 for x in plan if x["action"] == "new"),
        "attach": sum(1 for x in plan if x["action"] == "attach"),
    }
    counts["pending"] = pending_before + counts["new"]
    date = datetime.now(KST).strftime("%Y-%m-%d")
    subject = f"[보안 데일리] {date} (기사 {counts['articles']}건 · 새 제안 {counts['new']}건)"
    sections = build_sections(articles)
    numbers = {a["item"]["id"]: a["number"] for sec in sections for a in sec["articles"]}  # 판정 ID(날짜-번호-a)용
    ctx = {"subject": subject, "date": date, "counts": counts, "sections": sections,
           "failures": state.get("collect_failures", []),
           "names": {k: v["점검항목"] for k, v in load_checklist().items()}}
    return {"subject": subject, "html": _env.get_template("email.html").render(**ctx),
            "text": _env.get_template("email.txt").render(**ctx), "counts": counts, "numbers": numbers}


def compose_email(state: dict) -> dict:
    mail = compose(state)
    return {"mail": mail,
            "trace": [{"step": "compose_email", "input": {"articles": mail["counts"]["articles"]},
                       "output": {"subject": mail["subject"], "html_chars": len(mail["html"]), "text_chars": len(mail["text"])}}]}
