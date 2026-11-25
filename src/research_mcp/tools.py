"""The connector's tools (H-007 Tools). Each returns plain text for Claude."""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass

import httpx

from .config import Settings
from .log import log, ref_id, user_hash
from .runs import Busy, DailyLimit, Run, Runs

RUN_ID = re.compile(r"^[0-9a-f]{12}$")
# research-agent spec 026: /reports/<run_id>/<name>, run_id 12 hex, name a slug .md
REPORT_PATH = re.compile(r"/reports/([0-9a-f]{12})/([a-z0-9][a-z0-9-]{0,80}\.md)")


class ToolError(Exception):
    """Shown to Claude as the tool's error text. Never holds internals or tokens."""


@dataclass
class Caller:
    sub: str
    token: str                       # the caller's ID token; forwarded, never logged


TOOLS = [
    {"name": "research_start",
     "description": (
         "Ask research-agent, the AI Hub's research assistant, a research question. It searches "
         "the web and scholarly sources, reads them and writes a report with numbered citations "
         "(usually 1-3 minutes, about $0.30). Returns a run ID at once: then call "
         "research_result with it. Use for questions that need several sources and citations, "
         "not quick facts. One run at a time per user."),
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["question"],
                     "properties": {"question": {"type": "string", "minLength": 10, "maxLength": 2000,
                                                 "description": "The research question, in full."}}},
     "annotations": {"title": "Start research", "readOnlyHint": False, "destructiveHint": False,
                     "idempotentHint": False, "openWorldHint": True}},
    {"name": "research_result",
     "description": (
         "The report of a research run, waiting up to about a minute if it's still running. If "
         "it says 'still running', call research_result again with the same run ID."),
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["run_id"],
                     "properties": {"run_id": {"type": "string", "pattern": "^[0-9a-f]{12}$"}}},
     "annotations": {"title": "Get research result", "readOnlyHint": True}},
    {"name": "research_status",
     "description": "Progress of a research run (state, elapsed time, latest step), without waiting.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["run_id"],
                     "properties": {"run_id": {"type": "string", "pattern": "^[0-9a-f]{12}$"}}},
     "annotations": {"title": "Research status", "readOnlyHint": True}},
    {"name": "list_my_reports",
     "description": (
         "Research runs you started from Claude in the last 24 hours, with their report links. "
         "Older reports and runs started elsewhere are in the AI Hub's kagent UI."),
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
     "annotations": {"title": "List my research runs", "readOnlyHint": True}},
    {"name": "get_report",
     "description": (
         "Read one of your own research-agent reports by its link "
         "(https://kagent.../reports/<run>/<name>.md, or the /reports/... path)."),
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["link"],
                     "properties": {"link": {"type": "string", "maxLength": 300}}},
     "annotations": {"title": "Read a report", "readOnlyHint": True}},
]

PROBE_TOOL = {
    "name": "debug_hold",
    "description": "EVAL-06 only: hold the call open for `seconds`, then answer.",
    "inputSchema": {"type": "object", "additionalProperties": False, "required": ["seconds"],
                    "properties": {"seconds": {"type": "integer", "minimum": 1, "maximum": 900}}},
    "annotations": {"readOnlyHint": True},
}


