"""fetch_article 노드: 질의 입력이 URL이면 본문을 가져온다. 차단·실패하면 안내 메시지로 끝낸다."""

from src.guards import find_injection
from src.state import QueryState
from src.tools import BlockedURL, FetchFailed, fetch_article_text

MSG_BLOCKED = "접속할 수 없는 주소입니다. 공개된 인터넷 기사 주소만 입력할 수 있습니다."
MSG_FAILED = "본문을 가져오지 못했습니다. 기사 본문을 복사해서 붙여넣어 주세요."


def fetch_article(state: QueryState) -> dict:
    url = state["url"]
    try:
        article = fetch_article_text(url)
        update = {"article": article, "input_kind": "article"}
        output = {"ok": True, "length": len(article), "head": article[:100], "injection": find_injection(article)}
    except BlockedURL as e:
        update = {"refusal": MSG_BLOCKED, "input_kind": "refuse"}
        output = {"ok": False, "blocked": True, "reason": str(e)}
    except FetchFailed as e:
        update = {"refusal": MSG_FAILED, "input_kind": "refuse"}
        output = {"ok": False, "blocked": False, "reason": str(e)}
    return {**update, "trace": [{"step": "fetch_article", "input": url, "output": output}]}
