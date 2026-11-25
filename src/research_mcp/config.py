"""Settings from the environment (H-007). No secrets: the Cognito client is public."""
from __future__ import annotations

import os
from dataclasses import dataclass

# Through kagent's oauth2-proxy, which checks the caller's ID token (H-007 REQ-06, GUARD-01).
PROXY = "http://kagent-oauth2-proxy.kagent.svc.cluster.local:4180"
DEFAULT_AGENT_URL = PROXY + "/agents/kagent/research-agent"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    client_id: str                     # Cognito research-mcp-claude (public)
    cognito_issuer: str                # https://cognito-idp.<region>.amazonaws.com/<pool>
    cognito_domain: str                # hosted UI domain, no scheme
    public_base_url: str               # https://research.<domain> (this server)
    ui_base_url: str                   # https://kagent.<domain> (links for people)
    agent_url: str = DEFAULT_AGENT_URL
    reports_base_url: str = PROXY      # GET /reports/<run>/<name> as the caller
    cognito_idp: str = "IdentityCenter"
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080
    run_timeout_s: float = 600.0       # REQ-09
    start_wait_s: float = 8.0          # REQ-06: research_start answers within 10 s
    hold_s: float = 45.0               # REQ-10: set from EVAL-06
    runs_per_day: int = 20             # Budgets
    max_result_chars: int = 30_000     # REQ-08
    probe_tool: bool = False           # EVAL-06 only

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = dict(os.environ if env is None else env)
        required = ["COGNITO_CLIENT_ID", "COGNITO_ISSUER", "COGNITO_DOMAIN",
                    "PUBLIC_BASE_URL", "UI_BASE_URL"]
        missing = [k for k in required if not e.get(k)]
        if missing:
            raise ConfigError("missing settings: " + ", ".join(missing))
        for k in ("PUBLIC_BASE_URL", "UI_BASE_URL", "COGNITO_ISSUER"):
            if not e[k].startswith("https://"):
                raise ConfigError(f"{k} must be https://")
        return cls(
            client_id=e["COGNITO_CLIENT_ID"],
            cognito_issuer=e["COGNITO_ISSUER"].rstrip("/"),
            cognito_domain=e["COGNITO_DOMAIN"].removeprefix("https://").rstrip("/"),
            public_base_url=e["PUBLIC_BASE_URL"].rstrip("/"),
            ui_base_url=e["UI_BASE_URL"].rstrip("/"),
            agent_url=e.get("AGENT_URL") or DEFAULT_AGENT_URL,
            reports_base_url=(e.get("REPORTS_BASE_URL") or PROXY).rstrip("/"),
            cognito_idp=e.get("COGNITO_IDP", "IdentityCenter"),
            listen_port=int(e.get("LISTEN_PORT", "8080")),
            run_timeout_s=float(e.get("RUN_TIMEOUT_S", "600")),
            hold_s=float(e.get("HOLD_S", "45")),
            runs_per_day=int(e.get("RUNS_PER_DAY", "20")),
            max_result_chars=int(e.get("MAX_RESULT_CHARS", "30000")),
            probe_tool=e.get("PROBE_TOOL", "") == "1",
        )
