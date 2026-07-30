#!/usr/bin/env bash
# composition-test.sh — proves REAL miniwdl drives the spawn backend end-to-end,
# against the Substrate AWS emulator (no real AWS, no workload execution, no cost).
#
# Exercises the honest seam:
#   real `miniwdl run` → SpawnContainer backend → real `spawn task run`
#   (real RunInstances + real `aws s3` staging, all vs. Substrate) → poll the
#   completion record → return exit code → miniwdl marks the task succeeded/failed.
#
# Substrate (v0.75.0+) serves the completion record as a seedable, clock-aware
# outcome (spore-host/substrate#360): unseeded ⇒ nominal exit 0/completed (happy
# path needs NO seed); POST /v1/spawn/task-completion with a nonzero exit_code
# drives the failure path. Substrate does NOT execute the task, so this uses a
# NO-OUTPUT workflow (noout.wdl) — output collection needs real execution and is
# covered by the real-AWS run, not here.
#
# Requires on PATH / in env (the CI job provides): `miniwdl`, `spawn`, `aws`;
# AWS_ENDPOINT_URL → the Substrate server.
set -euo pipefail

: "${AWS_ENDPOINT_URL:?set to the Substrate server, e.g. http://localhost:4566}"
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="$AWS_REGION"
export SPAWN_REGION="$AWS_REGION"
export SPAWN_TTL="20m"
# Select the spawn backend + blank the named-profile defaults so the SDK uses the
# static test creds + AWS_ENDPOINT_URL (→ Substrate), not a real profile.
export MINIWDL__SCHEDULER__CONTAINER_BACKEND=spawn
export SPAWN_INFRA_PROFILE=""
export SPAWN_COMPUTE_PROFILE=""

WORKDIR_BUCKET="miniwdl-spawn-ci"
export SPAWN_WORKDIR_S3="s3://${WORKDIR_BUCKET}/runs"

HERE="$(cd "$(dirname "$0")" && pwd)"
WDL="$HERE/noout.wdl"
fail() { echo "COMPOSITION TEST FAILED: $*" >&2; exit 1; }

echo "spawn=$(command -v spawn)  miniwdl=$(command -v miniwdl)  endpoint=$AWS_ENDPOINT_URL"

# The backend stages uploads under SPAWN_WORKDIR_S3 before `spawn task run` runs
# (spawn creates the results bucket itself, but the workdir base must exist).
aws --endpoint-url "$AWS_ENDPOINT_URL" s3 mb "s3://${WORKDIR_BUCKET}" 2>/dev/null || true

# noout.wdl is a single task named `step`; the backend's task_id for a single-task
# run is deterministic: wdl-call-step-1 (wdl-<call>-<try>). That lets the failure
# test SEED BEFORE RUNNING — no log scraping, no race.
TASK_ID="wdl-call-step-1"

echo "=== TEST 1: happy path — unseeded completion ⇒ nominal success ==="
D1="$(mktemp -d)"
if miniwdl run "$WDL" --dir "$D1" --verbose > "$D1/log" 2>&1; then
    grep -qE "miniwdl-spawn: dispatching ${TASK_ID} via" "$D1/log" \
        || fail "TEST 1: backend never dispatched via spawn task run:\n$(tail -20 "$D1/log")"
    echo "TEST 1 PASS: miniwdl run succeeded; backend dispatched ${TASK_ID}, completion resolved to exit 0"
else
    tail -30 "$D1/log"; fail "TEST 1: miniwdl run failed on the happy path"
fi

echo "=== TEST 2: failure path — seed ${TASK_ID} to exit 7, expect miniwdl to fail ==="
curl -fsS -X POST "$AWS_ENDPOINT_URL/v1/spawn/task-completion" \
    -H 'content-type: application/json' \
    -d "{\"task_id\":\"${TASK_ID}\",\"exit_code\":7,\"state\":\"failed\",\"started_at\":\"2026-01-01T00:00:00Z\",\"ended_at\":\"2026-01-01T00:00:01Z\"}" \
    >/dev/null || fail "TEST 2: could not seed the failure completion"
D2="$(mktemp -d)"
if miniwdl run "$WDL" --dir "$D2" --verbose > "$D2/log" 2>&1; then
    tail -30 "$D2/log"; fail "TEST 2: miniwdl run SUCCEEDED despite a seeded FAILED completion"
fi
grep -qE "exit_status: 7|exit status 7|CommandFailed" "$D2/log" \
    || fail "TEST 2: miniwdl failed but not with the seeded exit 7:\n$(tail -20 "$D2/log")"
echo "TEST 2 PASS: seeded exit 7 → miniwdl reports CommandFailed exit_status 7 (backend surfaced the code)"

echo "=== COMPOSITION VERIFIED: real miniwdl ↔ spawn backend ↔ Substrate (dispatch + completion + exit-code) ==="
