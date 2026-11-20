"""자체 평가: test_queries.csv의 사례를 질의 모드로 실행하고 채점한다 (실습 day7_practice/run_eval.py·llm_judge.py 방식).

실행:
  python evaluation/run_eval.py                 전체 실행
  python evaluation/run_eval.py --ids 6,13      일부 사례만 (개선 후 실패 사례 재확인용)
  python evaluation/run_eval.py --out evaluation/round1_result.json   결과 저장 위치 지정
  python evaluation/run_eval.py --rescore tmp/eval.json   에이전트를 다시 돌리지 않고 저장된 결과로 지표만 다시 계산
  python evaluation/run_eval.py --coverage-mode fixed   점검 방식 지정 (생략하면 config.COVERAGE_MODE["query"], 기본 react)

최근 판정 이력(DB)은 기본으로 끈다. DB 내용에 따라 결과가 바뀌면 회차끼리 비교할 수 없기 때문이다 (--use-history로 켬).
ReAct 방식이면 사례마다 경로(바로 판정 / 원문 확인 / 재검색 / 상한 도달)와 도구 호출 수를 기록하고 분포를 낸다.

채점은 두 단계다. 최종 통과 = 코드 검사 통과 AND LLM 채점 통과.
  1) 코드 검사 (계산·규칙 판정은 코드로):
     - "근거 항목 X.Y.Z 인용" → 답변에 그 ID가 있는가
     - "근거 항목 A/B/C 중 하나 인용" → 답변에 셋 중 하나라도 있는가 (간결 점검: 핵심 대상이 여러 원인 중 하나일 때)
     - expected_tools → 응답 trace에 그 단계가 있는가
     - forbidden 중 문자 그대로 비교할 수 있는 값(숫자·@ 포함, 공백 없음) → 답변에 그대로 나오지 않는가
  2) LLM-as-Judge (고정 모델, Pydantic): expected_traits 충족, forbidden 미포함을 의미로 판정

RAGAS 4개 지표 (ragas 라이브러리 없이 같은 정의로 계산, SERVICE.md 16.2):
  - context_recall    = 검색 결과에 들어간 기대 묶음 수 / 기대 묶음 수                    (코드, 기대 항목이 있는 사례)
                        기대 묶음: "X 인용"은 X 하나, "A/B/C 중 하나 인용"은 A·B·C 중 하나만 검색돼도 맞은 것으로 센다
  - context_precision = Σ_k (precision@k × rel_k) / (상위 K 안의 기대 항목 수)            (코드, RAGAS 정의. 묶음 안 항목은 모두 관련 항목)
  - faithfulness      = 검색 결과 원문으로 뒷받침되는 주장 수 / 답변의 체크리스트 관련 주장 수 (LLM이 주장별 판정, 비율은 코드)
  - answer_relevancy  = 입력 기사 핵심을 다루는 정도 0~4점 / 4                          (LLM, 거절되지 않은 사례)
"""

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # python evaluation/run_eval.py로 실행해도 src를 찾도록

from langchain_core.prompts import ChatPromptTemplate  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from src import config  # noqa: E402
from src.agent import run_query  # noqa: E402
from src.llm import get_fixed_structured_llm  # noqa: E402

CASES_PATH = ROOT / "evaluation" / "test_queries.csv"
EXPECTED_ID = re.compile(r"근거 항목 (\d+\.\d+\.\d+) 인용")
EXPECTED_ONE_OF = re.compile(r"근거 항목 (\d+\.\d+\.\d+(?:/\d+\.\d+\.\d+)+) 중 하나 인용")


def expected_groups(traits: str) -> list[list[str]]:
    """기대 근거 항목 묶음. "X 인용"은 [X], "A/B/C 중 하나 인용"은 [A, B, C]."""
    return [[i] for i in EXPECTED_ID.findall(traits)] + [g.split("/") for g in EXPECTED_ONE_OF.findall(traits)]
ANSWER_LIMIT = 6000  # 채점관에게 보낼 답변 최대 글자 수


