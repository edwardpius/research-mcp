"""research-mcp: MCP (streamable HTTP, stateless, JSON responses) plus the OAuth front for
claude.ai (H-007). The MCP framing is ported from hub-control (openbao-platform D-33), which
claude.ai already uses.

Routes (all on research.<domain>, the connector ALB, HD-008):
  POST /mcp                                         MCP, bearer ID token (REQ-01, REQ-04)
  GET  /.well-known/oauth-protected-resource[/mcp]  RFC 9728
  GET  /.well-known/oauth-authorization-server      RFC 8414 (also openid-configuration)
  GET  /oauth/authorize, POST /oauth/token          pass-through to Cognito (REQ-02, REQ-03)
  GET  /healthz
"""
from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from . import oauth, otel
from .agent import AgentClient
from .auth import TokenVerifier
from .config import Settings
from .log import log, ref_id, user_hash
from .runs import Runs
from .tools import Caller, ToolError, Tools

MCP_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26"]
MAX_BODY = 64 * 1024
BEARER = re.compile(r"^Bearer\s+(\S+)$", re.I)
INSTRUCTIONS = (
    "research-agent on the AI Hub: deep research with cited reports. Start with research_start, "
    "then call research_result with the run ID until it returns the report. Only works while "
    "the AI Hub is up; if this connector can't be reached, the AI Hub connector's hub_status "
    "tells whether the hub is running.")


def create_app(s: Settings, *, cognito_transport: httpx.AsyncBaseTransport | None = None,
               agent_transport: httpx.AsyncBaseTransport | None = None,
               reports_transport: httpx.AsyncBaseTransport | None = None,
               runs: Runs | None = None) -> Starlette:
    cognito_http = httpx.AsyncClient(transport=cognito_transport)
    reports_http = httpx.AsyncClient(transport=reports_transport)
    verifier = TokenVerifier(s.cognito_issuer, s.client_id, cognito_http)
    runs = runs or Runs(AgentClient(s.agent_url, s.run_timeout_s, agent_transport), s.runs_per_day)
    tools = Tools(s, runs, reports_http)
    prm_url = f"{s.public_base_url}/.well-known/oauth-protected-resource"

    def challenge(extra: str = "") -> Response:
        return JSONResponse({"error": "unauthorized"}, status_code=401,
                            headers={"www-authenticate": f'Bearer resource_metadata="{prm_url}"{extra}'})

    async def mcp(request: Request) -> Response:
        if request.method != "POST":
            return Response(status_code=405, headers={"allow": "POST"})
        m = BEARER.match(request.headers.get("authorization", ""))
        if not m:
            return challenge()
        v = await verifier.verify(m.group(1))
        if not v.ok:                                                   # REQ-04
            log("mcp refused", reason=v.reason, user=user_hash(v.sub) if v.sub else None,
                ip=_client_ip(request))
            return challenge(', error="invalid_token"')
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return JSONResponse(_rpc_err(None, -32600, "request too large"), status_code=413)
        try:
            msg = json.loads(raw)
        except ValueError:
            return JSONResponse(_rpc_err(None, -32700, "parse error"), status_code=400)
        caller = Caller(v.sub, m.group(1))
        batch = msg if isinstance(msg, list) else [msg]
        replies = [r for r in [await rpc(x, caller, request) for x in batch] if r is not None]
        if not replies:
            return Response(status_code=202)
        return JSONResponse(replies if isinstance(msg, list) else replies[0])

    async def rpc(m: object, caller: Caller, request: Request) -> dict | None:
        if not isinstance(m, dict) or m.get("jsonrpc") != "2.0" or not isinstance(m.get("method"), str):
            return _rpc_err(m.get("id") if isinstance(m, dict) else None, -32600, "invalid request")
        if "id" not in m:
            return None                                                # notification
        mid, method, params = m["id"], m["method"], m.get("params") or {}
        if method == "initialize":
            want = params.get("protocolVersion") if isinstance(params, dict) else None
            return _ok(mid, {"protocolVersion": want if want in MCP_VERSIONS else MCP_VERSIONS[1],
                             "capabilities": {"tools": {}},
                             "serverInfo": {"name": "research-mcp", "version": "0.1.0"},
                             "instructions": INSTRUCTIONS})
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, {"tools": tools.list()})
        if method == "tools/call":
            p = params if isinstance(params, dict) else {}
            name, args = str(p.get("name", "")), p.get("arguments") or {}
            u = user_hash(caller.sub)
            with otel.span("mcp.request", **{"mcp.tool": name, "user.hash": u, "via": "claude"}) as sp:
                try:
                    text = await tools.call(name, args, caller, otel.trace_headers())
                    log("mcp tool", name=name, user=u, outcome="ok", ip=_client_ip(request))
                    otel.set_attr(sp, "outcome", "ok")
                    return _ok(mid, {"content": [{"type": "text", "text": text}]})
                except ToolError as e:
                    log("mcp tool", name=name, user=u, outcome="refused", ip=_client_ip(request))
                    otel.set_attr(sp, "outcome", "refused")
                    return _ok(mid, {"content": [{"type": "text", "text": str(e)}], "isError": True})
                except Exception as e:                                 # REQ-11
                    ref = ref_id()
                    log("mcp tool error", name=name, user=u, ref=ref, error=type(e).__name__)
                    otel.set_attr(sp, "outcome", "error")
                    return _ok(mid, {"content": [{"type": "text",
                                                  "text": f"Internal error (reference {ref})."}],
                                     "isError": True})
        return _rpc_err(mid, -32601, f"method not found: {method[:60]}")

    async def prm(_r: Request) -> Response:
        return JSONResponse(oauth.protected_resource(s))

    async def asm(_r: Request) -> Response:
        return JSONResponse(oauth.auth_server(s))

    async def authorize(r: Request) -> Response:
        return oauth.authorize(s, r)

    async def token(r: Request) -> Response:
        return await oauth.token(s, r, cognito_http)

    async def healthz(_r: Request) -> Response:
        return PlainTextResponse("ok")

    @asynccontextmanager
    async def lifespan(_app):
        yield
        await runs.close()
        await cognito_http.aclose()
        await reports_http.aclose()

    return Starlette(routes=[
        Route("/mcp", mcp, methods=["GET", "POST", "DELETE"]),
        Route("/.well-known/oauth-protected-resource", prm, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/mcp", prm, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", asm, methods=["GET"]),
        Route("/.well-known/openid-configuration", asm, methods=["GET"]),
        Route("/oauth/authorize", authorize, methods=["GET"]),
        Route("/oauth/token", token, methods=["POST"]),
        Route("/healthz", healthz, methods=["GET"]),
    ], lifespan=lifespan)


def _ok(mid: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _rpc_err(mid: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _client_ip(request: Request) -> str:
    """The ALB appends the client to X-Forwarded-For; logged to confirm Anthropic's ranges."""
    xff = request.headers.get("x-forwarded-for", "")
    return xff.split(",")[-1].strip() if xff else (request.client.host if request.client else "")


def main() -> None:
    import uvicorn
    s = Settings.from_env()
    otel.setup("research-mcp")
    log("starting", public=s.public_base_url, agent=s.agent_url, hold_s=s.hold_s,
        probe_tool=s.probe_tool)
    uvicorn.run(create_app(s), host=s.listen_host, port=s.listen_port, access_log=False,
                proxy_headers=False)


if __name__ == "__main__":
    main()
