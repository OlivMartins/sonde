"""Attribute names used by the agent.

`gen_ai.*` names follow the OpenTelemetry GenAI semantic conventions
(https://github.com/open-telemetry/semantic-conventions-genai), which are still
in Development status. Keeping every name in one module makes the next
rename a one-line change.

`sonde.*` names are project-specific: they carry agent-loop facts the
conventions do not cover yet (outcome, step index, policy decisions).
"""

# Operation names
OP_CHAT = "chat"
OP_EXECUTE_TOOL = "execute_tool"
OP_INVOKE_AGENT = "invoke_agent"

# GenAI semantic conventions
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_REQUEST_TEMPERATURE = "gen_ai.request.temperature"
GEN_AI_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
GEN_AI_RESPONSE_ID = "gen_ai.response.id"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS = "gen_ai.usage.cache_read.input_tokens"
GEN_AI_AGENT_ID = "gen_ai.agent.id"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_AGENT_VERSION = "gen_ai.agent.version"
GEN_AI_AGENT_DESCRIPTION = "gen_ai.agent.description"
GEN_AI_CONVERSATION_ID = "gen_ai.conversation.id"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_TYPE = "gen_ai.tool.type"
GEN_AI_TOOL_DESCRIPTION = "gen_ai.tool.description"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"
GEN_AI_TOOL_CALL_ARGUMENTS = "gen_ai.tool.call.arguments"  # opt-in content
GEN_AI_TOOL_CALL_RESULT = "gen_ai.tool.call.result"  # opt-in content
GEN_AI_INPUT_MESSAGES = "gen_ai.input.messages"  # opt-in content
GEN_AI_OUTPUT_MESSAGES = "gen_ai.output.messages"  # opt-in content

# General semantic conventions
ERROR_TYPE = "error.type"
SERVER_ADDRESS = "server.address"
SERVER_PORT = "server.port"

# Project-specific attributes
SONDE_TASK_CATEGORY = "sonde.task.category"
SONDE_AGENT_OUTCOME = "sonde.agent.outcome"
SONDE_AGENT_PARENT = "sonde.agent.parent"  # name of the delegating agent, on sub-agent runs
SONDE_AGENT_STEPS = "sonde.agent.steps"
SONDE_AGENT_STEP = "sonde.agent.step"
SONDE_AGENT_TOOL_CALLS = "sonde.agent.tool_calls"
SONDE_LLM_RETRIES = "sonde.llm.retries"
SONDE_TOOL_BLOCKED = "sonde.tool.blocked"
SONDE_FEEDBACK_SCORE = "sonde.feedback.score"
SONDE_SEMCONV_LEGACY = "sonde.semconv.legacy"  # set by the collector when it renamed a legacy attribute

# Span event emitted for each retried LLM request
EVENT_LLM_RETRY = "sonde.llm.retry"

# Values for sonde.agent.outcome
OUTCOME_COMPLETED = "completed"
OUTCOME_MAX_STEPS = "max_steps_exceeded"
OUTCOME_LLM_ERROR = "llm_error"
