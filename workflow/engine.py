"""The engine: starts workflows and changes states, following links to apply side effects.

It also runs automatic tasks. When an automatic task moves to in progress, the engine puts a
job on a queue. A fixed pool of workers takes jobs off the queue, does the task's (pretend)
work with a timeout and retries, then marks the task completed, or failed after the last try
or a crash.
"""

import asyncio
import contextvars
import logging
import random
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from workflow.errors import (
    CascadeTooLongError,
    PermissionDeniedError,
    UnknownComponentError,
)
from workflow.models import (
    SYSTEM,
    Actor,
    HistoryEntry,
    State,
    Task,
    Workflow,
    WorkflowDefinition,
)
from workflow.settings import Settings

logger = logging.getLogger(__name__)


class TaskFailedError(Exception):
    """An automatic task's work failed. It may be retried."""


@dataclass(frozen=True)
class Job:
    """One run of an automatic task, waiting on the queue."""

    workflow: Workflow
    key: str
    run: int
    # The context the job was queued in, e.g. the request id that logs tag lines with.
    context: contextvars.Context


class WorkflowEngine:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        # The latest run of each automatic task, by (workflow id, task key). A job whose run is
        # no longer the latest is stale: the task was restarted, or the workflow deleted.
        self._runs: dict[tuple[UUID, str], int] = {}
        # Jobs a worker is running right now, so a stale one can be stopped at once instead of
        # holding its worker until it finishes.
        self._running: dict[tuple[UUID, str], asyncio.Task[None]] = {}

    def start_workers(self) -> None:
        self._workers = [
            asyncio.create_task(self._worker()) for _ in range(self._settings.task_workers)
        ]

    async def stop_workers(self) -> None:
        """Stop the workers. Jobs still queued or running are dropped (nothing is persisted)."""
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        if dropped := self._queue.qsize():
            logger.warning("%d queued automatic tasks dropped at shutdown", dropped)

    def forget(self, workflow: Workflow) -> None:
        """Stop the jobs of a deleted workflow: cancel running ones, make queued ones stale."""
        for job_key in [k for k in self._runs if k[0] == workflow.id]:
            del self._runs[job_key]
            self._cancel_running(job_key)

    def _cancel_running(self, job_key: tuple[UUID, str]) -> None:
        if job := self._running.get(job_key):
            # The job stops at its next await. The cascade has none, so it never stops midway.
            job.cancel()
            logger.info("workflow %s: automatic task %s cancelled", *job_key)

    def start(self, definition: WorkflowDefinition) -> Workflow:
        """Create a running workflow where the workflow and all its tasks are pending."""
        return Workflow(definition=definition, tasks=[Task(key=t.key) for t in definition.tasks])

    def set_state(self, workflow: Workflow, key: str, state: State, actor: Actor) -> None:
        """Move a component to a new state by hand, then apply every change its links trigger.

        Any state can move to any other state, including back to an earlier one.
        Only the actor's own change is checked against permissions. Changes made by links are
        not: whoever wrote the links already approved them.
        """
        if key not in workflow.definition.keys:
            raise UnknownComponentError(key)
        if actor.role not in workflow.definition.editors_of(key):
            raise PermissionDeniedError(f"Role {actor.role!r} cannot change {key!r}")
        self._apply(workflow, key, state, actor, reason="manual")

    def _apply(self, workflow: Workflow, key: str, state: State, actor: Actor, reason: str) -> None:
        """Apply a change and its cascade, then queue any automatic tasks it moved to in progress.

        Every change is recorded in the history. If the cascade is too long, nothing changes.
        """
        states_before = {k: workflow.state_of(k) for k in workflow.definition.keys}
        history_before = len(workflow.history)
        try:
            started = self._cascade(workflow, key, state, actor, reason)
        except CascadeTooLongError:
            for k, s in states_before.items():
                workflow.set_state_of(k, s)
            del workflow.history[history_before:]
            raise

        # Jobs are queued only now that the cascade has succeeded, so a rolled-back change has
        # no side effects. Queue each task once, and only if the cascade left it in progress.
        for task_key in dict.fromkeys(started):
            if workflow.state_of(task_key) == State.IN_PROGRESS:
                self._queue_job(workflow, task_key)

    def _cascade(
        self, workflow: Workflow, key: str, state: State, actor: Actor, reason: str
    ) -> list[str]:
        """Apply the changes breadth first, so the order is easy to follow.

        Returns the automatic tasks that moved to in progress.
        """
        queue = deque([(key, state, reason)])
        started: list[str] = []
        steps = 0
        while queue:
            key, state, reason = queue.popleft()
            from_state = workflow.state_of(key)
            if from_state == state:
                continue  # Nothing changes, so no links fire. This also stops two-way links.

            steps += 1
            if steps > self._settings.max_cascade_steps:
                raise CascadeTooLongError(f"More than {steps - 1} changes; links may loop")

            workflow.set_state_of(key, state)
            logger.info(
                "workflow %s: %s %s → %s by %s (%s), %s",
                workflow.id,
                key,
                from_state,
                state,
                actor.user,
                actor.role,
                reason,
            )
            workflow.history.append(
                HistoryEntry(
                    at=datetime.now(UTC),
                    actor=actor,
                    component=key,
                    from_state=from_state,
                    to_state=state,
                    reason=reason,
                )
            )
            if state == State.IN_PROGRESS and workflow.definition.is_automatic(key):
                started.append(key)
            for link in workflow.definition.links:
                triggered = link.source == key and link.on == state
                if triggered and link.conditions_met(workflow.state_of):
                    cause = f"link: {key} → {state}"
                    queue.extend((target, new, cause) for target, new in link.then.items())
        return started

    def _queue_job(self, workflow: Workflow, key: str) -> None:
        job_key = (workflow.id, key)
        if job_key in self._runs:
            # Moved out of in progress and back while a job was queued or running: that job
            # is now stale. A queued one will do nothing; a running one is stopped.
            logger.info("workflow %s: automatic task %s restarted", workflow.id, key)
            self._cancel_running(job_key)
        run = self._runs.get(job_key, 0) + 1
        self._runs[job_key] = run
        self._queue.put_nowait(Job(workflow, key, run, contextvars.copy_context()))
        logger.info(
            "workflow %s: automatic task %s queued (%d waiting)",
            workflow.id,
            key,
            self._queue.qsize(),
        )

    def _is_current(self, job: Job) -> bool:
        return (
            self._runs.get((job.workflow.id, job.key)) == job.run
            and job.workflow.state_of(job.key) == State.IN_PROGRESS
        )

    async def _worker(self) -> None:
        while True:
            job = await self._queue.get()
            job_key = (job.workflow.id, job.key)
            if self._runs.get(job_key) != job.run:
                self._queue.task_done()  # Stale before it started: nothing to do.
                continue

            # Run in the context the job was queued in, so its logs keep the request id.
            task = asyncio.create_task(self._run_job(job), context=job.context)
            self._running[job_key] = task
            try:
                # Unlike `await task`, this doesn't raise when the job is cancelled for being
                # stale, so the worker carries on with the next job.
                await asyncio.wait([task])
            except asyncio.CancelledError:
                task.cancel()  # The worker itself is stopping (shutdown): stop its job too.
                raise
            finally:
                if self._running.get(job_key) is task:
                    del self._running[job_key]
                if self._runs.get(job_key) == job.run:
                    del self._runs[job_key]
                self._queue.task_done()

    async def _run_job(self, job: Job) -> None:
        """Run a job. Whatever goes wrong, the task ends up completed or failed, never stuck."""
        try:
            await self._attempt_job(job)
        except Exception:
            # A bug in the work, or a cascade too long when completing the task. Not retried:
            # trying the same thing again would most likely fail the same way.
            logger.exception("workflow %s: automatic task %s crashed", job.workflow.id, job.key)
            self._mark_failed(job, reason="automatic task crashed")

    async def _attempt_job(self, job: Job) -> None:
        workflow, key = job.workflow, job.key
        attempts = self._settings.task_max_attempts
        for attempt in range(1, attempts + 1):
            if not self._is_current(job):
                logger.info("workflow %s: automatic task %s skipped, it moved", workflow.id, key)
                return
            try:
                async with asyncio.timeout(self._settings.task_timeout_seconds):
                    await self._work(workflow, key)
                break
            except (TaskFailedError, TimeoutError) as error:
                reason = str(error) or "timed out"
                logger.warning(
                    "workflow %s: automatic task %s attempt %d/%d failed: %s",
                    workflow.id,
                    key,
                    attempt,
                    attempts,
                    reason,
                )
                if attempt < attempts:
                    await asyncio.sleep(self._settings.task_retry_seconds * 2 ** (attempt - 1))
        else:
            if self._is_current(job):  # A stale job's failure is nobody's concern.
                logger.error(
                    "workflow %s: automatic task %s failed after %d attempts",
                    workflow.id,
                    key,
                    attempts,
                )
                self._mark_failed(job, reason="automatic task failed")
            return

        if self._is_current(job):
            self._apply(workflow, key, State.COMPLETED, SYSTEM, reason="automatic task finished")

    def _mark_failed(self, job: Job, reason: str) -> None:
        if not self._is_current(job):
            return
        try:
            self._apply(job.workflow, job.key, State.FAILED, SYSTEM, reason=reason)
        except Exception:
            # E.g. the failure's own cascade is too long. It is rolled back; leave it logged.
            logger.exception(
                "workflow %s: could not mark automatic task %s failed", job.workflow.id, job.key
            )

    async def _work(self, workflow: Workflow, key: str) -> None:
        """Pretend work. A real task would call a service, e.g. a credit or sanctions check."""
        seconds = random.uniform(0, self._settings.max_task_seconds)
        logger.info("workflow %s: automatic task %s working (%.1f s)", workflow.id, key, seconds)
        await asyncio.sleep(seconds)
        if random.random() < self._settings.task_failure_rate:
            raise TaskFailedError("pretend failure")
