"""Codex refusal and plan evidence, independent of export policy."""

import re
from typing import Final

from session_log_extractors.codex_questions import USER_REFUSAL_PREFIX
from session_log_extractors.json_types import JsonValue
from session_log_extractors.session_model import (
    AuthorshipSource,
    DecisionActivityReference,
    DecisionEvidence,
    DeniedAction,
    Message,
    ObservedPlanDecision,
    PermissionRefusal,
    PlanOutcome,
    PlanVersion,
    PlanVersionKind,
    RefusalSource,
    Session,
    ToolCall,
    ToolResult,
)

_PLAN = re.compile(r"\s*<proposed_plan>(.*?)</proposed_plan>\s*", re.DOTALL)
_GENERIC_DENIAL: Final = "Permission for this tool use was denied."
_SETTINGS_DENIAL: Final = "rejected by user approval settings"
_PROCESS: Final = "exec_command failed: CreateProcess"
_REJECTED: Final = "rejected by user"
_EXTERNAL = re.compile(r"^\[external_agent_tool_result[^\]]*\]\s*", re.IGNORECASE)
_FEEDBACK = re.compile(
    r"the user said:\s*\n?(.*?)(?:\n\nNote:|\Z)", re.DOTALL | re.IGNORECASE
)
_APPROVAL: Final = "Implement the plan."
_REJECTION: Final = "Reject the plan."


def parse_permission_denial(text: JsonValue) -> str | None:
    """Return legacy candidate feedback, empty for denial without feedback."""
    if not isinstance(text, str):
        return None
    stripped = _EXTERNAL.sub("", text).lstrip()
    if not stripped.startswith((USER_REFUSAL_PREFIX, _GENERIC_DENIAL)):
        return None
    feedback = _FEEDBACK.search(stripped)
    return feedback.group(1).strip() if feedback else ""


def is_process_rejection(result: object) -> bool:
    """Recognize the older CreateProcess rejection, excluding cancellations."""
    return (
        isinstance(result, str)
        and _PROCESS in result
        and _REJECTED in result.lower()
        and _SETTINGS_DENIAL not in result
    )


def _result_text(value: JsonValue) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _denial_variant(text: str) -> tuple[str, RefusalSource] | None:
    text = _EXTERNAL.sub("", text).lstrip()
    if _SETTINGS_DENIAL in text:
        return _SETTINGS_DENIAL, RefusalSource.SETTINGS
    if text.startswith(USER_REFUSAL_PREFIX):
        return USER_REFUSAL_PREFIX, RefusalSource.CANDIDATE
    if text.startswith(_GENERIC_DENIAL):
        return _GENERIC_DENIAL, RefusalSource.UNKNOWN
    if is_process_rejection(text):
        return _PROCESS, RefusalSource.CANDIDATE
    return None


def _refusal(
    value: JsonValue,
    activity: DecisionActivityReference | None,
    call: ToolCall | None,
    ref: str,
    field: str = "output",
) -> PermissionRefusal | None:
    text = _result_text(value)
    variant = _denial_variant(text)
    if variant is None:
        return None
    native_variant, source = variant
    refusal = PermissionRefusal(
        ref=ref,
        action=None,
        source=source,
        native_variant=native_variant,
        evidence=[DecisionEvidence(field=field, value=value)],
        activity=activity,
        explanation=text,
    )
    if call is not None:
        refusal["action"] = DeniedAction(
            description=call["name"], tool_name=call["name"]
        )
    feedback = _FEEDBACK.search(text)
    if source == RefusalSource.CANDIDATE and feedback is not None:
        refusal["candidate_feedback"] = feedback.group(1).strip()
        refusal["explanation"] = text[: feedback.start()].rstrip()
    return refusal


def _activity(message: Message) -> DecisionActivityReference:
    evidence = [DecisionEvidence(field="message.text", value=message.get("text"))]
    if "id" in message:
        evidence.append(DecisionEvidence(field="message.id", value=message["id"]))
    return DecisionActivityReference(message_i=message["i"], evidence=evidence)


def _plan_facts(message: Message) -> None:
    text = message.get("text") or ""
    activity = _activity(message)
    match = _PLAN.fullmatch(text) if message["role"] == "assistant" else None
    if match is not None:
        message.setdefault("plan_versions", []).append(
            PlanVersion(
                ref=f"codex:message/{message['i']}/version",
                kind=PlanVersionKind.PROPOSED,
                text=match.group(1),
                evidence=activity["evidence"],
                activity=activity,
            )
        )
    statement, separator, feedback = text.partition("\n")
    if message["role"] != "user" or statement not in (_APPROVAL, _REJECTION):
        return
    # Exact explicit statements only. Ordinary feedback, mode transitions and
    # silence do not establish a decision. The statement names no version ID.
    spans = message.get("authorship", [])
    if not spans or any(span["source"] != AuthorshipSource.CANDIDATE for span in spans):
        return
    decision = ObservedPlanDecision(
        ref=f"codex:message/{message['i']}/decision",
        outcome=(
            PlanOutcome.APPROVED if statement == _APPROVAL else PlanOutcome.REJECTED
        ),
        native_decision=text,
        version=None,
        presentation=None,
        evidence=activity["evidence"],
        activity=activity,
    )
    if separator and feedback:
        decision["candidate_feedback"] = feedback
    message.setdefault("plan_decisions", []).append(decision)


def _result_refusal(
    result: ToolResult,
    call: ToolCall | None,
    activity: DecisionActivityReference | None,
    ref: str,
) -> None:
    refusal = _refusal(
        result.get("content"),
        activity,
        call,
        ref,
    )
    if refusal is not None:
        if "id" in result:
            refusal["native_id"] = result["id"]
        target = call if call is not None else result
        target.setdefault("permission_refusals", []).append(refusal)


def populate_decision_facts(session: Session) -> None:
    """Attach facts to canonical activity before the shared exporter copies it."""
    for message in session["events"]:
        _plan_facts(message)
        for index, call in enumerate(message.get("tool_calls") or []):
            result = call.get("result_record")
            if result is None:
                continue
            activity = DecisionActivityReference(
                message_i=message["i"],
                tool_call_index=index,
                evidence=[
                    DecisionEvidence(field="call_id", value=result["tool_call_id"])
                ],
            )
            _result_refusal(
                result,
                call,
                activity,
                f"codex:message/{message['i']}/call/{index}/refusal",
            )
        if "permission_denial" in message:
            refusal = _refusal(
                message.get("text"),
                _activity(message),
                None,
                f"codex:message/{message['i']}/refusal",
                field="message.text",
            )
            if refusal is not None:
                message.setdefault("permission_refusals", []).append(refusal)
    for index, result in enumerate(session.get("unmatched_tool_results", [])):
        _result_refusal(result, None, None, f"codex:unmatched/{index}/refusal")
