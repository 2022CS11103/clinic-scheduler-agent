# Design note

## Choices

The agent is a tool-using policy executor driven by a versioned JSON policy. Every rule is a constraint, and true is the stricter side. Tools enforce the invariants that should never be optional: a name and date of birth must match before a chart opens, a slot cannot be booked twice, and a cancellation inside 24 hours waits for the fee to be acknowledged.

Policy is where v0 is allowed to be wrong, so the loop has a real failure to repair. v0 may look a patient up by name, commit on the same turn a time "works," book through chest pain, and treat a date of birth as proof the caller is the patient. The other constraints start on, and they are the regression set: vague times get a numbered list, Saturday is refused, an existing same-day visit is named, the late fee is stated, and medical advice is refused.

The judge scores the tool trace and the patient-visible text. It does not use an LLM, because the score has to move for a reason that replays. Two failures are built so a transcript-only judge is blind. The chest-pain reply says "call 911" while `book_appointment` succeeds and `escalate_urgent` never runs. The Tuesday 2:00 PM reply says "Thanks for confirming" with no earlier readback. The simulated patient answers a structured question type so the harness is not glued to phrasing; the judge ignores that channel. A separate test uses only the vague sentence "you have an upcoming visit" and still fails, because `find_patient_by_name` is in the trace.

## Loop

`python -m eval.loop` runs 12 scenarios on `policies/v0.json`. Each failed check maps to a patch. The only legal operations are `set_rule` from false to true, and `remove_tool`. The applier rejects a weaker rule, a new tool, or any other operation, including a free-text prompt rewrite. Turning on identity-before-chart-access also strips `find_patient_by_name`. The loop writes `policies/v1.json`, reruns the same scenarios, and exits non-zero if any check that passed before now fails.

## Scores

Before, policy v0: 8/12 scenarios, 21/27 checks. After, policy v1: 12/12 scenarios, 27/27 checks. Regressions: none.

The four repairs are identity before chart access, a readback on an earlier turn than the commit, escalation instead of a routine book for chest pain, and a refusal when the caller is not the patient. The eight scenarios that already passed still pass.

## A real clinic

Date of birth is not authentication. The spouse scenario shows why: someone else in the household often knows it. Production identity should be a one-time code to the phone already on the chart. This loop also applies v1 by itself so the score move is visible. A clinic would have a person sign that patch before it reached the live agent.

## AI, and where judgment overrode it

An AI coding assistant in Cursor drafted the implementation. The usual next step is a second model that rewrites the system prompt, scored by a third model. That rewrite can drop a rule that was already working, and judge variance makes a before/after number hard to trust. The loop emits a schema-limited patch instead, the tools stay the enforcement layer, and the score is computed from traces so it replays.
