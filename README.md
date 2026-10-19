# DSO 429 Homework 3: The Hourly Quiz

An automatic arithmetic solver and GitHub submission pipeline for Logan Birckhead.
Python 3.12+, standard library only. No API key or paid model calls are needed.

## Cloud execution and fallbacks

`Cloud quiz worker` runs on GitHub-hosted Ubuntu and checks the official feed every
five minutes for up to five hours. At the end of each session it requests a new
worker using the repository's temporary `GITHUB_TOKEN`. The Mac can be off.
Only one cloud worker executes at a time, with at most one pending replacement.
No personal token or third-party hosting account is stored in the workflow.

The original `Hourly quiz` workflow remains an independent one-shot solver and
submitter, triggered by its existing schedule. A separate recovery job also explicitly
dispatches it after a failed cloud session whose regression tests passed. After each
fallback run it checks whether the cloud chain is running
or queued and restarts it if needed, even when its own solver run failed. The
existing Mac backup remains available whenever the Mac is awake and online.
Scheduled GitHub runs can still be delayed, so recovery from a terminated cloud
chain is best effort. An outage affecting all GitHub runners can affect both
cloud paths. The Mac backup supplies another execution environment when available.

Each cloud check builds on the latest remote branch in a temporary worktree,
preserves existing answer files, and retries from the updated remote if another
runner pushes first. Successful publication is checked against GitHub's remote
answer and audit contents. `cloud-status/latest.json` records the most recent
published check, any solver errors, and the worker's Actions URL. A recent timestamp
plus the live Actions log confirms activity; a stale timestamp needs investigation.

All execution and handoff paths stop at October 23, 2026, 10:00 UTC
(3:00 AM Pacific). The default five-hour session stays below GitHub's six-hour
job limit. Check failures are retried at the next poll and still make that session
fail visibly; a failed session also attempts handoff and invokes the original fallback.
An abrupt whole-run termination or manual cancellation can prevent those recovery
jobs from running; the independent schedule can restart the chain on its next run.
To stop all cloud execution early, disable both `cloud.yml` and
`quiz.yml`, then cancel any running/pending cloud worker.

Start or inspect cloud execution:

```sh
gh workflow run cloud.yml --repo loganbirckhead/dso429
gh run list --repo loganbirckhead/dso429 --workflow cloud.yml
```

For an accelerated handoff rehearsal, add `-f session_minutes=1 -f interval_seconds=30`
to the dispatch command. Its successor uses the normal five-hour/five-minute
settings. Regression tests include real Git push races against disposable local
remotes, partial solver failures, exhausted push retries, duplicate-worker
prevention, watchdog recovery, transient-check recovery, and cutoff enforcement.
Practice answer accuracy is checked against the 1,200 official practice keys;
graded-round replay only checks solver coverage and consistency, not correctness
against unpublished grading keys.

To exercise the failure recovery job, also pass `-f exercise_recovery=true` with
the short rehearsal. It deliberately produces a failed cloud run after successful
live checks, then invokes the original fallback; the normal successor does not
inherit this test input. Failure emails from that controlled test are expected.

## How it works

1. The cloud worker checks the official `questions.csv` every five minutes. The independent scheduled fallback requests checks at minutes 7, 22, 37, and 52 each hour.
2. Each available round without an existing valid answer file is solved independently.
3. The solver computes each result with explicit formulas (Decimal arithmetic and calendar dates).
4. A choice is accepted only if exactly one option matches the result at its displayed precision.
5. All 25 answers must be valid before the round is written to `answers/round-NNNN.json`.
6. The workflow commits and pushes answers and calculation audits to `main`, then fetches the remote branch to verify the submitted contents.

Repeated runs preserve existing answer files. Every run catches up all available unanswered rounds, not just the newest. A failing round does not block successful rounds. Unknown question formats or ambiguous choices cause a visible workflow failure instead of a guess.

## Sources and validation

