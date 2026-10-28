"""Known tool-input shapes and an explicit opaque-JSON fallback.

These types describe fields the adapter understands. They do not reconstruct
payloads or remove additional recorded keys, and are not runtime validators.
A schema name identifies recognized object inputs; unsupported names/shapes
remain complete JSON values for retention and diagnostics.
"""

from typing import Literal, TypeAlias, TypedDict, cast

JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)


class BashInput(TypedDict, total=False):
    command: str
    description: str
    timeout: int
    run_in_background: bool


class ReadInput(TypedDict, total=False):
    file_path: str
    offset: int
    limit: int
    pages: str


class QuestionOption(TypedDict, total=False):
    label: str
    description: str


class Question(TypedDict, total=False):
    question: str
    header: str
    options: list[QuestionOption]
    multiSelect: bool


class AskUserQuestionInput(TypedDict, total=False):
    questions: list[Question]


class ExitPlanModeInput(TypedDict, total=False):
    plan: str


class SkillInput(TypedDict, total=False):
    skill: str
    name: str
    args: str


class AgentInput(TypedDict, total=False):
    description: str
    prompt: str
    subagent_type: str
    run_in_background: bool


class FilePathInput(TypedDict, total=False):
    file_path: str
    path: str
    notebook_path: str
    filePath: str


class WriteInput(FilePathInput, total=False):
    content: str


class EditOperation(TypedDict, total=False):
    old_string: str
    new_string: str
    replace_all: bool


class EditInput(FilePathInput, EditOperation):
    pass


class MultiEditInput(FilePathInput, total=False):
    edits: list[EditOperation]


class NotebookEditInput(FilePathInput, total=False):
    cell_id: str
    new_source: str
    cell_type: str
    edit_mode: str


KnownToolInput: TypeAlias = (
    BashInput
    | ReadInput
    | AskUserQuestionInput
    | ExitPlanModeInput
    | SkillInput
    | AgentInput
    | WriteInput
    | EditInput
    | MultiEditInput
    | NotebookEditInput
)
ToolInput: TypeAlias = KnownToolInput | JsonValue
ToolInputSchema: TypeAlias = Literal[
    "Bash",
    "Read",
    "AskUserQuestion",
    "ExitPlanMode",
    "Skill",
    "Agent",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Create",
    "unsupported",
]

_INPUT_TYPES = {
    "Bash": BashInput,
    "Read": ReadInput,
    "AskUserQuestion": AskUserQuestionInput,
    "ExitPlanMode": ExitPlanModeInput,
    "Skill": SkillInput,
    "Agent": AgentInput,
    "Write": WriteInput,
    "Edit": EditInput,
    "MultiEdit": MultiEditInput,
    "NotebookEdit": NotebookEditInput,
    "Create": WriteInput,
}


def input_schema(name: str, payload: ToolInput) -> ToolInputSchema:
    """Name recognized object schemas without filtering or validating payloads."""
    canonical = "ExitPlanMode" if name == "exit_plan_mode" else name
    if canonical in _INPUT_TYPES and isinstance(payload, dict):
        return cast(ToolInputSchema, canonical)
    return "unsupported"
