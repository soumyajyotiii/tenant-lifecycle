# Tenant Lifecycle

A single-process CLI that provisions tenant environments against a fault-injectable simulated
cloud, records every action in a durable registry and audit trail, and uses a policy-gated ops
agent to resolve failures. An independent checker verifies the results.

## Requirements

- Python 3 and its bundled `sqlite3`. No other dependencies.
- Tested only with Python 3.14.4 and SQLite 3.53.0 on macOS.
- Run every command from the repository root.

## Architecture

```
fixtures/gen_fixtures.py    seeded requests.jsonl and faults.jsonl
tenant_lifecycle/
  cli.py                    provision, agent, dump
  models.py                 steps, states, transitions, deterministic names
  writes.py                 the one transaction helper for both files; CRASH_AFTER counter
  registry.py               registry.db: environments, attempts, resources, decisions, audit
  cloud.py                  inventory.db: simulated cloud, fault plan, cloud log
  workflow.py               provisioning steps, agent action executors, compensation
  policy.py                 rules for agent actions; every decision stored before execution
  agent.py                  deterministic stub (not a model); never touches the cloud
  agent_rules.json          which actions the agent proposes for each state, in order
  dump.py                   read-only, sorted JSONL export of both files
checker/
  check.py                  independent checker; imports nothing from tenant_lifecycle
  run_scenarios.py          replay, crash and runtime runs through the real CLI
tests/                      unittest suite
```

```
provision -> workflow -> registry.db (intent, outcome, audit)
                     \-> cloud -> inventory.db (resources, cloud log)
agent -> policy (decision stored) -> workflow executors -> cloud
dump -> both files, read-only        checker -> dump + agent report
```

Steps run in order `namespace -> database -> secret -> dns -> app`. A basic tenant gets a
logical database on `shared-cluster-<region>`; an enterprise tenant gets a dedicated database.

## Commands

```bash
WORK=$(mktemp -d)

# 1. Generate fixtures (200 unique requests plus duplicate lines, and faults)
python3 -m fixtures.gen_fixtures --seed 7 --out "$WORK/fixtures"

# 2. Provision every request in file order
python3 -m tenant_lifecycle --state-dir "$WORK/state" provision \
    "$WORK/fixtures/requests.jsonl" "$WORK/fixtures/faults.jsonl"

# 3. Run the agent; it prints the JSONL report and also writes state/agent_report.jsonl
python3 -m tenant_lifecycle --state-dir "$WORK/state" agent > "$WORK/agent_stdout.jsonl"

# 4. Export registry, inventory and audit as sorted JSONL
python3 -m tenant_lifecycle --state-dir "$WORK/state" dump > "$WORK/dump.jsonl"

# 5. Check the results independently (only after agent exited 0)
python3 -m checker.check --dump "$WORK/dump.jsonl" \
    --report "$WORK/state/agent_report.jsonl" \
    --requests "$WORK/fixtures/requests.jsonl" --faults "$WORK/fixtures/faults.jsonl"
```

Crash and resume. The same command without `CRASH_AFTER` resumes:

```bash
CRASH_AFTER=500 python3 -m tenant_lifecycle --state-dir "$WORK/crash" provision \
    "$WORK/fixtures/requests.jsonl" "$WORK/fixtures/faults.jsonl" || echo "exit $?"
python3 -m tenant_lifecycle --state-dir "$WORK/crash" provision \
    "$WORK/fixtures/requests.jsonl" "$WORK/fixtures/faults.jsonl"
```

Replay, crash recovery and runtime through the real CLI, then the unit tests:

```bash
python3 -m checker.run_scenarios --seed 7
env -u CRASH_AFTER python3 -m unittest discover -s tests
```

The tests take about two minutes. Do not run them with `CRASH_AFTER` set: in-process tests would
exit at their first write.

## Behaviour

| Item | Meaning |
|---|---|
| State files | `registry.db`, `inventory.db` and `agent_report.jsonl` in `--state-dir` (default `.`) |
| A write | One committed transaction on either SQLite file. `CRASH_AFTER=n` exits right after the n-th. |
| `provision`, `agent`, `dump` exit codes | `0` done, `2` bad input or missing state, `3` crashed by `CRASH_AFTER` |
| `checker.check` exit codes | `0` no violations, `1` violations (one JSON line each), `2` unreadable input |
| `checker.run_scenarios` exit codes | `0` all checks hold, `1` a check failed |
| `agent` output | stdout is the report (decisions, evidence, outcomes); state counts go to stderr |
| Fixed per state directory | The faults file and `agent_rules.json`. Changing either after it was first used makes `provision` or `agent` exit 2. |
| Valid final states | `active` with five resources, or `escalated` with none owned. Anything else, such as `cleanup_failed`, is a violation (checker `F1`); see Known limitations in `DECISIONS.md`. |

See `DECISIONS.md` for the agent boundary, policy, compensation and recovery decisions.
