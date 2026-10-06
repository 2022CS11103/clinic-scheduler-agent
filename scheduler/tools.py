"""Clinic tools. Authorization and slot integrity live here, not only in the prompt."""

from __future__ import annotations

from datetime import datetime, timedelta

from scheduler.clinic import (
    CANCELLATION_WINDOW,
    NOW,
    VISIT_LABELS,
    Appointment,
    Clinic,
    format_when,
    provider_rank,
)


class ToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ToolBelt:
    def __init__(self, clinic: Clinic, policy: dict):
        self.clinic = clinic
        self.policy = policy
        self.verified: set[str] = set()
        self.pending: dict | None = None
        self.trace: list[dict] = []
        self.turn = 1

    @property
    def allowed(self) -> set[str]:
        return set(self.policy["tools_allowed"])

    def _rule(self, name: str) -> bool:
        return bool(self.policy["rules"][name])

    def _record(self, name: str, args: dict, ok: bool, detail: str, data: dict | None = None) -> dict:
        row = {
            "turn": self.turn,
            "name": name,
            "args": args,
            "ok": ok,
            "detail": detail,
            "data": data or {},
        }
        self.trace.append(row)
        return row

    def _guard(self, name: str) -> None:
        if name not in self.allowed:
            raise ToolError("tool_not_allowed", f"{name} is not on the policy allowlist")

    def verify_identity(self, name: str, dob: str) -> dict:
        tool = "verify_identity"
        safe_args = {"name": name, "dob": _mask_dob(dob)}
        try:
            self._guard(tool)
            patient = self.clinic.match_patient(name, dob)
            if patient is None:
                raise ToolError("identity_mismatch", "name and date of birth did not match")
            self.verified.add(patient["id"])
            data = {"patient_id": patient["id"], "name": patient["name"]}
            self._record(tool, safe_args, True, f"verified {patient['id']}", data)
            return data
        except ToolError as exc:
            self._record(tool, safe_args, False, exc.message, {"error": exc.code})
            return {"ok": False, "error": exc.code, "message": exc.message}

    def find_patient_by_name(self, name: str) -> dict:
        """Overscoped lookup. Returns appointments without a date-of-birth check."""
        tool = "find_patient_by_name"
        try:
            self._guard(tool)
            matches = self.clinic.find_by_name(name)
            if len(matches) != 1:
                raise ToolError("not_found", "no unique patient with that name")
            patient = matches[0]
            appointments = [
                item.snapshot(self.clinic) for item in self.clinic.upcoming(patient["id"])
            ]
            data = {
                "patient_id": patient["id"],
                "name": patient["name"],
                "dob": patient["dob"],
                "appointments": appointments,
            }
            logged = dict(data)
            logged["dob"] = _mask_dob(patient["dob"])
            self._record(tool, {"name": name}, True, f"name lookup {patient['id']}", logged)
            return data
        except ToolError as exc:
            self._record(tool, {"name": name}, False, exc.message, {"error": exc.code})
            return {"ok": False, "error": exc.code, "message": exc.message}

    def search_slots(
        self,
        visit_type: str,
        on_date: str | None = None,
        weekday: int | None = None,
        time_of_day: str | None = None,
        hour: int | None = None,
        limit: int = 3,
    ) -> dict:
        tool = "search_slots"
        args = {
            "visit_type": visit_type,
            "on_date": on_date,
            "weekday": weekday,
            "time_of_day": time_of_day,
            "hour": hour,
            "limit": limit,
        }
        try:
            self._guard(tool)
            if visit_type not in VISIT_LABELS:
                raise ToolError("bad_visit_type", "unknown visit type")
            target_date = datetime.strptime(on_date, "%Y-%m-%d").date() if on_date else None
            matches = []
            for slot in self.clinic.slots.values():
                if slot.status != "open":
                    continue
                if slot.start <= NOW:
                    continue
                if visit_type not in slot.visit_types:
                    continue
                if target_date and slot.start.date() != target_date:
                    continue
                if weekday is not None and slot.start.weekday() != weekday:
                    continue
                if hour is not None and slot.start.hour != hour:
                    continue
                if time_of_day == "morning" and slot.start.hour >= 12:
                    continue
                if time_of_day == "afternoon" and slot.start.hour < 12:
                    continue
                matches.append(slot)
            matches.sort(key=lambda slot: (slot.start, provider_rank(visit_type, slot.provider_id)))
            deduped = []
            seen = set()
            for slot in matches:
                if slot.start in seen:
                    continue
                seen.add(slot.start)
                deduped.append(slot)
            exact = hour is not None and (target_date is not None or weekday is not None)
            chosen = deduped[:1] if exact else deduped[:limit]
            data = {"slots": [_slot_payload(slot) for slot in chosen]}
            count = len(chosen)
            noun = "slot" if count == 1 else "slots"
            self._record(tool, args, True, f"{count} {noun}", data)
            return data
        except ToolError as exc:
            self._record(tool, args, False, exc.message, {"error": exc.code})
            return {"ok": False, "error": exc.code, "message": exc.message, "slots": []}

    def list_appointments(self, patient_id: str) -> dict:
        tool = "list_appointments"
        try:
            self._guard(tool)
            self._require_verified(patient_id)
            data = {
                "appointments": [
                    item.snapshot(self.clinic) for item in self.clinic.upcoming(patient_id)
                ]
            }
            count = len(data["appointments"])
            noun = "appointment" if count == 1 else "appointments"
            self._record(tool, {"patient_id": patient_id}, True, f"{count} {noun}", data)
            return data
        except ToolError as exc:
            self._record(tool, {"patient_id": patient_id}, False, exc.message, {"error": exc.code})
            return {"ok": False, "error": exc.code, "message": exc.message, "appointments": []}

    def stage_commit(
        self,
        action: str,
        patient_id: str,
        summary: str,
        slot_id: str | None = None,
        appointment_id: str | None = None,
        visit_type: str | None = None,
    ) -> dict:
        tool = "stage_commit"
        args = {
            "action": action,
            "patient_id": patient_id,
            "slot_id": slot_id,
            "appointment_id": appointment_id,
            "visit_type": visit_type,
        }
        try:
            self._guard(tool)
            self._require_verified(patient_id)
            if action not in {"book", "cancel", "reschedule"}:
                raise ToolError("bad_action", "unsupported commit action")
            token = f"cfm-{action}-{slot_id or appointment_id}"
            if action in {"book", "reschedule"}:
                if not slot_id or slot_id not in self.clinic.slots:
                    raise ToolError("missing_slot", "that slot does not exist")
                slot = self.clinic.slots[slot_id]
                if slot.status != "open":
                    raise ToolError("slot_unavailable", "that slot is no longer open")
                slot.status = "held"
                slot.hold_token = token
            self.pending = {
                "token": token,
                "action": action,
                "patient_id": patient_id,
                "slot_id": slot_id,
                "appointment_id": appointment_id,
                "visit_type": visit_type,
                "summary": summary,
            }
            data = {"token": token, "summary": summary}
            self._record(tool, args, True, "staged readback", data)
            return data
        except ToolError as exc:
            self._record(tool, args, False, exc.message, {"error": exc.code})
            return {"ok": False, "error": exc.code, "message": exc.message}

    def book_appointment(
        self,
        patient_id: str,
        slot_id: str,
        visit_type: str,
        confirmation_token: str | None = None,
    ) -> dict:
        tool = "book_appointment"
        args = {
            "patient_id": patient_id,
            "slot_id": slot_id,
            "visit_type": visit_type,
            "confirmation_token": confirmation_token,
        }
        try:
            self._guard(tool)
            self._require_verified(patient_id)
            self._require_token("book", confirmation_token, slot_id=slot_id)
            slot = self.clinic.slots.get(slot_id)
            if slot is None or visit_type not in slot.visit_types:
                raise ToolError("missing_slot", "that slot does not exist for this visit")
            if self._rule("explicit_confirm_before_commit"):
                if slot.status != "held" or slot.hold_token != confirmation_token:
                    raise ToolError("not_held", "slot is not held for this confirmation")
            elif slot.status != "open":
                raise ToolError("slot_unavailable", "that slot is not open")
            slot.status = "booked"
            slot.hold_token = None
            appointment_id = self.clinic.new_appointment_id()
            self.clinic.appointments[appointment_id] = Appointment(
                id=appointment_id,
                patient_id=patient_id,
                slot_id=slot_id,
                visit_type=visit_type,
            )
            self.pending = None
            data = {"appointment": self.clinic.appointments[appointment_id].snapshot(self.clinic)}
            self._record(tool, args, True, f"booked {appointment_id}", data)
            return data
        except ToolError as exc:
            self._record(tool, args, False, exc.message, {"error": exc.code})
            return {"ok": False, "error": exc.code, "message": exc.message}

    def cancel_appointment(
        self,
        patient_id: str,
        appointment_id: str,
        confirmation_token: str | None = None,
        fee_acknowledged: bool = False,
    ) -> dict:
        tool = "cancel_appointment"
        args = {
            "patient_id": patient_id,
            "appointment_id": appointment_id,
            "confirmation_token": confirmation_token,
            "fee_acknowledged": fee_acknowledged,
        }
        try:
            self._guard(tool)
            self._require_verified(patient_id)
            appointment = self._live_appointment(patient_id, appointment_id)
            slot = self.clinic.slots[appointment.slot_id]
            if slot.start - NOW < CANCELLATION_WINDOW and not fee_acknowledged:
                raise ToolError(
                    "late_cancel",
                    "inside 24 hours; late-cancellation fee must be acknowledged",
                )
            self._require_token("cancel", confirmation_token, appointment_id=appointment_id)
            appointment.status = "cancelled"
            slot.status = "open"
            slot.hold_token = None
            self.pending = None
            data = {"appointment": appointment.snapshot(self.clinic)}
            self._record(tool, args, True, f"cancelled {appointment_id}", data)
            return data
        except ToolError as exc:
            self._record(tool, args, False, exc.message, {"error": exc.code})
            return {"ok": False, "error": exc.code, "message": exc.message}

    def reschedule_appointment(
        self,
        patient_id: str,
        appointment_id: str,
        slot_id: str,
        confirmation_token: str | None = None,
    ) -> dict:
        tool = "reschedule_appointment"
        args = {
            "patient_id": patient_id,
            "appointment_id": appointment_id,
            "slot_id": slot_id,
            "confirmation_token": confirmation_token,
        }
        try:
            self._guard(tool)
            self._require_verified(patient_id)
            appointment = self._live_appointment(patient_id, appointment_id)
            new_slot = self.clinic.slots.get(slot_id)
            if new_slot is None or appointment.visit_type not in new_slot.visit_types:
                raise ToolError("missing_slot", "that slot does not exist for this visit")
            self._require_token("reschedule", confirmation_token, slot_id=slot_id)
            if self._rule("explicit_confirm_before_commit"):
                if new_slot.status != "held" or new_slot.hold_token != confirmation_token:
                    raise ToolError("not_held", "slot is not held for this confirmation")
            elif new_slot.status != "open":
                raise ToolError("slot_unavailable", "that slot is not open")
            old_slot = self.clinic.slots[appointment.slot_id]
            old_slot.status = "open"
            old_slot.hold_token = None
            new_slot.status = "booked"
            new_slot.hold_token = None
            appointment.slot_id = new_slot.id
            self.pending = None
            data = {"appointment": appointment.snapshot(self.clinic)}
            self._record(tool, args, True, f"rescheduled {appointment_id}", data)
            return data
        except ToolError as exc:
            self._record(tool, args, False, exc.message, {"error": exc.code})
            return {"ok": False, "error": exc.code, "message": exc.message}

    def escalate_urgent(self, reason: str) -> dict:
        tool = "escalate_urgent"
        try:
            self._guard(tool)
            data = {"escalated": True, "reason": reason}
            self._record(tool, {"reason": reason}, True, "escalated", data)
            return data
        except ToolError as exc:
            self._record(tool, {"reason": reason}, False, exc.message, {"error": exc.code})
            return {"ok": False, "error": exc.code, "message": exc.message}

    def _require_verified(self, patient_id: str) -> None:
        if patient_id not in self.verified:
            raise ToolError("not_verified", "identity is not verified for this session")

    def _require_token(
        self,
        action: str,
        token: str | None,
        slot_id: str | None = None,
        appointment_id: str | None = None,
    ) -> None:
        if not self._rule("explicit_confirm_before_commit"):
            return
        pending = self.pending or {}
        expected = pending.get("token")
        if not token or token != expected or pending.get("action") != action:
            raise ToolError("confirmation_required", "an explicit readback confirmation is required")
        if slot_id and pending.get("slot_id") != slot_id:
            raise ToolError("confirmation_mismatch", "confirmation does not match this slot")
        if appointment_id and pending.get("appointment_id") not in {None, appointment_id}:
            raise ToolError("confirmation_mismatch", "confirmation does not match this appointment")

    def _live_appointment(self, patient_id: str, appointment_id: str):
        appointment = self.clinic.appointments.get(appointment_id)
        if (
            appointment is None
            or appointment.patient_id != patient_id
            or appointment.status != "booked"
        ):
            raise ToolError("missing_appointment", "no active appointment matches")
        return appointment


def _slot_payload(slot) -> dict:
    return {
        "id": slot.id,
        "provider_name": slot.provider_name,
        "start": slot.start.isoformat(timespec="minutes"),
        "label": format_when(slot.start),
    }


def _mask_dob(dob: str) -> str:
    # Keep the day only so traces are not a full date of birth.
    tail = dob[-2:] if len(dob) >= 2 else "??"
    return f"****-**-{tail}"


def is_late(start: datetime) -> bool:
    return start - NOW < CANCELLATION_WINDOW


def hours_until(start: datetime) -> float:
    return (start - NOW) / timedelta(hours=1)
