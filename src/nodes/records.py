"""누적 기록 노드. LLM 호출 없음.

- 0단계 load_checklist, load_records: 읽기 전용
- 10단계 commit_records: 발송에 성공한 뒤에만 실행한다. 처리 완료 URL, 보낸 기사, 판정, 제안, 체크리스트 사본 저장과
  보관 기간 정리를 **하나의 트랜잭션**으로 처리하고(중간에 실패하면 모두 되돌림), 보기용 output/*.md를 DB에서 다시 만든다.
  DB 쓰기는 이 노드 한 곳에서만 한다 (CLAUDE.md 구현 원칙, SERVICE.md 8장 10단계·14장).

DB 접근 함수는 src/tools.py에 있다.

확인용 실행: python -m src.nodes.records
"""

import re
from contextlib import closing
from datetime import datetime

from src import config
from src.retriever import load_checklist as read_checklist
from src.tools import (KST, connect_db, load_checklist_with_changes, load_records as read_records, prune_old_rows,
                       read_coverage_log, read_proposals_with_evidence, save_run, table_counts)

MD_HEADER = "> 자동 생성 파일입니다. 직접 수정하지 마세요. 데이터 원본은 `data/agent.db`이며 배치 실행마다 다시 만듭니다.\n"


def load_checklist(state: dict) -> dict:
    with closing(connect_db()) as conn:  # sqlite3의 with는 연결을 닫지 않으므로 closing으로 닫는다
        checklist, changed = load_checklist_with_changes(conn)
    return {
        "checklist_ids": sorted(checklist),
        "changed_items": changed,
        "trace": [{"step": "load_checklist", "input": None, "output": {"items": len(checklist), "changed": changed}}],
    }


def load_records(state: dict) -> dict:
    with closing(connect_db()) as conn:
        records = read_records(conn)
        counts = table_counts(conn)
    return {
        "records": records,
        "trace": [{"step": "load_records", "input": None, "output": {
            "table_rows": counts,
            "seen_urls": {k: len(v) for k, v in records["seen_urls"].items()},
            "proposals": len(records["proposals"]),
        }}],
    }



# ── commit_records (4-9) ─────────────────────────────


def commit_records(state: dict) -> dict:
    today = datetime.now(KST).strftime("%Y-%m-%d")
    articles = state.get("articles_merged") or state.get("articles") or []
    failed_urls = {a["item"]["url"] for a in articles if a.get("error")}  # 처리 실패 기사는 다음 실행에서 다시 시도
    # 처리 완료 표시: 선별 후보 전체 (선별에서 밀린 기사도 다음 날 다시 선별하지 않는다)
    seen_items = [it for it in state.get("new_items", []) if it["url"] not in failed_urls]
    with closing(connect_db()) as conn:
        with conn:  # 트랜잭션 하나: 블록이 끝나면 커밋, 예외가 나면 모두 되돌린다
            saved = save_run(conn, today=today, seen_items=seen_items, articles=articles,
                             numbers=(state.get("mail") or {}).get("numbers", {}),
                             plan=state.get("proposal_plan", []), checklist=read_checklist())
            pruned = prune_old_rows(conn, today)
        files = write_markdown(conn)
        counts = table_counts(conn)
    return {"trace": [{"step": "commit_records", "input": {"date": today, "seen_items": len(seen_items), "articles": len(articles)},
                       "output": {"saved": saved, "pruned": pruned, "table_rows": counts, "files": files}}]}


# ── 보기용 md (DB에서 생성) ──────────────────────────


def _md(text) -> str:
    """md 한 줄에 넣을 외부 텍스트 정리: 줄바꿈 제거, 표·링크 문법 문자와 HTML 꺾쇠 이스케이프."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return re.sub(r"([\\`*_\[\]|<>])", r"\\\1", text)


def _url(url: str) -> str:
    """링크 주소는 http(s)만 쓰고 괄호·공백을 인코딩한다."""
    url = str(url or "")
    if not url.startswith(("http://", "https://")):
        return ""
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def _link(title, url) -> str:
    u = _url(url)
    return f"[{_md(title)}]({u})" if u else _md(title)


def render_proposals_md(proposals: list[dict]) -> str:
    """SERVICE.md 14장의 proposals.md 항목 형식."""
    lines = [MD_HEADER, "# 체크리스트 반영 제안", ""]
    waiting = sum(1 for p in proposals if p["status"] == "검토대기")
    lines += [f"검토대기 {waiting}건 · 전체 {len(proposals)}건", ""]
    if not proposals:
        lines.append("아직 제안이 없습니다.")
    for p in proposals:
        head = p["item"] if p["type"] == "수정" else (p["area"] or p["item"])
        kind = f"수정 (대상: {p['target_id']})" if p["type"] == "수정" else f"신규 (분야: {_md(p['area'])})"
        evidence = ", ".join(_link(e["title"], e["url"]) for e in p["evidence"]) or "-"
        lines += [
            f"### {p['id']} {_md(head)} – {_md(p['title'])}",
            f"- 상태: {p['status']}",
            f"- 유형: {kind}",
            f"- 점검항목: {_md(p['item'])}",
            f"- 점검내용: {_md(p['content'])}",
            f"- 근거: {evidence}",
            f"- 최초 제안일: {p['first_date']} / 최근 근거 추가일: {p['last_date']}",
            "",
        ]
    return "\n".join(lines)


def render_coverage_md(rows: list[dict]) -> str:
    lines = [MD_HEADER, "# 판정 기록", "", f"전체 {len(rows)}건 (최근 것부터, 보관 1년)", ""]
    if not rows:
        lines.append("아직 판정 기록이 없습니다.")
    date = None
    for r in rows:
        if r["created"] != date:
            date = r["created"]
            lines += ["", f"## {date}", "", "| 판정 ID | 기사 | 점검 대상 | 판정 | 근거 항목 | 이유 |", "|---|---|---|---|---|---|"]
        lines.append(f"| {r['judgment_id']} | {_link(r.get('title') or '-', r.get('url'))} | {_md(r['target']) or '-'} | {_md(r['verdict'])} | "
                     f"{', '.join(_md(e) for e in r['evidence_ids']) or '-'} | {_md(r['reason']) or '-'} |")
    return "\n".join(lines) + "\n"


def write_markdown(conn) -> list[str]:
    """output/proposals.md, output/coverage_log.md를 DB에서 다시 만든다 (rejected.md는 2차)."""
    config.OUTPUT_DIR.mkdir(exist_ok=True)
    files = {"proposals.md": render_proposals_md(read_proposals_with_evidence(conn)),
             "coverage_log.md": render_coverage_md(read_coverage_log(conn))}
    for name, text in files.items():
        (config.OUTPUT_DIR / name).write_text(text, encoding="utf-8")
    return [f"output/{n}" for n in files]

if __name__ == "__main__":
    import sys

    from src import config

    sys.stdout.reconfigure(encoding="utf-8")
    existed = config.DB_PATH.exists()
    print(f"DB: {config.DB_PATH} ({'기존 파일' if existed else '새로 만듦'})")
    for node in (load_checklist, load_records):
        out = node({})
        print(f"[{out['trace'][0]['step']}] {out['trace'][0]['output']}")
