"""Native Copilot identifiers, separate from normalized domain concepts."""

ASK_USER = "ask_user"
VIEW = "view"
CREATE = "create"
EDIT = "edit"
GLOB = "glob"
GREP = "grep"
TASK = "task"
SKILL = "skill"
SKILL_INVOKED = "skill.invoked"
SUBAGENT_STARTED = "subagent.started"
USER_MESSAGE = "user.message"
USER_INVOKED = "user-invoked"
HOOK_START = "hook.start"
HOOK_END = "hook.end"
FILE_TOOLS = frozenset((VIEW, CREATE, EDIT, GLOB, GREP))
HUMAN_RESPONSE_RECORDED = "human_response.recorded"
TOOL_EXECUTION_COMPLETE = "tool.execution_complete"
ELICITATION_CANCEL = "cancel"

# Permission and plan evidence recorded by Copilot, not normalized outcomes.
EXIT_PLAN_MODE = "exit_plan_mode"
TOOL_EXECUTION_START = "tool.execution_start"
PERMISSION_REQUESTED = "permission.requested"
PERMISSION_COMPLETED = "permission.completed"
HUMAN_RESPONSE = "human_response"
DENIED_BY_USER = "denied-interactively-by-user"
DENIED_ERROR = "denied"
SETTINGS_REFUSAL_PREFIX = (
    "Permission to run this tool was denied due to the following rules:"
)

DENIED_BY_RULES = "denied-by-rules"
DENIED_UNAVAILABLE = "denied-no-approval-rule-and-could-not-request-from-user"
DENIED_CONTENT_POLICY = "denied-by-content-exclusion-policy"
PERMISSION_DENIAL_KINDS = frozenset(
    (
        DENIED_BY_USER,
        DENIED_BY_RULES,
        DENIED_UNAVAILABLE,
        DENIED_CONTENT_POLICY,
    )
)
SETTINGS_DENIAL_KINDS = frozenset((DENIED_BY_RULES, DENIED_CONTENT_POLICY))
