# Tenant Lifecycle — Repository Instructions for Claude Code

## Project mission

**Tenant Lifecycle** is a single-process platform for provisioning and recovering tenant environments against a fault-injectable simulated cloud. It maintains a durable environment registry and audit trail, exposes a CLI for provisioning and recovery, and uses a policy-gated operations agent to resolve failures safely.

Use the project requirements and explicitly approved engineering decisions as the implementation contract. Where behavior is underspecified, identify the ambiguity, present a minimal defensible choice, and ask the human engineer to decide.

Optimize for a small, finished, tested system with provable semantics—not feature breadth. The human owns architecture, safety decisions, acceptance criteria and final verification. You are the implementation and review partner.

## Collaboration protocol

- **Do not start writing implementation code until the human explicitly approves the architecture.** In design discussions, analyze and propose; do not silently create files or refactor.
- Work in small, reviewable milestones. Stop at the milestone boundary and report what changed, tests actually run, results, open questions, and risks.
- For consequential choices (persistence semantics, retry policy, compensation, agent authority, audit ordering, checker strategy), present options and trade-offs rather than deciding silently.
- Distinguish **requirement**, **assumption**, **design choice**, and **verified fact**. Do not claim a command was run or a test passed unless it actually was.
- Prefer minimal patches and focused regression tests. Never weaken tests to make failures disappear. Show a reproducible counterexample before fixing a suspected semantic bug.
- Do not invent human verification steps, AI mistakes, rejected proposals, test results, or evidence for `DECISIONS.md`.
- Preserve complete, unedited AI development sessions as project records. Do not summarize or curate them as substitutes for originals. Remind the human to export sessions and note if a tool cannot export.
- Do not introduce external services, credentials, network calls, or unrequested dependencies. Ask before destructive operations or large-scale rewrites.

## Project scope and contracts

- Use **Python 3** and **SQLite** unless the human explicitly changes this choice. Prefer the standard library (`sqlite3`, `argparse`, `json`, `unittest`/`pytest` only if approved).
- Implement a **single-process** CLI with `provision requests.jsonl faults.jsonl`, `agent`, and `dump`; no HTTP server, UI, auth, queue, ORM, or distributed workers.
- Registry and audit live in **one SQLite file**. The simulated-cloud module alone writes a **separate SQLite inventory file**.
- Provision in order: `namespace → database → secret → dns → app`. Basic uses a logical database on a shared cluster; enterprise uses a dedicated database.
- Every action has an actor kind: `workflow`, `agent`, or `human`, and must be auditable.
- The ops agent is limited to `retry_step`, `resume`, `cleanup_orphan`, and `escalate`. Every agent action passes a **recorded policy approval before execution**. The agent never directly calls the cloud.
- A deterministic decision stub is acceptable; if used, state that explicitly in `DECISIONS.md`. Do not build an LLM integration just for appearance.
- Generate deterministic seeded `requests.jsonl` (at least 200 requests across both tiers, including at least 10 duplicate `request_id`s) and `faults.jsonl` (at least 30 requests failed partway through provisioning, including create-success-then-timeout; at least five quota errors requiring escalation; and at least three compensation failures).
- The checker must be **independent** of the workflow and agent implementation: separate entrypoint, no imports of their domain logic, and tests against the exported dump and agent report.
- Out of scope: decommissioning, fleet rollouts, natural-language intake, real cloud accounts. Do not implement extensions unless the core requirements are demonstrably complete and the human explicitly approves.

## Required semantics — never compromise these

1. After `provision` and `agent`, each environment is fully **active**, fully **cleaned up**, or **escalated with zero cloud resources remaining**.
2. No cloud resource may exist without a corresponding account of it in the registry.
3. Replaying identical requests/faults changes nothing observable; duplicate `request_id`s never create a second environment. Include audit output in replay comparisons.
4. Agent actions stay within the allowlist, and each executed agent action has a **prior passing policy check** in durable evidence.
5. Every action by every actor has an audit record.
6. With `CRASH_AFTER=<n>`, exit after the specified number of writes; resuming with the same command without that variable produces the **same final dump** as uninterrupted execution. Specify precisely what is counted as a write and test boundaries across both databases.
7. The full scenario finishes in less than **60 seconds on a laptop**.

## Recovery and correctness discipline

- Treat cloud timeouts as **unknown outcomes**, not guaranteed failures. A resource may already exist.
- Record durable intent before uncertain side effects; reconcile actual inventory, deterministic identity, and ownership before retries or cleanup.
- Do not assume atomic commits across the registry and simulated-cloud SQLite files. Explicitly design and test the crash window between them.
- Make operation retries and compensation safe under repeated execution. A duplicate cloud response is evidence to investigate, not automatically success or a reason to create another resource.
- Never delete a resource without verifying that it is owned by the intended environment. Consider cleanup failures and quota escalation without leaving resources behind.
- `dump` must be deterministic, consistently sorted JSONL; avoid volatile fields that change on replay unless the specification requires them.
- Keep the independent checker adversarial: test it with deliberately corrupted evidence and use a separate runner for multi-run replay/crash comparisons.

## Working style and reporting

Before each implementation milestone, restate its acceptance tests. After each milestone, report:

1. Files modified and why.
2. Commands actually executed and their pass/fail results.
3. The relevant invariants covered.
4. Known gaps, risks, and any human decision needed.

Do not silently proceed to the next milestone. The human will bring responses and repository changes to an independent review before sending the next instruction.

## Project documentation and maintainability

Prepare a README with exact commands for fixture generation, provisioning, agent run, dump, and independent checking. `DECISIONS.md` must be one page and accurately cover agent permission boundary, policy enforcement, compensation decisions, real model proposals rejected (if any) and reasons, and what the human truly verified. Preserve unedited AI development sessions. Keep the design and code easy to explain, test, and modify during a technical walkthrough.
