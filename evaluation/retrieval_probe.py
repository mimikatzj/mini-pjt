"""검색 품질 점검: 유형별 검색어에 대해 기대 항목이 상위 몇 위에 나오는지 본다.

실행: python -m evaluation.retrieval_probe [-v]
벡터·BM25·하이브리드를 나란히 비교하고, 상위 3개(질의 그래프의 점검 대상당 개수)와 상위 5개 기준 점수를 낸다.
M2에서 검색 기능(하이브리드, 쿼리 확장, 리랭킹)을 붙일 때마다 다시 돌려 점수 변화를 기록한다.

기록 (하나 이상 찾음 / 모두 찾음):
  1-5 벡터만, 12개, 상위5:                 10/12, 5/12
  2-1 사례 2개 추가, 벡터만, 14개, 상위5:   12/14, 7/14
    ※ 추가한 2개는 2.2.4가 4위로 나오지만, 질의 그래프는 점검 대상당 3개만 가져와서 놓쳤다
  2-2 (14개, 상위3 → 상위5):
    vector              9/14, 5/14 → 12/14, 7/14
    bm25               13/14, 9/14 → 13/14, 9/14
    hybrid 0.3/0.7     12/14, 7/14 → 12/14, 7/14   (실습 가중치)
    hybrid 0.5/0.5     13/14, 9/14 → 14/14, 9/14   (채택. 0.6~0.8은 13/14, 9/14로 같거나 낮음)
    ※ 가중치를 같은 14개로 골랐으므로 과적합 가능성이 있다. M3 평가 세트로 다시 확인한다.
  2-3 (14개, 상위3 → 상위5):
    expand             14/14, 10/14 → 14/14, 12/14   (LLM 쿼리 확장 + 하이브리드 + RRF)
    ※ 프롬프트 예시는 이 14개와 겹치지 않는 사례로 썼다. LLM 호출이 있어 실행마다 조금씩 달라질 수 있다.
  2-4 (14개, 상위3 → 상위5):
    rerank             14/14, 12/14 → 14/14, 13/14   (expand 후보 10개를 LLM 관련도 0~3점으로 재정렬)
    ※ 첫 측정에서 LLM이 ID를 "[2.9.2] 이름" 형태로 돌려줘 점수가 전부 버려졌다 → normalize_id로 정리 후 재측정한 값
"""
import sys
from functools import lru_cache

from src.retriever import fused_search, load_checklist, search

# (유형, 검색어, 기대 항목 ID들)
CASES = [
    ("키워드", "백업 복구", ["2.9.2"]),
    ("키워드", "접속기록 보관", ["2.9.3"]),
    ("키워드", "보안 패치 적용", ["2.10.4"]),
    ("바꿔 말하기", "데이터를 되살리지 못해 서비스가 사흘간 멈췄다", ["2.9.2", "2.11.3"]),
    ("바꿔 말하기", "퇴사한 직원 계정이 그대로 살아 있었다", ["2.2.4", "2.5.1"]),
    ("바꿔 말하기", "외주 개발사가 고객 정보를 해외 서버에 저장했다", ["3.3.4", "3.3.2"]),
    ("기사 문장", "공격자는 VPN 장비의 알려진 취약점을 이용해 내부망에 침투한 뒤 랜섬웨어를 유포했다", ["2.10.4", "2.11.2", "2.6.3", "2.10.5"]),
    ("기사 문장", "직원이 피싱 메일의 첨부파일을 열어 악성코드에 감염됐다", ["2.10.5", "2.2.3"]),
    ("기사 문장", "관리자 계정 비밀번호가 기본값 그대로여서 무단 접속을 허용했다", ["2.5.4", "2.5.3"]),
    ("법·제도", "개인정보 보관기간이 지나면 지체 없이 파기하도록 시행령이 개정됐다", ["3.4.1"]),
    ("법·제도", "만 14세 미만 아동 개인정보 수집 시 법정대리인 동의 요건 강화", ["3.1.1"]),
    ("법·제도", "개인정보 유출 시 72시간 이내 정보주체 통지와 신고 의무화", ["2.11.3"]),
    # 2-1 실행에서 검색 누락으로 불필요한 제안이 나온 원인 표현 (요약이 뽑은 문구 그대로)
    ("원인 문구", "퇴사자 계정 및 접근권한 미회수", ["2.2.4"]),
    ("원인 문구", "인사부서의 정보보호 부서 미통지", ["2.2.4"]),
]
MODES = ["vector", "bm25", "hybrid", "expand", "rerank"]
RERANK_CANDIDATES = 10


@lru_cache(maxsize=None)
def expanded(q: str) -> tuple[str, ...]:
    from src.nodes.retrieve import expand_queries

    return tuple(expand_queries([q])[1])


@lru_cache(maxsize=None)
def results(q: str, mode: str) -> tuple[str, ...]:
    """검색 결과 상위 5개의 항목 ID.
    expand = LLM 쿼리 확장 + 하이브리드 + 순위 합산
    rerank = expand 후보 10개를 LLM 관련도로 다시 정렬 (질의 그래프와 같은 방식)
    """
    if mode == "expand":
        return tuple(d.metadata["id"] for d in fused_search(list(expanded(q)), k=5))
    if mode == "rerank":
        from src.nodes.retrieve import rerank

        cands = [d.metadata["id"] for d in fused_search(list(expanded(q)), k=RERANK_CANDIDATES)]
        ranked = rerank([q], {1: cands})[1]
        if "-v" in sys.argv:
            print(f"      (리랭킹 전) {cands}\n      (리랭킹 후) {[f'{i}:{s}' for i, s in ranked]}")
        return tuple(i for i, _ in ranked[:5])
    return tuple(d.metadata["id"] for d in search(q, k=5, mode=mode))


def score(mode: str) -> dict[int, list[int]]:
    """{k: [하나 이상 찾음, 모두 찾음]} (k = 3, 5)"""
    s = {3: [0, 0], 5: [0, 0]}
    for _, q, expected in CASES:
        got = list(results(q, mode))
        for k in (3, 5):
            found = [e for e in expected if e in got[:k]]
            s[k][0] += bool(found)
            s[k][1] += len(found) == len(expected)
    return s


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    names = {k: v["점검항목"] for k, v in load_checklist().items()}
    verbose = "-v" in sys.argv  # -v: 검색어마다 결과 목록 출력

    for kind, q, expected in CASES:
        line = []
        for mode in MODES:
            got = list(results(q, mode))
            line.append(f"{mode}=" + "/".join(f"{got.index(e) + 1}위" if e in got else "-" for e in expected))
            if verbose:
                print(f"      {mode:7} {got}")
        print(f"[{kind}] {q}\n    기대 {', '.join(f'{e} {names[e]}' for e in expected)} → {'  '.join(line)}")

    n = len(CASES)
    print(f"\n{'':8}{'상위3 하나이상':>14}{'상위3 모두':>10}{'상위5 하나이상':>14}{'상위5 모두':>10}")
    for mode in MODES:
        s = score(mode)
        print(f"{mode:8}{s[3][0]:>11}/{n}{s[3][1]:>7}/{n}{s[5][0]:>11}/{n}{s[5][1]:>7}/{n}")


if __name__ == "__main__":
    main()
