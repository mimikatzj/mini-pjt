"""collect_news 노드 (배치 모드 2단계): 지정 출처에서 최근 기사를 모은다. LLM 호출 없음.

수집 규칙은 src/tools.py의 collect_all, 출처 목록은 src/config.py의 SOURCES에 있다.
실패한 출처는 collect_failures에 남겨 메일 하단 "수집 실패: ○○"로 표시한다 (4-7).

확인용 실행: python -m src.nodes.collect
"""

from src.tools import collect_all


def collect_news(state: dict) -> dict:
    items, failures, stats = collect_all()
    return {
        "raw_items": items,
        "collect_failures": failures,
        "trace": [{"step": "collect_news", "input": None, "output": {"stats": stats, "failures": failures}}],
    }


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    result = collect_news({})
    stats = result["trace"][0]["output"]["stats"]
    print(f"{'지역':4} {'출처':32} {'가져옴':>6} {'최근':>4}")
    for s in stats:
        print(f"{s['region']:4} {s['source']:32} {s['fetched']:>6} {s['recent']:>4}")
    print(f"\n최근 기사 합계: {len(result['raw_items'])}건")
    for f in result["collect_failures"]:
        print(f"수집 실패: {f['source']} — {f['error']}")
    print("\n[출처별 최신 2건]")
    for name in dict.fromkeys(i["source"] for i in result["raw_items"]):
        for it in [i for i in result["raw_items"] if i["source"] == name][:2]:
            print(f"  [{it['region']}] {name} | {it['published'][:16]} | {it['title'][:50]}")
