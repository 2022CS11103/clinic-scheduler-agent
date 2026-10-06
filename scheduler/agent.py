"""Tool-using scheduler. Behavior changes only when the policy changes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from scheduler.clinic import VISIT_LABELS, WEEKDAY_NAMES
from scheduler.nlu import Parsed, next_date_for_weekday, parse
from scheduler.tools import ToolBelt, is_late


@dataclass
class AgentTurn:
    text: str
    ask: str | None = None
    done: bool = False


@dataclass
class Session:
    turn: int = 0
    name: str | None = None
    dob: str | None = None
    caller_is_patient: bool | None = None
    intent: str | None = None
    visit_type: str | None = None
    red_flags: list[str] = field(default_factory=list)
    advice: bool = False
    decline_scheduling: bool = False
    weekday: int | None = None
    hour: int | None = None
    on_date: object | None = None
    time_of_day: str | None = None
    verified: bool = False
    patient_id: str | None = None
    offered: list[dict] = field(default_factory=list)
    fee_pending: bool = False
    fee_acknowledged: bool = False
    cancel_target: str | None = None
    reschedule_target: str | None = None
    reschedule_from_label: str | None = None
    events: list[dict] = field(default_factory=list)


def _article(phrase: str) -> str:
    return "an" if phrase[:1].lower() in "aeiou" else "a"


POLICY_SENTENCE = (
    "Appointments cancelled with at least 24 hours' notice have no fee. "
    "Inside 24 hours, a late-cancellation fee may apply."
)


class Agent:
    def __init__(self, policy: dict, tools: ToolBelt):
        self.policy = policy
        self.tools = tools
        self.session: Session | None = None

    def handle(self, session: Session, user_text: str) -> AgentTurn:
        self.session = session
        session.turn += 1
        self.tools.turn = session.turn
        parsed = parse(user_text)
        session.events.append({"turn": session.turn, "kind": "user", "text": user_text})
        offered_before = list(session.offered)
        self._merge(session, parsed)

        if session.fee_pending and parsed.fee_ack:
            session.fee_acknowledged = True
            session.fee_pending = False
            return self._finish_cancel(session)

        if self.tools.pending and parsed.affirmative:
            return self._commit_pending(session)

        if session.red_flags and self._rule("escalate_red_flags"):
            return self._escalate(session)

        if session.intent == "advice":
            return self._refuse_advice()

        if session.caller_is_patient is False and self._rule("refuse_unauthorized_proxy"):
            return self._refuse_proxy()

        needs_identity = self._ensure_identity(session)
        if needs_identity is not None:
            return needs_identity

        if session.intent == "lookup":
            return self._lookup(session)
        if session.intent == "cancel":
            return self._cancel(session)
        if session.intent == "reschedule":
            return self._reschedule(session, offered_before, parsed)
        if session.intent == "book" or session.visit_type:
            return self._schedule_new(session, offered_before, parsed)
        return self._reply(
            "I can book, reschedule, cancel, or look up an appointment. Which do you want?",
            ask="intent",
        )

    def _rule(self, name: str) -> bool:
        return bool(self.policy["rules"][name])

    def _merge(self, session: Session, parsed: Parsed) -> None:
        if parsed.name and not session.name:
            session.name = parsed.name
        if parsed.dob and not session.dob:
            session.dob = parsed.dob
        if parsed.caller_is_patient is not None:
            session.caller_is_patient = parsed.caller_is_patient
        if parsed.intent and not session.intent:
            session.intent = parsed.intent
        if parsed.visit_type and not session.visit_type:
            session.visit_type = parsed.visit_type
        for flag in parsed.red_flags:
            if flag not in session.red_flags:
                session.red_flags.append(flag)
        if parsed.advice:
            session.advice = True
        if parsed.decline_scheduling:
            session.decline_scheduling = True
        if parsed.weekday is not None:
            session.weekday = parsed.weekday
        if parsed.hour is not None:
            session.hour = parsed.hour
        if parsed.on_date is not None:
            session.on_date = parsed.on_date
        if parsed.time_of_day:
            session.time_of_day = parsed.time_of_day

    def _reply(self, text: str, ask: str | None = None, done: bool = False) -> AgentTurn:
        session = self.session
        assert session is not None
        session.events.append(
            {"turn": session.turn, "kind": "agent", "text": text, "ask": ask, "done": done}
        )
        return AgentTurn(text=text, ask=ask, done=done)

    def _escalate(self, session: Session) -> AgentTurn:
        reason = ", ".join(session.red_flags) or "emergency symptoms"
        result = self.tools.escalate_urgent(reason)
        if result.get("error"):
            return self._reply(
                "Please call 911 or go to the nearest emergency department now.",
                done=True,
            )
        return self._reply(
            "Those symptoms can be an emergency. I am not booking this as a routine appointment. "
            "Please call 911 or go to the nearest emergency department now.",
            done=True,
        )

    def _refuse_advice(self) -> AgentTurn:
        return self._reply(
            "I can't give medical advice or suggest antibiotics. "
            "A clinician has to evaluate a rash in person. I have not scheduled anything.",
            done=True,
        )

    def _refuse_proxy(self) -> AgentTurn:
        return self._reply(
            "I can only do that with the patient. I can't share appointment details with a family member on this call.",
            done=True,
        )

    def _ensure_identity(self, session: Session) -> AgentTurn | None:
        if session.verified:
            return None

        chart_request = session.intent in {"lookup", "cancel", "reschedule"}
        if (
            chart_request
            and not self._rule("verify_before_phi")
            and session.name
            and not session.dob
            and "find_patient_by_name" in self.tools.allowed
        ):
            found = self.tools.find_patient_by_name(session.name)
            if found.get("error"):
                return self._reply("I couldn't find a unique chart under that name.", done=True)
            session.patient_id = found["patient_id"]
            if session.intent == "lookup":
                return self._speak_appointments(found.get("appointments") or [])

        if session.caller_is_patient is False and self._rule("refuse_unauthorized_proxy"):
            return self._refuse_proxy()
        if session.caller_is_patient is None and self._rule("refuse_unauthorized_proxy"):
            return self._reply("Are you the patient yourself?", ask="is_patient")
        if not session.name:
            return self._reply("What is the patient's full name?", ask="name")
        if not session.dob:
            return self._reply(
                "Please tell me the patient's date of birth so I can verify identity before I open the chart.",
                ask="dob",
            )

        result = self.tools.verify_identity(session.name, session.dob)
        if result.get("error"):
            return self._reply(
                "That name and date of birth don't match our records. Please check both and try again.",
                ask="identity",
            )
        session.verified = True
        session.patient_id = result["patient_id"]
        return None

    def _lookup(self, session: Session) -> AgentTurn:
        result = self.tools.list_appointments(session.patient_id)
        if result.get("error"):
            return self._reply("I couldn't open that chart.", done=True)
        return self._speak_appointments(result.get("appointments") or [])

    def _speak_appointments(self, appointments: list[dict]) -> AgentTurn:
        if not appointments:
            return self._reply("There are no upcoming appointments on that chart.", done=True)
        parts = [
            f"{item['visit_label']} on {item['label']} with {item['provider_name']}"
            for item in appointments
        ]
        return self._reply("Here is what is on the chart: " + "; ".join(parts) + ".", done=True)

    def _existing(self, session: Session) -> list[dict]:
        if not session.verified or not session.patient_id:
            return []
        result = self.tools.list_appointments(session.patient_id)
        if result.get("error"):
            return []
        return list(result.get("appointments") or [])

    def _schedule_new(self, session: Session, offered_before: list[dict], parsed: Parsed) -> AgentTurn:
        if session.red_flags and not session.visit_type:
            session.visit_type = "sick_visit"
        if not session.visit_type:
            return self._reply(
                "What kind of visit is this: an annual physical, a sick visit, or a follow-up?",
                ask="intent",
            )
        existing = self._existing(session)
        if session.red_flags and not self._rule("escalate_red_flags"):
            slots = self._search(session, existing)
            if not slots:
                return self._reply("I don't have an opening, and I have not booked anything.", done=True)
            return self._commit_book(session, slots[0], existing)

        chosen = self._choose_from_offer(offered_before, parsed)
        if offered_before and chosen is not None:
            return self._commit_book(session, chosen, existing)
        if offered_before and self._tried_choice(parsed):
            return self._reply(
                "I didn't catch which opening you want. Reply with a number, like 1.",
                ask="slot_choice",
            )

        slots = self._search(session, existing)
        if not slots:
            return self._recover_empty(session, existing)
        if self._is_specific(session) and len(slots) == 1:
            return self._commit_book(session, slots[0], existing)
        if not self._is_specific(session) and not self._rule("clarify_ambiguous_requests"):
            return self._commit_book(session, slots[0], existing)
        return self._offer(session, slots, existing, purpose="book")

    def _reschedule(self, session: Session, offered_before: list[dict], parsed: Parsed) -> AgentTurn:
        existing = self._existing(session)
        if not existing:
            return self._reply("There isn't an upcoming appointment to reschedule.", done=True)
        current = existing[0]
        session.reschedule_target = current["id"]
        session.reschedule_from_label = f"{current['visit_label']} on {current['label']}"
        if not session.visit_type:
            session.visit_type = current["visit_type"]

        chosen = self._choose_from_offer(offered_before, parsed)
        if offered_before and chosen is not None:
            return self._commit_reschedule(session, chosen, current)
        if offered_before and self._tried_choice(parsed):
            return self._reply(
                "I didn't catch which opening you want. Reply with a number, like 1.",
                ask="slot_choice",
            )
        slots = self._search(session, existing)
        if not slots:
            return self._recover_empty(session, existing)
        if self._is_specific(session) and len(slots) == 1:
            return self._commit_reschedule(session, slots[0], current)
        return self._offer(session, slots, existing, purpose="reschedule")

    def _cancel(self, session: Session) -> AgentTurn:
        existing = self._existing(session)
        if not existing:
            return self._reply("There isn't an upcoming appointment to cancel.", done=True)
        target = existing[0]
        if session.cancel_target:
            match = [item for item in existing if item["id"] == session.cancel_target]
            target = match[0] if match else existing[0]
        session.cancel_target = target["id"]
        start = datetime.fromisoformat(target["start"])
        if is_late(start) and not session.fee_acknowledged:
            session.fee_pending = True
            return self._reply(
                f"Your {target['visit_label']} is {target['label']}. "
                "Inside 24 hours, a late-cancellation fee may apply. "
                "Do you still want me to cancel it?",
                ask="fee_ack",
            )
        return self._finish_cancel(session)

    def _finish_cancel(self, session: Session) -> AgentTurn:
        existing = self._existing(session)
        target = next(item for item in existing if item["id"] == session.cancel_target)
        summary = f"cancel the {target['visit_label']} on {target['label']}"
        if self._rule("explicit_confirm_before_commit") and not self._pending_is("cancel"):
            staged = self.tools.stage_commit(
                action="cancel",
                patient_id=session.patient_id,
                summary=summary,
                appointment_id=target["id"],
            )
            if staged.get("error") or "token" not in staged:
                return self._reply("I couldn't prepare that cancellation.", done=True)
            text = f"Shall I {summary}?"
            if self._rule("state_cancellation_policy"):
                text += " " + POLICY_SENTENCE
            return self._reply(text, ask="confirm")

        token = (self.tools.pending or {}).get("token")
        result = self.tools.cancel_appointment(
            patient_id=session.patient_id,
            appointment_id=target["id"],
            confirmation_token=token,
            fee_acknowledged=session.fee_acknowledged,
        )
        if result.get("error"):
            return self._reply(f"I couldn't cancel that: {result['message']}", done=True)
        text = f"I've cancelled the {target['visit_label']} on {target['label']}."
        if self._rule("state_cancellation_policy"):
            text += " " + POLICY_SENTENCE
        return self._reply(text, done=True)

    def _commit_pending(self, session: Session) -> AgentTurn:
        pending = self.tools.pending or {}
        action = pending.get("action")
        token = pending.get("token")
        if action == "book":
            result = self.tools.book_appointment(
                patient_id=pending["patient_id"],
                slot_id=pending["slot_id"],
                visit_type=pending["visit_type"],
                confirmation_token=token,
            )
            if result.get("error"):
                return self._reply(f"I couldn't book that: {result['message']}", done=True)
            appointment = result["appointment"]
            visit = appointment["visit_label"]
            return self._reply(
                "You're booked for "
                f"{_article(visit)} {visit} on {appointment['label']} with {appointment['provider_name']}.",
                done=True,
            )
        if action == "cancel":
            result = self.tools.cancel_appointment(
                patient_id=pending["patient_id"],
                appointment_id=pending["appointment_id"],
                confirmation_token=token,
                fee_acknowledged=session.fee_acknowledged,
            )
            if result.get("error"):
                return self._reply(f"I couldn't cancel that: {result['message']}", done=True)
            appointment = result["appointment"]
            text = f"I've cancelled the {appointment['visit_label']} on {appointment['label']}."
            if self._rule("state_cancellation_policy"):
                text += " " + POLICY_SENTENCE
            return self._reply(text, done=True)
        if action == "reschedule":
            result = self.tools.reschedule_appointment(
                patient_id=pending["patient_id"],
                appointment_id=pending["appointment_id"],
                slot_id=pending["slot_id"],
                confirmation_token=token,
            )
            if result.get("error"):
                return self._reply(f"I couldn't reschedule that: {result['message']}", done=True)
            appointment = result["appointment"]
            return self._reply(
                f"I've moved that visit to {appointment['label']} with {appointment['provider_name']}.",
                done=True,
            )
        return self._reply("There's nothing waiting to confirm.", done=True)

    def _commit_book(self, session: Session, slot: dict, existing: list[dict]) -> AgentTurn:
        session.offered = []
        note = self._conflict_note(existing, slot)
        visit = VISIT_LABELS[session.visit_type]
        summary = f"{visit} on {slot['label']} with {slot['provider_name']}"
        if self._rule("explicit_confirm_before_commit"):
            staged = self.tools.stage_commit(
                action="book",
                patient_id=session.patient_id,
                summary=summary,
                slot_id=slot["id"],
                visit_type=session.visit_type,
            )
            if staged.get("error") or "token" not in staged:
                return self._reply("I couldn't hold that opening.", done=True)
            return self._reply(f"{note}Shall I book {_article(summary)} {summary}?", ask="confirm")

        result = self.tools.book_appointment(
            patient_id=session.patient_id,
            slot_id=slot["id"],
            visit_type=session.visit_type,
        )
        if result.get("error"):
            return self._reply(f"I couldn't book that: {result['message']}", done=True)
        text = f"{note}You're booked for {_article(summary)} {summary}. Thanks for confirming."
        if session.red_flags and not self._rule("escalate_red_flags"):
            text += " If this gets worse, call 911."
        return self._reply(text, done=True)

    def _commit_reschedule(self, session: Session, slot: dict, current: dict) -> AgentTurn:
        session.offered = []
        summary = (
            f"move the {session.reschedule_from_label} to {slot['label']} with {slot['provider_name']}"
        )
        if self._rule("explicit_confirm_before_commit"):
            staged = self.tools.stage_commit(
                action="reschedule",
                patient_id=session.patient_id,
                summary=summary,
                slot_id=slot["id"],
                appointment_id=current["id"],
                visit_type=current["visit_type"],
            )
            if staged.get("error") or "token" not in staged:
                return self._reply("I couldn't hold that opening.", done=True)
            return self._reply(f"Shall I {summary}?", ask="confirm")
        result = self.tools.reschedule_appointment(
            patient_id=session.patient_id,
            appointment_id=current["id"],
            slot_id=slot["id"],
        )
        if result.get("error"):
            return self._reply(f"I couldn't reschedule that: {result['message']}", done=True)
        appointment = result["appointment"]
        return self._reply(
            f"I've moved your {session.reschedule_from_label} to {appointment['label']} "
            f"with {appointment['provider_name']}. Thanks for confirming.",
            done=True,
        )

    def _offer(self, session: Session, slots: list[dict], existing: list[dict], purpose: str) -> AgentTurn:
        session.offered = slots
        lines = [
            f"{index}. {slot['label']} with {slot['provider_name']}"
            for index, slot in enumerate(slots, start=1)
        ]
        notes = []
        if purpose == "reschedule" and session.reschedule_from_label:
            current = session.reschedule_from_label
            notes.append(f"Your current visit is {_article(current)} {current}.")
        if self._rule("surface_schedule_conflicts"):
            offer_days = {slot["start"][:10] for slot in slots}
            relevant = [item for item in existing if item["start"][:10] in offer_days]
            if relevant:
                current = relevant[0]
                notes.append(
                    f"You already have a {current['visit_label']} on {current['label']} "
                    f"with {current['provider_name']}."
                )
        lead = " ".join(notes)
        question = "Which should I move it to?" if purpose == "reschedule" else "Which should I use?"
        body = "Here are the openings:\n" + "\n".join(lines) + f"\n{question}"
        text = f"{lead} {body}" if lead else body
        return self._reply(text, ask="slot_choice")

    def _recover_empty(self, session: Session, existing: list[dict]) -> AgentTurn:
        closed = None
        if session.on_date is not None and session.on_date.weekday() >= 5:
            closed = WEEKDAY_NAMES[session.on_date.weekday()]
        elif session.weekday is not None and session.weekday >= 5:
            closed = WEEKDAY_NAMES[session.weekday]
        if not closed:
            return self._reply(
                "I don't have an opening that matches. Tell me another day and I'll look again.",
                ask="preference",
            )
        session.on_date = None
        session.weekday = None
        session.hour = None
        session.time_of_day = None
        slots = self._search(session, existing)
        if not slots:
            return self._reply(
                f"We are not open {closed}, and I have no weekday openings to offer.",
                done=True,
            )
        session.offered = slots
        lines = [
            f"{index}. {slot['label']} with {slot['provider_name']}"
            for index, slot in enumerate(slots, start=1)
        ]
        return self._reply(
            f"We are not open {closed}. The clinic is open Monday through Friday, "
            "9:00 AM to 5:00 PM. Here are the next openings:\n"
            + "\n".join(lines)
            + "\nWhich should I use?",
            ask="slot_choice",
        )

    def _search(self, session: Session, existing: list[dict]) -> list[dict]:
        busy = {item["start"] for item in existing}
        on_date = session.on_date.isoformat() if session.on_date else None
        result = self.tools.search_slots(
            visit_type=session.visit_type,
            on_date=on_date,
            weekday=None if on_date else session.weekday,
            time_of_day=None if session.hour is not None else session.time_of_day,
            hour=session.hour,
            limit=3,
        )
        slots = []
        for slot in result.get("slots") or []:
            if slot["start"] in busy:
                continue
            slots.append(slot)
        return slots

    def _choose_from_offer(self, offered: list[dict], parsed: Parsed) -> dict | None:
        if not offered:
            return None
        if parsed.choice_index is not None and 0 <= parsed.choice_index < len(offered):
            return offered[parsed.choice_index]
        matches = list(offered)
        if parsed.on_date is not None:
            prefix = parsed.on_date.isoformat()
            matches = [slot for slot in matches if slot["start"].startswith(prefix)]
        if parsed.weekday is not None:
            matches = [
                slot
                for slot in matches
                if datetime.fromisoformat(slot["start"]).weekday() == parsed.weekday
            ]
        if parsed.hour is not None:
            matches = [
                slot
                for slot in matches
                if datetime.fromisoformat(slot["start"]).hour == parsed.hour
            ]
        if parsed.time_of_day == "morning":
            matches = [
                slot for slot in matches if datetime.fromisoformat(slot["start"]).hour < 12
            ]
        elif parsed.time_of_day == "afternoon":
            matches = [
                slot for slot in matches if datetime.fromisoformat(slot["start"]).hour >= 12
            ]
        narrowed = (
            parsed.hour is not None
            or parsed.time_of_day is not None
            or parsed.weekday is not None
            or parsed.on_date is not None
        )
        if len(matches) == 1 and narrowed:
            return matches[0]
        return None

    def _conflict_note(self, existing: list[dict], slot: dict) -> str:
        if not self._rule("surface_schedule_conflicts"):
            return ""
        day = slot["start"][:10]
        same_day = [item for item in existing if item["start"][:10] == day]
        if not same_day:
            return ""
        current = same_day[0]
        return (
            f"You already have a {current['visit_label']} on {current['label']} "
            f"with {current['provider_name']}. "
        )

    def _is_specific(self, session: Session) -> bool:
        if session.on_date is None and session.weekday is not None:
            session.on_date = next_date_for_weekday(session.weekday)
        return session.hour is not None or session.on_date is not None

    def _tried_choice(self, parsed: Parsed) -> bool:
        return bool(
            parsed.choice_index is not None
            or parsed.affirmative
            or parsed.hour is not None
            or parsed.time_of_day
            or parsed.weekday is not None
            or parsed.on_date is not None
        )

    def _pending_is(self, action: str) -> bool:
        pending = self.tools.pending or {}
        return pending.get("action") == action
