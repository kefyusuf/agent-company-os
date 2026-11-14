"""Offline, dependency-free verification of the complete checkout.

Run from any directory:
    python3 scripts/verify.py --output .local/verification.json

No CI minutes, network, credentials or external tools are required.
The command refuses truncated checkouts, a reduced test suite and any failed tests.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = re.compile(r"Ran (\d+) tests? in ", re.MULTILINE)

# Exact required test module inventory for the current M5.3-B2 contract.
# Raising this floor is monotonic; deliberate removals need an explicit review.
REQUIRED_TEST_FILES = (
    "tests/test_store.py",
    "tests/test_workflow.py",
    "tests/test_gateway.py",
    "tests/test_policy.py",
    "tests/test_policy_store.py",
    "tests/test_policy_gateway.py",
)
MIN_ACCEPTED_TESTS = 193


def inventory() -> tuple[int, list[str]]:
    """Count declared unittest test methods and reject missing files.

    AST parsing catches missing test modules and invalid Python in the test
    inventory before a deceptively small discovery can report success.
    """
    expected = 0
    problems: list[str] = []
    for relative in REQUIRED_TEST_FILES:
        source = ROOT / relative
        if not source.is_file():
            problems.append("missing required test module: " + relative)
            continue
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError, UnicodeError) as exc:
            problems.append(f"invalid test module {relative}: {exc}")
            continue
        count = sum(
            1 for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        )
        if count == 0:
            problems.append("empty required test module: " + relative)
        expected += count
    if expected < MIN_ACCEPTED_TESTS:
        problems.append(
            f"test inventory below reviewed floor: {expected} < {MIN_ACCEPTED_TESTS}"
        )
    return expected, problems


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, timeout=180
    )


def git_value(*args: str) -> str | None:
    try:
        result = run(["git", *args])
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify complete AI Company OS checkout")
    parser.add_argument("--output", type=Path,
                        help="Optional local JSON evidence file; never committed")
    args = parser.parse_args()

    checks = [
        ("tests", [sys.executable, "-m", "unittest", "discover",
                   "-s", "tests", "-v"]),
        ("syntax", [sys.executable, "-m", "compileall", "-q",
                    "company_os", "tests"]),
    ]
    evidence: dict[str, object] = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_value("rev-parse", "HEAD"),
        "git_branch": git_value("branch", "--show-current"),
        "git_dirty": bool(git_value("status", "--porcelain")),
        "python": sys.version.split()[0],
        "checks": [],
    }
    expected_tests, inventory_problems = inventory()
    evidence["expected_test_count"] = expected_tests
    evidence["required_test_modules"] = list(REQUIRED_TEST_FILES)
    evidence["inventory_problems"] = inventory_problems
    evidence["minimum_accepted_test_count"] = MIN_ACCEPTED_TESTS
    # A reduced checkout must fail even when its single surviving test passes.
    all_ok = not inventory_problems
    if inventory_problems:
        for problem in inventory_problems:
            print("inventory: FAIL (" + problem + ")", flush=True)
    for name, command in checks:
        try:
            result = run(command)
            output = result.stdout + result.stderr
            count = int(SUMMARY.search(output).group(1)) if name == "tests" and SUMMARY.search(output) else None
            passed = result.returncode == 0 and (
                name != "tests" or (
                    count is not None and count == expected_tests
                    and not inventory_problems
                )
            )
            entry = {
                "name": name,
                "command": command[1:],
                "returncode": result.returncode,
                "passed": passed,
                "test_count": count,
                "expected_test_count": expected_tests if name == "tests" else None,
            }
            if not passed:
                entry["diagnostic"] = output[-12000:]
                if name == "tests" and count != expected_tests:
                    entry["inventory_mismatch"] = (
                        f"discovered {count}, expected {expected_tests}"
                    )
            print(f"{name}: {'PASS' if passed else 'FAIL'}" +
                  (f" ({count} discovered)" if count is not None else ""), flush=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            passed = False
            entry = {"name": name, "passed": False, "diagnostic": str(exc)}
            print(f"{name}: FAIL ({type(exc).__name__})", flush=True)
        evidence["checks"].append(entry)
        all_ok = all_ok and passed

    evidence["all_passed"] = all_ok
    if args.output:
        destination = args.output
        if not destination.is_absolute():
            destination = ROOT / destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        os.replace(temporary, destination)
        print(f"Evidence: {destination}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
