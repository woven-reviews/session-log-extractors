"""Interpret complete Copilot permission and plan evidence before export policy."""

import json
from dataclasses import dataclass

from session_log_extractors.copilot_protocol import (
    DENIED_BY_USER,
    DENIED_ERROR,
    EXIT_PLAN_MODE,
    HUMAN_RESPONSE,
    HUMAN_RESPONSE_RECORDED,
    PERMISSION_COMPLETED,
    PERMISSION_DENIAL_KINDS,
    PERMISSION_REQUESTED,
    SETTINGS_DENIAL_KINDS,
    SETTINGS_REFUSAL_PREFIX,
    TOOL_EXECUTION_COMPLETE,
    TOOL_EXECUTION_START,
)
from session_log_extractors.json_types import JsonValue, json_value, string_value
from session_log_extractors.session_model import (
    DecisionActivityReference,
    DecisionEvidence,
    DeniedAction,
    ObservedPlanDecision,
    PermissionRefusal,
    PlanOutcome,
    PlanPresentation,
    PlanPresentationAssociation,
    PlanVersion,
    PlanVersionAssociation,
    PlanVersionKind,
    RefusalSource,
    Session,
    ToolCall,
)


def _evidence(field: str, value: JsonValue) -> list[DecisionEvidence]:
    return [DecisionEvidence(field=field, value=value)]


def _scope(
    record: dict[str, JsonValue], identifier: JsonValue
) -> tuple[str | None, str] | None:
    native_id = string_value(identifier)
    if not native_id:
        return None
    return string_value(record.get("agentId")), native_id


@dataclass
class _CallActivity:
    call: ToolCall
    activity: DecisionActivityReference


