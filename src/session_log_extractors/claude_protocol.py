"""Native Claude vocabulary, independent of normalized domain values."""

from typing import Final

BASH_TOOL: Final = "Bash"
READ_TOOL: Final = "Read"
WRITE_TOOL: Final = "Write"
EDIT_TOOL: Final = "Edit"
NOTEBOOK_EDIT_TOOL: Final = "NotebookEdit"
MULTI_EDIT_TOOL: Final = "MultiEdit"
CREATE_TOOL: Final = "Create"
SKILL_TOOL: Final = "Skill"
AGENT_TOOL: Final = "Agent"
TASK_TOOL: Final = "Task"
ASK_USER_QUESTION: Final = "AskUserQuestion"
EXIT_PLAN_MODE: Final = "ExitPlanMode"
LEGACY_EXIT_PLAN_MODE: Final = "exit_plan_mode"
USER_RECORD: Final = "user"
ASSISTANT_RECORD: Final = "assistant"
SYSTEM_RECORD: Final = "system"
TEXT_BLOCK: Final = "text"
TOOL_USE_BLOCK: Final = "tool_use"
TOOL_RESULT_BLOCK: Final = "tool_result"
IMAGE_BLOCK: Final = "image"
USER_REJECTED: Final = "user-rejected"
SETTINGS_RULE: Final = "permission-rule"
REJECTED_RESULT: Final = "User rejected tool use"
REJECTED_PREFIX: Final = "The user doesn't want to proceed with this tool use."

PLAN_TOOLS: Final = {EXIT_PLAN_MODE, LEGACY_EXIT_PLAN_MODE}
AGENT_TOOLS: Final = frozenset((AGENT_TOOL, TASK_TOOL))


# Native role values can coincide with record kinds; normalized model roles are
# constructed separately by the shared model/export layer.
ROLE_USER: Final = "user"
ROLE_ASSISTANT: Final = "assistant"
HUMAN_ORIGIN: Final = "human"
PROMPT_TYPED: Final = "typed"
PROMPT_QUEUED: Final = "queued"

SUMMARY_RECORD: Final = "summary"
ATTACHMENT_RECORD: Final = "attachment"
