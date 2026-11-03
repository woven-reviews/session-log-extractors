"""Recorded Codex invocation occurrences and explicitly related expansions."""

import re
from typing import Final

from session_log_extractors.json_types import JsonValue, json_value
from session_log_extractors.session_model import (
    ActionActivityReference,
    ActionEvidence,
    AuthorshipSource,
    Invocation,
    InvocationDefinition,
    InvocationKind,
    Message,
    RecordedSkillLink,
    Session,
    ToolCall,
)

_SKILL_LINK = re.compile(r"\[\$([^\]\n]+)\]\(([^)\n]+)\)")
_SKILL_TOKEN = re.compile(r"(?<![\w\[$])\$([A-Za-z0-9][\w:.-]*)")
_SLASH = re.compile(r"^\s*(/[A-Za-z0-9][\w:.-]*)(?:[ \t]+(.*))?$", re.DOTALL)
_COMMAND = re.compile(r"<command-name>\s*(/[^<]+?)\s*</command-name>")
_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_SKILL_TOOLS: Final = frozenset(("skill", "read_skill", "skills.read"))


def _evidence(field: str, value: JsonValue) -> list[ActionEvidence]:
    return [ActionEvidence(field=field, value=value)]


def _invocation(
    message: Message, kind: InvocationKind, name: str | None, index: int
) -> Invocation:
    evidence = _evidence("message.text", message.get("text"))
    invocation = Invocation(
        ref=f"codex:message/{message['i']}/invocation/{index}",
        kind=kind,
        name=name,
        evidence=evidence,
        activity=ActionActivityReference(message_i=message["i"], evidence=evidence),
    )

    if "id" in message:
        invocation["native_id"] = message["id"]
    return invocation


def _instruction_span(message: Message, start: int, end: int) -> bool:
    for span in message.get("authorship", []):
        if span["source"] in (AuthorshipSource.CANDIDATE, AuthorshipSource.UNKNOWN):
            continue
        if span["start"] < end and start < span["end"]:
            return True
    return False


def _command_invocation(message: Message, text: str) -> Invocation | None:
    tagged = _COMMAND.search(text)
    slash = _SLASH.fullmatch(text)
    if not (tagged or slash) or _instruction_span(message, 0, len(text)):
        return None
    name = tagged[1] if tagged else slash[1] if slash else None
    invocation = _invocation(message, InvocationKind.COMMAND, name, 0)
    if slash:
        invocation["slash_text"] = text
        invocation["arguments"] = slash[2] or ""
    if tagged:
        invocation["slash_text"] = tagged[1]
        arguments = _ARGS.search(text)
        if arguments:
            invocation["arguments"] = arguments[1]
    return invocation


def _message_invocations(message: Message) -> list[Invocation]:
    text = message.get("text") or ""
    if message["role"] != "user" or text.lstrip().startswith("<skill>"):
        return []
    command = _command_invocation(message, text)
    if command is not None:
        return [command]
    invocations: list[Invocation] = []
    # A standalone link, definition or path is content, not invocation evidence.
    plain = _SKILL_LINK.sub(lambda link: " " * len(link[0]), text)
    for match in _SKILL_TOKEN.finditer(plain):
        if _instruction_span(message, match.start(), match.end()):
            continue
        invocation = _invocation(
            message, InvocationKind.SKILL, match[1], len(invocations)
        )
        links = []
        for link in _SKILL_LINK.finditer(text):
            if link[1] == match[1]:
                links.append(RecordedSkillLink(syntax=link[0], target=link[2]))
        if links:
            invocation["skill_links"] = links
        invocations.append(invocation)
    return invocations


def _selection_occurrences(
    session: Session, turn_id: str | None, name: str
) -> list[Invocation]:
    matches: list[Invocation] = []
    if turn_id is None:
        return matches
    for owner in session["events"]:
        if owner.get("turn_id") != turn_id:
            continue
        for invocation in owner.get("invocations", []):
            if (
                invocation["kind"] == InvocationKind.SKILL
                and invocation["name"] == name
            ):
                matches.append(invocation)
    return matches


def _selected_expansions(session: Session) -> None:
    for message in session["events"]:
        if message["role"] != "user":
            continue
        selected = message.get("skill_invocation")
        if not isinstance(selected, dict):
            continue
        selected_name, selected_path = selected.get("name"), selected.get("path")
        if not isinstance(selected_name, str) or not isinstance(selected_path, str):
            continue
        turn_id = message.get("turn_id")
        matches = _selection_occurrences(session, turn_id, selected_name)
        if len(matches) == 1 and "definitions" not in matches[0]:
            invocation = matches[0]
            invocation["evidence"].extend(_evidence("selected_skill.turn_id", turn_id))
        else:
            invocation = _invocation(message, InvocationKind.SKILL, selected_name, 0)
            message.setdefault("invocations", []).append(invocation)
        invocation.setdefault("definitions", []).append(
            InvocationDefinition(text=message.get("text") or "", path=selected_path)
        )
        invocation["evidence"].extend(
            _evidence("selected_skill.message_id", message.get("id"))
            + _evidence("selected_skill.content", message.get("text"))
            + _evidence("content_item_kinds", ["skills.selected_skill_instructions"])
        )


def _call_invocation(call: ToolCall, message: Message, index: int) -> None:
    name = call["name"].replace("__", ".").lower()
    canonical = (
        "skills.read" if name.endswith("skills.read") else name.rsplit(".", 1)[-1]
    )
    arguments = json_value(call.get("input"))
    if canonical not in _SKILL_TOOLS or not isinstance(arguments, dict):
        return
    skill_name = None
    for key in ("skill", "name", "package"):
        value = arguments.get(key)
        if isinstance(value, str):
            skill_name = value
            break
    invocation = _invocation(message, InvocationKind.SKILL, skill_name, index)
    evidence = _evidence("name", call["name"]) + _evidence("input", arguments)
    native_id = call.get("native_id", call.get("id"))
    if native_id is not None:
        invocation["native_id"] = native_id
    invocation["evidence"] = evidence
    invocation["activity"] = ActionActivityReference(
        message_i=message["i"], tool_call_index=index, evidence=evidence
    )
    args = arguments.get("args")
    if isinstance(args, str):
        invocation["arguments"] = args
    definitions = []
    definition = arguments.get("definition")
    if isinstance(definition, str):
        definitions.append(InvocationDefinition(text=definition))
    result = call.get("result_record")
    content = result.get("content") if result is not None else None
    if result is not None and not result["is_error"] and isinstance(content, str):
        definitions.append(InvocationDefinition(text=content))
    if definitions:
        invocation["definitions"] = definitions
    call["invocations"] = [invocation]


def populate_invocations(session: Session) -> None:
    """Attach occurrences before export; never load definitions from disk."""
    for message in session["events"]:
        invocations = _message_invocations(message)
        if invocations:
            message["invocations"] = invocations
        for index, call in enumerate(message.get("tool_calls") or []):
            _call_invocation(call, message, index)
    _selected_expansions(session)
    for message in session["events"]:
        session.setdefault("invocations", []).extend(message.get("invocations", []))
        for call in message.get("tool_calls") or []:
            session["invocations"].extend(call.get("invocations", []))
