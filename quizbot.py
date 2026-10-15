"""Fetch, validate, and solve all available unsubmitted rounds. Standard library only."""
import argparse
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
from urllib.request import Request, urlopen

from solver import solve

QUESTIONS_URL = "https://raw.githubusercontent.com/FaisalXL/dso429-hourly-quiz/main/questions.csv"
KEYS_URL = "https://raw.githubusercontent.com/FaisalXL/dso429-hourly-quiz/main/practice_keys.csv"
FIRST_GRADED = datetime(2026, 10, 17, 21, tzinfo=timezone.utc)
STOP_AT = datetime(2026, 10, 23, 10, tzinfo=timezone.utc)
COLUMNS = {"round_id", "q_index", "released_at", "question_text", "option_a", "option_b", "option_c", "option_d"}


def fetch(url):
    last_error = None
    for attempt in range(3):
        try:
            request = Request(url, headers={"User-Agent": "dso429-quizbot/1.0", "Cache-Control": "no-cache"})
            with urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8-sig")
        except (OSError, UnicodeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Download failed after 3 attempts: {url}: {last_error}")


def load_source(path, url):
    return Path(path).read_text(encoding="utf-8-sig") if path else fetch(url)


def released_at(text):
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"released_at must specify UTC: {text}")
    return value


def parse_questions(text):
    reader = csv.DictReader(io.StringIO(text))
    if set(reader.fieldnames or []) != COLUMNS:
        raise ValueError(f"Unexpected CSV columns: {reader.fieldnames}")
    rounds = {}
    for row in reader:
        if None in row or any(value is None or value == "" for value in row.values()):
            raise ValueError("Malformed or empty CSV cell")
        rid, index = int(row["round_id"]), int(row["q_index"])
        if not 1 <= rid <= 168 or not 1 <= index <= 25:
            raise ValueError(f"Invalid round/question index: {rid}/{index}")
        released_at(row["released_at"])
        questions = rounds.setdefault(rid, {})
        if index in questions:
            raise ValueError(f"Duplicate round/question: {rid}/{index}")
        questions[index] = row
    if not rounds:
        raise ValueError("No questions in source")
    return rounds


def validate_answers(payload, rid):
    if not isinstance(payload, dict) or set(payload) != {"round_id", "answers"}:
        raise ValueError("Answer file must contain round_id and answers")
    if type(payload["round_id"]) is not int or payload["round_id"] != rid:
        raise ValueError("round_id does not match filename")
    answers = payload["answers"]
    if not isinstance(answers, dict) or set(answers) != {f"q{i}" for i in range(1, 26)}:
        raise ValueError("Answer file must have exactly q1 through q25")
    if any(not isinstance(v, str) or v not in {"A", "B", "C", "D"} for v in answers.values()):
        raise ValueError("Answers must be A, B, C, or D")


