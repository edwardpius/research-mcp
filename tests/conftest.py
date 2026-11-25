"""Fakes: Cognito (JWKS signed with a real RSA key, token endpoint), the A2A endpoint behind
oauth2-proxy, the reports service. No test touches the network."""
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from a2a.types import a2a_pb2 as pb
from cryptography.hazmat.primitives.asymmetric import rsa
from google.protobuf import json_format

from research_mcp.app import create_app
from research_mcp.config import Settings

ISS = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_TEST"
CLIENT = "researchclient"
ALICE, BOB = "sub-alice", "sub-bob"
BASE = "https://research.example.com"
UI = "https://kagent.example.com"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(key, kid):
    d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**d, "kid": kid, "alg": "RS256", "use": "sig"}


def make_token(sub=ALICE, aud=CLIENT, token_use="id", exp_in=3600, iss=ISS, key=None,
               kid="k1", alg="RS256", **extra) -> str:
    keep_aud = extra.pop("keep_aud", False)
    claims = {"sub": sub, "aud": aud, "iss": iss, "token_use": token_use,
              "exp": int(time.time()) + exp_in, "iat": int(time.time()), "email": "a@example.com",
              **extra}
    if token_use == "access" and not keep_aud:
        claims.pop("aud")
        claims["client_id"] = aud
    if alg == "HS256":
        return jwt.encode(claims, "x" * 32, algorithm="HS256", headers={"kid": kid})
    return jwt.encode(claims, key or _KEY, algorithm="RS256", headers={"kid": kid})


def make_settings(**kw) -> Settings:
    base = dict(client_id=CLIENT, cognito_issuer=ISS, cognito_domain="auth.example.com",
                public_base_url=BASE, ui_base_url=UI, start_wait_s=0.5, hold_s=0.5,
                run_timeout_s=3.0, runs_per_day=20)
    base.update(kw)
    return Settings(**base)


class FakeCognito:
    def __init__(self) -> None:
        self.jwks_calls = 0
        self.token_requests: list[dict] = []
        self.down = False
        self.status = 200
        self.id_token = make_token()

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("down")
        if request.url.path.endswith("/.well-known/jwks.json"):
            self.jwks_calls += 1
            return httpx.Response(200, json={"keys": [_jwk(_KEY, "k1")]})
        if request.url.path == "/oauth2/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append(form)
            if self.status != 200:
                return httpx.Response(self.status, json={"error": "invalid_grant"})
            body = {"id_token": self.id_token, "access_token": "cognito-access-token-x",
                    "expires_in": 3600, "token_type": "Bearer"}
            if form.get("grant_type") == "authorization_code":
                body["refresh_token"] = "refresh-token-x"
            return httpx.Response(200, json=body)
        return httpx.Response(404)


def sse(events: list[pb.StreamResponse]) -> bytes:
    out = []
    for ev in events:
        body = {"jsonrpc": "2.0", "id": "1", "result": json_format.MessageToDict(ev)}
        out.append(f"data: {json.dumps(body)}\n\n")
    return "".join(out).encode()


def status(state, text="", task_id="task-1", ctx="ctx-1") -> pb.StreamResponse:
    msg = pb.Message(message_id="m", role=pb.ROLE_AGENT, parts=[pb.Part(text=text)]) if text else None
    st = pb.TaskStatus(state=state, message=msg) if msg else pb.TaskStatus(state=state)
    return pb.StreamResponse(status_update=pb.TaskStatusUpdateEvent(
        task_id=task_id, context_id=ctx, status=st))


def task(task_id="task-1", ctx="ctx-1") -> pb.StreamResponse:
    return pb.StreamResponse(task=pb.Task(id=task_id, context_id=ctx,
                                          status=pb.TaskStatus(state=pb.TASK_STATE_SUBMITTED)))


