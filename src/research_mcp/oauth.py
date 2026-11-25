"""OAuth for claude.ai (H-007 REQ-01 to REQ-03), ported from hub-control (openbao-platform D-33).

Cognito has no dynamic client registration and isn't an MCP-aware authorization server, so
this server publishes the discovery documents and passes authorize and token through to
Cognito for ONE public client (PKCE) and ONE redirect URI. The token response hands
claude.ai Cognito's ID token as `access_token`: oauth2-proxy accepts ID tokens with this
client's audience, not Cognito access tokens (checked 2026-11-25, H-007 task 2). Nothing
here creates, signs or alters a token.
"""
from __future__ import annotations

import time
from urllib.parse import parse_qs, urlencode

import httpx
import jwt
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from .config import CLAUDE_CALLBACK, Settings
from .log import log

SCOPES = "openid email profile"   # oauth2-proxy needs the email claim in the ID token
NO_STORE = {"cache-control": "no-store"}


def protected_resource(s: Settings) -> dict:
    return {"resource": f"{s.public_base_url}/mcp",
            "authorization_servers": [s.public_base_url],
            "bearer_methods_supported": ["header"],
            "scopes_supported": SCOPES.split(),
            "resource_name": "Research (AI Hub research-agent)"}


def auth_server(s: Settings) -> dict:
    b = s.public_base_url
    return {"issuer": b,
            "authorization_endpoint": f"{b}/oauth/authorize",
            "token_endpoint": f"{b}/oauth/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": SCOPES.split()}


def authorize(s: Settings, request: Request) -> Response:
    q = request.query_params

    def bad(why: str) -> Response:
        log("oauth authorize refused", why=why)
        return PlainTextResponse(f"Invalid sign-in request: {why}", status_code=400)

    if q.get("client_id") != s.client_id:
        return bad("unknown client_id")
    if q.get("redirect_uri") != CLAUDE_CALLBACK:
        return bad("redirect_uri not allowed")
    if q.get("response_type") != "code":
        return bad("response_type must be code")
    if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
        return bad("PKCE (S256) required")
    out = {"response_type": "code", "client_id": s.client_id, "redirect_uri": CLAUDE_CALLBACK,
           "scope": SCOPES, "code_challenge": q["code_challenge"],
           "code_challenge_method": "S256", "identity_provider": s.cognito_idp}
    if q.get("state"):
        out["state"] = q["state"]
    # Unknown parameters (e.g. RFC 8707 `resource`) are dropped: Cognito may refuse them.
    return RedirectResponse(f"https://{s.cognito_domain}/oauth2/authorize?{urlencode(out)}",
                            status_code=302, headers=NO_STORE)


def _err(status: int, error: str, desc: str) -> Response:
    return JSONResponse({"error": error, "error_description": desc}, status_code=status,
                        headers=NO_STORE)


async def token(s: Settings, request: Request, http: httpx.AsyncClient) -> Response:
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    grant = form.get("grant_type", "")
    if form.get("client_id") != s.client_id:
        return _err(401, "invalid_client", "unknown client")
    out = {"grant_type": grant, "client_id": s.client_id}
    if grant == "authorization_code":
        if form.get("redirect_uri") != CLAUDE_CALLBACK:
            return _err(400, "invalid_grant", "redirect_uri")
        for k in ("code", "redirect_uri", "code_verifier"):
            if form.get(k):
                out[k] = form[k]
    elif grant == "refresh_token":
        if form.get("refresh_token"):
            out["refresh_token"] = form["refresh_token"]
    else:
        return _err(400, "unsupported_grant_type", grant[:40])
    try:
        r = await http.post(f"https://{s.cognito_domain}/oauth2/token", data=out, timeout=15)
    except httpx.HTTPError as e:
        log("oauth token", grant_type=grant, error=type(e).__name__)
        return _err(502, "temporarily_unavailable", "sign-in service unreachable")
    log("oauth token", grant_type=grant, status=r.status_code)
    if r.status_code != 200:
        # Cognito's own error body (invalid_grant etc.): no tokens in it.
        return Response(r.content, status_code=r.status_code,
                        media_type="application/json", headers=NO_STORE)
    data = r.json()
    if "id_token" not in data:
        return _err(502, "server_error", "no ID token from Cognito (scope must include openid)")
    body = {"access_token": data["id_token"], "token_type": "Bearer",
            "expires_in": _expires_in(data["id_token"], data.get("expires_in", 3600))}
    if data.get("refresh_token"):
        body["refresh_token"] = data["refresh_token"]
    return JSONResponse(body, headers=NO_STORE)


def _expires_in(id_token: str, fallback: int) -> int:
    try:
        exp = int(jwt.decode(id_token, options={"verify_signature": False})["exp"])
        return max(0, min(int(fallback), exp - int(time.time())))
    except Exception:
        return int(fallback)
