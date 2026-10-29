"""Claude tool payloads and recognition; additional recorded fields survive.

Schema recognition identifies supported names and object inputs. It does not
validate native fields or reconstruct payloads from the types below.
"""

from typing import Literal, TypeAlias, TypedDict

from session_log_extractors.json_types import JsonValue
from session_log_extractors.question_types import QuestionOption


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

_INPUT_SCHEMAS: dict[str, ToolInputSchema] = {
    "Bash": "Bash",
    "Read": "Read",
    "AskUserQuestion": "AskUserQuestion",
    "ExitPlanMode": "ExitPlanMode",
    "Skill": "Skill",
    "Agent": "Agent",
    "Write": "Write",
    "Edit": "Edit",
    "MultiEdit": "MultiEdit",
    "NotebookEdit": "NotebookEdit",
    "Create": "Create",
}


def input_schema(name: str, payload: ToolInput) -> ToolInputSchema:
    """Recognize Claude's tool vocabulary without validating or filtering input."""
    canonical = "ExitPlanMode" if name == "exit_plan_mode" else name
    schema = _INPUT_SCHEMAS.get(canonical)
    if schema is not None and isinstance(payload, dict):
        return schema
    return "unsupported"
