"""Runs started from claude.ai (H-007 REQ-06 to REQ-11).

A run is one A2A call, held open by this server in the background while claude.ai polls
with research_status / research_result. Runs live in memory only: a restart loses them,
but their reports stay in kagent and the reports bucket (Memory and context).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Mapping

from .agent import AgentAuthError, AgentClient, AgentError, RunTimeout
from .log import log, ref_id, user_hash

KEEP_S = 24 * 3600


class Busy(Exception):
    def __init__(self, run_id: str) -> None:
        super().__init__(run_id)
        self.run_id = run_id


class DailyLimit(Exception):
    pass


@dataclass
class Run:
    id: str
    owner: str                       # Cognito sub
    question: str
    started: float
    state: str = "starting"          # starting | working | completed | failed | canceled
    progress: str = ""               #   | timeout | error
    text: str = ""
    error: str = ""
    task_id: str = ""
    context_id: str = ""
    finished: float = 0.0
    done: asyncio.Event = field(default_factory=asyncio.Event)
    started_evt: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def running(self) -> bool:
        return self.state in ("starting", "working")


class Runs:
    def __init__(self, agent: AgentClient, runs_per_day: int,
                 now: Callable[[], float] = time.time) -> None:
        self.agent, self.runs_per_day, self.now = agent, runs_per_day, now
        self._runs: dict[str, Run] = {}
        self._tasks: set[asyncio.Task] = set()

    def _prune(self) -> None:
        cutoff = self.now() - KEEP_S
        for rid in [r.id for r in self._runs.values() if not r.running and r.started < cutoff]:
            del self._runs[rid]

    def mine(self, owner: str) -> list[Run]:
        self._prune()
        return sorted((r for r in self._runs.values() if r.owner == owner),
                      key=lambda r: r.started, reverse=True)

    def get(self, owner: str, run_id: str) -> Run | None:
        """REQ-07: another user's run is 'not found', the same as a missing one."""
        r = self._runs.get(run_id)
        return r if r is not None and r.owner == owner else None

    def start(self, owner: str, token: str, question: str,
              headers: Mapping[str, str] | None = None) -> Run:
        mine = self.mine(owner)
        busy = next((r for r in mine if r.running), None)
        if busy:
            raise Busy(busy.id)                                     # REQ-09
        if sum(1 for r in mine if r.started > self.now() - 86400) >= self.runs_per_day:
            raise DailyLimit()
        run = Run(id=uuid.uuid4().hex[:12], owner=owner, question=question, started=self.now())
        self._runs[run.id] = run
        t = asyncio.create_task(self._go(run, token, dict(headers or {})))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return run

    async def _go(self, run: Run, token: str, headers: dict[str, str]) -> None:
        def on_task(task_id: str, ctx: str) -> None:
            run.task_id, run.context_id = task_id, ctx
            run.state = "working"
            run.started_evt.set()

        async def on_progress(text: str) -> None:
            run.progress = text[:300]

        u = user_hash(run.owner)
        try:
            res = await self.agent.run(run.question, token, None, on_progress, headers, on_task)
            run.state, run.text = res.state, res.text
            run.task_id, run.context_id = res.task_id or run.task_id, res.context_id or run.context_id
            log("run finished", run_id=run.id, user=u, state=res.state, task_id=run.task_id,
                seconds=round(self.now() - run.started, 1))
        except RunTimeout:
            run.state, run.error = "timeout", "The run passed 10 minutes and was cancelled."
            log("run timeout", run_id=run.id, user=u, task_id=run.task_id)
        except AgentAuthError:
            run.state = "error"
            run.error = "The hub refused your sign-in. Reconnect the Research connector and try again."
            log("run refused", run_id=run.id, user=u)
        except AgentError as e:
            ref = ref_id()
            run.state, run.error = "error", f"research-agent couldn't be reached (reference {ref})."
            log("run error", run_id=run.id, user=u, ref=ref, error=str(e))
        except Exception as e:                       # never let a run die silently
            ref = ref_id()
            run.state, run.error = "error", f"Internal error (reference {ref})."
            log("run error", run_id=run.id, user=u, ref=ref, error=type(e).__name__)
        finally:
            run.finished = self.now()
            run.started_evt.set()
            run.done.set()

    async def wait_started(self, run: Run, timeout: float) -> None:
        try:
            await asyncio.wait_for(run.started_evt.wait(), timeout)
        except TimeoutError:
            pass

    async def wait_done(self, run: Run, timeout: float) -> None:
        try:
            await asyncio.wait_for(run.done.wait(), timeout)
        except TimeoutError:
            pass

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
