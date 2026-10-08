"""The workflow definitions this app knows about.

Supplier onboarding: a buyer onboards a new supplier. The supplier fills in company info
and uploads a certificate while the system runs an automatic risk check. Then the buyer
does a quality review. When the review is done, onboarding is complete.

Roles: the buyer runs the onboarding and the review, and can send the certificate back.
The supplier fills in company info and the certificate.
"""

from workflow.models import Link, State, TaskDefinition, WorkflowDefinition, join

SUPPLIER_ONBOARDING = WorkflowDefinition(
    key="supplier_onboarding",
    name="Supplier onboarding",
    editors=frozenset({"buyer"}),
    tasks=[
        TaskDefinition(key="company_info", name="Company info", editors=frozenset({"supplier"})),
        TaskDefinition(
            key="certificate", name="Certificate", editors=frozenset({"supplier", "buyer"})
        ),
        TaskDefinition(
            key="risk_check", name="Risk check", editors=frozenset({"buyer"}), automatic=True
        ),
        TaskDefinition(key="quality_review", name="Quality review", editors=frozenset({"buyer"})),
    ],
    links=[
        # Fan-out: starting onboarding opens all tasks that can run in parallel.
        Link(
            source="supplier_onboarding",
            on=State.IN_PROGRESS,
            then={
                "company_info": State.IN_PROGRESS,
                "certificate": State.IN_PROGRESS,
                "risk_check": State.IN_PROGRESS,
            },
        ),
        # Two-way: a supplier who starts on company info also starts onboarding.
        Link(
            source="company_info",
            on=State.IN_PROGRESS,
            then={"supplier_onboarding": State.IN_PROGRESS},
        ),
        # Merge: the review starts once the supplier's tasks and the risk check are all done.
        *join(
            ["company_info", "certificate", "risk_check"],
            State.COMPLETED,
            then={"quality_review": State.IN_PROGRESS},
        ),
        # Going back: if the buyer sends the certificate back, the review waits for it again.
        Link(
            source="certificate",
            on=State.IN_PROGRESS,
            then={"quality_review": State.PENDING},
        ),
        # Linear: a finished review completes onboarding.
        Link(
            source="quality_review",
            on=State.COMPLETED,
            then={"supplier_onboarding": State.COMPLETED},
        ),
    ],
)

DEFINITIONS = {definition.key: definition for definition in [SUPPLIER_ONBOARDING]}
