"""Command line entry point.

    python -m tenant_lifecycle [--state-dir DIR] provision requests.jsonl faults.jsonl
    python -m tenant_lifecycle [--state-dir DIR] agent
"""

import argparse
import functools
import json
import os
import sys

from tenant_lifecycle import agent, workflow, writes
from tenant_lifecycle.cloud import Cloud, load_faults
from tenant_lifecycle.registry import Registry

REGISTRY_FILE = "registry.db"
INVENTORY_FILE = "inventory.db"
AGENT_REPORT_FILE = "agent_report.jsonl"
INPUT_ERROR_EXIT_CODE = 2


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tenant_lifecycle")
    parser.add_argument("--state-dir", default=".",
                        help=f"directory holding {REGISTRY_FILE} and {INVENTORY_FILE}")
    commands = parser.add_subparsers(dest="command", required=True)
    provision = commands.add_parser("provision", help="provision every request in file order")
    provision.add_argument("requests")
    provision.add_argument("faults")
    commands.add_parser("agent", help="resolve failed environments through policy-approved actions")
    args = parser.parse_args(argv)

    try:
        writes.crash_limit()
        if args.command == "provision":
            return _provision(args)
        return _agent(args)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return INPUT_ERROR_EXIT_CODE


def _provision(args):
    # Read and validate all input before the first write, so a bad start leaves no state.
    requests = workflow.parse_requests(args.requests)
    faults = load_faults(args.faults)
    os.makedirs(args.state_dir, exist_ok=True)
    cloud = Cloud(os.path.join(args.state_dir, INVENTORY_FILE), faults={})
    reg = Registry(os.path.join(args.state_dir, REGISTRY_FILE))
    try:
        cloud.install_faults(faults)
        workflow.provision(reg, cloud, requests)
        print(json.dumps(reg.state_counts(), sort_keys=True))
    finally:
        reg.close()
        cloud.close()
    return 0


def _agent(args):
    registry_path = os.path.join(args.state_dir, REGISTRY_FILE)
    if not os.path.exists(registry_path):
        raise ValueError(f"no registry in {args.state_dir}; run provision first")
    reg = Registry(registry_path)
    cloud = Cloud(os.path.join(args.state_dir, INVENTORY_FILE))
    try:
        agent.run(reg, functools.partial(workflow.execute, reg, cloud))
        _write_jsonl(os.path.join(args.state_dir, AGENT_REPORT_FILE), agent.report(reg))
        print(json.dumps(reg.state_counts(), sort_keys=True))
    finally:
        reg.close()
        cloud.close()
    return 0


def _write_jsonl(path, records):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.writelines(json.dumps(r, sort_keys=True) + "\n" for r in records)
    os.replace(tmp, path)
