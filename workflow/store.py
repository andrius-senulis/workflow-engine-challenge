"""Keeps running workflows in memory. A real app would use a database."""

from uuid import UUID

from workflow.models import Workflow


class WorkflowStore:
    def __init__(self) -> None:
        self._workflows: dict[UUID, Workflow] = {}

    def add(self, workflow: Workflow) -> None:
        self._workflows[workflow.id] = workflow

    def get(self, workflow_id: UUID) -> Workflow | None:
        return self._workflows.get(workflow_id)

    def all(self) -> list[Workflow]:
        return list(self._workflows.values())

    def delete(self, workflow_id: UUID) -> bool:
        return self._workflows.pop(workflow_id, None) is not None
