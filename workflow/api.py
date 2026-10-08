"""HTTP routes: CRUD for running workflows, plus a read-only list of definitions."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from workflow.definitions import DEFINITIONS
from workflow.engine import WorkflowEngine
from workflow.errors import CascadeTooLongError, PermissionDeniedError, UnknownComponentError
from workflow.models import Actor, State, Workflow, WorkflowDefinition
from workflow.store import WorkflowStore

router = APIRouter()
logger = logging.getLogger(__name__)


class CreateWorkflow(BaseModel):
    definition: str


class ChangeState(BaseModel):
    state: State


# Dependencies are `async` on purpose: FastAPI runs sync ones in a threadpool, and that
# thread hop lets another request (say, a delete) run between looking up a workflow and
# changing it. Async dependencies keep the whole request on the event loop.
async def get_engine(request: Request) -> WorkflowEngine:
    return request.app.state.engine


async def get_store(request: Request) -> WorkflowStore:
    return request.app.state.store


type Engine = Annotated[WorkflowEngine, Depends(get_engine)]
type Store = Annotated[WorkflowStore, Depends(get_store)]


async def find_workflow(workflow_id: UUID, store: Store) -> Workflow:
    workflow = store.get(workflow_id)
    if workflow is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found")
    return workflow


type FoundWorkflow = Annotated[Workflow, Depends(find_workflow)]


async def get_actor(x_user: Annotated[str, Header()], x_role: Annotated[str, Header()]) -> Actor:
    """Who is calling. Stands in for real authentication (a session or a JWT)."""
    return Actor(user=x_user, role=x_role)


type CurrentActor = Annotated[Actor, Depends(get_actor)]


@router.get("/definitions")
async def list_definitions() -> list[WorkflowDefinition]:
    return list(DEFINITIONS.values())


@router.post("/workflows", status_code=status.HTTP_201_CREATED)
async def create_workflow(body: CreateWorkflow, engine: Engine, store: Store) -> Workflow:
    definition = DEFINITIONS.get(body.definition)
    if definition is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Definition not found")
    workflow = engine.start(definition)
    store.add(workflow)
    logger.info("workflow %s: started from %s", workflow.id, definition.key)
    return workflow


@router.get("/workflows")
async def list_workflows(store: Store) -> list[Workflow]:
    return store.all()


@router.get("/workflows/{workflow_id}")
async def get_workflow(workflow: FoundWorkflow) -> Workflow:
    return workflow


@router.patch("/workflows/{workflow_id}")
async def change_workflow_state(
    workflow: FoundWorkflow, body: ChangeState, actor: CurrentActor, engine: Engine
) -> Workflow:
    return change_state(engine, workflow, workflow.definition.key, body.state, actor)


@router.patch("/workflows/{workflow_id}/tasks/{key}")
async def change_task_state(
    workflow: FoundWorkflow, key: str, body: ChangeState, actor: CurrentActor, engine: Engine
) -> Workflow:
    if key not in {task.key for task in workflow.tasks}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown task: {key}")
    return change_state(engine, workflow, key, body.state, actor)


def change_state(
    engine: WorkflowEngine, workflow: Workflow, key: str, state: State, actor: Actor
) -> Workflow:
    """Change the workflow's or a task's state, turning engine errors into HTTP errors."""
    try:
        engine.set_state(workflow, key, state, actor)
    except UnknownComponentError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown component: {error}") from None
    except PermissionDeniedError as error:
        logger.warning("workflow %s: change denied: %s", workflow.id, error)
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(error)) from None
    except CascadeTooLongError as error:
        logger.warning("workflow %s: change rolled back: %s", workflow.id, error)
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
    return workflow


@router.delete("/workflows/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow(workflow: FoundWorkflow, store: Store, engine: Engine) -> None:
    store.delete(workflow.id)
    engine.forget(workflow)
    logger.info("workflow %s: deleted", workflow.id)