def split(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split(";") if x.strip()]


# ── 1) 코드 검사 ─────────────────────────────────────
# 시험지의 expected_tools는 고정 순서 단계 이름으로 적혀 있다. ReAct 점검에서 같은 역할을 하는 단계도 실행한 것으로 본다.
# (retrieve_checklist는 ReAct에서도 첫 검색으로 그대로 실행된다)
STEP_ALIASES = {"coverage_check": {"coverage_react.judge"}}


def code_checks(case: dict, answer: str, steps: list[str]) -> list[dict]:
    checks = []
    for group in expected_groups(case["expected_traits"]):
        label = group[0] if len(group) == 1 else "/".join(group) + " 중 하나"
        checks.append({"check": f"근거 항목 {label} 인용", "ok": any(doc_id in answer for doc_id in group)})
    for tool in split(case["expected_tools"]):
        checks.append({"check": f"단계 {tool} 실행", "ok": tool in steps or bool(STEP_ALIASES.get(tool, set()) & set(steps))})
    for item in split(case["forbidden"]):
        if " " not in item and re.search(r"[\d@]", item):  # 문자 그대로 비교할 수 있는 값 (전화번호, 이메일 등)
            checks.append({"check": f"금지 문자열 '{item}' 없음", "ok": item not in answer})
    return checks


# ── 2) LLM-as-Judge ──────────────────────────────────
class TraitResult(BaseModel):
    trait: str = Field(description="expected_traits의 조건 하나 (원문 그대로)")
    met: bool = Field(description="충족 여부")
    reason: str = Field(description="판단 근거 한 문장")


class JudgeResult(BaseModel):
    traits: list[TraitResult] = Field(description="expected_traits 조건별 판정")
    forbidden_found: list[str] = Field(default_factory=list, description="답변에 나타난 forbidden 항목")
    passed: bool = Field(description="모든 조건 충족이고 금지 항목이 없으면 true")
    summary: str = Field(description="통과·실패 이유 한두 문장")


_judge = None


def judge(case: dict, result: dict) -> JudgeResult:
    global _judge
    if _judge is None:
        system = (config.PROMPTS_DIR / "judge.md").read_text(encoding="utf-8")
        prompt = ChatPromptTemplate.from_messages([
            ("system", system),
            ("human",
             "사례 분류: {category}\n\n<input>\n{input}\n</input>\n\n<answer>\n{answer}\n</answer>\n\n"
             "실행된 단계: {steps}\n근거로 쓴 체크리스트 항목: {contexts}\n\n"
             "expected_traits: {traits}\nforbidden: {forbidden}"),
        ])
        _judge = prompt | get_fixed_structured_llm(config.JUDGE_MODEL, JudgeResult)
    return _judge.invoke({
        "category": case["category"],
        "input": case["input"][:3000],
        "answer": result["answer"][:ANSWER_LIMIT],
        "steps": " → ".join(t["step"] for t in result["trace"]),
        "contexts": ", ".join(c["doc_id"] for c in result["contexts"]) or "없음",
        "traits": case["expected_traits"],
        "forbidden": case["forbidden"] or "없음",
    })


# ── 3) RAGAS 지표 ────────────────────────────────────
def context_recall(groups: list[list[str]], retrieved: list[str]) -> float | None:
    """기대 묶음 중 검색 결과에 들어간 비율 (묶음 안 항목 하나라도 있으면 맞음). 기대 항목이 없으면 계산하지 않는다(None)."""
    if not groups:
        return None
    return sum(any(i in retrieved for i in g) for g in groups) / len(groups)


def context_precision(expected: list[str], retrieved: list[str]) -> float | None:
    """RAGAS Context Precision@K: 맞는 항목이 나온 순위 k마다 precision@k를 구해 평균. 위에 있을수록 높다."""
    if not expected:
        return None
    hits, total = 0, 0.0
    for k, doc_id in enumerate(retrieved, 1):
        if doc_id in expected:
            hits += 1
            total += hits / k  # precision@k
    return total / hits if hits else 0.0


