#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Set igniter password (writes salted+hashed auth file)."
    )
    parser.add_argument(
        "--file",
        default="igniter_auth.json",
        help="Auth file path (default: igniter_auth.json)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=120000,
        help="PBKDF2 iterations (default: 120000)",
    )
    args = parser.parse_args()

    pw1 = getpass.getpass("Enter new igniter password: ")
    pw2 = getpass.getpass("Confirm password: ")
    if pw1 != pw2:
        print("Error: passwords do not match.")
        return 1
    if not pw1:
        print("Error: password cannot be empty.")
        return 1

    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw1.encode("utf-8"), salt, args.iterations)

    data = {
        "version": 1,
        "algo": "pbkdf2_sha256",
        "iterations": args.iterations,
        "salt_hex": salt.hex(),
        "hash_hex": dk.hex(),
    }

    path = Path(args.file)
    path.write_text(json.dumps(data, indent=2))
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
