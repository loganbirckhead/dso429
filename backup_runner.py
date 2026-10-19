"""Five-minute Mac backup. Uses normal Git credentials and an isolated checkout."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from quizbot import STOP_AT

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".local-backup"
CHECKOUT = STATE / "checkout"
REMOTE = "https://github.com/loganbirckhead/dso429.git"


def command(*args, cwd=None, allow_failure=False):
    result = subprocess.run(args, cwd=CHECKOUT if cwd is None else cwd, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                            timeout=180, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout, flush=True)
    if result.stderr:
        print(result.stderr, file=sys.stderr, flush=True)
    if result.returncode and not allow_failure:
        raise RuntimeError(f"Command failed ({result.returncode}): {args[0:2]}")
    return result


def check():
    if not (CHECKOUT / ".git").exists():
        command("git", "clone", REMOTE, str(CHECKOUT), cwd=STATE)
    command("git", "pull", "--rebase", "origin", "main")
    command(sys.executable, "quizbot.py", "run", allow_failure=True)
    command(sys.executable, "quizbot.py", "validate")
    report_path = CHECKOUT / "reports/latest.json"
    report = json.loads(report_path.read_text())
    # Solve errors must not block the publication of other valid rounds.
    command("git", "add", "answers/", "audit/")
    changed = command("git", "diff", "--cached", "--quiet", allow_failure=True).returncode
    if changed:
        command("git", "-c", "user.name=Logan Birckhead", "-c",
                "user.email=loganbirckhead@users.noreply.github.com", "commit", "-m",
                "Submit available quiz rounds from local backup")
    # Also retries a previously committed submission if a network failure prevented its push.
    command("git", "push", "origin", "HEAD:main")
    command("git", "fetch", "origin", "main")
    command("git", "diff", "--exit-code", "origin/main", "--", "answers/", "audit/")
    status = {"checked_at": datetime.now(timezone.utc).isoformat(),
              "created": report["created"], "errors": report["errors"],
              "late": report["late"], "missing_from_feed": report["missing_from_feed"],
              "remote_verified": True}
    (STATE / "status.json").write_text(json.dumps(status, indent=2) + "\n")
    print(json.dumps(status), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    STATE.mkdir(exist_ok=True)
    with (STATE / "runner.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Backup already running")
        while datetime.now(timezone.utc) < STOP_AT:
            try:
                check()
            except Exception as exc:
                print(f"BACKUP ERROR: {exc}", file=sys.stderr, flush=True)
                (STATE / "status.json").write_text(json.dumps({
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "remote_verified": False, "error": str(exc)}, indent=2) + "\n")
                if not args.loop:
                    return 1
            if not args.loop:
                return 0
            time.sleep(min(300, max(0, (STOP_AT-datetime.now(timezone.utc)).total_seconds())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
