"""로컬 파일 트레이서: LLM 호출을 trace/날짜.jsonl에 한 줄씩 기록한다 (실습 day7_practice/local_tracer.py 방식).

- 노드 단위 기록은 그래프 상태의 trace(API 응답에 포함)가 맡고, 이 파일은 LLM 호출 단위의 상세 기록이다.
- 모델에 실제로 보낸 메시지(개인정보 마스킹 뒤), 응답(구조화 출력은 도구 호출 인자), 걸린 시간, 토큰 수, 오류를 남긴다.
- 같은 질의의 기록은 query_id로 묶인다 (run_query가 metadata로 넘긴다).
- trace/ 폴더는 .gitignore에 있어 커밋되지 않는다. 기사 본문이 들어 있으므로 외부에 공유하지 않는다.
"""

import json
import threading
import time
from datetime import datetime

from langchain_core.callbacks import BaseCallbackHandler

from src import config

MAX_CHARS = 2000  # 메시지·응답 1개당 기록할 최대 글자 수


def get_text(message) -> str:
    """ChatBedrockConverse는 content를 블록 리스트로 주기도 하므로 텍스트만 모은다."""
    content = message.content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content or ""


def _cut(s: str) -> str:
    return s if len(s) <= MAX_CHARS else s[:MAX_CHARS] + f"…(+{len(s) - MAX_CHARS}자)"


class FileTracer(BaseCallbackHandler):
    """LLM 호출 시작·끝·오류를 JSONL로 기록하고, 질의별 합계(호출 수, 토큰, 시간)를 모은다."""

    def __init__(self):
        self._starts: dict = {}
        self._totals: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _write(self, record: dict) -> None:
        now = datetime.now()
        record = {"ts": now.strftime("%Y-%m-%d %H:%M:%S"), **record}
        config.TRACE_DIR.mkdir(exist_ok=True)
        path = config.TRACE_DIR / f"{now:%Y-%m-%d}.jsonl"
        with self._lock, path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _query_id(self, kw) -> str:
        return (kw.get("metadata") or {}).get("query_id", "")

    # ── LLM ──
    def on_chat_model_start(self, serialized, messages, *, run_id, **kw):
        params = kw.get("invocation_params") or {}
        model = params.get("model") or params.get("model_id") or (serialized.get("kwargs") or {}).get("model", "")
        self._starts[run_id] = (time.time(), model, self._query_id(kw))
        self._write({
            "event": "llm_start", "query_id": self._query_id(kw), "run_id": str(run_id), "model": model,
            "node": (kw.get("metadata") or {}).get("langgraph_node", ""),
            "input": [{"role": m.type, "text": _cut(get_text(m))} for batch in messages for m in batch],
        })

    def on_llm_end(self, response, *, run_id, **kw):
        started, model, query_id = self._starts.pop(run_id, (time.time(), "", ""))
        elapsed = round(time.time() - started, 2)
        gen = response.generations[0][0]
        msg = getattr(gen, "message", None)
        usage = dict(getattr(msg, "usage_metadata", None) or {})
        # 구조화 출력(with_structured_output)은 답이 텍스트가 아니라 도구 호출 인자로 온다
        tool_calls = [{"name": t.get("name"), "args": t.get("args")} for t in (getattr(msg, "tool_calls", None) or [])]
        raw = json.dumps(tool_calls, ensure_ascii=False)
        self._write({
            "event": "llm_end", "query_id": query_id, "run_id": str(run_id), "model": model,
            "latency_s": elapsed, "usage": usage,
            "output": _cut(gen.text or ""),
            "tool_calls": tool_calls if len(raw) <= MAX_CHARS * 3 else _cut(raw),  # 길면 문자열로 잘라 남긴다
        })
        with self._lock:
            t = self._totals.setdefault(query_id, {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "llm_seconds": 0.0, "errors": 0})
            t["llm_calls"] += 1
            t["input_tokens"] += usage.get("input_tokens", 0)
            t["output_tokens"] += usage.get("output_tokens", 0)
            t["llm_seconds"] = round(t["llm_seconds"] + elapsed, 2)

    def on_llm_error(self, error, *, run_id, **kw):
        _, model, query_id = self._starts.pop(run_id, (0, "", ""))
        self._write({"event": "llm_error", "query_id": query_id, "run_id": str(run_id), "model": model,
                     "error": f"{type(error).__name__}: {_cut(str(error))}"})
        with self._lock:
            self._totals.setdefault(query_id, {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "llm_seconds": 0.0, "errors": 0})["errors"] += 1

    # ── 질의 단위 요약 ──
    def finish(self, query_id: str, **extra) -> dict:
        """질의 1건이 끝나면 합계를 기록하고 돌려준다."""
        with self._lock:
            totals = self._totals.pop(query_id, {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "llm_seconds": 0.0, "errors": 0})
        self._write({"event": "query_end", "query_id": query_id, **totals, **extra})
        if "total_seconds" in extra:
            totals["total_seconds"] = extra["total_seconds"]
        return totals


tracer = FileTracer()
