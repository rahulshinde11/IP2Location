#!/usr/bin/env python3
"""
Container entrypoint that self-heals bind-mount ownership, then drops to an
unprivileged user before exec'ing the real command.

The container starts as root so it can chown the bind-mounted volumes (Docker
creates missing mount sources as root). After fixing ownership it drops to
appuser and replaces itself with the target command, so the app runs unprivileged
and still receives signals (SIGTERM from `docker stop`) directly.
"""

import os
import sys
import subprocess

TARGET_UID = int(os.getenv("APP_UID", "1000"))
TARGET_GID = int(os.getenv("APP_GID", "1000"))
TARGET_USER = os.getenv("APP_USER", "appuser")

# Colon-separated list of directories to take ownership of on startup.
CHOWN_DIRS = os.getenv("CHOWN_DIRS", "/app/logs").split(":")


def fix_ownership() -> None:
    if os.geteuid() != 0:
        # Not root (e.g. invoked via `docker exec` with -u). Skip silently;
        # ownership will be re-asserted on the next container start.
        return
    for d in CHOWN_DIRS:
        if not d:
            continue
        try:
            os.makedirs(d, exist_ok=True)
            subprocess.run(["chown", "-R", f"{TARGET_UID}:{TARGET_GID}", d], check=False)
        except Exception as e:
            print(f"entrypoint: could not fix ownership of {d}: {e}", file=sys.stderr)


def drop_privileges() -> None:
    if os.geteuid() != 0:
        return
    try:
        os.initgroups(TARGET_USER, TARGET_GID)
    except (PermissionError, KeyError, OSError):
        os.setgroups([TARGET_GID])
    os.setgid(TARGET_GID)
    os.setuid(TARGET_UID)
    os.environ["HOME"] = f"/home/{TARGET_USER}"


def main() -> None:
    if len(sys.argv) < 2:
        print("entrypoint: no command given", file=sys.stderr)
        sys.exit(1)
    fix_ownership()
    drop_privileges()
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