- [Official questions](https://github.com/FaisalXL/dso429-hourly-quiz/blob/main/questions.csv)
- [Official practice keys](https://github.com/FaisalXL/dso429-hourly-quiz/blob/main/practice_keys.csv)
- The public practice CSVs were downloaded October 15, 2026 and are frozen in `tests/fixtures/` for reproducible checks.
- Development rounds 1–36: 900/900 correct.
- Reserved validation rounds 37–48: 300/300 correct on the initial solver.
- Keys are used only for benchmarks and tests. The live `run` command does not load them.

Practice success does not establish coverage of unseen formats. Review workflow failures promptly during graded rounds.

## Local commands

From this repository:

```sh
python3 -m unittest discover -s tests -v
python3 quizbot.py benchmark --questions tests/fixtures/practice_questions.csv --keys tests/fixtures/practice_keys.csv
python3 quizbot.py run
python3 quizbot.py validate
```

`run` writes files locally; it does not push them. For a manual recovery, use GitHub's **Actions → Hourly quiz → Run workflow**, or run locally and commit/push `answers/` and `audit/` to `main`.

To rehearse without modifying this checkout's answers:

```sh
python3 quizbot.py run --questions tests/fixtures/practice_questions.csv --root /tmp/dso429-rehearsal
```

## Operations

- [Workflow runs](https://github.com/loganbirckhead/dso429/actions/workflows/quiz.yml): failures include details in the job log and uploaded `quiz-check` report. Enable GitHub Actions failure notifications for this repository in your GitHub notification settings.
- `reports/latest.json` records created/existing rounds, failed rounds, late rounds, and expected rounds missing from the feed. Reports are workflow artifacts, not public commits.
- `audit/round-NNNN.json` records each formula, computed value, answer, release time, solve time, and source hash. Solve time is diagnostic, **not** proof of GitHub receipt time.
- Publication is allowed a 20-minute grace period before a missing feed round is flagged. Actual timing credit always follows the source's `released_at`.
- Full timing credit lasts three hours after release. Corrections inside that window can count; after that, an existing submission is locked under the assignment rules. The bot never automatically overwrites submitted files.
- For a correction within the window: independently verify the result, edit the answer and corresponding audit, validate, then push to `main` before the window closes. Do not delete/recreate a late submission expecting a new grading attempt.
- GitHub schedules can be delayed or dropped. Check the Actions page at least morning and evening, and manually trigger a run if checks stop. The schedule and backlog recovery provide retries but do not guarantee delivery.
- No laptop process needs to remain running for GitHub Actions.
- October 19 incident: scheduled runs arrived hours apart despite the 15-minute cron. The failing runs successfully pushed answers, then reported newly created late rounds. Do not remove the late-round failure signal: it identifies a real timing problem.
- `python3 backup_runner.py --loop` provides an independent five-minute Mac backup through the existing October 23 10:00 UTC cutoff. It needs working command-line Git authentication (`gh auth login`, then `gh auth setup-git`), an awake Mac, and Internet access. Run it with `caffeinate -i` to prevent idle sleep. Closing the lid or losing power/network can still interrupt it.
- The backup uses an isolated checkout under ignored `.local-backup/checkout`, preserves existing submissions, and verifies remote answer contents after pushing. Inspect `.local-backup/status.json` for `remote_verified: true` and a recent `checked_at`. Errors are recorded there and retried every five minutes; a conflicting rebase needs manual attention. It does not send email alerts.
- Source/workflow changes on `main` also trigger a check. The first successful cloud run publishes `status/automation-ready.json` once, proving the workflow can push with its own token.
- The schedule covers October 15–23 UTC; live fetching stops October 23, 2026 at 10:00 UTC. Disable the workflow once the assignment is complete to avoid its annual cron recurrence.

## Schedule and submissions (Pacific time)

| Item | Time |
| --- | --- |
| Register `https://github.com/loganbirckhead/dso429` in Brightspace, “Homework 3 GitHub Repo Link” | October 16, 2026, by 11:59 PM |
| First graded round, 49 | October 17, 2026, 2:00 PM |
| Last graded round, 168 | October 22, 2026, 1:00 PM |
| Assignment header deadline | October 22, 2026, 2:00 PM |
| Generate usage report | October 22, 2026, **after** 2:00 PM, per the detailed instructions |

The header deadline and after-2-PM report instruction need to be followed as stated; confirm Brightspace's report upload cutoff if it closes at 2 PM. Keep the repo public and its name and owner unchanged until grades are released.

From the local course root (`DSO 429 `, including its trailing space), generate the official report:

```sh
python3 .claude/weekly_report.py --days-back 7
```

Submit only `usage_reports/<date>.json` to Brightspace “Homework 3”; keep the transcript local. The official tracker reads Claude Code sessions, not Codex. Logan confirmed on October 15 that Codex is accepted. Do not alter the tracker or fabricate usage to make Codex activity appear as Claude activity.

## The calculations to explain

| Type | Calculation |
| --- | --- |
| Compound interest | principal × (1 + annual rate)^years |
| Gross margin | (revenue − cost of goods sold) / revenue × 100 |
| Discount | listed price × (1 − discount rate) |
| Percentage change | (new − old) / old × 100 |
| Break-even | fixed costs / (unit price − unit variable cost), rounded to nearest unit as requested |
| Ratio allocation | budget × first ratio term / sum of terms |
| Weighted average | sum(order count × average value) / total order count |
| Currency conversion | dollars × euros per dollar |
| Probability | billing tickets / total tickets × 100 |
| Revenue | units × unit price |
| Arithmetic series | last value + common difference, after checking every difference |
| Settlement | signing date + stated number of calendar days |

Break-even questions explicitly ask for the nearest unit; an operational requirement to cover all costs would normally round up. Dates add elapsed days without counting the signing date as day one.
