"""Bearer token check (H-007 REQ-04): a Cognito **ID token** of research-mcp-claude.

claude.ai presents the ID token as its access token (REQ-03). Checked here: RS256 signature
against the pool's JWKS, `iss`, `aud` = this client, `token_use` = id, `exp`. oauth2-proxy
checks the same token again before kagent sees it (GUARD-01).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

import httpx
import jwt

JWKS_TTL_S = 6 * 3600
REFETCH_MIN_S = 60       # an unknown `kid` can't make us hammer Cognito


@dataclass
class Verified:
    ok: bool
    sub: str = ""
    reason: str = ""
    exp: int = 0


class TokenVerifier:
    def __init__(self, issuer: str, client_id: str, http: httpx.AsyncClient,
                 now: Callable[[], float] = time.time) -> None:
        self.issuer, self.client_id, self.http, self.now = issuer, client_id, http, now
        self._keys: dict[str, object] = {}
        self._at = -1e18

    async def _fetch(self) -> None:
        r = await self.http.get(f"{self.issuer}/.well-known/jwks.json", timeout=10)
        r.raise_for_status()
        self._keys = {k["kid"]: jwt.PyJWK(k).key for k in r.json().get("keys", [])
                      if k.get("kid") and k.get("kty") == "RSA"}
        self._at = self.now()

    async def _key(self, kid: str) -> object | None:
        if self.now() - self._at > JWKS_TTL_S:
            await self._fetch()
        if kid not in self._keys and self.now() - self._at > REFETCH_MIN_S:
            await self._fetch()
        return self._keys.get(kid)

    async def verify(self, token: str) -> Verified:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            return Verified(False, reason="malformed")
        if header.get("alg") != "RS256":
            return Verified(False, reason="alg")
        try:
            key = await self._key(str(header.get("kid", "")))
        except httpx.HTTPError:
            return Verified(False, reason="jwks unavailable")
        if key is None:
            return Verified(False, reason="unknown key")
        try:
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.client_id,
                                issuer=self.issuer,
                                options={"require": ["exp", "iss", "aud", "sub"]})
        except jwt.ExpiredSignatureError:
            return Verified(False, reason="expired")
        except jwt.InvalidAudienceError:
            return Verified(False, reason="audience")
        except jwt.InvalidIssuerError:
            return Verified(False, reason="issuer")
        except jwt.InvalidSignatureError:
            return Verified(False, reason="signature")
        except jwt.PyJWTError as e:
            return Verified(False, reason=f"invalid ({type(e).__name__})")
        if claims.get("token_use") != "id":
            return Verified(False, reason="token_use", sub=str(claims.get("sub", "")))
        return Verified(True, sub=str(claims["sub"]), exp=int(claims["exp"]))
