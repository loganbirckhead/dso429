"""Bounded GitHub-hosted quiz worker with optimistic publication and handoff."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import quizbot

ROOT = Path(__file__).resolve().parent
WORKFLOW = "cloud.yml"
REPOSITORY = "loganbirckhead/dso429"
ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}


def command(*args, cwd=ROOT, check=True, input=None, env=None):
    result = subprocess.run(args, cwd=cwd, input=input, text=True, capture_output=True,
                            timeout=120, env=env)
    if check and result.returncode:
        # Never print credential-bearing environment variables or HTTP headers.
        raise RuntimeError(f"{args[0:2]} failed: {result.stderr.strip()}")
    return result


def git(*args, cwd=ROOT, check=True):
    return command("git", *args, cwd=cwd, check=check)


def validate_tree(root):
    for path in sorted((root / "answers").glob("*.json")):
        rid = int(path.stem.removeprefix("round-"))
        if path.name != f"round-{rid:04d}.json" or not 1 <= rid <= 168:
            raise ValueError(f"Invalid answer filename: {path.name}")
        quizbot.validate_answers(json.loads(path.read_text()), rid)


def check_once(root=ROOT, questions=None, now=None, attempts=3):
    """Rebuild on the latest remote after a race; never overwrite a submitted round."""
    now = now or datetime.now(timezone.utc)
    if now >= quizbot.STOP_AT:
        return {"stopped": True}
    questions = questions if questions is not None else quizbot.fetch(quizbot.QUESTIONS_URL)
    for attempt in range(attempts):
        git("fetch", "origin", "main", cwd=root)
        with tempfile.TemporaryDirectory(prefix="quiz-cloud-") as directory:
            tree = Path(directory) / "tree"
            git("worktree", "add", "--detach", str(tree), "origin/main", cwd=root)
            try:
                report = quizbot.run(questions, tree, now)
                validate_tree(tree)
                status = {key: report[key] for key in (
                    "checked_at", "available_rounds", "created", "errors", "late", "missing_from_feed")}
                status["workflow_run"] = (
                    f"https://github.com/{REPOSITORY}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
                    if os.environ.get("GITHUB_RUN_ID") else "local-test")
                # Keep the heartbeat outside status/: the independent original
                # fallback verifies that directory against its checkout.
                quizbot.write_json(tree / "cloud-status/latest.json", status)
                git("add", "answers/", "audit/", "cloud-status/latest.json", cwd=tree)
                if git("diff", "--cached", "--quiet", cwd=tree, check=False).returncode:
                    git("-c", "user.name=github-actions[bot]", "-c",
                        "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                        "commit", "-m", "Check and submit quiz rounds from cloud worker", cwd=tree)
                    pushed = git("push", "origin", "HEAD:main", cwd=tree, check=False)
                    if pushed.returncode:
                        if attempt + 1 < attempts:
                            continue
                        raise RuntimeError(f"Publication failed after {attempts} attempts: {pushed.stderr.strip()}")
                # The server must contain the actual files, not just a successful local commit.
                git("fetch", "origin", "main", cwd=tree)
                for rid in report["created"]:
                    name = f"round-{rid:04d}.json"
                    for folder in ("answers", "audit"):
                        remote = git("show", f"origin/main:{folder}/{name}", cwd=tree).stdout
                        if json.loads(remote) != json.loads((tree / folder / name).read_text()):
                            raise RuntimeError(f"Remote verification failed: {folder}/{name}")
                status["remote_verified"] = True
                quizbot.write_json(root / "reports/cloud-latest.json", status)
                print(json.dumps(status), flush=True)
                return status
            finally:
                git("worktree", "remove", "--force", str(tree), cwd=root)
    raise RuntimeError("Publication retry budget exhausted")


def api(endpoint, payload=None):
    args = ["gh", "api", f"repos/{REPOSITORY}/{endpoint}"]
    if payload is not None:
        args += ["--method", "POST", "--input", "-"]
    result = command(*args, input=json.dumps(payload) if payload is not None else None)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def ensure_worker(now=None, api_call=api, current_run=None):
    """A replacement is requested only while the assignment is open and none is queued."""
    if (now or datetime.now(timezone.utc)) >= quizbot.STOP_AT:
        return {"stopped": True}
    current_run = str(current_run or os.environ.get("GITHUB_RUN_ID", ""))
    for attempt in range(3):
        try:
            runs = api_call(f"actions/workflows/{WORKFLOW}/runs?per_page=100")["workflow_runs"]
            active = [r for r in runs if r["status"] in ACTIVE and str(r["id"]) != current_run]
            if active:
                return {"existing_worker": active[0]["id"]}
            result = api_call(f"actions/workflows/{WORKFLOW}/dispatches", {
                "ref": "main", "inputs": {"predecessor": current_run or "watchdog"}})
            return {"dispatched": True, "predecessor": current_run, "response": result}
        except (RuntimeError, KeyError, json.JSONDecodeError):
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))


def run_session(minutes, interval, check=check_once, utcnow=None, monotonic=None, sleep=None):
    if not 1 <= minutes <= 300 or not 30 <= interval <= 900:
        raise ValueError("Session must be 1–300 minutes; interval must be 30–900 seconds")
    utcnow = utcnow or (lambda: datetime.now(timezone.utc))
    monotonic, sleep = monotonic or time.monotonic, sleep or time.sleep
    end = monotonic() + minutes * 60
    failures = 0
    while utcnow() < quizbot.STOP_AT and monotonic() < end:
        try:
            status = check()
            if status.get("errors") or status.get("late") or status.get("missing_from_feed"):
                failures += 1
                print(f"::error::Quiz check needs attention: {json.dumps(status)}", flush=True)
        except Exception as exc:
            failures += 1
            print(f"::error::Cloud check failed; retrying: {exc}", flush=True)
        remaining = min(end - monotonic(), (quizbot.STOP_AT - utcnow()).total_seconds())
        if remaining <= 0:
            break
        sleep(min(interval, remaining))
    return int(failures > 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "ensure", "once"])
    parser.add_argument("--session-minutes", type=int, default=300)
    parser.add_argument("--interval-seconds", type=int, default=300)
    args = parser.parse_args()
    if args.command == "run":
        return run_session(args.session_minutes, args.interval_seconds)
    status = ensure_worker() if args.command == "ensure" else check_once()
    print(json.dumps(status), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
