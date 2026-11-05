"""Claude refusal and plan evidence, independent of raw-result export policy."""

import re
from typing import Optional

from session_log_extractors.json_types import JsonValue
from session_log_extractors.session_model import (
    DecisionActivityReference,
    DecisionEvidence,
    DecisionFacts,
    DeniedAction,
    Message,
    ObservedPlanDecision,
    PermissionRefusal,
    PlanOutcome,
    PlanPresentation,
    PlanPresentationAssociation,
    PlanVersion,
    PlanVersionAssociation,
    PlanVersionKind,
    RefusalSource,
    ToolCall,
    ToolResult,
)

PLAN_TOOLS = {"ExitPlanMode", "exit_plan_mode"}
_USER_REJECTED = "user-rejected"
_SETTINGS_RULE = "permission-rule"
_NATIVE_USER_REJECTED = "User rejected tool use"

# When the human denies a permission prompt, the harness feeds the assistant a
# canned tool_result (is_error) whose content opens with one of these preambles.
# An approval leaves no distinct record — the tool just runs — so denials are the
# only permission *decision* we can observe.
_PERMISSION_DENIAL_PREFIXES = (
    "The user doesn't want to proceed with this tool use.",
    "Permission for this tool use was denied.",
)
# Settings-driven denials (e.g. don't-ask mode) name the tool instead. Anchored on
# "has been denied" so reading an ISC LICENSE ("Permission to use, copy…") can't match.
_PERMISSION_DENIED_TOOL = re.compile(
    r"Permission to use \S+(?: with command [^\r\n]+)? has been denied\b"
)
# Optional steering message the human typed when denying, e.g.
# "…the user said:\nWe need a new branch for this". A plan sent back in plan mode
# uses a different lead-in ("…reason for the rejection: …") for the same thing.
# Captured up to the trailing "Note:" hint block (or end of string).
_DENIAL_MESSAGE = re.compile(
    r"(?:the user said|reason for the rejection):\s*\n?(.*?)(?:\n\nNote:|\Z)",
    re.DOTALL | re.IGNORECASE,
)


def parse_permission_denial(content: str) -> Optional[str]:
    """If ``content`` is a permission-denial tool_result, return the human's
    steering message (``""`` if they just denied). Return ``None`` otherwise."""
    if not isinstance(content, str) or not (
        content.startswith(_PERMISSION_DENIAL_PREFIXES)
        or _PERMISSION_DENIED_TOOL.match(content)
    ):
        return None
    m = _DENIAL_MESSAGE.search(content)
    return m.group(1).strip() if m else ""


# An approved plan comes back as a tool_result opening with this preamble. When
# the human edited the plan in the approval dialog, the *edited* text follows an
# "## Approved Plan (edited by user):" heading — that version is what the
# assistant actually works from, so it supersedes the proposed one.
_PLAN_APPROVAL_PREFIX = "User has approved your plan"
_PLAN_EDITED = re.compile(
    r"^##\s*Approved Plan \(edited by user\):\s*\n(.*)\Z", re.DOTALL | re.MULTILINE
)


def _text(content: JsonValue) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                value = block.get("text")
                if isinstance(value, str):
                    parts.append(value)
    return " ".join(parts)


def _refusal_source(record: ToolResult, text: str) -> RefusalSource:
    kind = record.get("native_denial_kind")
    if kind == _SETTINGS_RULE:
        return RefusalSource.SETTINGS
    if kind == _USER_REJECTED:
        return RefusalSource.CANDIDATE
    if kind:
        return RefusalSource.UNKNOWN
    if _PERMISSION_DENIED_TOOL.match(text):
        return RefusalSource.SETTINGS
    if (
        text.startswith(_PERMISSION_DENIAL_PREFIXES)
        or record.get("native_result") == _NATIVE_USER_REJECTED
    ):
        return RefusalSource.CANDIDATE
    return RefusalSource.UNKNOWN


def _denial_variant(kind: str | None, text: str) -> str | None:
    if kind:
        return kind
    for prefix in _PERMISSION_DENIAL_PREFIXES:
        if text.startswith(prefix):
            return prefix
    match = _PERMISSION_DENIED_TOOL.match(text)
    return match.group() if match else None


def _refusal_explanation(text: str) -> str:
    feedback = _DENIAL_MESSAGE.search(text)
    return text[: feedback.start()].rstrip() if feedback else text


