"""OpenAI 연결 (llm_openai.py): 형식 변환·호출 설정·오류 대응. 실제 OpenAI는 부르지 않는다 (가짜 client)."""

import json
from types import SimpleNamespace as NS

import pytest

from stock_agent import llm as L
from stock_agent.llm import LLMError, LLMUnavailable, PolicyLLM, ToolCall, UnavailableLLM, load_llm
from stock_agent.llm_agent import SYSTEM, TOOLS
from stock_agent.llm_openai import (OpenAILLM, from_openai_response, to_openai_messages,
                                    to_openai_tools)
from stock_agent.multi import run_request
from stock_agent.schemas import TREND_RESULT_EXAMPLE


def response(*calls, text=None):
    """OpenAI 응답 모양. calls: (id, name, args 또는 arguments 문자열)"""
    tcs = [NS(id=i, type="function", function=NS(name=n, arguments=a if isinstance(a, str) else json.dumps(a)))
           for i, n, a in calls]
    return NS(choices=[NS(message=NS(content=text, tool_calls=tcs or None))])


class FakeClient:
    """chat.completions.create만 있는 가짜. replies: 응답 또는 예외, 또는 함수(kwargs → 응답)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.kwargs = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kw):
        self.kwargs.append(kw)
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(r, BaseException):
            raise r
        return r(kw) if callable(r) else r


# ---------------------------------------------------------------- 형식 변환

def test_messages_to_openai():
    msgs = [{"role": "user", "content": "작업"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "find_colors",
                                                                  "args": {"base_colors": ["화이트"]}}]},
            {"role": "tool", "id": "c1", "name": "find_colors", "content": '{"colors": ["그레이"]}'}]
    out = to_openai_messages("시스템", msgs)
    assert out[0] == {"role": "system", "content": "시스템"}
    assert out[1] == {"role": "user", "content": "작업"}
    assert out[2]["content"] is None and out[2]["tool_calls"][0] == {
        "id": "c1", "type": "function", "function": {"name": "find_colors", "arguments": '{"base_colors": ["화이트"]}'}}
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": '{"colors": ["그레이"]}'}


def test_tools_to_openai():
    out = to_openai_tools(TOOLS)
    assert [t["function"]["name"] for t in out] == [t["name"] for t in TOOLS]
    assert all(t["type"] == "function" and t["function"]["parameters"]["type"] == "object" for t in out)


def test_response_parsing():
    r = from_openai_response(response(("c1", "rank_candidates", {"top_k": 5}), text="생각"))
    assert r.tool_calls == [ToolCall(name="rank_candidates", args={"top_k": 5}, id="c1")] and r.text == "생각"
    assert from_openai_response(response()).tool_calls == []          # 도구 없음 → agent가 invalid_response
    with pytest.raises(LLMError):
        from_openai_response(response(("c1", "finish", "{깨진 JSON")))
    with pytest.raises(LLMError):
        from_openai_response(response(("c1", "finish", "[1, 2]")))
    with pytest.raises(LLMError):
        from_openai_response(NS(choices=[]))


def test_call_settings():
    client = FakeClient(response(("c1", "find_categories", {})))
    OpenAILLM(model="gpt-4o-mini", client=client).next("시스템", [{"role": "user", "content": "x"}], TOOLS)
    kw = client.kwargs[0]
    assert kw["model"] == "gpt-4o-mini" and kw["temperature"] == 0
    assert kw["tool_choice"] == "required" and kw["parallel_tool_calls"] is False
    assert kw["messages"][0]["role"] == "system" and len(kw["tools"]) == len(TOOLS)


# ---------------------------------------------------------------- 오류 대응

@pytest.mark.parametrize("name, exc", [
    ("APITimeoutError", TimeoutError),
    ("AuthenticationError", LLMUnavailable),
    ("PermissionDeniedError", LLMUnavailable),
    ("NotFoundError", LLMUnavailable),
    ("APIConnectionError", LLMUnavailable),
    ("RateLimitError", LLMError),
    ("InternalServerError", LLMError),
])
def test_error_mapping(name, exc):
    llm = OpenAILLM(client=FakeClient(type(name, (Exception,), {})("sk-secret")))
    with pytest.raises(exc) as e:
        llm.next("s", [{"role": "user", "content": "x"}], TOOLS)
    assert "sk-secret" not in str(e.value)                  # 원래 메시지(키가 섞일 수 있음)는 옮기지 않음


@pytest.mark.parametrize("exc, code, reason, http", [
    (type("AuthenticationError", (Exception,), {})("x"), "SOURCE_UNAVAILABLE", "llm_unavailable", 503),
    (type("APITimeoutError", (Exception,), {})("x"), "TIMEOUT", "timeout", 504),
    (type("RateLimitError", (Exception,), {})("x"), "INTERNAL_ERROR", "invalid_response", 500),
])
def test_failure_reaches_response(exc, code, reason, http):
    """첫 LLM 호출에서 실패하면 DB를 쓰기 전이라 con 없이 확인할 수 있다."""
    from stock_agent.errors import http_status
    r = run_request(TREND_RESULT_EXAMPLE, con=None, llm=OpenAILLM(client=FakeClient(exc)))
    tr = r["trend_results"][0]
    assert tr["errors"][0]["code"] == code and tr["errors"][0]["reason"] == reason
    assert r["errors"][0]["code"] == code and http_status(r) == http


# ---------------------------------------------------------------- 이미지 분석 see() (무신사 스냅 착샷)

def json_reply(obj):
    return NS(choices=[NS(message=NS(content=json.dumps(obj, ensure_ascii=False), tool_calls=None))])


BadRequest = type("BadRequestError", (Exception,), {})


def test_see_sends_image_url_and_parses_json():
    client = FakeClient(json_reply({"full_outfit": True, "garments": {}}))
    out = OpenAILLM(client=client).see("시스템", "어휘", "https://img.example/a.jpg")
    assert out == {"full_outfit": True, "garments": {}}
    kw = client.kwargs[0]
    assert kw["temperature"] == 0 and kw["response_format"] == {"type": "json_object"}
    part = kw["messages"][1]["content"][1]
    assert part == {"type": "image_url", "image_url": {"url": "https://img.example/a.jpg", "detail": "low"}}


def test_see_falls_back_to_download():
    client = FakeClient(BadRequest("cannot fetch"), json_reply({"full_outfit": False, "garments": {}}))
    got = []
    llm = OpenAILLM(client=client, fetch=lambda u: got.append(u) or "data:image/jpeg;base64,AAAA")
    assert llm.see("s", "p", "https://img.example/a.jpg")["full_outfit"] is False
    assert got == ["https://img.example/a.jpg"]
    assert client.kwargs[1]["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg")


def test_see_image_unavailable():
    from stock_agent.llm import ImageUnavailable

    def no(u):
        raise ImageUnavailable("404")
    with pytest.raises(ImageUnavailable):
        OpenAILLM(client=FakeClient(BadRequest("x")), fetch=no).see("s", "p", "https://img.example/a.jpg")
    with pytest.raises(ImageUnavailable):           # 내려받아 보내도 OpenAI가 못 읽음
        OpenAILLM(client=FakeClient(BadRequest("x")), fetch=lambda u: "data:image/png;base64,AA").see("s", "p", "https://a")


@pytest.mark.parametrize("reply, exc", [
    (NS(choices=[NS(message=NS(content="그냥 글", tool_calls=None))]), LLMError),
    (NS(choices=[NS(message=NS(content="[1]", tool_calls=None))]), LLMError),
    (type("APITimeoutError", (Exception,), {})("x"), TimeoutError),
    (type("AuthenticationError", (Exception,), {})("x"), LLMUnavailable),
])
def test_see_errors(reply, exc):
    with pytest.raises(exc):
        OpenAILLM(client=FakeClient(reply)).see("s", "p", "https://img.example/a.jpg")


def test_fetch_image_rejects_non_http():
    from stock_agent.llm import ImageUnavailable
    from stock_agent.llm_openai import fetch_image
    with pytest.raises(ImageUnavailable):
        fetch_image("file:///etc/passwd")


# ---------------------------------------------------------------- load_llm

def test_load_llm_without_key_is_none(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)                       # .env 없음
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert load_llm() is None


def test_load_llm_reads_dotenv_and_model(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for k in ("LLM_API_KEY", "LLM_MODEL", "LLM_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / ".env").write_text("# 주석\nLLM_API_KEY=sk-test\nLLM_MODEL=\nLLM_TIMEOUT=12\n", encoding="utf-8")
    made = {}

    class Fake:
        def __init__(self, api_key, model, timeout):
            made.update(api_key=api_key, model=model, timeout=timeout)
    monkeypatch.setattr("stock_agent.llm_openai.OpenAILLM", Fake)
    assert isinstance(load_llm(), Fake)
    assert made == {"api_key": "sk-test", "model": "gpt-4o-mini", "timeout": 12.0}   # 빈 LLM_MODEL → 기본값


def test_load_llm_unavailable_does_not_crash(monkeypatch, tmp_path):
    """openai 패키지가 없는 등 만들 수 없으면 서버는 뜨고, 부를 때 llm_unavailable."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")

    def boom(**kw):
        raise LLMUnavailable("openai 패키지가 없습니다")
    monkeypatch.setattr("stock_agent.llm_openai.OpenAILLM", boom)
    llm = load_llm()
    assert isinstance(llm, UnavailableLLM)
    with pytest.raises(LLMUnavailable):
        llm.next("s", [], TOOLS)


