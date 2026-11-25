"""Sign-in: discovery documents, authorize and token pass-through (H-007 REQ-01 to REQ-03)."""
from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import jwt

from conftest import BASE, CLIENT

CB = "https://claude.ai/api/mcp/auth_callback"


async def test_req01_discovery_documents(world):
    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        d = (await world.http.get(path)).json()
        assert d["resource"] == f"{BASE}/mcp"
        assert d["authorization_servers"] == [BASE]
    for path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
        a = (await world.http.get(path)).json()
        assert a["authorization_endpoint"] == f"{BASE}/oauth/authorize"
        assert a["token_endpoint"] == f"{BASE}/oauth/token"
        assert a["code_challenge_methods_supported"] == ["S256"]
        assert a["token_endpoint_auth_methods_supported"] == ["none"]
        assert "registration_endpoint" not in a


def authorize_qs(**over):
    q = {"response_type": "code", "client_id": CLIENT, "redirect_uri": CB, "state": "st-1",
         "code_challenge": "abc", "code_challenge_method": "S256",
         "resource": f"{BASE}/mcp", "scope": "openid"}
    q.update(over)
    return {k: v for k, v in q.items() if v is not None}


async def test_req02_authorize_redirects_to_cognito_with_idp_and_pkce(world):
    r = await world.http.get("/oauth/authorize", params=authorize_qs())
    assert r.status_code == 302
    u = urlparse(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert u.netloc == "auth.example.com" and u.path == "/oauth2/authorize"
    assert q["identity_provider"] == "IdentityCenter"
    assert q["client_id"] == CLIENT and q["redirect_uri"] == CB and q["state"] == "st-1"
    assert q["code_challenge_method"] == "S256" and q["code_challenge"] == "abc"
    assert set(q["scope"].split()) == {"openid", "email", "profile"}
    assert "resource" not in q                     # dropped: Cognito may refuse it


async def test_req02_authorize_refuses_other_clients_callbacks_and_no_pkce(world):
    for bad in (dict(client_id="evil"), dict(redirect_uri="https://evil.example/cb"),
                dict(code_challenge_method="plain"), dict(code_challenge=None),
                dict(response_type="token")):
        r = await world.http.get("/oauth/authorize", params=authorize_qs(**bad))
        assert r.status_code == 400, bad


async def token_req(world, **form):
    return await world.http.post("/oauth/token", data=form)


async def test_req03_code_exchange_hands_claude_the_id_token(world):
    r = await token_req(world, grant_type="authorization_code", client_id=CLIENT, code="c1",
                        redirect_uri=CB, code_verifier="v1")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["access_token"] == world.cognito.id_token          # the ID token, unaltered
    assert body["refresh_token"] == "refresh-token-x" and body["token_type"] == "Bearer"
    assert 0 < body["expires_in"] <= 3600
    assert "cognito-access-token-x" not in r.text                  # Cognito's access token dropped
    sent = world.cognito.token_requests[-1]
    assert sent == {"grant_type": "authorization_code", "client_id": CLIENT, "code": "c1",
                    "redirect_uri": CB, "code_verifier": "v1"}


async def test_req03_refresh_passes_through(world):
    r = await token_req(world, grant_type="refresh_token", client_id=CLIENT,
                        refresh_token="refresh-token-x")
    assert r.status_code == 200 and r.json()["access_token"] == world.cognito.id_token
    assert "refresh_token" not in r.json()        # Cognito sends none on refresh: claude.ai keeps its own
    assert world.cognito.token_requests[-1]["refresh_token"] == "refresh-token-x"


async def test_req03_expires_in_follows_the_id_token(world):
    world.cognito.id_token = jwt.encode({"exp": int(time.time()) + 120, "sub": "s"}, "k" * 32)
    r = await token_req(world, grant_type="refresh_token", client_id=CLIENT, refresh_token="r")
    assert 100 <= r.json()["expires_in"] <= 120


async def test_req03_token_errors(world):
    r = await token_req(world, grant_type="authorization_code", client_id="evil", code="c",
                        redirect_uri=CB, code_verifier="v")
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
    r = await token_req(world, grant_type="authorization_code", client_id=CLIENT, code="c",
                        redirect_uri="https://evil.example/cb", code_verifier="v")
    assert r.status_code == 400
    r = await token_req(world, grant_type="client_credentials", client_id=CLIENT)
    assert r.status_code == 400 and r.json()["error"] == "unsupported_grant_type"
    world.cognito.status = 400
    r = await token_req(world, grant_type="refresh_token", client_id=CLIENT, refresh_token="x")
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    world.cognito.down = True
    r = await token_req(world, grant_type="refresh_token", client_id=CLIENT, refresh_token="x")
    assert r.status_code == 502
