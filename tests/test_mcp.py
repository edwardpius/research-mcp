"""MCP framing and the tools (H-007 REQ-06 to REQ-12)."""
from __future__ import annotations

import asyncio
import re

from a2a.types import a2a_pb2 as pb

from conftest import ALICE, BOB, REPORT, UI, status, task

Q = "What does RFC 9309 standardize, and since when?"


async def start(world, token, q=Q) -> str:
    text, err = await world.call("research_start", {"question": q}, token)
    assert not err, text
    return re.search(r"run ([0-9a-f]{12})", text).group(1)


async def test_initialize_list_ping_and_errors(world):
    t = world.token()
    r = (await world.rpc("initialize", {"protocolVersion": "2025-06-18"}, t)).json()["result"]
    assert r["protocolVersion"] == "2025-06-18" and r["capabilities"] == {"tools": {}}
    assert "research_start" in r["instructions"]
    names = [x["name"] for x in (await world.rpc("tools/list", token=t)).json()["result"]["tools"]]
    assert names == ["research_start", "research_result", "research_status", "list_my_reports",
                     "get_report"]
    assert (await world.rpc("ping", token=t)).json()["result"] == {}
    assert (await world.rpc("nope", token=t)).json()["error"]["code"] == -32601
    r = await world.http.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                              headers={"authorization": f"Bearer {t}"})
    assert r.status_code == 202
    r = await world.http.post("/mcp", content=b"{nope", headers={"authorization": f"Bearer {t}"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700


async def test_probe_tool_only_when_enabled(make_world):
    w = make_world(probe_tool=True)
    t = w.token()
    names = [x["name"] for x in (await w.rpc("tools/list", token=t)).json()["result"]["tools"]]
    assert "debug_hold" in names
    text, err = await w.call("debug_hold", {"seconds": 1}, t)
    assert not err and text.startswith("held")


async def test_req06_start_then_result_with_the_callers_own_token(world):
    t = world.token()
    rid = await start(world, t)
    text, err = await world.call("research_result", {"run_id": rid}, t)
    assert not err and text == REPORT.strip()
    req = world.a2a.requests[0]
    assert req["auth"] == f"Bearer {t}"                   # the caller's token, unaltered
    assert req["headers"]["a2a-version"] == "1.0"
    assert req["params"]["message"]["parts"][0]["text"] == Q


async def test_req06_start_answers_quickly_even_for_a_long_run(world):
    world.a2a.slow_s = 30
    t = world.token()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    rid = await start(world, t)
    assert loop.time() - t0 < 2
    text, _ = await world.call("research_status", {"run_id": rid}, t)
    assert text.startswith("working (")


async def test_req07_other_users_run_is_not_found(world):
    a, b = world.token(ALICE), world.token(BOB)
    rid = await start(world, a)
    for tool in ("research_result", "research_status"):
        text, err = await world.call(tool, {"run_id": rid}, b)
        assert err and "not found" in text
    text, _ = await world.call("list_my_reports", {}, b)
    assert rid not in text


async def test_req08_long_report_is_cut_with_the_ui_link(make_world):
    w = make_world(max_result_chars=40)
    t = w.token()
    rid = await start(w, t)
    text, err = await w.call("research_result", {"run_id": rid}, t)
    assert not err and "Report cut at 40 characters" in text
    assert f"{UI}/reports/0123456789ab/lithium-supply-2030.md" in text


async def test_req09_one_run_at_a_time_and_daily_limit(make_world):
    w = make_world(runs_per_day=2)
    w.a2a.slow_s = 0.2
    t = w.token()
    rid = await start(w, t)
    text, err = await w.call("research_start", {"question": Q}, t)
    assert err and rid in text and "already in progress" in text
    await w.call("research_result", {"run_id": rid}, t)
    await start(w, t)
    await asyncio.sleep(0.4)
    text, err = await w.call("research_start", {"question": Q}, t)
    assert err and "Daily limit" in text
    other = w.token(BOB)
    await start(w, other)                                  # limits are per user


async def test_req09_run_cancelled_after_the_wall_clock_cap(make_world):
    w = make_world(run_timeout_s=0.5, hold_s=2)
    w.a2a.hang = True
    t = w.token()
    rid = await start(w, t)
    text, _ = await w.call("research_result", {"run_id": rid}, t)
    assert "passed 10 minutes" in text
    assert w.a2a.cancelled == ["task-1"]


async def test_req10_result_returns_still_running_within_the_hold(make_world):
    w = make_world(hold_s=0.3)
    w.a2a.slow_s = 30
    t = w.token()
    rid = await start(w, t)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    text, err = await w.call("research_result", {"run_id": rid}, t)
    assert not err and text.startswith("Still running") and rid in text
    assert loop.time() - t0 < 1.5


async def test_req11_backend_failures_get_fixed_text_with_a_reference(world):
    world.a2a.status_code = 500
    t = world.token()
    text, err = await world.call("research_start", {"question": Q}, t)
    assert err and re.search(r"reference [0-9a-f]{8}", text)
    assert "stack trace" not in text and "500" not in text


async def test_req11_token_refused_downstream_says_reconnect(world):
    t = world.token()
    world.a2a.valid_tokens.discard(t)                   # oauth2-proxy refuses it
    text, err = await world.call("research_start", {"question": Q}, t)
    assert err and "Reconnect" in text


async def test_failed_run_state_is_reported(world):
    world.a2a.script = [task(), status(pb.TASK_STATE_FAILED, "Budget exhausted")]
    t = world.token()
    rid = await start(world, t)
    text, err = await world.call("research_result", {"run_id": rid}, t)
    assert not err and "failed" in text and "Budget exhausted" in text


async def test_argument_validation(world):
    t = world.token()
    for name, args in (("research_start", {"question": "short"}), ("research_start", {}),
                       ("research_result", {"run_id": "../../etc"}),
                       ("get_report", {"link": "https://evil.example/x"}),
                       ("nope", {})):
        text, err = await world.call(name, args, t)
        assert err, (name, args)
    assert world.a2a.requests == []


async def test_list_my_reports_and_get_report(world):
    t = world.token()
    rid = await start(world, t)
    await world.call("research_result", {"run_id": rid}, t)
    text, _ = await world.call("list_my_reports", {}, t)
    assert rid in text and f"{UI}/reports/0123456789ab/lithium-supply-2030.md" in text
    text, err = await world.call("get_report",
                                 {"link": f"{UI}/reports/0123456789ab/lithium-supply-2030.md"}, t)
    assert not err and text == REPORT
    assert world.reports.requests[-1] == {"path": "/reports/0123456789ab/lithium-supply-2030.md",
                                          "auth": t}
    text, err = await world.call("get_report",
                                 {"link": "/reports/0123456789ab/lithium-supply-2030.md"},
                                 world.token(BOB))
    assert err and "No such report" in text


async def test_req12_no_secrets_in_logs(world, capsys):
    t = world.token()
    rid = await start(world, t)
    await world.call("research_result", {"run_id": rid}, t)
    await world.http.post("/oauth/token", data={"grant_type": "authorization_code",
                                                "client_id": "researchclient", "code": "code-x",
                                                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                                                "code_verifier": "v"})
    await world.rpc("tools/list", token="forged.token.value")
    world.a2a.status_code = 500
    q2 = "Another question about lithium?"
    await world.call("research_start", {"question": q2}, t)
    out = capsys.readouterr().out
    assert out.strip()
    for secret in (t, t.split(".")[2], world.cognito.id_token, "refresh-token-x", "code-x",
                   "cognito-access-token-x", Q, q2, "Lithium supply", ALICE, "stack trace"):
        assert secret not in out, secret