class _CopilotDecisionInterpreter:
    """Resolve unique, same-agent recorded IDs; retain everything else unassociated."""

    def __init__(self, session: Session, native_session_id: str | None):
        self.session = session
        self.native_session_id = native_session_id
        self.calls: dict[tuple[str | None, str], list[_CallActivity]] = {}
        self.presentations: dict[tuple[str | None, str], list[PlanPresentation]] = {}
        self.refusals: dict[tuple[str | None, str], list[PermissionRefusal]] = {}
        self.requests: dict[tuple[str | None, str], list[dict[str, JsonValue]]] = {}
        # These internal indexes reference the same canonical call facts and
        # also retain records whose ownership could not be established.
        session["permission_refusals"] = []
        session["plan_versions"] = []
        session["plan_presentations"] = []
        session["plan_decisions"] = []
        for event in session["events"]:
            for index, call in enumerate(event.get("tool_calls") or []):
                call_id = call.get("id")
                activity = DecisionActivityReference(
                    message_i=event["i"],
                    tool_call_index=index,
                    evidence=_evidence("tool.input", json_value(call.get("input"))),
                )
                if call_id:
                    activity["evidence"] = _evidence("toolCallId", call_id)
                    scope = call.get("agent_id", event.get("agent_id")), call_id
                    self.calls.setdefault(scope, []).append(
                        _CallActivity(call, activity)
                    )
                self.proposal(call, activity)

    def owner(
        self, record: dict[str, JsonValue], data: dict[str, JsonValue]
    ) -> _CallActivity | None:
        scope = _scope(record, data.get("toolCallId"))
        matches = self.calls.get(scope, []) if scope is not None else []
        return matches[0] if len(matches) == 1 else None

    def plan_owner(
        self, record: dict[str, JsonValue], data: dict[str, JsonValue]
    ) -> _CallActivity | None:
        owner = self.owner(record, data)
        if owner is not None and owner.call["name"] == EXIT_PLAN_MODE:
            return owner
        return None

    def proposal(self, call: ToolCall, activity: DecisionActivityReference) -> None:
        payload = call.get("input")
        if call["name"] != "create" or not isinstance(payload, dict):
            return
        path = string_value(payload.get("path"))
        text = string_value(payload.get("file_text"))
        if path is None or text is None:
            return
        if not self.native_session_id or not path.endswith(
            f"/session-state/{self.native_session_id}/plan.md"
        ):
            return
        self.version(
            text,
            PlanVersionKind.PROPOSED,
            _CallActivity(call, activity),
            _evidence("arguments", json_value(payload)),
        )

    def version(
        self,
        text: str,
        kind: PlanVersionKind,
        owner: _CallActivity | None,
        evidence: list[DecisionEvidence],
    ) -> PlanVersionAssociation:
        versions = self.session["plan_versions"]
        version = PlanVersion(
            ref=f"copilot:v{len(versions)}",
            kind=kind,
            text=text,
            evidence=evidence,
            activity=owner.activity if owner is not None else None,
        )
        if owner is not None:
            owner.call.setdefault("plan_versions", []).append(version)
        versions.append(version)
        return PlanVersionAssociation(version_ref=version["ref"], evidence=evidence)

    def presentation(
        self, record: dict[str, JsonValue], data: dict[str, JsonValue]
    ) -> None:
        if data.get("toolName") != EXIT_PLAN_MODE:
            return
        owner = self.plan_owner(record, data)
        presentations = self.session["plan_presentations"]
        presentation = PlanPresentation(
            ref=f"copilot:p{len(presentations)}",
            version=None,
            evidence=_evidence("tool.execution_start.data", data),
            activity=owner.activity if owner is not None else None,
        )
        if owner is not None:
            owner.call.setdefault("plan_presentations", []).append(presentation)
        presentations.append(presentation)
        scope = _scope(record, data.get("toolCallId"))
        if scope is not None:
            self.presentations.setdefault(scope, []).append(presentation)

    def plan_response(
        self, record: dict[str, JsonValue], data: dict[str, JsonValue]
    ) -> None:
        response = data.get("response")
        if (
            not isinstance(response, dict)
            or response.get("responseKind") != EXIT_PLAN_MODE
        ):
            return
        owner = self.plan_owner(record, data)
        activity = owner.activity if owner is not None else None
        evidence = _evidence("human_response.recorded.data", data)
        version = None
        text = string_value(response.get("planContent"))
        if text is not None:
            version = self.version(text, PlanVersionKind.UNKNOWN, owner, evidence)
        decisions = self.session["plan_decisions"]
        decision = ObservedPlanDecision(
            ref=f"copilot:d{len(decisions)}",
            outcome=_plan_outcome(data, response),
            version=version,
            presentation=self.review(record, data),
            evidence=evidence,
            activity=activity,
        )
        if "approved" in response:
            decision["native_decision"] = response["approved"]
        if data.get("actor") == HUMAN_RESPONSE:
            feedback = string_value(response.get("feedback"))
            if feedback is not None:
                decision["candidate_feedback"] = feedback
        if owner is not None:
            owner.call.setdefault("plan_decisions", []).append(decision)
        decisions.append(decision)

    def review(
        self, record: dict[str, JsonValue], data: dict[str, JsonValue]
    ) -> PlanPresentationAssociation | None:
        scope = _scope(record, data.get("toolCallId"))
        matches = self.presentations.get(scope, []) if scope is not None else []
        if len(matches) != 1:
            return None
        return PlanPresentationAssociation(
            presentation_ref=matches[0]["ref"],
            evidence=_evidence("toolCallId", data.get("toolCallId")),
        )

    def permission(
        self, record: dict[str, JsonValue], data: dict[str, JsonValue]
    ) -> None:
        result = data.get("result")
        if not isinstance(result, dict):
            return
        kind = string_value(result.get("kind"))
        if kind not in PERMISSION_DENIAL_KINDS:
            return
        request = self.permission_request(record, data)
        owner = self.owner(record, data)
        if not string_value(data.get("toolCallId")) and request is not None:
            owner = self.owner(record, request)
        refusal = self.refusal(record, data, kind, owner)
        if request is not None:
            refusal["action"] = _denied_action(request, owner)
            refusal["evidence"].extend(
                _evidence("permission.requested.data.permissionRequest", request)
            )
        elif owner is not None:
            refusal["action"] = _call_action(owner.call)
        _refusal_details(refusal, data, result)

    def permission_request(
        self, record: dict[str, JsonValue], data: dict[str, JsonValue]
    ) -> dict[str, JsonValue] | None:
        scope = _scope(record, data.get("requestId"))
        matches = self.requests.get(scope, []) if scope is not None else []
        if len(matches) != 1:
            return None
        request = matches[0].get("permissionRequest")
        if not isinstance(request, dict):
            return None
        call_id = string_value(data.get("toolCallId"))
        if call_id and request.get("toolCallId") not in (None, call_id):
            return None
        return request

    def refusal(
        self,
        record: dict[str, JsonValue],
        data: dict[str, JsonValue],
        variant: str | None,
        owner: _CallActivity | None,
    ) -> PermissionRefusal:
        refusals = self.session["permission_refusals"]
        refusal = PermissionRefusal(
            ref=f"copilot:f{len(refusals)}",
            action=_call_action(owner.call) if owner else None,
            source=RefusalSource.UNKNOWN,
            native_variant=variant,
            evidence=_evidence(f"{record.get('type')}.data", data),
            activity=owner.activity if owner else None,
        )
        native_id = string_value(record.get("id"))
        if native_id:
            refusal["native_id"] = native_id
        if owner is not None:
            owner.call.setdefault("permission_refusals", []).append(refusal)
        refusals.append(refusal)
        scope = _scope(record, record.get("id"))
        if scope is not None:
            self.refusals.setdefault(scope, []).append(refusal)
        return refusal

    def tool_refusal(
        self, record: dict[str, JsonValue], data: dict[str, JsonValue]
    ) -> None:
        error = data.get("error")
        error = error if isinstance(error, dict) else {}
        text = string_value(error.get("message"))
        if text is None:
            result = data.get("result")
            text = (
                string_value(result.get("content"))
                if isinstance(result, dict)
                else string_value(result)
            )
        settings = text is not None and text.startswith(SETTINGS_REFUSAL_PREFIX)
        if error.get("code") != DENIED_ERROR and not settings:
            return
        refusal = self.parent_refusal(record, data)
        if refusal is None:
            refusal = self.refusal(
                record, data, string_value(error.get("code")), self.owner(record, data)
            )
        else:
            refusal["evidence"].extend(_evidence("tool.execution_complete.data", data))
        if settings:
            refusal["source"] = RefusalSource.SETTINGS
        if text is not None:
            refusal["explanation"] = text

    def parent_refusal(
        self, record: dict[str, JsonValue], data: dict[str, JsonValue]
    ) -> PermissionRefusal | None:
        scope = _scope(record, record.get("parentId"))
        matches = self.refusals.get(scope, []) if scope is not None else []
        if len(matches) != 1:
            return None
        refusal = matches[0]
        native = refusal["evidence"][0]["value"]
        call_id = string_value(data.get("toolCallId"))
        if call_id and isinstance(native, dict) and native.get("toolCallId") == call_id:
            return refusal
        return None

    def index(self, records: list[dict[str, JsonValue]]) -> None:
        """Index native presentations and permission requests independently of order."""
        for record in records:
            data = record.get("data")
            if not isinstance(data, dict):
                continue
            if record.get("type") == TOOL_EXECUTION_START:
                self.presentation(record, data)
            elif record.get("type") == PERMISSION_REQUESTED:
                scope = _scope(record, data.get("requestId"))
                if scope is not None:
                    self.requests.setdefault(scope, []).append(data)


