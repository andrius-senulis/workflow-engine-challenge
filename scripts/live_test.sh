#!/usr/bin/env bash
# Live test: drive a running API through several supplier onboarding scenarios.
# Start the API first with `make run`, then run `make live-test`.
# Set API to test another address (default: http://localhost:8000).
# The automatic risk check takes up to WORKFLOW_MAX_TASK_SECONDS (default 10) per scenario.
set -u
API=${API:-http://localhost:8000}
BODY=$(mktemp)
trap 'rm -f "$BODY"' EXIT

show() {  # print the workflow's state and its tasks' states on one line
  jq -r '"supplier_onboarding=\(.state)  " + (.tasks | map("\(.key)=\(.state)") | join("  "))'
}
path_of() {  # path_of <id> <component>: the workflow itself, or one of its tasks
  if [ "$2" = supplier_onboarding ]; then echo "/workflows/$1"; else echo "/workflows/$1/tasks/$2"; fi
}
start() {
  curl -s -X POST "$API/workflows" -H 'Content-Type: application/json' \
    -d '{"definition": "supplier_onboarding"}' | jq -r .id
}
patch() {  # patch <id> <component> <state> <role: supplier|buyer>
  local user=bea; [ "$4" = supplier ] && user=sam
  printf '  %-8s %-19s → %-12s | ' "$4" "$2" "$3"
  curl -s -X PATCH "$API$(path_of "$1" "$2")" -H 'Content-Type: application/json' \
    -H "X-User: $user" -H "X-Role: $4" -d "{\"state\": \"$3\"}" | show
}
wait_for() {  # wait_for <id> <component> <state>: wait for an automatic task (the system)
  printf '  %-8s %-19s → %-12s | ' system "$2" "$3"
  for _ in $(seq 1 15); do  # check once a second, for up to 15 seconds
    if [ "$(curl -s "$API/workflows/$1" | jq -r --arg k "$2" '.tasks[] | select(.key == $k) | .state')" = "$3" ]; then
      curl -s "$API/workflows/$1" | show; return
    fi
    sleep 1
  done
  echo "timed out"
}
status_of() {  # status_of <method> <path> [body], sent as a buyer
  curl -s -o "$BODY" -w '%{http_code}' -X "$1" "$API$2" -H 'Content-Type: application/json' \
    -H 'X-User: bea' -H 'X-Role: buyer' ${3:+-d "$3"}
  printf '  %s\n' "$(cat "$BODY")"
}

echo "== Definitions =="
curl -s "$API/definitions" | jq -r '.[] | "\(.key): \(.tasks | map(.key) | join(", ")) (\(.links | length) links)"'

echo; echo "== A: happy path (supplier starts, everything completes) =="
A=$(start); echo "  id $A"
patch "$A" company_info in_progress supplier
patch "$A" company_info completed supplier
patch "$A" certificate completed supplier
wait_for "$A" risk_check completed
patch "$A" quality_review completed buyer

echo; echo "== B: buyer sends the certificate back during review =="
B=$(start); echo "  id $B"
patch "$B" supplier_onboarding in_progress buyer
patch "$B" company_info completed supplier
patch "$B" certificate completed supplier
wait_for "$B" risk_check completed
patch "$B" certificate in_progress buyer
patch "$B" certificate completed supplier

echo; echo "== C: any → any, including skipping ahead and going back =="
C=$(start); echo "  id $C"
patch "$C" quality_review completed buyer
patch "$C" supplier_onboarding pending buyer
patch "$C" quality_review pending buyer

echo; echo "== D: created, then deleted =="
D=$(start); echo "  id $D"
printf '  DELETE              → '; status_of DELETE "/workflows/$D"
printf '  GET after delete    → '; status_of GET "/workflows/$D"

echo; echo "== Errors =="
printf '  unknown definition  → '; status_of POST /workflows '{"definition": "nope"}'
printf '  unknown workflow    → '; status_of GET /workflows/00000000-0000-0000-0000-000000000000
printf '  unknown task        → '; status_of PATCH "/workflows/$A/tasks/nope" '{"state": "completed"}'
printf '  invalid state       → '; status_of PATCH "/workflows/$A/tasks/certificate" '{"state": "done"}' | cut -c1-110
printf '  not an editor       → '; curl -s -w '%{http_code}  ' -o "$BODY" -X PATCH "$API/workflows/$A/tasks/quality_review" \
  -H 'Content-Type: application/json' -H 'X-User: sam' -H 'X-Role: supplier' -d '{"state": "pending"}'; cat "$BODY"; echo
printf '  missing identity    → '; curl -s -w '%{http_code}  ' -o "$BODY" -X PATCH "$API/workflows/$A/tasks/certificate" \
  -H 'Content-Type: application/json' -d '{"state": "completed"}'; cut -c1-90 "$BODY"
printf '  invalid id          → '; status_of GET /workflows/not-a-uuid | cut -c1-110

echo; echo "== History of B (who changed what, and why) =="
curl -s "$API/workflows/$B" | jq -r '.history[] | "  \(.actor.user) (\(.actor.role))  \(.component): \(.from_state) → \(.to_state)  [\(.reason)]"'

echo; echo "== All workflows =="
curl -s "$API/workflows" | jq -r '.[] | "  \(.id)  onboarding=\(.state)"'