def _refusal(
    record: ToolResult,
    call: ToolCall | None,
    activity: DecisionActivityReference | None,
    ref: str,
) -> PermissionRefusal | None:
    text = _text(record.get("content"))
    feedback = parse_permission_denial(text)
    # A native classification alone does not establish an observed refusal.
    if feedback is None and record.get("native_result") != _NATIVE_USER_REJECTED:
        return None
    evidence = [
        DecisionEvidence(field="tool_result.content", value=record.get("content"))
    ]
    kind = record.get("native_denial_kind")
    if kind is not None:
        evidence.append(DecisionEvidence(field="toolDenialKind", value=kind))
    if "native_result" in record:
        evidence.append(
            DecisionEvidence(field="toolUseResult", value=record["native_result"])
        )
    source = _refusal_source(record, text)
    refusal = PermissionRefusal(
        ref=ref,
        action=None,
        source=source,
        native_variant=_denial_variant(kind, text),
        evidence=evidence,
        activity=activity,
        explanation=_refusal_explanation(text),
    )
    if call is not None and activity is not None:
        refusal["action"] = DeniedAction(
            description=call["name"], tool_name=call["name"]
        )
    if source == RefusalSource.CANDIDATE and feedback:
        refusal["candidate_feedback"] = feedback
    return refusal


def _version(
    ref: str,
    text: str,
    kind: PlanVersionKind,
    evidence: list[DecisionEvidence],
    activity: DecisionActivityReference | None,
) -> PlanVersion:
    return PlanVersion(
        ref=ref, text=text, kind=kind, evidence=evidence, activity=activity
    )


def _version_link(version: PlanVersion) -> PlanVersionAssociation:
    return PlanVersionAssociation(
        version_ref=version["ref"],
        evidence=version["evidence"]
        + (version["activity"]["evidence"] if version["activity"] is not None else []),
    )


def _present_plan(
    call: ToolCall, activity: DecisionActivityReference, scope: str
) -> None:
    evidence = activity["evidence"] + [
        DecisionEvidence(field="tool_use.name", value=call["name"])
    ]
    tool_input = call.get("input")
    plan = tool_input.get("plan") if isinstance(tool_input, dict) else None
    version = None
    if isinstance(plan, str) and plan:
        proposed = _version(
            f"{scope}/proposed",
            plan,
            PlanVersionKind.PROPOSED,
            [DecisionEvidence(field="tool_use.input.plan", value=plan)],
            activity,
        )
        call["plan_versions"] = [proposed]
        version = _version_link(proposed)
    record = call.get("result_record")
    if record is not None and record["is_error"]:
        refusal = _refusal(record, call, activity, scope)
        if refusal is None or refusal["source"] != RefusalSource.CANDIDATE:
            return
    call["plan_presentations"] = [
        PlanPresentation(
            ref=f"{scope}/presentation",
            version=version,
            evidence=evidence,
            activity=activity,
        )
    ]


def _edited_version(
    record: ToolResult, activity: DecisionActivityReference | None, scope: str
) -> PlanVersion | None:
    payload = record.get("native_result")
    if isinstance(payload, dict) and payload.get("planWasEdited") is True:
        plan = payload.get("plan")
        if isinstance(plan, str):
            return _version(
                f"{scope}/edited",
                plan,
                PlanVersionKind.EDITED,
                [
                    DecisionEvidence(field="toolUseResult.planWasEdited", value=True),
                    DecisionEvidence(field="toolUseResult.plan", value=plan),
                ],
                activity,
            )
    text = _text(record.get("content"))
    if text.startswith(_PLAN_APPROVAL_PREFIX):
        match = _PLAN_EDITED.search(text)
        if match:
            return _version(
                f"{scope}/edited",
                match.group(1),
                PlanVersionKind.EDITED,
                [
                    DecisionEvidence(
                        field="tool_result.content", value=record.get("content")
                    )
                ],
                activity,
            )
    return None