def _refusal_details(
    refusal: PermissionRefusal, data: dict[str, JsonValue], result: dict[str, JsonValue]
) -> None:
    if result.get("kind") in SETTINGS_DENIAL_KINDS:
        refusal["source"] = RefusalSource.SETTINGS
    elif (
        result.get("kind") == DENIED_BY_USER
        and data.get("decisionSource") == HUMAN_RESPONSE
    ):
        refusal["source"] = RefusalSource.CANDIDATE
        feedback = string_value(result.get("feedback"))
        if feedback is not None:
            refusal["candidate_feedback"] = feedback
    explanation = string_value(result.get("message"))
    if explanation is not None:
        refusal["explanation"] = explanation


def _plan_outcome(
    data: dict[str, JsonValue], response: dict[str, JsonValue]
) -> PlanOutcome:
    if data.get("actor") == HUMAN_RESPONSE:
        if response.get("approved") is True:
            return PlanOutcome.APPROVED
        if response.get("approved") is False:
            return PlanOutcome.REJECTED
    return PlanOutcome.UNKNOWN


def _call_action(call: ToolCall) -> DeniedAction:
    payload = call.get("input")
    description = call["name"]
    if isinstance(payload, dict):
        description = string_value(payload.get("command")) or json.dumps(
            payload, ensure_ascii=False
        )
    return DeniedAction(description=description, tool_name=call["name"])


def _denied_action(
    request: dict[str, JsonValue], owner: _CallActivity | None
) -> DeniedAction | None:
    description = (
        string_value(request.get("fullCommandText"))
        or string_value(request.get("intention"))
        or string_value(request.get("fileName"))
        or string_value(request.get("kind"))
    )
    if description is None:
        return _call_action(owner.call) if owner else None
    action = DeniedAction(description=description)
    if owner is not None:
        action["tool_name"] = owner.call["name"]
    return action


def interpret_decisions(
    session: Session,
    records: list[dict[str, JsonValue]],
    native_session_id: str | None,
) -> None:
    """Populate typed facts without I/O, truncation, or inferred edit relationships."""
    interpreter = _CopilotDecisionInterpreter(session, native_session_id)
    interpreter.index(records)
    for record in records:
        data = record.get("data")
        if not isinstance(data, dict):
            continue
        if record.get("type") == HUMAN_RESPONSE_RECORDED:
            interpreter.plan_response(record, data)
        elif record.get("type") == PERMISSION_COMPLETED:
            interpreter.permission(record, data)
        elif record.get("type") == TOOL_EXECUTION_COMPLETE:
            interpreter.tool_refusal(record, data)
