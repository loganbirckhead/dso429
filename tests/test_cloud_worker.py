from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import cloud_worker as cloud
import quizbot
from test_quizbot import source, NOW


def git(*args, cwd=None):
    return subprocess.run(["git", *map(str, args)], cwd=cwd, text=True,
                          capture_output=True, check=True).stdout.strip()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "checkout"
        self.remote = Path(self.temp.name) / "remote.git"
        git("init", "--bare", self.remote)
        git("clone", self.remote, self.root)
        (self.root / "README.md").write_text("Disposable integration-test repository\n")
        (self.root / ".gitignore").write_text("reports/\n")
        git("add", ".", cwd=self.root)
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-m", "Initial", cwd=self.root)
        git("branch", "-M", "main", cwd=self.root)
        git("push", "origin", "main", cwd=self.root)
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.remote)

    def read_remote(self, name):
        return json.loads(git("show", f"main:{name}", cwd=self.remote))

    def test_live_style_publication_and_repeat_preserve_answers_and_audit(self):
        questions = source((49, "2026-10-17T21:00:00Z"), (50, "2026-10-17T22:00:00Z"))
        first = cloud.check_once(self.root, questions, NOW)
        self.assertEqual(first["created"], [49, 50])
        self.assertTrue(first["remote_verified"])
        answer = self.read_remote("answers/round-0049.json")
        audit = self.read_remote("audit/round-0049.json")
        second = cloud.check_once(self.root, questions, NOW + timedelta(minutes=5))
        self.assertEqual(second["created"], [])
        self.assertEqual(self.read_remote("answers/round-0049.json"), answer)
        self.assertEqual(self.read_remote("audit/round-0049.json"), audit)
        self.assertEqual(git("status", "--porcelain", cwd=self.root), "")

    def test_simultaneous_fallback_submission_is_preserved_after_retry(self):
        questions = source((49, "2026-10-17T21:00:00Z"))
        original_git = cloud.git
        competed = []

        def race(*args, **kwargs):
            if args[:2] == ("push", "origin") and not competed:
                other = Path(self.temp.name) / "fallback"
                git("clone", self.remote, other)
                quizbot.run(questions, other, NOW - timedelta(minutes=1))
                git("add", "answers", "audit", cwd=other)
                git("-c", "user.name=Fallback", "-c", "user.email=test@example.invalid",
                    "commit", "-m", "Fallback wins race", cwd=other)
                git("push", "origin", "main", cwd=other)
                competed.append(self.read_remote("audit/round-0049.json"))
            return original_git(*args, **kwargs)

        with patch("cloud_worker.git", side_effect=race):
            result = cloud.check_once(self.root, questions, NOW)
        self.assertEqual(result["created"], [])
        self.assertTrue(result["remote_verified"])
        self.assertEqual(self.read_remote("audit/round-0049.json"), competed[0])

    def test_unsolved_round_does_not_block_a_valid_round(self):
        questions = source((49, "2026-10-17T21:00:00Z"), (50, "2026-10-17T22:00:00Z"))
        questions = questions.replace(
            "An investment of $31,500.00 grows at 12% compounded annually. What is its value after 7 years?",
            "An unfamiliar question", 1)
        result = cloud.check_once(self.root, questions, NOW)
        self.assertEqual(result["created"], [50])
        self.assertEqual(result["errors"][0]["round_id"], 49)
        quizbot.validate_answers(self.read_remote("answers/round-0050.json"), 50)
        missing = subprocess.run(["git", "show", "main:answers/round-0049.json"],
                                 cwd=self.remote, capture_output=True)
        self.assertNotEqual(missing.returncode, 0)

    def test_push_failure_is_visible_and_temporary_worktrees_are_removed(self):
        original_git = cloud.git

        def fail_push(*args, **kwargs):
            if args[:2] == ("push", "origin"):
                return subprocess.CompletedProcess(args, 1, "", "simulated outage")
            return original_git(*args, **kwargs)

        with patch("cloud_worker.git", side_effect=fail_push):
            with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
                cloud.check_once(self.root, source((49, "2026-10-17T21:00:00Z")), NOW)
        self.assertEqual(git("rev-list", "--count", "main", cwd=self.remote), "1")
        self.assertEqual(git("worktree", "list", "--porcelain", cwd=self.root).count("worktree "), 1)


class LifecycleTests(unittest.TestCase):
    def test_cutoff_stops_publication_and_handoff_without_network(self):
        with patch("cloud_worker.git") as network:
            self.assertEqual(cloud.check_once(now=quizbot.STOP_AT), {"stopped": True})
            network.assert_not_called()
        def forbidden(*args):
            self.fail("No API call is allowed after cutoff")
        self.assertEqual(cloud.ensure_worker(now=quizbot.STOP_AT, api_call=forbidden), {"stopped": True})

    def test_handoff_ignores_itself_and_dispatches_normal_length_successor(self):
        calls = []
        def api(endpoint, payload=None):
            calls.append((endpoint, payload))
            return {"workflow_runs": [{"id": 10, "status": "in_progress"}]} if payload is None else {}
        self.assertTrue(cloud.ensure_worker(NOW, api, "10")["dispatched"])
        self.assertEqual(calls[-1][1], {"ref": "main", "inputs": {"predecessor": "10"}})

    def test_watchdog_retains_existing_worker_or_queued_successor(self):
        for status in cloud.ACTIVE:
            calls = []
            def api(endpoint, payload=None):
                calls.append(payload)
                return {"workflow_runs": [{"id": 11, "status": status}]}
            self.assertEqual(cloud.ensure_worker(NOW, api, "10"), {"existing_worker": 11})
            self.assertEqual(calls, [None])

    def test_watchdog_restarts_failed_worker(self):
        calls = []
        def api(endpoint, payload=None):
            calls.append(payload)
            return {"workflow_runs": [{"id": 9, "status": "completed", "conclusion": "failure"}]}
        self.assertTrue(cloud.ensure_worker(NOW, api, "10")["dispatched"])
        self.assertEqual(len(calls), 2)

    def test_dispatch_retry_checks_for_success_before_sending_again(self):
        state = {"active": False}
        def api(endpoint, payload=None):
            if payload:
                state["active"] = True
                raise RuntimeError("response lost after dispatch")
            return {"workflow_runs": [{"id": 11, "status": "queued"}] if state["active"] else []}
        with patch("cloud_worker.time.sleep"):
            self.assertEqual(cloud.ensure_worker(NOW, api, "10"), {"existing_worker": 11})

    def test_session_retries_a_failed_check_and_obeys_duration(self):
        clock, checks = [0], []
        def sleep(seconds):
            clock[0] += seconds
        def check():
            checks.append(clock[0])
            if len(checks) == 1:
                raise RuntimeError("temporary download failure")
            return {"errors": []}
        with patch("builtins.print"):
            result = cloud.run_session(1, 30, check, lambda: NOW,
                                       lambda: clock[0], sleep)
        self.assertEqual(checks, [0, 30])
        self.assertEqual(clock[0], 60)
        self.assertEqual(result, 1)

    def test_invalid_session_inputs_are_rejected(self):
        for minutes, interval in [(0, 300), (301, 300), (1, 0), (1, 901)]:
            with self.assertRaises(ValueError):
                cloud.run_session(minutes, interval)


if __name__ == "__main__":
    unittest.main()