def _plan_result(
    target: DecisionFacts,
    record: ToolResult,
    activity: DecisionActivityReference | None,
    scope: str,
) -> None:
    text = _text(record.get("content"))
    evidence = [
        DecisionEvidence(field="tool_result.content", value=record.get("content"))
    ]
    refusal = _refusal(record, None, activity, f"{scope}/refusal")
    if (not text and refusal is None) or (
        record["is_error"]
        and (refusal is None or refusal["source"] != RefusalSource.CANDIDATE)
    ):
        return
    outcome = PlanOutcome.UNKNOWN
    if text.startswith(_PLAN_APPROVAL_PREFIX) and not record["is_error"]:
        outcome = PlanOutcome.APPROVED
    elif refusal is not None and refusal["source"] == RefusalSource.CANDIDATE:
        outcome = PlanOutcome.REJECTED
    decision = ObservedPlanDecision(
        ref=f"{scope}/decision",
        outcome=outcome,
        version=None,
        presentation=None,
        evidence=(activity["evidence"] if activity is not None else []) + evidence,
        activity=activity,
        explanation=refusal["explanation"] if refusal is not None else text,
    )
    presentations = target.get("plan_presentations", [])
    if presentations:
        presentation = presentations[0]
        decision["presentation"] = PlanPresentationAssociation(
            presentation_ref=presentation["ref"],
            evidence=presentation["evidence"] + evidence,
        )
    decision["version"] = _reviewed_version(target, record, activity, scope)
    if refusal is not None and "candidate_feedback" in refusal:
        decision["candidate_feedback"] = refusal["candidate_feedback"]
    target.setdefault("plan_decisions", []).append(decision)


def _reviewed_version(
    target: DecisionFacts,
    record: ToolResult,
    activity: DecisionActivityReference | None,
    scope: str,
) -> PlanVersionAssociation | None:
    """Resolve the reviewed content, preserving explicit edits and unknown origins."""
    versions = target.get("plan_versions", [])
    edited = _edited_version(record, activity, scope)
    if edited is not None:
        if versions:
            original = _version_link(versions[0])
            original["evidence"] += edited["evidence"]
            if activity is not None:
                original["evidence"] += activity["evidence"]
            edited["edited_from"] = original
        target.setdefault("plan_versions", []).append(edited)
        return _version_link(edited)
    elif not versions:
        returned = _returned_plan_version(record, scope)
        if returned is not None:
            target["plan_versions"] = [returned]
            return _version_link(returned)
    return _version_link(versions[0]) if versions else None


def _call_decisions(message: Message, index: int, call: ToolCall) -> None:
    scope = f"claude/decision/{message['i']}/call/{index}"
    call_id = call.get("id")
    evidence = [DecisionEvidence(field="tool_use containment", value=index)]
    if call_id:
        evidence.append(DecisionEvidence(field="tool_use.id", value=call_id))
    activity = DecisionActivityReference(
        message_i=message["i"], tool_call_index=index, evidence=evidence
    )
    if call["name"] in PLAN_TOOLS:
        _present_plan(call, activity, scope)
    record = call.get("result_record")
    if record is None:
        return
    result_activity = None
    if call_id and record["tool_call_id"] == call_id:
        result_activity = DecisionActivityReference(
            message_i=message["i"],
            tool_call_index=index,
            evidence=evidence
            + [DecisionEvidence(field="tool_result.tool_use_id", value=call_id)],
        )
    refusal = _refusal(record, call, result_activity, f"{scope}/refusal")
    if refusal is not None:
        call["permission_refusals"] = [refusal]
    if call["name"] in PLAN_TOOLS:
        _plan_result(call, record, result_activity, scope)


def _returned_plan_version(record: ToolResult, scope: str) -> PlanVersion | None:
    """Retain standalone plan content without inventing its proposal or review."""
    payload = record.get("native_result")
    if not isinstance(payload, dict):
        return None
    plan = payload.get("plan")
    if not isinstance(plan, str):
        return None
    edited = _edited_version(record, None, scope)
    return (
        edited
        if edited is not None
        else _version(
            f"{scope}/version",
            plan,
            PlanVersionKind.UNKNOWN,
            [DecisionEvidence(field="toolUseResult.plan", value=plan)],
            None,
        )
    )


def _attach_decisions(events: list[Message]) -> None:
    """Attach evidence where it was recorded; unmatched results stay unmatched."""
    for message in events:
        for index, call in enumerate(message.get("tool_calls") or []):
            _call_decisions(message, index, call)
        for index, record in enumerate(message.get("tool_results", [])):
            scope = f"claude/decision/{message['i']}/result/{index}"
            refusal = _refusal(record, None, None, f"{scope}/refusal")
            if refusal is not None:
                record["permission_refusals"] = [refusal]
            if _text(record.get("content")).startswith(_PLAN_APPROVAL_PREFIX):
                _plan_result(record, record, None, scope)
            else:
                version = _returned_plan_version(record, scope)
                if version is not None:
                    record["plan_versions"] = [version]
