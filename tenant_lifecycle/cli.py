"""Command line entry point: python -m tenant_lifecycle provision requests.jsonl faults.jsonl"""

import argparse
import json
import os
import sys

from tenant_lifecycle import workflow, writes
from tenant_lifecycle.cloud import Cloud, load_faults
from tenant_lifecycle.registry import Registry

REGISTRY_FILE = "registry.db"
INVENTORY_FILE = "inventory.db"
INPUT_ERROR_EXIT_CODE = 2


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tenant_lifecycle")
    parser.add_argument("--state-dir", default=".",
                        help=f"directory holding {REGISTRY_FILE} and {INVENTORY_FILE}")
    commands = parser.add_subparsers(dest="command", required=True)
    provision = commands.add_parser("provision", help="provision every request in file order")
    provision.add_argument("requests")
    provision.add_argument("faults")
    args = parser.parse_args(argv)

    # Reject bad input before the first write, so a failed start leaves no partial state.
    try:
        writes.crash_limit()
        requests = workflow.parse_requests(args.requests)
        faults = load_faults(args.faults)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return INPUT_ERROR_EXIT_CODE

    os.makedirs(args.state_dir, exist_ok=True)
    reg = Registry(os.path.join(args.state_dir, REGISTRY_FILE))
    cloud = Cloud(os.path.join(args.state_dir, INVENTORY_FILE), faults)
    try:
        workflow.provision(reg, cloud, requests)
        print(json.dumps(reg.state_counts(), sort_keys=True))
    finally:
        reg.close()
        cloud.close()
    return 0
