"""팀 공통 오류 코드 (2026-11-08 결정)

API 응답의 errors[].code는 팀 공통 코드만 쓴다. 03 안에서 쓰던 자세한 코드는 reason에 남긴다.
agent.run(규칙 방식, 트렌드 1개)은 기준선·검증용이라 안쪽 코드를 그대로 쓰고,
API 입구(multi.run_request)가 응답을 내보낼 때 to_team()으로 바꾼다.

    안쪽 코드 (reason)                                    팀 코드              HTTP
    missing_fields, invalid_input,
    schema_version_mismatch, upstream_error(02 status=error)  INVALID_REQUEST     400
    llm_failed + no_llm·llm_unavailable, db_unavailable      SOURCE_UNAVAILABLE  503
    llm_failed + timeout                                     TIMEOUT             504
    llm_failed + 그 밖의 원인, invalid_response,
    trend_failed(추세만 실패, 결과는 정상 → 200), 그 밖       INTERNAL_ERROR      500

llm_failed는 reason에 LLM 실패 원인(no_llm, llm_unavailable, timeout, invalid_response, rule_violation,
step_limit, invalid_final)을 그대로 둔다. 그 밖은 reason = 안쪽 코드.
"""

from __future__ import annotations

INVALID_REQUEST = "INVALID_REQUEST"
SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
TIMEOUT = "TIMEOUT"
INTERNAL_ERROR = "INTERNAL_ERROR"
TEAM_CODES = (INVALID_REQUEST, "UNAUTHORIZED", "FORBIDDEN", SOURCE_UNAVAILABLE, TIMEOUT, INTERNAL_ERROR)

HTTP_STATUS = {INVALID_REQUEST: 400, "UNAUTHORIZED": 401, "FORBIDDEN": 403,
               SOURCE_UNAVAILABLE: 503, TIMEOUT: 504, INTERNAL_ERROR: 500}

CLIENT = {"missing_fields", "invalid_input", "schema_version_mismatch", "upstream_error"}
SOURCE = {"db_unavailable"}

# 모든 트렌드가 서로 다른 오류일 때 전체 코드 (서버 쪽 문제를 먼저)
PRIORITY = (SOURCE_UNAVAILABLE, TIMEOUT, INTERNAL_ERROR, INVALID_REQUEST)


def team_code(code: str, reason: str | None = None) -> str:
    if code in TEAM_CODES:
        return code
    if code in CLIENT:
        return INVALID_REQUEST
    if code in SOURCE:
        return SOURCE_UNAVAILABLE
    if code == "llm_failed":
        return (SOURCE_UNAVAILABLE if reason in ("no_llm", "llm_unavailable")
                else TIMEOUT if reason == "timeout" else INTERNAL_ERROR)
    return INTERNAL_ERROR


def to_team(err: dict) -> dict:
    """오류 1개 → 팀 코드. 이미 팀 코드면 그대로."""
    code = err.get("code")
    if code in TEAM_CODES:
        return err
    reason = err.get("reason") if code == "llm_failed" else code
    return {**err, "code": team_code(code, err.get("reason")), "reason": reason}


def pick(codes) -> str:
    """여러 팀 코드 중 전체 응답에 쓸 하나."""
    codes = set(codes)
    for c in PRIORITY:
        if c in codes:
            return c
    return INTERNAL_ERROR


def http_status(resp: dict) -> int:
    """응답 → HTTP 상태. status=error일 때만 오류 번호, 나머지(ok·partial·no_results)는 200."""
    if resp.get("status") != "error" or not resp.get("errors"):
        return 200
    return HTTP_STATUS.get(resp["errors"][0].get("code"), 500)
