"""Codex-owned tool payloads and schema recognition."""

from typing import Literal, TypeAlias, TypedDict

from session_log_extractors.codex_protocol import (
    APPLY_PATCH,
    EXEC_COMMAND,
    REQUEST_USER_INPUT,
    SKILLS_READ,
    SPAWN_AGENT,
    UPDATE_PLAN,
)
from session_log_extractors.json_types import JsonValue
from session_log_extractors.question_types import QuestionOption


class Question(TypedDict, total=False):
    id: str
    question: str
    header: str
    options: list[QuestionOption]


class RequestUserInput(TypedDict, total=False):
    questions: list[Question]


class ExecCommandInput(TypedDict, total=False):
    cmd: str
    workdir: str
    max_output_tokens: int
    yield_time_ms: int
    sandbox_permissions: str
    justification: str
    prefix_rule: list[str]
    shell: str
    login: bool
    tty: bool


class PlanStepInput(TypedDict, total=False):
    step: str
    status: str


class UpdatePlanInput(TypedDict, total=False):
    explanation: str
    plan: list[PlanStepInput]


class ApplyPatchInput(TypedDict, total=False):
    patch: str
    input: str
    content: str


class SpawnAgentInput(TypedDict, total=False):
    message: str
    description: str
    task_name: str
    agent_type: str
    agent_id: str
    fork_turns: str


class ReadSkillInput(TypedDict, total=False):
    skill: str
    name: str
    args: str
    package: str
    resource: str
    definition: str


KnownToolInput: TypeAlias = (
    ExecCommandInput
    | UpdatePlanInput
    | ApplyPatchInput
    | ReadSkillInput
    | RequestUserInput
    | SpawnAgentInput
)
ToolInputSchema: TypeAlias = Literal[
    "exec_command",
    "request_user_input",
    "update_plan",
    "apply_patch",
    "skills.read",
    "spawn_agent",
    "unsupported",
]

_INPUT_SCHEMAS: dict[str, ToolInputSchema] = {
    EXEC_COMMAND: EXEC_COMMAND,
    REQUEST_USER_INPUT: REQUEST_USER_INPUT,
    UPDATE_PLAN: UPDATE_PLAN,
    APPLY_PATCH: APPLY_PATCH,
    SKILLS_READ: SKILLS_READ,
    SPAWN_AGENT: SPAWN_AGENT,
}


def input_schema(name: str, payload: JsonValue) -> ToolInputSchema:
    """Recognize Codex names without validating or filtering native payloads."""
    normalized = name.replace("__", ".")
    canonical = (
        SKILLS_READ
        if normalized.endswith(SKILLS_READ)
        else normalized.rsplit(".", 1)[-1]
    )
    return (
        _INPUT_SCHEMAS.get(canonical, "unsupported")
        if isinstance(payload, dict)
        else "unsupported"
    )
