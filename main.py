"""배치 모드 진입점: 매일 1회 실행해 보안 기사를 수집·선별·점검하고 메일을 보낸다 (SERVICE.md 8장).

실행:
  python main.py --dry-run   메일을 보내지 않고 DB에도 저장하지 않는다. 결과를 tmp/에 저장한다 (개발용)
  python main.py             실제 실행: 메일을 보내고, 발송에 성공하면 기록을 저장하고 output/*.md를 다시 만든다

발송에 실패하면 종료 코드 1로 끝난다 (예약 실행에서 실패를 알 수 있게).
"""

import argparse
import json
import sys

from src import config
from src.agent import run_batch


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="보안 데일리 에이전트 배치 실행")
    parser.add_argument("--dry-run", action="store_true", help="메일 발송·DB 저장 없이 결과만 tmp/에 저장")
    args = parser.parse_args()
    result = run_batch(dry_run=args.dry_run)

    # ── 실행 요약 출력 ──
    print(f"\n실행 ID: {result['run_id']}")
    for f in result.get("collect_failures", []):
        print(f"수집 실패: {f['source']} — {f['error']}")
    print(f"수집 {len(result.get('raw_items', []))}건 → 중복 제거 {len(result.get('new_items', []))}건 → 선별 {len(result.get('top_news', []))}건")
    for a in sorted(result.get("articles_merged") or result.get("articles", []), key=lambda a: (a["item"]["track"], a["item"]["region"])):
        it = a["item"]
        head = f"[{it['track']}·{it['region']}] {it['title'][:50]}"
        if a.get("error"):
            print(f"  ✗ {head} — 처리 실패: {a['error']}")
            continue
        verdicts = [c["verdict"] for c in a["coverage"]]
        print(f"  ✓ {head}")
        print(f"      분류 {a['summary']['category']} | 본문: {a['body_source']} | 판정 {verdicts} | 제안 {len(a['proposals'])}건 | {a['seconds']}초")
    usage = result["trace"][-1]["output"]
    print(f"\nLLM {usage['llm_calls']}회, 입력 {usage['input_tokens']:,} / 출력 {usage['output_tokens']:,} 토큰, 전체 {usage.get('total_seconds', '-')}초")

    # ── 결과 저장 (dry-run): 메일 미리보기와 실행 결과 ──
    config.TMP_DIR.mkdir(exist_ok=True)
    mail = result.get("mail") or {}
    if mail:
        (config.TMP_DIR / "preview.html").write_text(mail["html"], encoding="utf-8")
        (config.TMP_DIR / "preview.txt").write_text(mail["text"], encoding="utf-8")
        print(f"\n메일 제목: {mail['subject']}")
        print(f"미리보기: {config.TMP_DIR / 'preview.html'} (텍스트 본문: preview.txt)")
    out = config.TMP_DIR / "batch_result.json"
    keys = ("run_id", "collect_failures", "top_news", "articles", "articles_merged", "proposal_plan", "trace")
    out.write_text(json.dumps({k: result.get(k) for k in keys}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"실행 결과: {out}")

    # ── 발송 결과 ──
    sent = result.get("sent") or {}
    if sent.get("status") == "sent":
        print(f"\n메일 발송 완료: 받는 사람 {len(sent['to'])}명")
        commit = next((t["output"] for t in result["trace"] if t["step"] == "commit_records"), None)
        if commit:
            print(f"기록 저장: {commit['saved']} | 보관 기간 정리: {commit['pruned']}")
            print(f"보기용 파일: {', '.join(commit['files'])}")
    elif sent.get("status") == "failed":
        print(f"\n메일 발송 실패: {sent['error']} (기록은 저장하지 않았습니다. 다음 실행에서 다시 처리합니다)")
        sys.exit(1)
    else:
        print("\n(dry-run: 메일을 보내지 않았습니다)")


if __name__ == "__main__":
    main()
