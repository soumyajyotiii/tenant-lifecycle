# Decisions

## Agent boundary and policy
- The agent is a deterministic stub, not an LLM. `agent_rules.json` sets which actions it proposes per state;
  code derives exact targets. It reads only the registry and never has access to the cloud.
- Allowed actions: `retry_step`, `resume`, `cleanup_orphan`, `escalate`. Every proposal is judged from registry
  facts and stored, immutably, before it runs. The executor refuses missing or denied decisions.
- Retries: 2 after the first attempt, only for `timeout` or a false `already_exists`; never for quota, a
  foreign owner or a mismatch. Deletes: up to 3 attempts per resource.

## Compensation
- `escalate` deletes in reverse step order and stops at the first delete that fails; `cleanup_orphan` retries it.
- A delete runs only on an exact inventory match (owner, step, kind, parent). Foreign and mismatched resources
  are never deleted. `escalated` is recorded only when the environment owns nothing in the cloud.

## Recovery
- Intent is stored before each cloud call. The registry then records what the inventory shows; the cloud's
  error is kept only when the call changed nothing. Every action is audited once, failures included.
- Faults are keyed by request, step, op and attempt; resume resends the same attempt, so the same fault fires.
- The fault plan and the agent rules digest are fixed per state directory; a change is rejected.
- A write is one committed transaction on either file; `CRASH_AFTER=n` exits with code 3 after the n-th.
- The agent may close a create attempt a crashed `provision` left open, only inside an approved `resume`.
  Once the agent acts on an environment, `provision` leaves it alone.

## Known limitations
- A mismatched resource, or a delete still failing after 3 attempts, leaves the environment `cleanup_failed`.
  That is **not a valid final state**: it violates semantic 1 and the checker reports `F1_not_final`.
- `cleaned_up` is never reached. The checker restates the rules, so rule changes must be made in both places.

## Suggestions rejected during review
| Suggestion | Decision and reason |
|---|---|
| Audit only state changes | Failures must be audited too; records keyed by attempt keep replay clean |
| `observe()` checks only the owner | Also check kind, step, parent, so recovery never accepts a wrong resource |
| Agent resumes only its own environments | A crash left one `provisioning`; the agent must recover it |
| One fixed shared-cluster name | Region specific, still not an inventory resource |
| `agent` prints only state counts | Print the report with actions, evidence and outcomes |
| Agent choices hardcoded in code | Proposals come from `agent_rules.json`; policy and targets stay in code |
| No region; request fields unchecked | Add region; `request_id`, `tenant`, `tier`, `region` must match the payload |

## Verification
Verified personally by me from a fresh clone:
- 200 environments: 187 active, 13 escalated.
- The independent checker reported 0 violations.
- Replay produced identical dumps and agent reports.
- Crash recovery after 500 writes produced identical results.
- The scenario runner passed all 25 checks; runtime was 1.78 s.