REPORT = f"""# Lithium supply in 2030

Supply grows fast [1].

[1] https://www.iea.org/report

[Download the report]({UI}/reports/0123456789ab/lithium-supply-2030.md)

---
*12 turns · 4 searches · 9 pages read · $0.14 · 87 s*"""


class FakeA2A:
    """kagent's A2A endpoint behind oauth2-proxy: only tokens it was told are valid."""

    def __init__(self) -> None:
        self.valid_tokens: set[str] = set()
        self.requests: list[dict] = []
        self.script = [task(), status(pb.TASK_STATE_WORKING, "Searching the web: lithium 2030"),
                       status(pb.TASK_STATE_COMPLETED, REPORT)]
        self.hang = False
        self.slow_s = 0.0
        self.cancelled: list[str] = []
        self.status_code = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        auth = request.headers.get("authorization", "")
        self.requests.append({"method": body["method"], "auth": auth, "params": body["params"],
                              "headers": dict(request.headers)})
        if auth.removeprefix("Bearer ") not in self.valid_tokens:
            return httpx.Response(403, text="forbidden")
        if request.headers.get("a2a-version") != "1.0":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {
                "code": -32009, "message": "this version is not supported"}})
        if self.status_code != 200:
            return httpx.Response(self.status_code, text="internal: stack trace here")
        if body["method"] == "CancelTask":
            self.cancelled.append(body["params"]["id"])
            t = pb.Task(id=body["params"]["id"], status=pb.TaskStatus(state=pb.TASK_STATE_CANCELED))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": json_format.MessageToDict(t)})
        script, hang, slow = list(self.script), self.hang, self.slow_s

        async def stream():
            yield sse(script[:1])
            if hang:
                await asyncio.sleep(3600)
            if slow:
                await asyncio.sleep(slow)
            yield sse(script[1:])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream())


class FakeReports:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.reports = {"/reports/0123456789ab/lithium-supply-2030.md": (ALICE, REPORT)}

    def handler(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "").removeprefix("Bearer ")
        self.requests.append({"path": request.url.path, "auth": auth})
        try:
            sub = jwt.decode(auth, options={"verify_signature": False})["sub"]
        except Exception:
            return httpx.Response(403)
        owner, body = self.reports.get(request.url.path, (None, None))
        if owner != sub:
            return httpx.Response(404, json={"error": "no such report"})
        return httpx.Response(200, text=body, headers={"content-type": "text/markdown"})


class World:
    def __init__(self, **kw) -> None:
        self.settings = make_settings(**kw)
        self.cognito = FakeCognito()
        self.a2a = FakeA2A()
        self.reports = FakeReports()
        self.app = create_app(self.settings,
                              cognito_transport=httpx.MockTransport(self.cognito.handler),
                              agent_transport=httpx.MockTransport(self.a2a.handler),
                              reports_transport=httpx.MockTransport(self.reports.handler))
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE)
        self._id = 0

    def token(self, sub=ALICE, **kw) -> str:
        t = make_token(sub=sub, **kw)
        self.a2a.valid_tokens.add(t)
        return t

    async def rpc(self, method: str, params: dict | None = None, token: str | None = None):
        self._id += 1
        headers = {"authorization": f"Bearer {token}"} if token else {}
        r = await self.http.post("/mcp", json={"jsonrpc": "2.0", "id": self._id, "method": method,
                                               "params": params or {}}, headers=headers)
        return r

    async def call(self, name: str, args: dict, token: str) -> tuple[str, bool]:
        r = await self.rpc("tools/call", {"name": name, "arguments": args}, token)
        assert r.status_code == 200, r.text
        res = r.json()["result"]
        return res["content"][0]["text"], bool(res.get("isError"))


@pytest.fixture
async def world():
    w = World()
    yield w
    await w.http.aclose()


@pytest.fixture
def make_world():
    made: list[World] = []

    def _make(**kw) -> World:
        w = World(**kw)
        made.append(w)
        return w
    yield _make