class Claim(BaseModel):
    claim: str = Field(description="답변에서 체크리스트 항목 내용에 대해 말하는 주장 하나")
    supported: bool = Field(description="contexts의 체크리스트 원문으로 뒷받침되는가")


class RagasJudge(BaseModel):
    claims: list[Claim] = Field(default_factory=list, description="faithfulness: 주장별 근거 판정")
    relevancy: int = Field(ge=0, le=4, description="answer_relevancy: 입력 기사 핵심을 다루는 정도 0~4")
    relevancy_reason: str = Field(description="관련성 점수의 이유 한 문장")


_ragas_judge = None


def ragas_llm(case: dict, answer: str, contexts: list[dict]) -> RagasJudge:
    global _ragas_judge
    if _ragas_judge is None:
        system = (config.PROMPTS_DIR / "judge_ragas.md").read_text(encoding="utf-8")
        prompt = ChatPromptTemplate.from_messages([
            ("system", system),
            ("human", "<input>\n{input}\n</input>\n\n<answer>\n{answer}\n</answer>\n\n<contexts>\n{contexts}\n</contexts>"),
        ])
        _ragas_judge = prompt | get_fixed_structured_llm(config.JUDGE_MODEL, RagasJudge)
    return _ragas_judge.invoke({
        "input": case["input"][:3000],
        "answer": answer[:ANSWER_LIMIT],
        "contexts": "\n\n".join(c.get("text", c["doc_id"]) for c in contexts) or "(검색 결과 없음)",
    })


def ragas(case: dict, r: dict) -> dict:
    """사례 1건의 RAGAS 지표. 해당하지 않는 지표는 None."""
    groups = expected_groups(case["expected_traits"])
    expected = sorted({i for g in groups for i in g})
    retrieved = [c["doc_id"] for c in r.get("contexts", [])]
    out = {"context_recall": context_recall(groups, retrieved),
           "context_precision": context_precision(expected, retrieved),
           "faithfulness": None, "answer_relevancy": None, "claims": [], "relevancy_reason": ""}
    refused = "refuse" in r.get("steps", [])
    if refused or not r.get("answer"):
        return out
    j = ragas_llm(case, r["answer"], r.get("contexts", []))
    if retrieved and j.claims:  # 검색 결과와 주장이 있을 때만 근거성을 잰다
        out["faithfulness"] = sum(c.supported for c in j.claims) / len(j.claims)
    out["answer_relevancy"] = j.relevancy / 4
    out["claims"] = [c.model_dump() for c in j.claims]
    out["relevancy_reason"] = j.relevancy_reason
    return out


METRICS = ["context_recall", "context_precision", "faithfulness", "answer_relevancy"]


def summarize_metrics(results: list[dict]) -> dict:
    """지표별 평균과 계산된 사례 수."""
    summary = {}
    for m in METRICS:
        vals = [r["ragas"][m] for r in results if r.get("ragas") and r["ragas"][m] is not None]
        summary[m] = {"mean": round(sum(vals) / len(vals), 3) if vals else None, "n": len(vals)}
    return summary


# ── 실행 ────────────────────────────────────────────
def react_info(trace: list[dict]) -> dict | None:
    """ReAct 점검 단계의 경로·도구 호출 수 (ReAct로 돌지 않았으면 None)."""
    step = next((t for t in trace if t["step"] == "coverage_react.research"), None)
    if not step:
        return None
    out = step["output"]
    return {"path": out["path"], "tool_calls": out["tool_calls"], "collected": out["collected"],
            "tools": [f"{x['tool']}({', '.join(map(str, x['args'].values()))})" for x in out["steps"] if x["action"] == "tool_call"]}