class Tools:
    def __init__(self, s: Settings, runs: Runs, http: httpx.AsyncClient) -> None:
        self.s, self.runs, self.http = s, runs, http

    def list(self) -> list[dict]:
        return TOOLS + ([PROBE_TOOL] if self.s.probe_tool else [])

    async def call(self, name: str, args: dict, caller: Caller, headers: dict[str, str]) -> str:
        if not isinstance(args, dict):
            raise ToolError("arguments must be an object")
        if name == "research_start":
            return await self.start(caller, args, headers)
        if name == "research_result":
            return await self.result(caller, _run_id(args))
        if name == "research_status":
            return self.status(caller, _run_id(args))
        if name == "list_my_reports":
            return self.mine(caller)
        if name == "get_report":
            return await self.report(caller, args)
        if name == "debug_hold" and self.s.probe_tool:
            n = int(args.get("seconds", 1))
            t0 = time.monotonic()
            await asyncio.sleep(max(1, min(n, 900)))
            return f"held {time.monotonic() - t0:.0f} s"
        raise ToolError(f"unknown tool {name}")

    # -------------------------------------------------------------- research_start
    async def start(self, caller: Caller, args: dict, headers: dict[str, str]) -> str:
        q = args.get("question")
        if not isinstance(q, str) or not 10 <= len(q.strip()) <= 2000:
            raise ToolError("question must be 10 to 2000 characters, "
                            "e.g. {\"question\": \"What does RFC 9309 standardize?\"}")
        try:
            run = self.runs.start(caller.sub, caller.token, q.strip(), headers)
        except Busy as b:
            raise ToolError(f"A run is already in progress: {b.run_id}. "
                            "Call research_result with that run ID.") from None
        except DailyLimit:
            raise ToolError(f"Daily limit of {self.s.runs_per_day} runs reached; "
                            "try again tomorrow.") from None
        log("run started", run_id=run.id, user=user_hash(caller.sub))
        await self.runs.wait_started(run, self.s.start_wait_s)          # REQ-06
        if run.state == "error":
            raise ToolError(run.error)
        return (f"Started research run {run.id}. It usually takes 1-3 minutes. "
                f"Call research_result with run_id \"{run.id}\" for the report.")

    # -------------------------------------------------------------- result / status
    def _get(self, caller: Caller, run_id: str) -> Run:
        run = self.runs.get(caller.sub, run_id)
        if run is None:
            raise ToolError(f"Run {run_id} not found (runs are kept 24 hours).")
        return run

    async def result(self, caller: Caller, run_id: str) -> str:
        run = self._get(caller, run_id)
        if run.running:
            await self.runs.wait_done(run, self.s.hold_s)               # REQ-10
        if run.running:
            return (f"Still running ({_elapsed(run)}). Latest step: {run.progress or 'starting'}. "
                    f"Call research_result again with run_id \"{run.id}\".")
        if run.state == "completed":
            return self._report_text(run)
        return _ended(run)

    def status(self, caller: Caller, run_id: str) -> str:
        run = self._get(caller, run_id)
        if run.running:
            return f"{run.state} ({_elapsed(run)}). Latest step: {run.progress or 'starting'}."
        return f"{run.state} after {_elapsed(run)}." + (f" {run.error}" if run.error else "")

    def _report_text(self, run: Run) -> str:
        text = run.text.strip() or "(research-agent returned an empty report)"
        cap = self.s.max_result_chars
        if len(text) > cap:                                             # REQ-08
            link = _link(run.text, self.s.ui_base_url)
            text = (text[:cap].rstrip() + f"\n\n[Report cut at {cap} characters. Full report: "
                    f"{link or self.s.ui_base_url} (or get_report with that link).]")
        return text

    def mine(self, caller: Caller) -> str:
        runs = self.runs.mine(caller.sub)
        if not runs:
            return ("No runs started from Claude in the last 24 hours. "
                    f"Earlier reports are in the kagent UI: {self.s.ui_base_url}")
        lines = []
        for r in runs:
            link = _link(r.text, self.s.ui_base_url) if r.state == "completed" else ""
            when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(r.started))
            lines.append(f"- {r.id} · {when} · {r.state} · {r.question[:120]}"
                         + (f" · {link}" if link else ""))
        return "\n".join(lines)

    # -------------------------------------------------------------- get_report
    async def report(self, caller: Caller, args: dict) -> str:
        m = REPORT_PATH.search(str(args.get("link", "")))
        if not m:
            raise ToolError("link must be a report link like "
                            "https://kagent.example/reports/0123456789ab/my-report.md")
        url = f"{self.s.reports_base_url}/reports/{m.group(1)}/{m.group(2)}"
        try:
            r = await self.http.get(url, headers={"Authorization": f"Bearer {caller.token}"},
                                    timeout=20)
        except httpx.HTTPError as e:
            ref = ref_id()
            log("report error", ref=ref, error=type(e).__name__, user=user_hash(caller.sub))
            raise ToolError(f"The report service couldn't be reached (reference {ref}).") from None
        if r.status_code == 404:
            raise ToolError("No such report among yours.")
        if r.status_code in (401, 403):
            raise ToolError("The hub refused your sign-in. Reconnect the Research connector.")
        if r.status_code != 200:
            ref = ref_id()
            log("report error", ref=ref, status=r.status_code, user=user_hash(caller.sub))
            raise ToolError(f"The report service answered {r.status_code} (reference {ref}).")
        text = r.text
        if len(text) > self.s.max_result_chars:
            text = text[: self.s.max_result_chars] + "\n\n[Cut; download the full report from the link.]"
        return text


def _run_id(args: dict) -> str:
    rid = str(args.get("run_id", "")) if isinstance(args, dict) else ""
    if not RUN_ID.match(rid):
        raise ToolError("run_id must be the 12-character ID research_start returned")
    return rid


def _elapsed(run: Run) -> str:
    end = run.finished or time.time()
    s = max(0, int(end - run.started))
    return f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s"


def _ended(run: Run) -> str:
    if run.state == "timeout":
        return run.error
    if run.state == "error":
        return run.error
    detail = f" {run.text.strip()[:500]}" if run.text.strip() else ""
    return f"The run ended as {run.state}.{detail}"


def _link(text: str, ui_base: str) -> str:
    m = REPORT_PATH.search(text or "")
    return f"{ui_base}/reports/{m.group(1)}/{m.group(2)}" if m else ""
