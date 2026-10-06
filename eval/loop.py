"""Run every scenario, patch the policy from the failures, and run them again."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from eval.improve import propose_patch
from eval.judge import evaluate
from eval.scenarios import SCENARIOS, SimulatedPatient
from scheduler.agent import Agent, Session
from scheduler.clinic import fresh_clinic
from scheduler.policy import V0_PATH, V1_PATH, apply_patch, load_policy, save_policy
from scheduler.tools import ToolBelt

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "eval_results"
MAX_TURNS = 8


def run_scenario(scenario, policy: dict) -> dict:
    clinic = fresh_clinic()
    tools = ToolBelt(clinic, policy)
    agent = Agent(policy, tools)
    patient = SimulatedPatient(scenario)
    session = Session()
    incoming = scenario.opening
    for _ in range(MAX_TURNS):
        turn = agent.handle(session, incoming)
        if turn.done or not turn.ask:
            break
        incoming = patient.reply(turn.ask)
        if not incoming:
            break
    appointments = [
        appointment.snapshot(clinic) for appointment in clinic.appointments.values()
    ]
    return {
        "scenario_id": scenario.id,
        "policy_version": policy["version"],
        "events": session.events,
        "tools": tools.trace,
        "appointments": appointments,
    }


def score_scenario(scenario, policy: dict) -> dict:
    record = run_scenario(scenario, policy)
    checks = [check.as_dict() for check in evaluate(scenario.id, record)]
    return {
        "id": scenario.id,
        "title": scenario.title,
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
        "record": record,
    }


def run_suite(policy: dict) -> list[dict]:
    return [score_scenario(scenario, policy) for scenario in SCENARIOS]


def _passed_checks(results: list[dict]) -> set[tuple[str, str]]:
    found = set()
    for result in results:
        for check in result["checks"]:
            if check["passed"]:
                found.add((result["id"], check["id"]))
    return found


def _passed_scenarios(results: list[dict]) -> set[str]:
    return {result["id"] for result in results if result["passed"]}


def summarize(results: list[dict]) -> dict:
    checks = [check for result in results for check in result["checks"]]
    passed_scenarios = sum(1 for result in results if result["passed"])
    passed_checks = sum(1 for check in checks if check["passed"])
    return {
        "scenarios_passed": passed_scenarios,
        "scenarios_total": len(results),
        "checks_passed": passed_checks,
        "checks_total": len(checks),
    }


def format_transcript(record: dict) -> str:
    turns = {event["turn"] for event in record["events"]}
    turns.update(tool["turn"] for tool in record["tools"])
    lines: list[str] = []
    for turn in sorted(turns):
        for event in record["events"]:
            if event["turn"] == turn and event["kind"] == "user":
                lines.append(f"Patient: {event['text']}")
        for tool in record["tools"]:
            if tool["turn"] != turn:
                continue
            status = "ok" if tool["ok"] else "ERROR"
            lines.append(f"  [tool] {tool['name']} {status} - {tool['detail']}")
        for event in record["events"]:
            if event["turn"] == turn and event["kind"] == "agent":
                lines.append(f"Agent: {event['text']}")
                lines.append("")
    return "\n".join(lines).rstrip()


def _score_line(summary: dict) -> str:
    return (
        f"{summary['scenarios_passed']}/{summary['scenarios_total']} scenarios, "
        f"{summary['checks_passed']}/{summary['checks_total']} checks"
    )


def _dump(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


def run_loop() -> dict:
    baseline = load_policy(V0_PATH)
    before = run_suite(baseline)
    before_summary = summarize(before)
    patch = propose_patch(before, baseline["version"])
    if not patch["changes"]:
        raise RuntimeError("baseline produced no mapped failures; the loop has nothing to repair")
    patched = apply_patch(baseline, patch)
    save_policy(patched, V1_PATH)
    after = run_suite(patched)
    after_summary = summarize(after)

    scenario_regressions = sorted(_passed_scenarios(before) - _passed_scenarios(after))
    check_regressions = sorted(_passed_checks(before) - _passed_checks(after))
    improved = before_summary["scenarios_passed"] < after_summary["scenarios_passed"]
    closed = (
        improved
        and after_summary["scenarios_passed"] == after_summary["scenarios_total"]
        and not scenario_regressions
        and not check_regressions
    )

    report = {
        "before": before_summary,
        "after": after_summary,
        "improved": improved,
        "closed": closed,
        "scenario_regressions": scenario_regressions,
        "check_regressions": [list(item) for item in check_regressions],
        "patch": patch,
    }
    _dump(OUT / "before.json", {"summary": before_summary, "results": before})
    _dump(OUT / "after.json", {"summary": after_summary, "results": after})
    _dump(OUT / "patch.json", patch)
    report_text = render_report(before, after, patch, report)
    (OUT / "REPORT.md").write_text(report_text, encoding="utf-8")
    report["text"] = report_text
    return report


def render_report(before: list[dict], after: list[dict], patch: dict, report: dict) -> str:
    failed_ids = [result["id"] for result in before if not result["passed"]]
    after_by_id = {result["id"]: result for result in after}
    lines = [
        "# Eval loop",
        "",
        f"Before (policy v0): {_score_line(report['before'])}",
        f"After (policy v1): {_score_line(report['after'])}",
        "",
        "## Before",
        "",
        _table(before),
        "",
        "## Patch",
        "",
    ]
    for change in patch["changes"]:
        if change["op"] == "set_rule":
            lines.append(f"- `{change['rule']}` set to true. {change['rationale']}")
        else:
            lines.append(f"- remove tool `{change['tool']}`. {change['rationale']}")
        for evidence in change["evidence"]:
            lines.append(f"  - {evidence['scenario']} / {evidence['check']}: {evidence['detail']}")
    if patch["unmapped_failures"]:
        lines.extend(["", "Unmapped failures (no patch rule):"])
        for item in patch["unmapped_failures"]:
            lines.append(f"- {item['scenario']} / {item['check']}: {item['detail']}")
    lines.extend(["", "## After", "", _table(after), ""])
    if report["scenario_regressions"] or report["check_regressions"]:
        lines.append("Regressions:")
        for scenario_id in report["scenario_regressions"]:
            lines.append(f"- scenario {scenario_id}")
        for scenario_id, check_id in report["check_regressions"]:
            lines.append(f"- check {scenario_id} / {check_id}")
    else:
        lines.append("Regressions: none.")
    lines.extend(["", "## Transcripts for scenarios that failed on v0", ""])
    for result in before:
        if result["id"] not in failed_ids:
            continue
        lines.append(f"### {result['id']} before")
        lines.append("")
        lines.append("```")
        lines.append(format_transcript(result["record"]))
        lines.append("```")
        lines.append("")
        lines.append(f"### {result['id']} after")
        lines.append("")
        lines.append("```")
        lines.append(format_transcript(after_by_id[result["id"]]["record"]))
        lines.append("```")
        lines.append("")
    lines.extend(
        [
            "## What this judge cannot see",
            "",
            "A transcript that says \"Thanks for confirming\" or \"call 911\" can still hide a bad tool call. "
            "The passing checks above use the tool trace for that reason. "
            "The judge does not score bedside manner, and the simulated patient answers a structured question type rather than interrupting the way a real caller would.",
            "",
        ]
    )
    return "\n".join(lines)


def _table(results: list[dict]) -> str:
    rows = ["| Scenario | Result | Failed checks |", "| --- | --- | --- |"]
    for result in results:
        failed = [check["id"] for check in result["checks"] if not check["passed"]]
        mark = "pass" if result["passed"] else "fail"
        rows.append(f"| {result['id']} | {mark} | {', '.join(failed) if failed else '-'} |")
    return "\n".join(rows)


def main() -> int:
    report = run_loop()
    print(report["text"])
    print(f"Wrote {OUT / 'REPORT.md'}")
    print(f"Wrote {V1_PATH}")
    if report["closed"]:
        print("Loop closed: score moved up and nothing that previously passed regressed.")
        return 0
    print("Loop did not close. See the report above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