def evaluate(case: dict, coverage_mode: str | None = None, use_history: bool = False) -> dict:
    started = time.time()
    try:
        result = run_query(case["input"], coverage_mode=coverage_mode, use_history=use_history)
    except Exception as e:  # 에이전트 오류도 실패로 기록하고 다음 사례로 넘어간다
        return {"id": case["id"], "category": case["category"], "passed": False, "error": f"실행 오류: {type(e).__name__}: {e}",
                "code_checks": [], "judge": None, "seconds": round(time.time() - started, 1)}
    checks = code_checks(case, result["answer"], [t["step"] for t in result["trace"]])
    verdict = judge(case, result)
    code_ok = all(c["ok"] for c in checks)
    r = {
        "id": case["id"], "category": case["category"],
        "passed": code_ok and verdict.passed,
        "code_ok": code_ok, "judge_ok": verdict.passed,
        "code_checks": checks, "judge": verdict.model_dump(),
        "answer": result["answer"], "contexts": result["contexts"],
        "steps": [t["step"] for t in result["trace"]],
        "usage": next((t["output"] for t in result["trace"] if t["step"] == "usage"), {}),
        # 검색 단계 상세 (대상, 확장 검색어, 리랭킹 전후 후보, 최종 선택). 검색 누락 원인 분석용
        "retrieval": next((t for t in result["trace"] if t["step"] == "retrieve_checklist"), None),
        "react": react_info(result["trace"]),
    }
    r["ragas"] = ragas(case, r)
    r["seconds"] = round(time.time() - started, 1)
    return r


