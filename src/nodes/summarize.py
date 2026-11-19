"""summarize_news 노드: 기사 본문 → 카테고리·요약·핵심 정보 (NewsSummary).

체인: 프롬프트 | 저가 모델.with_structured_output(NewsSummary)
"""

from functools import lru_cache

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from src import config
from src.llm import get_structured_llm
from src.state import NewsSummary, QueryState


@lru_cache(maxsize=1)
def build_chain() -> Runnable:
    system = (config.PROMPTS_DIR / "summarize_news.md").read_text(encoding="utf-8")
    prompt = ChatPromptTemplate.from_messages([
        ("system", system),
        # 외부 텍스트는 구분 표시로 감싸 데이터로만 취급하게 한다
        ("human", "<article>\n{article}\n</article>"),
    ])
    return prompt | get_structured_llm("fast", NewsSummary)


def summarize_news(state: QueryState) -> dict:
    """그래프 노드. state["article"]을 요약해 state["summary"]에 넣는다."""
    article = state["article"]
    summary: NewsSummary = build_chain().invoke({"article": article})
    return {
        "summary": summary,
        "trace": [{
            "step": "summarize_news",
            "input": article[:200],
            "output": summary.model_dump(),
        }],
    }


if __name__ == "__main__":
    # 확인용: python -m src.nodes.summarize  (data/samples/*.txt 전체 요약)
    import json
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    for path in sorted(config.SAMPLES_DIR.glob("*.txt")):
        result = summarize_news({"article": path.read_text(encoding="utf-8")})
        print(f"\n===== {path.name}")
        print(json.dumps(result["summary"].model_dump(), ensure_ascii=False, indent=2))