def solve_round(rid, questions):
    if set(questions) != set(range(1, 26)):
        raise ValueError(f"Round {rid} is incomplete: {len(questions)}/25 questions")
    if len({r["released_at"] for r in questions.values()}) != 1:
        raise ValueError(f"Round {rid} has inconsistent release timestamps")
    answers, evidence = {}, {}
    for index, row in sorted(questions.items()):
        result = solve(row)
        answers[f"q{index}"] = result.answer
        evidence[f"q{index}"] = {"kind": result.kind, "computed_value": result.value, "formula": result.formula, "answer": result.answer}
    payload = {"round_id": rid, "answers": answers}
    validate_answers(payload, rid)
    return payload, evidence


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def expected_rounds(now):
    if now < FIRST_GRADED:
        return set()
    last = min(168, 49 + int((now-FIRST_GRADED).total_seconds() // 3600))
    return set(range(49, last+1))


def run(text, root, now):
    rounds = parse_questions(text)
    root = Path(root)
    report = {"checked_at": now.isoformat(), "available_rounds": len(rounds), "created": [], "existing": [], "errors": [], "late": [], "missing_from_feed": [], "approaching_deadline": []}
    # Independently handle each round so one malformed/unknown round cannot block the rest.
    for rid, questions in sorted(rounds.items()):
        path = root / "answers" / f"round-{rid:04d}.json"
        try:
            if path.exists():
                validate_answers(json.loads(path.read_text()), rid)
                report["existing"].append(rid)
                continue  # Never silently overwrite an existing submission, even after the deadline.
            release = released_at(next(iter(questions.values()))["released_at"])
            if rid >= 49 and release > now:
                continue
            age = (now-release).total_seconds()
            if rid >= 49 and age >= 3*3600:
                report["late"].append(rid)
            if rid >= 49 and age >= 90*60:
                report["approaching_deadline"].append(rid)
            payload, evidence = solve_round(rid, questions)
            audit = {"round_id": rid, "released_at": release.isoformat(), "solved_at": now.isoformat(), "source_sha256": hashlib.sha256(json.dumps(questions, sort_keys=True).encode()).hexdigest(), "questions": evidence}
            write_json(root / "audit" / path.name, audit)
            write_json(path, payload)
            report["created"].append(rid)
        except (ValueError, KeyError, ArithmeticError, OSError) as exc:
            report["errors"].append({"round_id": rid, "error": str(exc)})
    # A feed can be stale without causing a download failure. Allow 20 minutes for publication.
    report["missing_from_feed"] = sorted(expected_rounds(now-timedelta(minutes=20))-set(rounds))
    write_json(root / "reports" / "latest.json", report)
    return report


def benchmark(questions_text, keys_text, first, last):
    rounds = parse_questions(questions_text)
    keys = {}
    for row in csv.DictReader(io.StringIO(keys_text)):
        key = (int(row["round_id"]), int(row["q_index"]))
        if key in keys or row["answer"] not in {"A", "B", "C", "D"}:
            raise ValueError(f"Invalid practice key {key}")
        keys[key] = row["answer"]
    total, correct, failures, kinds = 0, 0, [], {}
    for rid in range(first, last+1):
        if rid not in rounds or set(rounds[rid]) != set(range(1, 26)):
            raise ValueError(f"Missing or incomplete benchmark round {rid}")
        for index, row in sorted(rounds[rid].items()):
            total += 1
            expected = keys[(rid, index)]
            try:
                result = solve(row)
                kinds[result.kind] = kinds.get(result.kind, 0)+1
                if result.answer == expected:
                    correct += 1
                else:
                    failures.append({"round": rid, "question": index, "expected": expected, "actual": result.answer})
            except (ValueError, ArithmeticError) as exc:
                failures.append({"round": rid, "question": index, "error": str(exc)})
    return {"first_round": first, "last_round": last, "correct": correct, "total": total, "accuracy": correct/total, "question_types": kinds, "failures": failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "benchmark", "validate"])
    parser.add_argument("--questions", help="Local CSV; defaults to fetching official source")
    parser.add_argument("--keys", help="Local practice keys; benchmark only")
    parser.add_argument("--root", default=".")
    parser.add_argument("--first-round", type=int, default=1)
    parser.add_argument("--last-round", type=int, default=48)
    args = parser.parse_args()
    if args.command == "validate":
        paths = sorted((Path(args.root)/"answers").glob("*.json"))
        for path in paths:
            import re
            if not re.fullmatch(r"round-\d{4}\.json", path.name):
                raise ValueError(f"Misnamed answer file: {path}")
            rid = int(path.stem.split("-")[1])
            if not 1 <= rid <= 168:
                raise ValueError(f"Invalid round {rid}")
            validate_answers(json.loads(path.read_text()), rid)
        print(f"Validated {len(paths)} answer files")
        return 0
    now = datetime.now(timezone.utc)
    if args.command == "run" and not args.questions and now >= STOP_AT:
        print("Assignment window ended; no new submissions generated.")
        return 0
    text = load_source(args.questions, QUESTIONS_URL)
    if args.command == "benchmark":
        if not 1 <= args.first_round <= args.last_round <= 48:
            raise ValueError("Benchmark range must be within practice rounds 1-48")
        report = benchmark(text, load_source(args.keys, KEYS_URL), args.first_round, args.last_round)
        write_json(Path(args.root)/"reports"/"benchmark.json", report)
        print(json.dumps(report, indent=2))
        return int(bool(report["failures"]))
    report = run(text, args.root, now)
    print(json.dumps(report, indent=2))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as out:
            out.write(f"## Quiz check\n\nCreated: {report['created']}\n\nExisting: {len(report['existing'])}\n\nErrors: {report['errors']}\n\nMissing from feed: {report['missing_from_feed']}\n\nLate: {report['late']}\n")
    return int(bool(report["errors"] or report["missing_from_feed"] or report["late"]))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
