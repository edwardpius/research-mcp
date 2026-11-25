# research-mcp

Ask **research-agent** on Edward's AI Hub from **claude.ai**, as your own hub user.

A remote MCP server (streamable HTTP) that claude.ai adds as a custom connector. You sign
in with the hub's Cognito and IAM Identity Center; the server then starts research-agent
runs over A2A **with your own ID token**, through kagent's oauth2-proxy, the same path as
the Slack bridge. Your runs and reports are the same as in the kagent UI and Slack.

Spec: `ai-hub/specs/H-007-claude-connector/spec.md` · decisions HD-007, HD-008 ·
infrastructure openbao-platform D-35.

## How it works

```
claude.ai ──HTTPS──▶ connector ALB (research.<domain>; Anthropic's ranges + your /32s)
                         │
                         ▼
                    research-mcp ──A2A, your ID token──▶ kagent oauth2-proxy ──▶ research-agent
                         │
                         └── /oauth/* ──▶ Cognito (Identity Center sign-in)
```

- **Sign-in:** the server publishes OAuth discovery documents and passes `/oauth/authorize`
  and `/oauth/token` through to Cognito for one public PKCE client whose only callback is
  claude.ai's. claude.ai receives Cognito's **ID token** as its access token, because
  oauth2-proxy accepts ID tokens (not Cognito access tokens) as bearer tokens. The server
  never creates, signs or stores a token.
- **Every MCP request** carries that ID token; the server checks its signature, issuer,
  audience, `token_use` and expiry, and oauth2-proxy checks it again.
- **Tools:** `research_start` (returns a run ID at once), `research_result` (waits up to
  `HOLD_S`, then says "still running"), `research_status`, `list_my_reports` (runs started
  from Claude in the last 24 hours), `get_report` (one of your reports by its link).
- One run per user at a time, 20 per day, 10 minutes each.

## Configuration

| Variable | Example |
|---|---|
| `COGNITO_CLIENT_ID` | research-mcp-claude's ID (openbao-platform layer 25 output `research_mcp_client_id`) |
| `COGNITO_ISSUER` | `https://cognito-idp.us-west-2.amazonaws.com/<pool>` |
| `COGNITO_DOMAIN` | `<prefix>.auth.us-west-2.amazoncognito.com` |
| `PUBLIC_BASE_URL` | `https://research.<domain>` |
| `UI_BASE_URL` | `https://kagent.<domain>` |
| `AGENT_URL`, `REPORTS_BASE_URL` | default: kagent's oauth2-proxy Service |
| `HOLD_S` | how long `research_result` waits (set from H-007 EVAL-06); default 45 |
| `PROBE_TOOL=1` | adds `debug_hold` for EVAL-06 only |

## Development

```bash
uv run pytest            # offline tests: fake Cognito (real RSA keys), fake A2A, fake reports
```
