# Conversation Understanding Internal Preview Checklist

## Scope

This checklist is for the first internal-preview rollout of the conversation-understanding + revision workflow.

Included in this preview:

- conversation context loading
- understanding engine responses
- task revisions and lazy backfill
- response modes in `/api/v1/messages`
- task detail revision history
- read-only revision / blocked-state UI in `apps/web`

Not included in this preview:

- inline form editing for revisions
- shadow mode / canary rollout
- production dashboards and alerting

## Required Flags

The internal-preview baseline expects these settings to be enabled:

- `GIS_AGENT_CONVERSATION_CONTEXT_ENABLED=true`
- `GIS_AGENT_UNDERSTANDING_ENGINE_ENABLED=true`
- `GIS_AGENT_TASK_REVISIONS_ENABLED=true`
- `GIS_AGENT_RESPONSE_MODE_ENABLED=true`
- `GIS_AGENT_MESSAGE_UNDERSTANDING_PAYLOAD_ENABLED=true`
- `GIS_AGENT_REVISION_BACKFILL_LAZY_ENABLED=true`

The current defaults in `packages/domain/config.py` already match this baseline.

## Preflight

Before starting the manual walkthrough:

1. Start the API and worker stack for the feature branch.
2. Open `apps/web`.
3. Confirm the workbench loads and a new session is created.
4. Confirm uploads still work from the left panel.

## Manual Walkthrough

### 1. New Task -> Confirm Understanding -> Approval

1. Send a high-confidence task request with AOI, time range, and analysis type.
2. Confirm the UI notice reflects either:
   - `confirm_understanding`, or
   - direct plan creation when the request is high-confidence and confirmation is disabled.
3. Reply with `继续` if the system is waiting for confirmation.
4. Confirm:
   - a task is created
   - `TaskDetail.interaction_state` is visible
   - `TaskDetail.active_revision` is visible
   - the plan reaches `awaiting_approval`

### 2. Blocked AOI -> Natural-Language Correction -> Recovery

1. Use a request that leads to `execution_blocked` for AOI normalization.
2. Confirm the UI shows:
   - blocked reason
   - active revision summary
   - revision history
3. Send a natural-language correction, for example changing the AOI source.
4. Confirm:
   - a new active revision is created
   - the blocked reason disappears
   - `interaction_state` leaves `execution_blocked`
   - the task becomes reviewable again

### 3. Follow-up / Correction Revision

1. After a task exists, send a correction such as changing the time range or AOI.
2. Confirm:
   - no legacy confirmation prompt is shown
   - the response uses `show_revision` or `ask_missing_fields`
   - the right panel revision history updates
   - the newest revision is highlighted as the active one

### 4. Approval Flow Still Works

1. Approve the draft plan from the right panel.
2. Confirm the task transitions into execution as before.
3. Confirm no `execution_blocked` task can bypass the gate.

### 5. Legacy Task Detail Backfill

1. Open an older task that predates revisions.
2. Confirm `active_revision` and `revisions` are still visible through lazy backfill.

## Expected UX Signals

Internal-preview is considered healthy if the team can understand these states without reading backend code:

- what the assistant currently thinks the task is
- whether execution is blocked or simply awaiting approval
- what changed between revisions
- what fields still need correction

## Minimum Verification Commands

```bash
uv run pytest -q \
  tests/test_message_schema.py \
  tests/test_orchestrator.py \
  tests/test_plan_approval_flow.py \
  tests/test_task_samples.py \
  tests/test_message_intent_routing.py \
  tests/test_understanding.py \
  tests/test_conversation_context.py \
  tests/test_response_policy.py \
  tests/test_aoi.py

uv run ruff check \
  packages/domain/services/orchestrator.py \
  packages/domain/services/task_revisions.py \
  apps/web/src \
  tests/test_message_intent_routing.py \
  tests/test_orchestrator.py \
  tests/test_plan_approval_flow.py \
  tests/test_message_schema.py

npm run build:web
```

## Exit Criteria

The internal preview is ready when:

1. The verification commands pass.
2. The manual walkthrough completes end to end.
3. Team members can identify `confirm_understanding`, `ask_missing_fields`, `show_revision`, `awaiting_approval`, and `execution_blocked` from the UI alone.
