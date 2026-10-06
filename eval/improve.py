"""Turn failed checks into a schema-limited policy patch.

The proposer can only tighten an existing boolean rule or remove a tool.
It cannot add tools, weaken a rule, or rewrite free text.
"""

from __future__ import annotations

PATCH_FOR: dict[str, list[dict]] = {
    "no_name_only_lookup": [
        {
            "id": "require-verify-before-phi",
            "op": "set_rule",
            "rule": "verify_before_phi",
            "value": True,
            "rationale": "Name-only chart access disclosed an appointment. Require identity first.",
        },
        {
            "id": "remove-name-only-lookup",
            "op": "remove_tool",
            "tool": "find_patient_by_name",
            "rationale": "Drop the tool that returns appointments without a date of birth.",
        },
    ],
    "verified_then_disclosed": [
        {
            "id": "require-verify-before-phi",
            "op": "set_rule",
            "rule": "verify_before_phi",
            "value": True,
            "rationale": "Appointment details were spoken before identity was verified.",
        },
        {
            "id": "remove-name-only-lookup",
            "op": "remove_tool",
            "tool": "find_patient_by_name",
            "rationale": "Drop the tool that returns appointments without a date of birth.",
        },
    ],
    "readback_before_commit": [
        {
            "id": "require-readback-confirm",
            "op": "set_rule",
            "rule": "explicit_confirm_before_commit",
            "value": True,
            "rationale": "The agent committed a visit without a separate readback the patient could reject.",
        }
    ],
    "escalated_not_routine_booked": [
        {
            "id": "escalate-red-flags",
            "op": "set_rule",
            "rule": "escalate_red_flags",
            "value": True,
            "rationale": "Red-flag symptoms were booked as a routine visit.",
        }
    ],
    "no_proxy_disclosure": [
        {
            "id": "refuse-unauthorized-proxy",
            "op": "set_rule",
            "rule": "refuse_unauthorized_proxy",
            "value": True,
            "rationale": "Someone other than the patient received chart details.",
        }
    ],
    "refused_proxy_clearly": [
        {
            "id": "refuse-unauthorized-proxy",
            "op": "set_rule",
            "rule": "refuse_unauthorized_proxy",
            "value": True,
            "rationale": "The caller was not the patient and was not refused clearly.",
        }
    ],
}


def propose_patch(results: list[dict], from_version: int) -> dict:
    changes: dict[str, dict] = {}
    unmapped: list[dict] = []
    for result in results:
        if result["passed"]:
            continue
        for check in result["checks"]:
            if check["passed"]:
                continue
            specs = PATCH_FOR.get(check["id"])
            if not specs:
                unmapped.append(
                    {
                        "scenario": result["id"],
                        "check": check["id"],
                        "detail": check["detail"],
                    }
                )
                continue
            for spec in specs:
                bucket = changes.setdefault(
                    spec["id"],
                    {
                        "id": spec["id"],
                        "op": spec["op"],
                        "rationale": spec["rationale"],
                        "evidence": [],
                    },
                )
                if spec["op"] == "set_rule":
                    bucket["rule"] = spec["rule"]
                    bucket["value"] = spec["value"]
                elif spec["op"] == "remove_tool":
                    bucket["tool"] = spec["tool"]
                bucket["evidence"].append(
                    {
                        "scenario": result["id"],
                        "check": check["id"],
                        "detail": check["detail"],
                    }
                )
    return {
        "from_version": from_version,
        "source": "failed eval checks",
        "changes": list(changes.values()),
        "unmapped_failures": unmapped,
    }
