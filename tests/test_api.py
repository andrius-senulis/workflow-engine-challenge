"""Integration tests: drive the supplier onboarding flow through the HTTP API."""

import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from workflow.app import create_app
from workflow.engine import WorkflowEngine
from workflow.settings import Settings

WORKFLOW = "supplier_onboarding"
SUPPLIER = {"X-User": "sam", "X-Role": "supplier"}
BUYER = {"X-User": "bea", "X-Role": "buyer"}


def make_client(settings: Settings | None = None) -> TestClient:
    # By default automatic tasks finish right away, so tests don't wait on the random sleep.
    return TestClient(create_app(settings or Settings(max_task_seconds=0)))


@pytest.fixture
def client() -> Iterator[TestClient]:
    with make_client() as client:
        yield client


def start(client: TestClient) -> str:
    response = client.post("/workflows", json={"definition": "supplier_onboarding"})
    assert response.status_code == 201
    return response.json()["id"]


def states_of(workflow: dict[str, Any]) -> dict[str, str]:
    """The workflow's own state and each task's, by key, for short assertions."""
    tasks = {task["key"]: task["state"] for task in workflow["tasks"]}
    return {WORKFLOW: workflow["state"], **tasks}


def get_states(client: TestClient, workflow_id: str) -> dict[str, str]:
    return states_of(client.get(f"/workflows/{workflow_id}").json())


def set_state(
    client: TestClient, workflow_id: str, key: str, state: str, actor: dict[str, str]
) -> dict[str, Any]:
    path = f"/workflows/{workflow_id}" + ("" if key == WORKFLOW else f"/tasks/{key}")
    response = client.patch(path, json={"state": state}, headers=actor)
    assert response.status_code == 200, response.text
    return states_of(response.json())


def wait_for(client: TestClient, workflow_id: str, key: str, state: str) -> None:
    """Wait for a background job (an automatic task) to move a component to a state."""
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if get_states(client, workflow_id)[key] == state:
            return
        time.sleep(0.01)
    pytest.fail(f"{key} did not reach {state}")


def test_workflow_crud(client: TestClient) -> None:
    workflow_id = start(client)

    assert [w["id"] for w in client.get("/workflows").json()] == [workflow_id]
    workflow = client.get(f"/workflows/{workflow_id}").json()
    assert workflow["definition_key"] == "supplier_onboarding"
    assert set(states_of(workflow).values()) == {"pending"}
    assert workflow["tasks"][0] == {"key": "company_info", "state": "pending"}
    assert workflow["history"] == []

    assert client.delete(f"/workflows/{workflow_id}").status_code == 204
    assert client.get(f"/workflows/{workflow_id}").status_code == 404


def test_completing_quality_review_completes_onboarding(client: TestClient) -> None:
    workflow_id = start(client)

    states = set_state(client, workflow_id, "quality_review", "completed", BUYER)

    assert states == {
        "supplier_onboarding": "completed",
        "company_info": "pending",
        "certificate": "pending",
        "risk_check": "pending",
        "quality_review": "completed",
    }


def test_supplier_starting_company_info_starts_onboarding_and_the_other_tasks(
    client: TestClient,
) -> None:
    workflow_id = start(client)

    states = set_state(client, workflow_id, "company_info", "in_progress", SUPPLIER)

    # The supplier may not change the onboarding by hand, but a link may change it for them.
    assert states == {
        "supplier_onboarding": "in_progress",
        "company_info": "in_progress",
        "certificate": "in_progress",
        "risk_check": "in_progress",
        "quality_review": "pending",
    }


def test_quality_review_starts_only_when_all_earlier_tasks_are_completed(
    client: TestClient,
) -> None:
    workflow_id = start(client)
    set_state(client, workflow_id, "supplier_onboarding", "in_progress", BUYER)
    wait_for(client, workflow_id, "risk_check", "completed")  # An automatic task.

    states = set_state(client, workflow_id, "company_info", "completed", SUPPLIER)
    assert states["quality_review"] == "pending"

    states = set_state(client, workflow_id, "certificate", "completed", SUPPLIER)
    assert states["quality_review"] == "in_progress"


def test_sending_certificate_back_resets_quality_review_until_it_is_completed_again(
    client: TestClient,
) -> None:
    workflow_id = start(client)
    set_state(client, workflow_id, "supplier_onboarding", "in_progress", BUYER)
    wait_for(client, workflow_id, "risk_check", "completed")
    set_state(client, workflow_id, "company_info", "completed", SUPPLIER)
    set_state(client, workflow_id, "certificate", "completed", SUPPLIER)

    states = set_state(client, workflow_id, "certificate", "in_progress", BUYER)
    assert states["quality_review"] == "pending"

    states = set_state(client, workflow_id, "certificate", "completed", SUPPLIER)
    assert states["quality_review"] == "in_progress"


