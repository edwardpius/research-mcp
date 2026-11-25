"""Bearer checks on /mcp (H-007 REQ-04, FM-02, FM-05)."""
from __future__ import annotations

from conftest import BASE, _OTHER_KEY, make_token


def challenged(r) -> bool:
    return (r.status_code == 401 and
            f'resource_metadata="{BASE}/.well-known/oauth-protected-resource"'
            in r.headers.get("www-authenticate", ""))


async def test_req04_no_token_gets_the_discovery_challenge(world):
    r = await world.rpc("tools/list")
    assert challenged(r) and "invalid_token" not in r.headers["www-authenticate"]


async def test_req04_bad_tokens_refused_and_nothing_called(world):
    bad = {
        "expired": make_token(exp_in=-10),
        "audience": make_token(aud="slack-bridge-client"),
        "issuer": make_token(iss="https://cognito-idp.us-west-2.amazonaws.com/other"),
        "access token": make_token(token_use="access"),        # Cognito access token
        "token_use": make_token(token_use="access", keep_aud=True),  # right aud, wrong use
        "signature": make_token(key=_OTHER_KEY),
        "unknown key": make_token(kid="k9", key=_OTHER_KEY),
        "alg": make_token(alg="HS256"),
        "malformed": "not-a-jwt",
    }
    for why, t in bad.items():
        world.a2a.valid_tokens.add(t)            # even if the backend would take it
        r = await world.rpc("tools/call", {"name": "research_start",
                                           "arguments": {"question": "What is RFC 9309 about?"}}, t)
        assert challenged(r), why
        assert 'error="invalid_token"' in r.headers["www-authenticate"], why
    assert world.a2a.requests == []


async def test_req04_forged_payload_with_real_signature_refused(world):
    good = world.token()
    h, p, s = good.split(".")
    import base64, json
    claims = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    claims["sub"] = "sub-mallory"
    p2 = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    r = await world.rpc("tools/list", token=f"{h}.{p2}.{s}")
    assert challenged(r)


async def test_req04_valid_token_works_and_jwks_is_cached(world):
    t = world.token()
    for _ in range(3):
        r = await world.rpc("tools/list", token=t)
        assert r.status_code == 200
    assert world.cognito.jwks_calls == 1


async def test_req04_unknown_kid_refetch_is_rate_limited(world):
    await world.rpc("tools/list", token=world.token())
    for _ in range(5):
        await world.rpc("tools/list", token=make_token(kid="k9", key=_OTHER_KEY))
    assert world.cognito.jwks_calls == 1          # within a minute of the last fetch


async def test_req04_get_and_delete_on_mcp_not_allowed(world):
    t = world.token()
    for m in ("GET", "DELETE"):
        r = await world.http.request(m, "/mcp", headers={"authorization": f"Bearer {t}"})
        assert r.status_code == 405
