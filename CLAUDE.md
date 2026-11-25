# Working in this repo

Spec-driven, as the other hub repos: the spec is `ai-hub/specs/H-007-claude-connector/spec.md`.
No spec, no code; never build past a `[NEEDS CLARIFICATION: …]` marker.

## Rules that are code, not prompt text

- **Only Cognito-issued tokens:** the server never creates, signs or alters a JWT (REQ-03).
  It forwards the caller's own ID token to kagent through oauth2-proxy.
- **Every `/mcp` request is checked** (REQ-04): signature, `iss`, `aud`, `token_use=id`, `exp`.
- **A run belongs to its starter** (REQ-07): another user's run ID is "not found".
- **No token, question, report or stack trace in logs or traces** (REQ-11, REQ-12): fixed
  error text with a reference ID. `test_req12_no_secrets_in_logs` must keep passing.

## Commands

```bash
uv run pytest            # offline unit tests
```

Live checks need the hub and claude.ai: ask before running them. Reference requirement IDs
in tests and commit messages.