def test_a_role_cannot_change_what_it_may_not_edit(client: TestClient) -> None:
    workflow_id = start(client)

    response = client.patch(
        f"/workflows/{workflow_id}/tasks/quality_review",
        json={"state": "completed"},
        headers=SUPPLIER,
    )

    assert response.status_code == 403
    workflow = client.get(f"/workflows/{workflow_id}").json()
    assert set(states_of(workflow).values()) == {"pending"}
    assert workflow["history"] == []


def test_history_records_who_changed_what_and_why_including_the_system(client: TestClient) -> None:
    workflow_id = start(client)

    set_state(client, workflow_id, "company_info", "in_progress", SUPPLIER)
    wait_for(client, workflow_id, "risk_check", "completed")

    history = client.get(f"/workflows/{workflow_id}").json()["history"]
    supplier = {"user": "sam", "role": "supplier"}
    assert [{k: v for k, v in entry.items() if k != "at"} for entry in history] == [
        {
            "actor": supplier,
            "component": "company_info",
            "from_state": "pending",
            "to_state": "in_progress",
            "reason": "manual",
        },
        {
            "actor": supplier,
            "component": "supplier_onboarding",
            "from_state": "pending",
            "to_state": "in_progress",
            "reason": "link: company_info → in_progress",
        },
        {
            "actor": supplier,
            "component": "certificate",
            "from_state": "pending",
            "to_state": "in_progress",
            "reason": "link: supplier_onboarding → in_progress",
        },
        {
            "actor": supplier,
            "component": "risk_check",
            "from_state": "pending",
            "to_state": "in_progress",
            "reason": "link: supplier_onboarding → in_progress",
        },
        {
            "actor": {"user": "system", "role": "system"},
            "component": "risk_check",
            "from_state": "in_progress",
            "to_state": "completed",
            "reason": "automatic task finished",
        },
    ]


def test_automatic_task_that_keeps_failing_is_marked_failed() -> None:
    settings = Settings(
        max_task_seconds=0, task_failure_rate=1, task_max_attempts=2, task_retry_seconds=0
    )
    with make_client(settings) as client:
        workflow_id = start(client)
        set_state(client, workflow_id, "supplier_onboarding", "in_progress", BUYER)

        wait_for(client, workflow_id, "risk_check", "failed")

        workflow = client.get(f"/workflows/{workflow_id}").json()
        assert states_of(workflow)["quality_review"] == "pending"
        last = workflow["history"][-1]
        assert (last["actor"]["user"], last["component"], last["to_state"], last["reason"]) == (
            "system",
            "risk_check",
            "failed",
            "automatic task failed",
        )


def test_automatic_tasks_beyond_the_worker_pool_wait_their_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("workflow.engine.random.uniform", lambda _low, _high: 0.3)
    with make_client(Settings(task_workers=1)) as client:
        first, second = start(client), start(client)
        set_state(client, first, "supplier_onboarding", "in_progress", BUYER)
        set_state(client, second, "supplier_onboarding", "in_progress", BUYER)

        wait_for(client, first, "risk_check", "completed")
        # The only worker just finished the first job; the second takes another 0.3 s.
        assert get_states(client, second)["risk_check"] == "in_progress"
        wait_for(client, second, "risk_check", "completed")


def test_automatic_task_that_crashes_is_marked_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def crash(*_: object) -> None:
        raise RuntimeError("a bug in the task's work")

    monkeypatch.setattr(WorkflowEngine, "_work", crash)
    with make_client() as client:
        workflow_id = start(client)
        set_state(client, workflow_id, "supplier_onboarding", "in_progress", BUYER)

        wait_for(client, workflow_id, "risk_check", "failed")
        last = client.get(f"/workflows/{workflow_id}").json()["history"][-1]
        assert last["reason"] == "automatic task crashed"


def test_deleting_a_workflow_stops_its_running_automatic_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The first workflow's risk check would take 5 s; the second one's is instant.
    durations = iter([5.0, 0.0])
    monkeypatch.setattr("workflow.engine.random.uniform", lambda *_: next(durations))
    with make_client(Settings(task_workers=1)) as client:
        deleted, waiting = start(client), start(client)
        set_state(client, deleted, "supplier_onboarding", "in_progress", BUYER)
        set_state(client, waiting, "supplier_onboarding", "in_progress", BUYER)
        time.sleep(0.05)  # Let the only worker start the first job.

        assert client.delete(f"/workflows/{deleted}").status_code == 204

        # The worker is free at once, so the second job finishes within wait_for's 2 s.
        wait_for(client, waiting, "risk_check", "completed")
