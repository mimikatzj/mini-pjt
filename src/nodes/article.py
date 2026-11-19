"""배치 모드: 선별된 기사 1건의 본문 확보 (process_article의 첫 단계). LLM 호출 없음.

본문 우선순위:
1) 수집 단계에서 이미 얻은 본문 (과기정통부 보도자료 MD 등)
2) 기사 URL에서 본문 추출 (tools.fetch_article_text: 주소 차단 검사 포함)
3) 실패하면 제목 + 수집 설명(snippet)으로 진행하고 body_source에 남긴다 (기사 하나 실패로 전체를 멈추지 않는다)
가져온 본문에도 인젝션 의심 문구 검사를 다시 한다 (4-3은 제목·설명만 검사했다).
"""

from src import config
from src.guards import find_injection
from src.tools import BlockedURL, FetchFailed, fetch_article_text


def prepare_article(item: dict) -> dict:
    """{"text": 점검할 본문, "body_source": 출처, "injection": 의심 문구, "error": 추출 실패 사유}"""
    if item.get("body"):
        text, source, error = item["body"], "수집 본문", ""
    else:
        try:
            text, source, error = fetch_article_text(item["url"]), "기사 페이지", ""
        except (FetchFailed, BlockedURL) as e:
            text, source, error = "", "", f"{type(e).__name__}: {e}"
    if len(text) < config.MIN_ARTICLE_CHARS:  # 본문을 못 얻었거나 너무 짧으면 제목·설명으로 진행
        text = f"{item['title']}\n\n{item.get('snippet', '')}".strip()
        source = "제목·설명만 (본문 추출 실패)" if error else "제목·설명만 (본문 짧음)"
    return {"text": text[: config.QUERY_MAX_CHARS], "body_source": source,
            "injection": find_injection(text), "error": error}
