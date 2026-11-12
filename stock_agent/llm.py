"""03 LLM agent가 쓰는 LLM 인터페이스 (2026-11-08 결정: 교체 가능하게만)

어떤 LLM이든 LLM.next()만 구현하면 붙일 수 있다. 실제 연결은 OpenAI(llm_openai.py, 기본 gpt-4o-mini).
    load_llm()  STOCK_LLM_PROVIDER(openai 기본 | policy)를 따른다.
                openai: 환경변수 LLM_API_KEY가 있으면 OpenAILLM, 없으면 None(연결 전).
                        None이면 agent는 트렌드마다 llm_failed(no_llm) 오류를 돌려준다 (API 응답: SOURCE_UNAVAILABLE)
                policy: 아래 PolicyLLM (결정적, 네트워크·키 없음, no_llm이 나지 않음). stub 프로필·오프라인 점검용
                그 밖의 값: UnavailableLLM (트렌드마다 llm_unavailable)
                환경변수가 없으면 현재 폴더의 .env를 읽는다(이미 있는 환경변수는 덮어쓰지 않음)

메시지 형식 (특정 회사 형식에 묶이지 않게 단순하게)
    {"role": "user", "content": "..."}
    {"role": "assistant", "content": "...", "tool_calls": [{"id": "c1", "name": "find_colors", "args": {...}}]}
    {"role": "tool", "id": "c1", "name": "find_colors", "content": "<JSON 문자열>"}

도구 형식: [{"name", "description", "parameters": JSON Schema}] (Anthropic·OpenAI 둘 다 이 모양으로 바꾸기 쉬움)

이미지 분석 (2026-11-09, 무신사 스냅 착샷)
    LLM.see(system, prompt, image_url) → dict (JSON 답)
    이미지를 볼 수 없는 LLM은 NotImplementedError → agent는 착샷 반영을 생략(no_vision)
    이미지 URL을 열 수 없으면 ImageUnavailable → agent는 착샷 반영을 생략(image_error)

LLM이 실패하면 아래 예외를 던진다. agent가 llm_failed 오류의 원인(reason)으로 옮긴다.
    TimeoutError    → timeout
    LLMUnavailable  → llm_unavailable (키 오류·권한·모델 없음·연결 실패)
    LLMError        → invalid_response (응답을 읽을 수 없음 등)

테스트·점검용 LLM (API는 STOCK_LLM_PROVIDER=policy일 때만 PolicyLLM을 쓴다)
    ScriptedLLM   정해 둔 도구 호출을 순서대로 돌려줌
    PolicyLLM     LLM 없이 정해진 순서로 도구를 부르는 가짜 LLM. agent 반복 구조 점검용이고, 실패 시 대신 쓰는 용도가 아니다
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class LLMError(Exception):
    """LLM 응답을 쓸 수 없음 (형식 오류 등)."""


class LLMUnavailable(LLMError):
    """LLM에 연결할 수 없음 (키 오류, 권한, 모델 없음, 네트워크)."""


class ImageUnavailable(Exception):
    """이미지 URL을 열 수 없음 (만료·차단·이미지 아님). LLM 실패가 아니라 데이터 문제라 착샷 반영만 생략한다."""


@dataclass
class ToolCall:
    name: str
    args: dict = field(default_factory=dict)
    id: str = ""


@dataclass
class LLMReply:
    tool_calls: list[ToolCall] = field(default_factory=list)
    text: str = ""


class LLM(Protocol):
    def next(self, system: str, messages: list[dict], tools: list[dict]) -> LLMReply: ...

    def see(self, system: str, prompt: str, image_url: str) -> dict: ...


def read_dotenv(path: str | Path = ".env") -> None:
    """KEY=VALUE 줄만 읽어 환경변수에 넣는다. 이미 있는 값은 덮어쓰지 않는다. 파일이 없으면 아무것도 안 함."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:      # 빈 값은 넣지 않음 (기본값을 빈 문자열로 덮지 않게)
            os.environ[k] = v


