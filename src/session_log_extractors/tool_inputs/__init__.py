"""Explicit shared unions of harness-owned tool payloads and schema identifiers.

Adapters import recognition from their own harness module. This package joins
types for the shared model without introducing a global tool-name registry.
"""

from typing import TypeAlias

from session_log_extractors.json_types import JsonValue
from session_log_extractors.tool_inputs.claude import (
    KnownToolInput as ClaudeToolInput,
)
from session_log_extractors.tool_inputs.claude import (
    ToolInputSchema as ClaudeToolInputSchema,
)
from session_log_extractors.tool_inputs.copilot import (
    KnownToolInput as CopilotToolInput,
)
from session_log_extractors.tool_inputs.copilot import (
    ToolInputSchema as CopilotToolInputSchema,
)

KnownToolInput: TypeAlias = ClaudeToolInput | CopilotToolInput
ToolInput: TypeAlias = KnownToolInput | JsonValue
ToolInputSchema: TypeAlias = ClaudeToolInputSchema | CopilotToolInputSchema