def fail_reason(r: dict) -> str:
    if r.get("error"):
        return r["error"]
    parts = [f"[코드] {c['check']} 실패" for c in r["code_checks"] if not c["ok"]]
    j = r["judge"] or {}
    parts += [f"[채점] {t['trait']} — {t['reason']}" for t in j.get("traits", []) if not t["met"]]
    parts += [f"[채점] 금지 항목 등장: {f}" for f in j.get("forbidden_found", [])]
    return " / ".join(parts) or j.get("summary", "")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="test_queries.csv 자체 평가")
    parser.add_argument("--ids", help="실행할 사례 id (쉼표 구분). 생략하면 전체")
    parser.add_argument("--out", default=str(ROOT / "evaluation" / "eval_result.json"), help="결과 JSON 저장 경로")
    parser.add_argument("--rescore", metavar="JSON", help="저장된 결과를 읽어 RAGAS 지표만 다시 계산 (에이전트 실행 없음)")
    parser.add_argument("--coverage-mode", choices=["fixed", "react"], help="점검 방식 (생략하면 config.COVERAGE_MODE['query'])")
    parser.add_argument("--use-history", action="store_true", help="최근 판정 이력(DB, 읽기 전용)을 점검에 참고 (기본 끔)")
    args = parser.parse_args()
    mode = args.coverage_mode or config.COVERAGE_MODE["query"]
    if args.rescore:  # 재채점은 원래 실행의 점검 방식·이력 설정을 그대로 쓴다
        saved = json.loads(Path(args.rescore).read_text(encoding="utf-8"))
        mode = saved.get("coverage_mode", mode)
        args.use_history = saved.get("use_history", args.use_history)

    with open(CASES_PATH, encoding="utf-8-sig", newline="") as f:
        cases = list(csv.DictReader(f))
    if args.ids:
        wanted = {s.strip() for s in args.ids.split(",")}
        cases = [c for c in cases if c["id"] in wanted]

    if args.rescore:
        by_id = {c["id"]: c for c in cases}
        results = [r for r in json.loads(Path(args.rescore).read_text(encoding="utf-8"))["results"] if r["id"] in by_id]
        print(f"재채점: {len(results)}건 ({args.rescore}, 채점관 {config.JUDGE_MODEL}) — 코드 검사와 RAGAS 지표를 다시 계산 (LLM 채점 판정은 저장된 값)")
        for r in results:
            if r.get("error"):
                continue
            r["code_checks"] = code_checks(by_id[r["id"]], r["answer"], r["steps"])
            r["code_ok"] = all(c["ok"] for c in r["code_checks"])
            r["passed"] = r["code_ok"] and r["judge_ok"]
            r["ragas"] = ragas(by_id[r["id"]], r)
    else:
        print(f"평가 시작: {len(cases)}건 (점검 방식 {mode}, 이력 {'참고' if args.use_history else '끔'}, 채점관 {config.JUDGE_MODEL})")
        results = []
        for n, case in enumerate(cases, 1):
            r = evaluate(case, mode, args.use_history)
            results.append(r)
            # flush: 파일로 보낼 때도 사례가 끝날 때마다 바로 기록되게 한다
            print(f"  [{n:>2}/{len(cases)}] #{case['id']:>2} {case['category']:9} {'PASS' if r['passed'] else 'FAIL'}  ({r['seconds']}초)", flush=True)

    # ── 표 출력 ──
    print("\n| id | 분류 | 결과 | 코드 | 채점 | 사유 |\n|---|---|---|---|---|---|")
    for r in results:
        reason = (r["judge"] or {}).get("summary", "") if r["passed"] else fail_reason(r)
        mark = lambda v: "-" if v is None else ("✅" if v else "❌")
        print(f"| {r['id']} | {r['category']} | {'PASS' if r['passed'] else '**FAIL**'} | {mark(r.get('code_ok'))} | {mark(r.get('judge_ok'))} | {reason.replace('|', '/')} |")

    by_cat: dict[str, list[int]] = {}
    for r in results:
        by_cat.setdefault(r["category"], [0, 0])
        by_cat[r["category"]][0] += r["passed"]
        by_cat[r["category"]][1] += 1
    total = sum(r["passed"] for r in results)
    print(f"\n통과: {total}/{len(results)} ({total / len(results):.0%})  |  " + ", ".join(f"{k} {p}/{n}" for k, (p, n) in by_cat.items()))

    # ── RAGAS 지표 ──
    fmt = lambda v: "-" if v is None else f"{v:.2f}"
    print("\n| id | 분류 | recall | precision | faithfulness | relevancy |\n|---|---|---|---|---|---|")
    for r in results:
        g = r.get("ragas") or {}
        print(f"| {r['id']} | {r['category']} | " + " | ".join(fmt(g.get(m)) for m in METRICS) + " |")
    metrics = summarize_metrics(results)
    print("\nRAGAS 평균: " + ", ".join(f"{m} {fmt(v['mean'])} (n={v['n']})" for m, v in metrics.items()))

    # ── 시간·ReAct 경로 (계산은 코드) ──
    secs = [r["seconds"] for r in results if "seconds" in r]
    timing = {"total": round(sum(secs), 1), "mean": round(sum(secs) / len(secs), 1) if secs else None}
    print(f"\n시간: 합계 {timing['total']}초, 사례당 평균 {timing['mean']}초")
    reacts = [r["react"] for r in results if r.get("react")]
    react_summary = None
    if reacts:
        paths: dict[str, int] = {}
        for x in reacts:
            paths[x["path"]] = paths.get(x["path"], 0) + 1
        react_summary = {"n": len(reacts), "mean_tool_calls": round(sum(x["tool_calls"] for x in reacts) / len(reacts), 2),
                         "paths": paths}
        print(f"ReAct: {len(reacts)}건, 평균 도구 호출 {react_summary['mean_tool_calls']}회, 경로 " +
              ", ".join(f"{k} {v}" for k, v in paths.items()))
        print("\n| id | 경로 | 도구 호출 |\n|---|---|---|")
        for r in results:
            if r.get("react"):
                print(f"| {r['id']} | {r['react']['path']} | {' → '.join(r['react']['tools']) or '-'} |")

    out = {"run_at": datetime.now().isoformat(timespec="seconds"), "judge_model": config.JUDGE_MODEL,
           "coverage_mode": mode, "use_history": args.use_history,
           "passed": total, "total": len(results), "by_category": by_cat, "ragas": metrics,
           "timing": timing, "react": react_summary, "results": results}
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"결과 저장: {args.out}")


if __name__ == "__main__":
    main()
