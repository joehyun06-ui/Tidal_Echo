"""Local test-admin writer. HTTP never exposes these commands."""

from argparse import ArgumentParser
from dataclasses import asdict
from getpass import getpass
import json

from .store import AuthorityStore


def main():
    parser = ArgumentParser()
    parser.add_argument("action", choices=("seed", "revoke"))
    parser.add_argument("event_id")
    args = parser.parse_args()
    store = AuthorityStore.deployed()
    token = getpass("Test-admin credential: ")
    operation = store.seed_test_event if args.action == "seed" else store.revoke_test_event
    print(json.dumps(asdict(operation(args.event_id, token=token)), sort_keys=True))


if __name__ == "__main__":
    main()