LLM_PROVIDERS = ("openai", "policy")
POLICY_LIMITATION = ("STOCK_LLM_PROVIDER=policy: 언어 모델 대신 정해진 순서로 도구를 부르는 결정적 PolicyLLM이 "
                     "조건과 상품을 골랐습니다(오프라인 점검용, 외부 호출 없음). reason.decided_by·llm_reason도 PolicyLLM의 값입니다.")


def llm_provider() -> str:
    """STOCK_LLM_PROVIDER: openai(기본) | policy. 대소문자·앞뒤 공백은 무시하고, 그 밖의 값은 "invalid"."""
    value = os.environ.get("STOCK_LLM_PROVIDER", "").strip().lower() or "openai"
    return value if value in LLM_PROVIDERS else "invalid"


def load_llm() -> LLM | None:
    """API·스크립트가 쓸 LLM (STOCK_LLM_PROVIDER).

    policy → PolicyLLM (키·네트워크 없음). openai → LLM_API_KEY가 없으면 None (키는 환경변수 또는 .env,
    .env는 커밋하지 않음). 알 수 없는 provider → UnavailableLLM (조용히 다른 LLM으로 바꾸지 않는다).
    """
    read_dotenv()
    provider = llm_provider()
    if provider == "policy":
        return PolicyLLM()
    if provider != "openai":
        return UnavailableLLM("STOCK_LLM_PROVIDER는 openai 또는 policy여야 합니다")
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        return None
    from stock_agent.llm_openai import DEFAULT_MODEL, DEFAULT_TIMEOUT, OpenAILLM
    try:
        return OpenAILLM(api_key=key, model=os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODEL,
                         timeout=float(os.environ.get("LLM_TIMEOUT", "").strip() or DEFAULT_TIMEOUT))
    except (LLMUnavailable, ValueError) as e:     # openai 미설치, LLM_TIMEOUT 오류 등: 서버는 뜨고 요청마다 원인을 알림
        return UnavailableLLM(f"{type(e).__name__}: {e}")


class UnavailableLLM:
    """만들 수 없는 LLM. 부를 때마다 LLMUnavailable → 트렌드마다 SOURCE_UNAVAILABLE(reason=llm_unavailable)."""

    def __init__(self, message: str):
        self.message = message

    def next(self, system, messages, tools):
        raise LLMUnavailable(self.message)

    def see(self, system, prompt, image_url):
        raise LLMUnavailable(self.message)


# ---------------------------------------------------------------- 테스트·점검용

def _vision(answers: list | None, seen: list, system, prompt, image_url):
    """가짜 LLM의 see: answers가 None이면 이미지를 못 보는 LLM. 답은 dict / 예외 / 함수(image_url → 위 중 하나)."""
    seen.append({"system": system, "prompt": prompt, "image_url": image_url})
    if answers is None:
        raise NotImplementedError("이미지를 볼 수 없는 LLM")
    if not answers:
        raise LLMError("가짜 LLM: 남은 이미지 답이 없습니다")
    a = answers.pop(0)
    if callable(a):
        a = a(image_url)
    if isinstance(a, BaseException):
        raise a
    return a


class ScriptedLLM:
    """steps: 각 차례에 돌려줄 것. ToolCall / [ToolCall, ...] / LLMReply / 예외(던짐) / 함수(messages → 위 중 하나).
    vision: see()가 차례로 돌려줄 답 (None이면 이미지를 못 보는 LLM)."""

    def __init__(self, steps, vision: list | None = None):
        self.steps = list(steps)
        self.vision = None if vision is None else list(vision)
        self.seen: list[dict] = []             # see()로 받은 것 (검사용)
        self.calls: list[list[dict]] = []      # 받은 messages (검사용)

    def see(self, system, prompt, image_url):
        return _vision(self.vision, self.seen, system, prompt, image_url)

    def next(self, system, messages, tools):
        self.calls.append([dict(m) for m in messages])
        if not self.steps:
            raise LLMError("ScriptedLLM: 남은 응답이 없습니다")
        s = self.steps.pop(0)
        if callable(s) and not isinstance(s, (ToolCall, LLMReply)):
            s = s(messages)
        if isinstance(s, BaseException):
            raise s
        if isinstance(s, LLMReply):
            return s
        calls = s if isinstance(s, list) else [s]
        return LLMReply([ToolCall(c.name, c.args, c.id or f"c{len(self.calls)}_{i}") for i, c in enumerate(calls)])


