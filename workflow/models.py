"""Building blocks: states, links, workflow definitions and running workflows."""

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


class State(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"  # An automatic task that ran out of attempts.


class Link(BaseModel):
    """When `source` moves to state `on`, move each target in `then` to its given state,
    but only if every component in `only_if` is in its given state."""

    model_config = ConfigDict(frozen=True)

    source: str
    on: State
    then: dict[str, State]
    only_if: dict[str, State] = Field(default_factory=dict)

    def conditions_met(self, state_of: Callable[[str], State]) -> bool:
        return all(state_of(key) == state for key, state in self.only_if.items())


def join(sources: list[str], on: State, then: dict[str, State]) -> list[Link]:
    """Links that fire once all `sources` are in state `on`, whichever gets there last."""
    return [
        Link(
            source=source,
            on=on,
            then=then,
            only_if={other: on for other in sources if other != source},
        )
        for source in sources
    ]


class TaskDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    name: str
    editors: frozenset[str]  # Roles allowed to change this task's state by hand.
    # An automatic task does its own work once it is in progress, then marks itself completed,
    # or failed if the work keeps failing or crashes.
    automatic: bool = False


class WorkflowDefinition(BaseModel):
    """The template of a process: its tasks and the links between them. Holds no state."""

    model_config = ConfigDict(frozen=True)

    key: str
    name: str
    editors: frozenset[str]  # Roles allowed to change the workflow's own state by hand.
    tasks: list[TaskDefinition]
    links: list[Link]

    @property
    def keys(self) -> list[str]:
        """The keys of the workflow itself and all its tasks."""
        return [self.key, *(task.key for task in self.tasks)]

    def editors_of(self, key: str) -> frozenset[str]:
        if key == self.key:
            return self.editors
        return next(task.editors for task in self.tasks if task.key == key)

    def is_automatic(self, key: str) -> bool:
        return any(task.automatic for task in self.tasks if task.key == key)

    @model_validator(mode="after")
    def _check_links(self) -> Self:
        known = set(self.keys)
        for link in self.links:
            unknown = {link.source, *link.then, *link.only_if} - known
            if unknown:
                raise ValueError(f"Link refers to unknown components: {sorted(unknown)}")
            # The workflow may only affect its tasks, never itself.
            if link.source == self.key and self.key in link.then:
                raise ValueError(f"Workflow {self.key!r} cannot link to itself")
        return self


class Actor(BaseModel):
    """Who is making a change."""

    model_config = ConfigDict(frozen=True)

    user: str
    role: str


# The actor for changes the app makes by itself, such as an automatic task completing.
SYSTEM = Actor(user="system", role="system")


class HistoryEntry(BaseModel):
    """One state change. `actor` is who started it, also for changes made by links."""

    model_config = ConfigDict(frozen=True)

    at: datetime
    actor: Actor
    component: str
    from_state: State
    to_state: State
    # "manual"; "automatic task finished", "automatic task failed" or "automatic task crashed";
    # or the link that made the change, e.g. "link: certificate → completed".
    reason: str


class Task(BaseModel):
    """One task of a running workflow. It owns its state."""

    key: str
    state: State = State.PENDING


class Workflow(BaseModel):
    """One running instance of a definition: its own state, its tasks, and the history of every
    change."""

    id: UUID = Field(default_factory=uuid4)
    definition: WorkflowDefinition = Field(exclude=True)
    state: State = State.PENDING
    tasks: list[Task]
    history: list[HistoryEntry] = Field(default_factory=list)

    @computed_field
    @property
    def definition_key(self) -> str:
        return self.definition.key

    def task(self, key: str) -> Task:
        return next(task for task in self.tasks if task.key == key)

    # Links point at the workflow or a task by key, so the engine reads and writes states
    # through these two methods, whichever component the key names.
    def state_of(self, key: str) -> State:
        return self.state if key == self.definition.key else self.task(key).state

    def set_state_of(self, key: str, state: State) -> None:
        if key == self.definition.key:
            self.state = state
        else:
            self.task(key).state = state
