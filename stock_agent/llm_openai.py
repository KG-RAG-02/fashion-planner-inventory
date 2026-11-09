"""OpenAI Chat Completions 연결 (2026-11-09 결정: gpt-4o-mini)

stock_agent.llm의 LLM 인터페이스(next(system, messages, tools) → LLMReply)를 OpenAI 도구 호출 형식으로 바꾼다.
agent 코드는 이 파일을 모른다. load_llm()이 환경변수를 보고 만든다.

환경변수 (.env 또는 docker run --env-file .env. .env는 커밋하지 않는다)
    LLM_API_KEY   OpenAI 키. 없으면 load_llm()이 None → 트렌드마다 SOURCE_UNAVAILABLE(reason=no_llm)
    LLM_MODEL     기본 gpt-4o-mini
    LLM_TIMEOUT   호출 1번당 초, 기본 30

호출 설정
    temperature 0 (같은 요청이면 결과가 최대한 같게), tool_choice="required"(매 차례 도구를 부름. 끝낼 때도 finish 도구),
    parallel_tool_calls=False(한 번에 도구 1개. 앞 도구 결과를 보고 다음을 정하게),
    재시도 2번(SDK 기본: 연결 오류·408·409·429·5xx)

예외 → agent의 reason
    시간 초과                       TimeoutError    → timeout            (TIMEOUT 504)
    키 오류·권한·모델 없음·연결 실패   LLMUnavailable  → llm_unavailable    (SOURCE_UNAVAILABLE 503)
    그 밖의 API 오류, 읽을 수 없는 응답 LLMError        → invalid_response   (INTERNAL_ERROR 500)
"""

from __future__ import annotations

import json

from stock_agent.llm import LLMError, LLMReply, LLMUnavailable, ToolCall

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    """agent 메시지 형식 → OpenAI messages."""
    out = [{"role": "system", "content": system}]
    for m in messages:
        role = m.get("role")
        if role == "user":
            out.append({"role": "user", "content": m.get("content", "")})
        elif role == "assistant":
            one = {"role": "assistant", "content": m.get("content") or None}
            calls = m.get("tool_calls") or []
            if calls:
                one["tool_calls"] = [{"id": c["id"], "type": "function",
                                      "function": {"name": c["name"],
                                                   "arguments": json.dumps(c.get("args") or {}, ensure_ascii=False)}}
                                     for c in calls]
            out.append(one)
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": m["id"], "content": m.get("content", "")})
        else:
            raise LLMError(f"알 수 없는 메시지 role: {role!r}")
    return out


def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t.get("parameters") or {"type": "object", "properties": {}}}}
            for t in tools]


def from_openai_response(resp) -> LLMReply:
    """OpenAI 응답 → LLMReply. 도구 인자가 JSON이 아니면 LLMError."""
    try:
        msg = resp.choices[0].message
    except (AttributeError, IndexError, TypeError) as e:
        raise LLMError(f"응답에 choices가 없습니다: {e}") from e
    calls = []
    for i, tc in enumerate(getattr(msg, "tool_calls", None) or []):
        fn = getattr(tc, "function", None)
        if fn is None:
            raise LLMError("tool_calls에 function이 없습니다")
        try:
            args = json.loads(fn.arguments or "{}")
        except json.JSONDecodeError as e:
            raise LLMError(f"도구 인자가 JSON이 아닙니다 ({fn.name}): {fn.arguments!r}") from e
        if not isinstance(args, dict):
            raise LLMError(f"도구 인자가 객체가 아닙니다 ({fn.name})")
        calls.append(ToolCall(name=fn.name, args=args, id=getattr(tc, "id", None) or f"call_{i}"))
    return LLMReply(tool_calls=calls, text=getattr(msg, "content", None) or "")


class OpenAILLM:
    """OpenAI Chat Completions. client를 넘기면 그것을 쓴다(테스트용)."""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL, timeout: float = DEFAULT_TIMEOUT,
                 client=None):
        self.model = model
        self.timeout = timeout
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as e:   # requirements.txt의 openai가 설치되지 않음
                raise LLMUnavailable("openai 패키지가 없습니다. pip install -r requirements.txt") from e
            client = OpenAI(api_key=api_key, timeout=timeout, max_retries=MAX_RETRIES)
        self.client = client

    def next(self, system: str, messages: list[dict], tools: list[dict]) -> LLMReply:
        try:
            resp = self.client.chat.completions.create(
                model=self.model, temperature=0, messages=to_openai_messages(system, messages),
                tools=to_openai_tools(tools), tool_choice="required", parallel_tool_calls=False)
        except LLMError:
            raise
        except Exception as e:
            raise _map_error(e) from e
        return from_openai_response(resp)


def _map_error(e: Exception) -> Exception:
    """openai 예외 → TimeoutError / LLMUnavailable / LLMError. 키 값은 메시지에 넣지 않는다."""
    name = type(e).__name__
    if name == "APITimeoutError" or isinstance(e, TimeoutError):
        return TimeoutError(f"OpenAI 응답 시간 초과: {name}")
    if name in ("AuthenticationError", "PermissionDeniedError", "NotFoundError", "APIConnectionError"):
        return LLMUnavailable(f"OpenAI 연결 실패: {name} (키·모델 이름·네트워크를 확인하세요)")
    status = getattr(e, "status_code", None)
    return LLMError(f"OpenAI 오류: {name}" + (f" (HTTP {status})" if status else ""))
