"""Copilot tool payloads and recognition, independent of other harnesses.

These types name understood fields; they neither filter additional recorded
fields nor validate inputs at runtime. Unknown payloads remain complete JSON.
"""

from typing import Literal, TypeAlias, TypedDict

from session_log_extractors.copilot_protocol import (
    ASK_USER,
    BASH,
    CREATE,
    EDIT,
    EXIT_PLAN_MODE,
    GLOB,
    GREP,
    READ_BASH,
    SKILL,
    SQL,
    TASK,
    TASK_COMPLETE,
    VIEW,
)
from session_log_extractors.json_types import JsonValue


class BashInput(TypedDict, total=False):
    command: str
    description: str


class SkillInput(TypedDict, total=False):
    skill: str


class ViewInput(TypedDict, total=False):
    path: str
    view_range: list[int]


class EditInput(TypedDict, total=False):
    path: str
    old_str: str
    new_str: str


class CreateInput(TypedDict, total=False):
    path: str
    file_text: str


class AskUserInput(TypedDict, total=False):
    message: str
    requestedSchema: dict[str, JsonValue]


class ExitPlanInput(TypedDict, total=False):
    summary: str
    recommendedAction: str


class GlobInput(TypedDict, total=False):
    pattern: str
    path: str


class GrepInput(TypedDict, total=False):
    pattern: str
    path: str
    output_mode: str


class SqlInput(TypedDict, total=False):
    query: str
    description: str


class TaskInput(TypedDict, total=False):
    agent_type: str
    name: str
    description: str
    mode: str
    prompt: str


class TaskCompleteInput(TypedDict, total=False):
    summary: str


class ReadBashInput(TypedDict, total=False):
    shellId: str
    delay: int


KnownToolInput: TypeAlias = (
    BashInput
    | SkillInput
    | ViewInput
    | EditInput
    | CreateInput
    | AskUserInput
    | ExitPlanInput
    | GlobInput
    | GrepInput
    | SqlInput
    | TaskInput
    | TaskCompleteInput
    | ReadBashInput
)
ToolInput: TypeAlias = KnownToolInput | JsonValue
ToolInputSchema: TypeAlias = Literal[
    "CopilotBash",
    "CopilotSkill",
    "CopilotView",
    "CopilotEdit",
    "CopilotCreate",
    "CopilotAskUser",
    "CopilotExitPlan",
    "CopilotGlob",
    "CopilotGrep",
    "CopilotSql",
    "CopilotTask",
    "CopilotTaskComplete",
    "CopilotReadBash",
    "unsupported",
]

_INPUT_SCHEMAS: dict[str, ToolInputSchema] = {
    BASH: "CopilotBash",
    SKILL: "CopilotSkill",
    VIEW: "CopilotView",
    EDIT: "CopilotEdit",
    CREATE: "CopilotCreate",
    ASK_USER: "CopilotAskUser",
    EXIT_PLAN_MODE: "CopilotExitPlan",
    GLOB: "CopilotGlob",
    GREP: "CopilotGrep",
    SQL: "CopilotSql",
    TASK: "CopilotTask",
    TASK_COMPLETE: "CopilotTaskComplete",
    READ_BASH: "CopilotReadBash",
}


def input_schema(name: str, payload: ToolInput) -> ToolInputSchema:
    """Recognize Copilot's tool vocabulary without validating or filtering input."""
    schema = _INPUT_SCHEMAS.get(name)
    if schema is not None and isinstance(payload, dict):
        return schema
    return "unsupported"