def last_result(messages: list[dict]) -> dict:
    """마지막 도구 결과 (JSON)."""
    for m in reversed(messages):
        if m.get("role") == "tool":
            return json.loads(m["content"])
    return {}


def _tool_history(messages: list[dict]) -> tuple[list[tuple[str, dict]], list[ToolCall]]:
    results = [(m["name"], json.loads(m["content"])) for m in messages if m.get("role") == "tool"]
    calls = [ToolCall(c["name"], c.get("args") or {}, c.get("id", "")) for m in messages
             if m.get("role") == "assistant" for c in m.get("tool_calls") or []]
    return results, calls


class PolicyLLM:
    """정해진 순서로 도구를 부르는 가짜 LLM (점검용). 상태 없이 메시지 기록만 보고 다음 도구를 정한다.

    find_categories → find_colors → get_inventory(앞 결과 그대로) → rank_candidates → 이미지 있는 최상위로 finish.
    - 후보가 0개이고 트렌드 색이 있으면 트렌드 색 조건을 풀어(빈 기준 색) 한 번 더 찾는다.
      사용자 색이 있으면 색은 풀지 않는다 (사용자 조건은 완화하지 않음)
    - 착샷 소재·핏 단계(A/B/C)는 코드가 정한다
    vision: see()가 차례로 돌려줄 답 (None이면 이미지를 못 보는 LLM → 착샷 반영 생략)
    실패했을 때 대신 쓰는 용도가 아니다.
    """

    def __init__(self, vision: list | None = None):
        self.vision = None if vision is None else list(vision)
        self.seen: list[dict] = []

    def see(self, system, prompt, image_url):
        return _vision(self.vision, self.seen, system, prompt, image_url)

    @staticmethod
    def _call(name, **args):
        return LLMReply([ToolCall(name, args, f"p_{name}")])

    def next(self, system, messages, tools):
        task = json.loads(messages[0]["content"])
        hist, calls = _tool_history(messages)
        if not hist:
            return self._call("find_categories")
        last = lambda tool: next((r for n, r in reversed(hist) if n == tool), {})  # noqa: E731
        name, r = hist[-1]
        if name == "find_categories":
            if not r.get("areas"):
                return self._call("finish", article_id=None, reason="제외 카테고리를 빼니 찾을 영역이 없습니다.")
            return self._call("find_colors", base_colors=task["base_colors"])
        if name == "find_colors":
            return self._call("get_inventory")
        if name == "get_inventory":
            if r.get("n_candidates", 0) == 0:
                relaxed = any(c.name == "find_colors" and c.args.get("base_colors") == [] for c in calls)
                if task["base_colors"] and not task["user_colors"] and not relaxed:
                    return self._call("find_colors", base_colors=[], note="후보가 없어 트렌드 색 조건을 풂")
                return self._call("finish", article_id=None, reason="조건에 맞는 후보가 없습니다.")
            return self._call("rank_candidates", top_k=10)
        if name == "rank_candidates":
            items = r.get("candidates", [])
            pick = next((x for x in items if x.get("has_image")), items[0] if items else None)
            if pick is None:
                return self._call("finish", article_id=None, reason="조건에 맞는 후보가 없습니다.")
            extra = {} if pick["rank"] == 1 else {"why_not_higher": "더 높은 순위 상품에 사진이 없습니다."}
            return self._call("finish", article_id=pick["article_id"], reason=f"순위 {pick['rank']}위 상품입니다.", **extra)
        raise LLMError(f"PolicyLLM: {name} 다음에 할 일이 없습니다")
