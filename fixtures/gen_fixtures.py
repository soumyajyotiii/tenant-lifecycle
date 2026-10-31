"""Generate deterministic requests.jsonl and faults.jsonl from a seed.

    python3 -m fixtures.gen_fixtures --seed 7 --out DIR

Every fault targets an attempt the system is certain to make, so each one is
exercised. Every create failure clears within the retry budget or ends in an
escalation, and every delete failure clears within the cleanup budget, so
every environment can reach active or escalated with nothing left behind.
"""

import argparse
import json
import os
import random

STEPS = ("namespace", "database", "secret", "dns", "app")
TIERS = ("basic", "enterprise")
REGIONS = ("eu-west-1", "us-east-1", "ap-south-1")

UNIQUE_REQUESTS = 200
DUPLICATED_IDS = 12
CHANGED_DUPLICATES = 2  # duplicates whose payload differs from the original

# Requests with a create fault after the first step, by scenario.
RECOVERED = 15          # one or two retryable failures, then success via retry_step
TIMEOUT_AFTER_SUCCESS = 8
RETRIES_EXHAUSTED = 5   # three failed attempts, then escalation
QUOTA = 8               # quota, then escalation
COMPENSATION_FAILURES = 4  # escalations whose first delete fails at least once
DELETE_TIMEOUT_AFTER_SUCCESS = 2


def generate(seed):
    rng = random.Random(seed)
    requests = _requests(rng)
    faults = _faults(rng, [r["request_id"] for r in requests])
    return _with_duplicates(rng, requests), faults


def _requests(rng):
    out = []
    for i in range(1, UNIQUE_REQUESTS + 1):
        out.append({"request_id": f"req-{i:04d}", "tenant": f"tenant-{rng.randrange(80):03d}",
                    "tier": TIERS[i % 2] if i <= 2 else rng.choice(TIERS),
                    "region": rng.choice(REGIONS)})
    return out


def _with_duplicates(rng, requests):
    lines = list(requests)
    for n, original in enumerate(rng.sample(requests, DUPLICATED_IDS)):
        dup = dict(original)
        if n < CHANGED_DUPLICATES:
            dup["region"] = next(r for r in REGIONS if r != original["region"])
        position = lines.index(original)
        lines.insert(rng.randrange(position + 1, len(lines) + 1), dup)
    return lines


def _faults(rng, request_ids):
    groups = (RECOVERED, TIMEOUT_AFTER_SUCCESS, RETRIES_EXHAUSTED, QUOTA)
    chosen = rng.sample(request_ids, sum(groups))
    recovered, tas, exhausted, quota = _split(chosen, groups)
    faults = []

    def add(request_id, step, op, attempt, kind):
        faults.append({"request_id": request_id, "step": step, "op": op, "attempt": attempt,
                       "kind": kind})

    failed_step = {}
    for rid in recovered + tas + exhausted + quota:
        # Index 1 or later, so earlier steps exist: the failure is partway through.
        failed_step[rid] = rng.randrange(1, len(STEPS))
    for rid in recovered:
        step = STEPS[failed_step[rid]]
        add(rid, step, "create", 1, rng.choice(("timeout", "duplicate")))
        if rng.random() < 0.5:
            add(rid, step, "create", 2, "timeout")
    for rid in tas:
        add(rid, STEPS[failed_step[rid]], "create", 1, "timeout_after_success")
    for rid in exhausted:
        for attempt in (1, 2, 3):
            add(rid, STEPS[failed_step[rid]], "create", attempt,
                rng.choice(("timeout", "duplicate")))
    for rid in quota:
        add(rid, STEPS[failed_step[rid]], "create", 1, "quota")

    # Compensation only deletes steps that were created, i.e. before the failed step.
    escalated = exhausted + quota
    picks = rng.sample(escalated, COMPENSATION_FAILURES + DELETE_TIMEOUT_AFTER_SUCCESS)
    for n, rid in enumerate(picks):
        step = STEPS[rng.randrange(0, failed_step[rid])]
        if n < COMPENSATION_FAILURES:
            add(rid, step, "delete", 1, "timeout")
            if n % 2 == 0:
                add(rid, step, "delete", 2, "duplicate")
        else:
            add(rid, step, "delete", 1, "timeout_after_success")
    return sorted(faults, key=lambda f: (f["request_id"], STEPS.index(f["step"]), f["op"],
                                         f["attempt"]))


def _split(items, sizes):
    out, start = [], 0
    for size in sizes:
        out.append(items[start:start + size])
        start += size
    return out


def write_jsonl(path, records):
    with open(path, "w") as f:
        f.writelines(json.dumps(r, sort_keys=True) + "\n" for r in records)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="fixtures.gen_fixtures")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", required=True, help="directory for requests.jsonl and faults.jsonl")
    args = parser.parse_args(argv)
    requests, faults = generate(args.seed)
    os.makedirs(args.out, exist_ok=True)
    write_jsonl(os.path.join(args.out, "requests.jsonl"), requests)
    write_jsonl(os.path.join(args.out, "faults.jsonl"), faults)
    print(json.dumps({"requests": len(requests), "faults": len(faults)}, sort_keys=True))


if __name__ == "__main__":
    main()
