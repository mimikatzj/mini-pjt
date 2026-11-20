"""모델 호출 공통 래퍼. 모든 LLM 호출은 이 파일의 함수로 모델을 받아 쓴다.

- 재시도 (with_retry): 모델마다 config.LLM_RETRY_ATTEMPTS번까지 다시 부른다. 잠깐 생긴 오류(일시적 호출 제한, 지연)용.
- 대체 모델 (with_fallbacks): 계속 실패하면 config.MODELS의 다음 순위 모델로 바꿔 부른다. 일일 한도 초과, 접근 권한 없음, 모델 ID 오류 등.
  대체 모델이 쓰이면 기록해 두고, 그래프 실행 결과의 trace에 남긴다 (pop_fallback_events).
- 개인정보 마스킹: 모델에 보내기 직전 모든 메시지의 전화번호·이메일·주민등록번호 등을 [종류]로 바꾼다 (guards.mask_pii).
  대체 모델로 넘어가도 마스킹된 메시지가 그대로 전달되도록 대체 모델 묶음보다 앞에 둔다.
  마스킹 개수는 기록해 두고 trace에 남긴다 (pop_mask_events).
"""

import logging
import threading

from langchain_aws import ChatBedrockConverse
from langchain_core.messages import BaseMessage
from langchain_core.prompt_values import PromptValue
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel

from src import config
from src.guards import mask_pii

log = logging.getLogger(__name__)

_events: list[dict] = []
_mask_events: list[dict] = []
_events_lock = threading.Lock()


def _mask_messages(value) -> list[BaseMessage] | str:
    """프롬프트 결과(메시지들 또는 문자열)의 개인정보를 가린다."""
    if isinstance(value, str):
        masked, counts = mask_pii(value)
        _record_mask(counts)
        return masked
    messages = value.to_messages() if isinstance(value, PromptValue) else list(value)
    out = []
    for m in messages:
        if isinstance(m.content, str):
            masked, counts = mask_pii(m.content)
            _record_mask(counts)
            m = m.model_copy(update={"content": masked})
        out.append(m)
    return out


def _record_mask(counts: dict[str, int]) -> None:
    if counts:
        log.info("개인정보 마스킹: %s", counts)
        with _events_lock:
            _mask_events.append(counts)


_masker = RunnableLambda(_mask_messages, name="mask_pii")


def _chat_model(model_id: str, temperature: float) -> ChatBedrockConverse:
    return ChatBedrockConverse(model=model_id, region_name=config.AWS_REGION, temperature=temperature)


def _model_ids(role: str) -> list[str]:
    if role not in config.MODELS:
        raise ValueError(f"알 수 없는 모델 용도: {role} (가능: {list(config.MODELS)})")
    return config.MODELS[role]


def _mark_fallback(role: str, model_id: str):
    """대체 모델이 실제로 호출될 때만 실행되어 사용 기록을 남긴다 (입력은 그대로 넘긴다)."""
    def mark(x):
        event = {"role": role, "fallback_model": model_id, "primary_model": _model_ids(role)[0]}
        log.warning("대체 모델 사용: %s", event)
        with _events_lock:
            _events.append(event)
        return x
    return RunnableLambda(mark)


def get_llm(role: str = "fast", temperature: float = 0.0) -> Runnable:
    """용도("fast" 또는 "smart")에 맞는 채팅 모델. 재시도와 대체 모델이 붙어 있다.

    - fast: 선별·요약·검색 보조 (저가 모델)
    - smart: 점검·제안·병합·재확인 (고성능 모델)
    판정이 실행마다 흔들리지 않도록 temperature 기본값은 0이다.
    """
    return _with_fallbacks(role, lambda m: m, temperature)


def get_structured_llm(role: str, schema: type[BaseModel], temperature: float = 0.0) -> Runnable:
    """구조화 출력(Pydantic) 모델. 모델마다 with_structured_output을 붙인 뒤 대체 모델로 묶는다.

    (대체 모델도 같은 Pydantic 형식으로 답해야 하므로, 묶은 뒤가 아니라 모델마다 붙인다.)
    """
    return _with_fallbacks(role, lambda m: m.with_structured_output(schema), temperature)


def get_tool_llm(role: str, tools: list, temperature: float = 0.0) -> Runnable:
    """도구를 고를 수 있는 채팅 모델 (ReAct 점검 에이전트용, SERVICE.md 10.5). 모델마다 bind_tools를 붙인 뒤 대체 모델로 묶는다.

    답은 AIMessage로 온다: 도구를 부르려면 tool_calls가 채워지고, 조사를 마치면 tool_calls 없이 텍스트만 온다.
    마스킹은 대화 전체(도구 결과 ToolMessage 포함)에 적용된다.
    """
    return _with_fallbacks(role, lambda m: m.bind_tools(tools), temperature)


def get_fixed_structured_llm(model_id: str, schema: type[BaseModel], temperature: float = 0.0) -> Runnable:
    """대체 모델 없이 지정한 모델 하나만 쓰는 구조화 출력 모델 (평가 채점관용). 마스킹·재시도는 적용한다.

    채점관이 실행마다 다른 모델로 바뀌면 1차·2차 평가를 비교할 수 없으므로 대체 모델을 붙이지 않는다.
    """
    model = _chat_model(model_id, temperature).with_structured_output(schema).with_retry(
        stop_after_attempt=config.LLM_RETRY_ATTEMPTS, wait_exponential_jitter=True,
    )
    return _masker | model


def _with_fallbacks(role: str, wrap, temperature: float) -> Runnable:
    ids = _model_ids(role)
    runnables = []
    for n, model_id in enumerate(ids):
        r = wrap(_chat_model(model_id, temperature)).with_retry(
            stop_after_attempt=config.LLM_RETRY_ATTEMPTS, wait_exponential_jitter=True,
        )
        runnables.append(r if n == 0 else _mark_fallback(role, model_id) | r)
    primary, *fallbacks = runnables
    model = primary.with_fallbacks(fallbacks) if fallbacks else primary
    return _masker | model  # 마스킹 → (1순위 모델 → 대체 모델)


def pop_fallback_events() -> list[dict]:
    """지금까지 쌓인 대체 모델 사용 기록을 꺼내고 비운다."""
    with _events_lock:
        events = list(_events)
        _events.clear()
    return events


def pop_mask_events() -> dict[str, int]:
    """지금까지 마스킹한 개인정보 개수를 종류별로 합쳐 꺼내고 비운다."""
    with _events_lock:
        total: dict[str, int] = {}
        for counts in _mask_events:
            for k, v in counts.items():
                total[k] = total.get(k, 0) + v
        _mask_events.clear()
    return total


if __name__ == "__main__":
    # 스모크 실행: python -m src.llm
    for role in config.MODELS:
        reply = get_llm(role).invoke("한 문장으로 답해줘: ISMS-P는 무엇의 약자야?")
        print(f"[{role}] {config.MODELS[role][0]}")
        print(f"  → {reply.content}")
    print("대체 모델 사용:", pop_fallback_events() or "없음")