def test_dotenv_does_not_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODEL", "gpt-4o")
    (tmp_path / ".env").write_text("LLM_MODEL=other\n", encoding="utf-8")
    L.read_dotenv(tmp_path / ".env")
    import os
    assert os.environ["LLM_MODEL"] == "gpt-4o"


# ---------------------------------------------------------------- 끝까지 (DB 필요): 가짜 OpenAI가 PolicyLLM 판단을 OpenAI 형식으로 돌려줌

def _policy_client():
    """OpenAI 형식 messages → agent 형식으로 되돌려 PolicyLLM에 묻고, 답을 OpenAI 응답 모양으로."""
    policy = PolicyLLM()

    def reply(kw):
        names, msgs = {}, []
        for m in kw["messages"][1:]:
            if m["role"] == "assistant":
                calls = [{"id": c["id"], "name": c["function"]["name"], "args": json.loads(c["function"]["arguments"])}
                         for c in m.get("tool_calls") or []]
                names.update({c["id"]: c["name"] for c in calls})
                msgs.append({"role": "assistant", "content": m["content"] or "", "tool_calls": calls})
            elif m["role"] == "tool":
                msgs.append({"role": "tool", "id": m["tool_call_id"], "name": names[m["tool_call_id"]],
                             "content": m["content"]})
            else:
                msgs.append({"role": m["role"], "content": m["content"]})
        tools = [{"name": t["function"]["name"], "description": t["function"]["description"],
                  "parameters": t["function"]["parameters"]} for t in kw["tools"]]
        r = policy.next(kw["messages"][0]["content"], msgs, tools)
        return response(*[(f"call_{len(msgs)}_{i}", t.name, t.args) for i, t in enumerate(r.tool_calls)])
    return FakeClient(reply)


def test_end_to_end_through_openai_format(con, tmp_path):
    client = _policy_client()
    r = run_request(TREND_RESULT_EXAMPLE, con, image_dir=str(tmp_path), llm=OpenAILLM(client=client))
    tr = r["trend_results"][0]
    assert tr["status"] in ("ok", "no_results"), tr["errors"]
    assert tr["search"]["steps"][-1]["tool"] == "finish"
    assert len(client.kwargs) == len(tr["search"]["steps"])          # 도구 1개씩 (parallel_tool_calls=False)
    assert client.kwargs[0]["messages"][0]["content"] == SYSTEM
