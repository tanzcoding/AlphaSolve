# AlphaSolve LLM Provider Abstraction — Design

**Date**: 2026-05-22
**Status**: Approved for implementation
**Branch (to be created)**: `refactor/llm-provider-abstraction`
**Scope**: Milestone 1 of multi-milestone refactor. Provider abstraction + native Anthropic Messages wire support + ergonomic profile-based model switching.

---

## 1. Problem & Goals

### 1.1 Problem

AlphaSolve currently calls LLM providers exclusively via OpenAI Chat Completions wire format. All 11 provider presets in `src/alphasolve/config/agent_config.py` (DeepSeek, Parasail, Longcat, Moonshot, Volcano, Dashscope, MIMO, OpenRouter, …) are reached through this single wire format. Two concrete pains:

1. **Tool-use reliability on older models**. The OpenAI Chat Completions tool-call protocol nests JSON inside a string (`function.arguments` is a JSON-stringified string), forcing weak models to handle four levels of backslash escaping for content that itself contains backslashes (LaTeX in Edit calls, regex in Grep, etc.). Older DeepSeek models, in particular, lose track of escape counts and emit invalid JSON. Providers like Kimi and DeepSeek expose alternative Anthropic-Messages-format endpoints where `tool_use.input` is a JSON object (not string), cutting escaping levels from 4 to 2. AlphaSolve has no way to use these.

2. **Switching models is friction-heavy**. The active per-role model assignment is encoded as Python constants (`GENERATOR_CONFIG = {**DEEPSEEK_PRO_CONFIG}`, etc.) at module import time. Changing models means editing `agent_config.py` or editing per-agent YAML files. There is no one-flag way to swap "all agents → cheap models" or "orchestrator → stronger model".

### 1.2 Goals

- **G1**. Support two LLM wire formats as first-class: OpenAI Chat Completions and Anthropic Messages. Each provider preset declares its wire format.
- **G2**. Migrate all 11 existing provider presets into a YAML registry with no behavioural regression.
- **G3**. Introduce profile-based switching. Three profiles ship: `cheap`, `balanced`, `strategic`. `balanced` reproduces today's default per-role model assignment exactly.
- **G4**. CLI exposes `--profile <name>` and supports `ALPHASOLVE_PROFILE` env var. Switching all agents to a new profile is one flag.
- **G5**. Make `alphasolve.llm` a clean sub-package with a small, intentional public API. Single-direction dependency: agent layer may import `alphasolve.llm`, reverse forbidden.
- **G6**. Type the message exchange (no more `list[dict[str, Any]]` flowing through the agent loop) — set up clean ground for the next milestone (compaction, skills, etc.) without re-doing it then.

### 1.3 Non-goals (explicit out-of-scope)

The following are recognized as valuable but **NOT** in this milestone:

- Compaction (automatic context summarisation when token budget fills).
- Skill registry / system-prompt rendering of skills (the `skills:` field stays in `GeneralAgentConfig` but is not freshly wired).
- Per-agent tool *description* customisation (today `tool_parameters` lets each agent constrain schema; per-agent description text is not added).
- Native Gemini, Bedrock, Vertex, Mistral, Azure OpenAI providers — only OpenAI Chat Completions + Anthropic Messages.
- Prompt caching / extended thinking as first-class abstraction fields (passed through opaquely via `params`; not surfaced as typed fields).
- Extracting `alphasolve.agent` as its own sub-package (kept in `agents/general/`, will move in Milestone 2).
- Persistent / interactive profile picker like `cc-switch`'s desktop tray. Profile is purely per-invocation (flag / env / yaml default).
- Backward-compatibility shims. `agent_config.py` provider constants are deleted outright; YAML files using `model_config:` fail to load with a clear error.

---

## 2. Architecture

### 2.1 Layout

```
src/alphasolve/
├── llm/                                ← new sub-package
│   ├── __init__.py                     ← public API (16 names; see §3.3)
│   ├── types.py                        ← lingua franca dataclasses + ChatClient Protocol
│   ├── client.py                       ← (reserved for shared client helpers; may stay empty)
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── openai_chat.py              ← ported from general_agent.py's OpenAIChatClient
│   │   └── anthropic_messages.py       ← new
│   ├── config/
│   │   ├── __init__.py
│   │   ├── preset.py                   ← Preset dataclass
│   │   ├── profile.py                  ← Profile dataclass
│   │   └── loader.py                   ← yaml loading + env-var resolution + user override
│   └── factory.py                      ← make_client, make_client_factory
│
├── agents/general/                     ← unchanged location; internal types updated
│   ├── general_agent.py                ← uses Message / ToolDef / CompletionResponse
│   ├── config.py                       ← GeneralAgentConfig with `role` (model_config deleted)
│   └── tool_registry.py                ← `tool_defs()` replaces `openai_tools()`
│
├── agents/team/                        ← workflow code; updated to use Message dataclasses
│   └── …
│
├── config/
│   ├── presets.yaml                    ← new; canonical registry
│   ├── profiles.yaml                   ← new; cheap / balanced / strategic + default
│   ├── agent_config.py                 ← reduced to Wolfram block only
│   ├── agents/*.yaml                   ← migrated: model_config → role
│   └── subagents/*.yaml                ← migrated: model_config → role
│
└── cli.py                              ← --profile / --list-profiles / --list-presets
```

### 2.2 Dependency rule

`alphasolve.llm` imports only stdlib + `openai` SDK + `anthropic` SDK + `yaml`. It does **not** import from `alphasolve.agents`, `alphasolve.workflow`, `alphasolve.execution`, or any other internal package.

`alphasolve.agents` and downstream code may import from `alphasolve.llm.types` (Protocols, dataclasses) and `alphasolve.llm` (factory functions). They **must not** import from `alphasolve.llm.config.*` directly — only `alphasolve.llm` re-exports.

`alphasolve.workflow` (`team/workflow.py`) must not import from `alphasolve.llm.config` either — it receives a fully constructed `client_factory` from the CLI.

These rules guarantee the layer can later be lifted into a separate Python package if needed.

---

## 3. Lingua Franca Types

### 3.1 Why typed dataclasses, not dicts

Two reasons:

1. **Eliminate dict-shape ambiguity at the LLM boundary**. The agent loop currently builds messages as `dict[str, Any]` and reads `response["tool_calls"]`, `response["content"]`, etc. Each provider returns slightly differently shaped dicts. Typed dataclasses make the shape unambiguous and the converter's job explicit.

2. **Set up next milestone cleanly**. Compaction / skills / event-log refactors all need to walk the conversation. Walking typed `list[Message]` is dramatically less error-prone than walking `list[dict]`. Doing the dataclass conversion now (as part of a refactor with comprehensive test updates) is cheaper than doing it later as a separate breaking change.

Conversion to/from wire format happens entirely in `providers/*.py`. The agent layer sees only typed objects.

### 3.2 Definitions

```python
# alphasolve/llm/types.py

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

Role = Literal["system", "user", "assistant", "tool"]
FinishReason = Literal["stop", "tool_calls", "length", "content_filter", "error"]


@dataclass(frozen=True)
class ToolCall:
    id: str                                 # provider-assigned tool_call_id
    name: str                               # tool name
    args: dict[str, Any]                    # parsed dict — never a JSON string


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]              # JSON schema


@dataclass(frozen=True)
class Message:
    role: Role
    content: str = ""                       # text content
    tool_calls: tuple[ToolCall, ...] = ()   # role=="assistant" only
    tool_call_id: str | None = None         # role=="tool" only
    name: str | None = None                 # tool name, role=="tool" only


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


@dataclass(frozen=True)
class CompletionResponse:
    message: Message                        # always role="assistant"
    finish_reason: FinishReason
    usage: Usage = field(default_factory=Usage)
    raw: Any = field(default=None, compare=False, repr=False)   # provider raw response; excluded from equality


@dataclass(frozen=True)
class StreamDelta:
    type: Literal["text", "tool_input"]
    text: str = ""                          # type=="text"
    tool_call_id: str = ""                  # type=="tool_input"
    arg_delta: str = ""                     # type=="tool_input"


ChatDeltaSink = Callable[[StreamDelta], None]


class ChatCompletionError(RuntimeError):
    """Raised on transport, malformed-response, or provider 5xx failures."""


class ChatClient(Protocol):
    def complete(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDef],
        delta_sink: ChatDeltaSink | None = None,
    ) -> CompletionResponse: ...
```

**Immutability notes**:
- `Message`, `ToolCall`, `ToolDef`, `CompletionResponse`, `StreamDelta`, `Usage` are all `frozen=True`. Mutations are done by construction (`dataclasses.replace` or new instance).
- `tool_calls` is a `tuple`, not a `list`. This prevents the trap "I have a `frozen=True` dataclass but its internal list is still mutable".
- `parameters: dict[str, Any]` and `args: dict[str, Any]` are dicts (mutable). Treat them as immutable by convention; consumers must not mutate. This is a pragmatic trade — making them `MappingProxyType` would complicate every construction site.

### 3.3 Public API of `alphasolve.llm`

`alphasolve/llm/__init__.py` re-exports these 19 names and no others:

```python
# Types
ChatClient, ChatDeltaSink, ChatCompletionError
Message, Role, ToolCall, ToolDef
CompletionResponse, Usage, StreamDelta, FinishReason

# Config
Preset, Profile, WireFormat

# Loaders and factory
load_presets, load_profile, load_active_profile
make_client, make_client_factory
```

Implementation modules use `_` prefix for any names not in this list. Tests inside `tests/llm/` may import from sub-modules directly; production code outside `alphasolve.llm` must not.

---

## 4. Preset & Profile Configuration

### 4.1 Preset schema

```python
# alphasolve/llm/config/preset.py

from dataclasses import dataclass, field
from typing import Any, Literal
import os

WireFormat = Literal["openai_chat", "anthropic_messages"]


@dataclass(frozen=True)
class Preset:
    name: str
    wire_format: WireFormat
    base_url: str
    api_key_env: str                    # env-var name; never the key value itself
    model: str
    timeout: float = 3600
    params: dict[str, Any] = field(default_factory=dict)

    def resolve_api_key(self) -> str:
        value = os.getenv(self.api_key_env)
        if not value:
            raise RuntimeError(
                f"env var {self.api_key_env!r} not set "
                f"(required by preset {self.name!r})"
            )
        return value
```

### 4.2 Profile schema

```python
# alphasolve/llm/config/profile.py

from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    name: str
    role_to_preset: dict[str, str]

    def preset_for(self, role: str) -> str:
        try:
            return self.role_to_preset[role]
        except KeyError:
            raise KeyError(
                f"role {role!r} not defined in profile {self.name!r}; "
                f"available roles: {sorted(self.role_to_preset)}"
            )
```

### 4.3 `src/alphasolve/config/presets.yaml` (canonical, complete)

```yaml
# ===== OpenAI Chat Completions presets (1:1 migration from agent_config.py) =====

deepseek-flash:
  wire_format: openai_chat
  base_url: https://api.deepseek.com
  api_key_env: DEEPSEEK_API_KEY
  model: deepseek-v4-flash
  timeout: 3600
  params: { extra_body: { reasoning: { effort: max } } }

deepseek-pro:
  wire_format: openai_chat
  base_url: https://api.deepseek.com
  api_key_env: DEEPSEEK_API_KEY
  model: deepseek-v4-pro
  timeout: 3600
  params: { extra_body: { reasoning: { effort: max } } }

parasail-deepseek:
  wire_format: openai_chat
  base_url: https://api.parasail.io/v1
  api_key_env: PARASAIL_API_KEY
  model: deepseek-ai/DeepSeek-V3.2
  timeout: 3600
  params: { extra_body: { enable_thinking: true } }

longcat:
  wire_format: openai_chat
  base_url: https://api.longcat.chat/openai
  api_key_env: LONGCAT_API_KEY
  model: LongCat-Flash-Thinking-2601
  timeout: 3600

moonshot-kimi:
  wire_format: openai_chat
  base_url: https://api.moonshot.cn/v1
  api_key_env: MOONSHOT_API_KEY
  model: kimi-k2-thinking
  timeout: 3600
  params: { temperature: 1.0 }

volcano-doubao:
  wire_format: openai_chat
  base_url: https://ark.cn-beijing.volces.com/api/v3
  api_key_env: ARK_API_KEY
  model: doubao-seed-2-0-pro-260215
  timeout: 180
  params: { extra_body: { thinking: { type: enabled } } }

volcano-deepseek:
  wire_format: openai_chat
  base_url: https://ark.cn-beijing.volces.com/api/v3
  api_key_env: ARK_API_KEY
  model: deepseek-v3-2-251201
  timeout: 180
  params: { extra_body: { thinking: { type: enabled } } }

dashscope-deepseek:
  wire_format: openai_chat
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  api_key_env: DASHSCOPE_API_KEY
  model: deepseek-v3.2
  timeout: 3600
  params: { temperature: 1.0, extra_body: { enable_thinking: true } }

mimo:
  wire_format: openai_chat
  base_url: https://api.xiaomimimo.com/v1
  api_key_env: MIMO_API_KEY
  model: mimo-v2-flash
  timeout: 3600
  params: { temperature: 1.0, extra_body: { thinking: { type: enabled } } }

openrouter-gemini:
  wire_format: openai_chat
  base_url: https://openrouter.ai/api/v1
  api_key_env: OPENROUTER_API_KEY
  model: google/gemini-2.5-flash
  timeout: 3600
  params: { extra_body: { reasoning: { effort: high } } }

# ===== Anthropic Messages presets (new) =====

deepseek-pro-anthropic:
  wire_format: anthropic_messages
  base_url: https://api.deepseek.com/anthropic    # TODO: confirm at implementation time
  api_key_env: DEEPSEEK_API_KEY
  model: deepseek-v4-pro
  timeout: 3600

moonshot-kimi-anthropic:
  wire_format: anthropic_messages
  base_url: https://api.moonshot.cn/anthropic     # TODO: confirm at implementation time
  api_key_env: MOONSHOT_API_KEY
  model: kimi-k2-thinking
  timeout: 3600

# ===== Strategic-tier orchestrator model =====

qwen-3.7-max:
  wire_format: openai_chat
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  api_key_env: DASHSCOPE_API_KEY
  model: qwen3.7-max                              # TODO: confirm exact model_id string
  timeout: 3600
```

### 4.4 `src/alphasolve/config/profiles.yaml` (canonical, complete)

```yaml
# balanced reproduces the per-role defaults in the original agent_config.py exactly.
balanced:
  orchestrator:      deepseek-pro
  generator:         deepseek-pro
  verifier:          deepseek-pro
  reviser:           deepseek-pro
  curator:           deepseek-flash
  compute_subagent:  deepseek-flash
  proof_subagent:    deepseek-flash

# cheap: everything on flash-tier.
cheap:
  orchestrator:      deepseek-flash
  generator:         deepseek-flash
  verifier:          deepseek-flash
  reviser:           deepseek-flash
  curator:           deepseek-flash
  compute_subagent:  deepseek-flash
  proof_subagent:    deepseek-flash

# strategic: balanced with a stronger orchestrator.
strategic:
  orchestrator:      qwen-3.7-max
  generator:         deepseek-pro
  verifier:          deepseek-pro
  reviser:           deepseek-pro
  curator:           deepseek-flash
  compute_subagent:  deepseek-flash
  proof_subagent:    deepseek-flash

default: balanced
```

### 4.5 User override

Loader path (`alphasolve/llm/config/loader.py`):

1. Load `src/alphasolve/config/presets.yaml` → repo registry.
2. If `$ALPHASOLVE_CONFIG_DIR/presets.yaml` exists, or `~/.alphasolve/presets.yaml` if that env var is not set, load it.
3. **Merge semantics**: per-preset whole-record replacement. Repo's `deepseek-pro` is replaced by user's `deepseek-pro` if user defines one. User-only preset names are added. Repo-only preset names are kept. **No field-level deep-merge.**
4. Identical procedure for `profiles.yaml`.

Override path resolution helper:

```python
def _user_config_dir() -> Path:
    override = os.getenv("ALPHASOLVE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".alphasolve"
```

### 4.6 The `agent_config.py` aftermath

After this refactor `src/alphasolve/config/agent_config.py` contains only:

```python
from __future__ import annotations
import os

PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class AlphaSolveConfig:
    WOLFRAM_AVAILABLE = True
    WOLFRAM_STATUS = "not_checked"
    CHECK_IS_THEOREM_TIMES = 5

    @classmethod
    def configure_wolfram_availability(cls, available: bool, reason: str = "") -> None:
        cls.WOLFRAM_AVAILABLE = bool(available)
        cls.WOLFRAM_STATUS = reason or ("available" if available else "unavailable")
```

All 11 provider constants and all 7 `*_CONFIG` role aliases are deleted. Any test or code referencing them must be migrated in the same commit as the deletion; references that escape the migration will surface as `AttributeError` at runtime.

---

## 5. Agent Layer Adaptation

### 5.1 `GeneralAgentConfig`

```python
# alphasolve/agents/general/config.py

@dataclass(frozen=True)
class GeneralAgentConfig:
    name: str
    role: str | None = None
    system_prompt: str = ""
    tools: tuple[str, ...] = ()
    tool_parameters: dict[str, dict[str, Any]] = field(default_factory=dict)
    max_turns: int = 80
    skills: tuple[str, ...] = ()
    when_to_use: str = ""
    system_prompt_template: str = ""
    system_prompt_args: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def effective_role(self) -> str:
        return self.role or self.name
```

Changes vs. current:
- Field `model_config: str | None = None` — **removed**.
- Field `role: str | None = None` — **added**.
- `tools` and `skills` change from `list` to `tuple` for hashability and immutability symmetry with `Message.tool_calls`.
- Method `effective_role()` — added.

### 5.2 `ToolRegistry` changes

`tool_registry.py`:

- Method `openai_tools()` → renamed `tool_defs()`. Returns `list[ToolDef]`.
- `RegisteredTool.to_openai_tool()` — **deleted**. Wire-format-aware conversion belongs in `providers/`, not in the registry.
- `ToolResult` unchanged (orthogonal to wire format).
- The OpenAI-format dict serialization (`{"type": "function", "function": {...}}`) is built inside `providers/openai_chat.py` when the call is dispatched. Likewise the Anthropic-format serialization (`{"name", "description", "input_schema"}`) is built inside `providers/anthropic_messages.py`.

Signature change in detail:

```python
class ToolRegistry:
    def tool_defs(
        self,
        enabled: tuple[str, ...] | list[str] | None = None,
        tool_parameters: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> list[ToolDef]: ...
```

### 5.3 `GeneralPurposeAgent.run()` shape

```python
def run(
    self,
    task: str,
    *,
    description: str = "",
    extra_messages: list[Message] | None = None,
) -> AgentRunResult:
    messages: list[Message] = [Message(role="system", content=self.config.system_prompt)]
    if extra_messages:
        messages.extend(extra_messages)
    messages.append(Message(role="user", content=task))

    tools: list[ToolDef] = self.tool_registry.tool_defs(
        self.config.tools, self.config.tool_parameters
    )

    for turn in range(1, self.config.max_turns + 1):
        if self.stop_event is not None and self.stop_event.is_set():
            return _stopped_result(messages, trace, turn - 1)

        response = self.client.complete(
            messages=messages,
            tools=tools,
            delta_sink=self._make_delta_sink(...),
        )
        messages.append(response.message)

        if response.finish_reason == "stop":
            return AgentRunResult(
                final_answer=response.message.content,
                messages=messages,
                trace=trace,
                turns=turn,
            )

        if response.message.tool_calls:
            for tc in response.message.tool_calls:
                result = self.tool_registry.execute(
                    tc.name, tc.args,
                    enabled=self.config.tools,
                    tool_parameters=self.config.tool_parameters,
                )
                messages.append(Message(
                    role="tool",
                    content=result.content,
                    tool_call_id=tc.id,
                    name=tc.name,
                ))
                if result.stop_agent:
                    return AgentRunResult(
                        final_answer=result.stop_answer or "",
                        messages=messages,
                        trace=trace,
                        turns=turn,
                    )
            continue

        # No tool calls and not "stop" → loop again with the assistant message as the latest turn.
```

The event_log / trace structure (`trace: list[dict[str, Any]]`) keeps using dicts because it is a serialized log, not a structured exchange. Where it embeds a message, it uses `dataclasses.asdict(msg)`.

### 5.4 `team/*.py` follow-on changes

Files that build messages or call `GeneralPurposeAgent.run(extra_messages=...)`:

- `team/worker.py`
- `team/curator.py`
- `team/orchestrator.py`
- `team/debug_agent.py`
- `team/tools.py` (where it dispatches subagents via the `Agent` tool)

All dict literals `{"role": "user", "content": ...}` become `Message(role="user", content=...)`. Mechanical, but high count.

`team/tools.py` defines:

```python
ClientFactory = Callable[[GeneralAgentConfig], Any]   # current
```

After refactor:

```python
ClientFactory = Callable[[GeneralAgentConfig], ChatClient]
```

The factory's behaviour comes from `alphasolve.llm.make_client_factory(profile, presets)`. The shape stays as a `Callable` to avoid coupling `team/` to the construction details.

### 5.5 Agent YAML migration

The existing YAML structure wraps fields under an `agent:` key:

```yaml
# before — src/alphasolve/config/agents/verifier_adversarial.yaml
version: 1
agent:
  name: verifier_adversarial
  system_prompt_path: ../../prompts/verifier_adversarial.md
  model_config: VERIFIER_CONFIG
  max_turns: 80
  tools: [Read, Write, ...]
```

```yaml
# after
version: 1
agent:
  name: verifier_adversarial
  system_prompt_path: ../../prompts/verifier_adversarial.md
  role: verifier                              # ← replaces model_config
  max_turns: 80
  tools: [Read, Write, ...]
```

For each file under `src/alphasolve/config/agents/` and `src/alphasolve/config/subagents/`:

1. Remove the `model_config:` line.
2. Add `role: <role-name>` line per the mapping below.

The mapping is **mechanical**: take the original `model_config:` constant name and lowercase + drop the `_CONFIG` suffix. Files that today share the same `model_config:` constant continue to share the same role.

| Old `model_config:` value | New `role:` value | Affected files (verified against current YAML content) |
|---|---|---|
| `GENERATOR_CONFIG`        | `generator`        | `agents/generator.yaml` |
| `VERIFIER_CONFIG`         | `verifier`         | `agents/verifier.yaml`, `agents/verifier_adversarial.yaml`, `agents/verifier_citation.yaml`, `agents/verifier_failure_modes.yaml`, `agents/verifier_premise_chain.yaml`, `agents/verifier_stepwise.yaml`, `agents/theorem_checker.yaml` |
| `REVISER_CONFIG`          | `reviser`          | `agents/reviser.yaml` |
| `ORCHESTRATOR_CONFIG`     | `orchestrator`     | `agents/orchestrator.yaml`, `subagents/research_reviewer.yaml` |
| `CURATOR_CONFIG`          | `curator`          | `subagents/curator.yaml` |
| `COMPUTE_SUBAGENT_CONFIG` | `compute_subagent` | `subagents/compute_subagent.yaml`, `subagents/numerical_experiment_subagent.yaml` |
| `PROOF_SUBAGENT_CONFIG`   | `proof_subagent`   | `subagents/reasoning_subagent.yaml` |

Notes:

- The seven verifier-family files (six verifiers + `theorem_checker`) intentionally share `role: verifier` — one preset drives all of them. If, post-refactor, you want to give `theorem_checker` or one of the verifier variants its own model, add a new role to `profiles.yaml` and update only that file's `role:` field.
- `research_reviewer.yaml` shares `role: orchestrator` because today it uses `ORCHESTRATOR_CONFIG`. This precisely preserves current behaviour. If the asymmetry feels odd, splitting it into its own `role: research_reviewer` is a one-line `profiles.yaml` change later and out of scope here.
- No YAML in the current tree lacks a `model_config:` field, so the "fallback `role = name`" case does not arise in practice today. The fallback exists for new agents authored after the refactor that don't need a profile-driven model.
- Pre-existing inconsistency: `subagents/curator.yaml` uses `prompt_path:` while others use `system_prompt_path:`. This refactor does not touch that field — the YAML loader continues to accept both. (Flagged here so it isn't mistaken for new breakage.)

### 5.6 No silent backward compat

`load_agent_suite_config()` is modified so that, if any YAML in the config tree still contains a `model_config:` field, loading **raises** immediately with a message of the form:

```
verifier.yaml: field 'model_config' is no longer supported.
Replace with 'role: verifier' (see docs/superpowers/specs/2026-05-22-alphasolve-llm-provider-abstraction-design.md §5.5).
```

---

## 6. CLI

### 6.1 New flags

```
alphasolve run [PROJECT_DIR] [OPTIONS]

  --profile NAME           Model profile (cheap | balanced | strategic | <user-defined>).
                           Default: ALPHASOLVE_PROFILE env var, else profiles.yaml `default:`.
  --list-profiles          List all profiles and exit.
  --list-presets           List all presets and exit.
```

### 6.2 Resolution order for active profile

```
--profile flag  >  ALPHASOLVE_PROFILE env var  >  profiles.yaml `default:` key
```

If none of the three resolves to a valid profile name, `cli.py` exits non-zero with a clear error listing available profiles.

### 6.3 Workflow constructor change

`alphasolve.workflow.AlphaSolve.__init__` currently accepts:

```python
client_factory: ClientFactory | None = None,
```

After refactor:

```python
client_factory: ClientFactory,    # required
```

`workflow.py` does not load `presets.yaml` or `profiles.yaml`. The CLI does that and hands the constructed factory to `AlphaSolve`. This isolation is enforced by the dependency rule (§2.2).

### 6.4 Not in scope

- No persistent / interactive profile switcher (no `alphasolve switch`).
- No state file remembering the last-used profile.
- No mid-run profile change. Profile is captured at workflow construction time and immutable for the duration of a run.

---

## 7. Migration Plan

Six commits on `refactor/llm-provider-abstraction`. After each commit, `pytest` must pass and the package must import cleanly.

### Commit 1 — `feat(llm): scaffold llm package + Preset/Profile loaders`

- Create `src/alphasolve/llm/` directory tree.
- Implement `types.py` dataclasses.
- Implement `config/preset.py`, `config/profile.py`, `config/loader.py`.
- Write `src/alphasolve/config/presets.yaml` and `profiles.yaml`.
- Add `tests/llm/test_preset_loader.py` and `tests/llm/test_profile_loader.py`.
- No other file modified. Existing code path unchanged.

### Commit 2 — `feat(llm): port OpenAIChatClient + add AnthropicMessagesClient`

- Implement `providers/openai_chat.py`. Lift the existing `OpenAIChatClient` body from `general_agent.py`; rework `__init__` to take `Preset`.
- Implement `providers/anthropic_messages.py` from scratch. Use the `anthropic` SDK.
- Implement `factory.py` with `make_client` and `make_client_factory`.
- Add `tests/llm/test_openai_chat_client.py`, `tests/llm/test_anthropic_messages_client.py`, `tests/llm/test_factory.py`, `tests/llm/test_message_conversion.py`.
- Both providers expose the new `ChatClient` Protocol with typed `Message`/`ToolDef`/`CompletionResponse`.
- `general_agent.py` still uses its own old `OpenAIChatClient` definition; the new one is unwired.

### Commit 3 — `refactor(llm): wire new ChatClient through general_agent (temporary shim)`

- `general_agent.py` deletes its inline `OpenAIChatClient` class and imports from `alphasolve.llm.providers.openai_chat`.
- Add `add anthropic to pyproject.toml dependencies`.
- A small adapter inside `general_agent.py` translates between its current dict-shaped messages and the new typed `Message` at the boundary of `client.complete()`. The adapter is marked `# REMOVE in commit 4` in the code.
- Tests in `tests/test_general_agent.py` keep passing without source-level changes (the adapter preserves dict-shape internals).

### Commit 4 — `refactor(agent): adopt typed messages in GeneralPurposeAgent`

This is the largest commit. In one atomic change:

- `GeneralAgentConfig`: delete `model_config`, add `role`, list→tuple for `tools`/`skills`, add `effective_role()`.
- `ToolRegistry`: rename `openai_tools` → `tool_defs`; delete `RegisteredTool.to_openai_tool()`.
- `general_agent.py`: remove the temporary shim from commit 3; use `Message`/`ToolDef`/`CompletionResponse` throughout `run()`.
- `team/worker.py`, `team/curator.py`, `team/orchestrator.py`, `team/debug_agent.py`, `team/tools.py`: replace all `{"role": ..., "content": ...}` dict literals with `Message(role=..., content=...)`.
- Delete the 11 provider constants and 7 role aliases from `agent_config.py`. Reduce `AlphaSolveConfig` to its Wolfram block.
- Update test fixtures and assertions accordingly.

If this commit is unmanageably large in practice, split it into 4a (`GeneralAgentConfig` + `ToolRegistry`), 4b (`general_agent.py`), 4c (`team/*.py`).

### Commit 5 — `refactor(config): migrate agent YAML files to role: schema`

- Edit every file under `src/alphasolve/config/agents/` and `subagents/` per §5.5.
- Update `load_agent_suite_config()` to raise on `model_config:` field.
- Add test asserting that loading a YAML containing `model_config:` raises with the expected message.

### Commit 6 — `feat(cli): wire --profile flag end-to-end + smoke runs`

- Add `--profile`, `--list-profiles`, `--list-presets` to `cli.py`.
- Make `workflow.AlphaSolve.__init__`'s `client_factory` required.
- Run three manual smoke tests:
  - `alphasolve run <fixture> --profile balanced` (must produce trace identical to pre-refactor baseline)
  - `alphasolve run <fixture> --profile cheap`
  - `alphasolve run <fixture> --profile strategic`
- Update README / AGENTS.md as needed.

### Branch & merge policy

- Branch: `refactor/llm-provider-abstraction`
- Do not squash on merge. Preserve the 6-commit boundary for bisecting.

---

## 8. Test Plan

### 8.1 New tests under `tests/llm/`

| File | Scope |
|---|---|
| `test_preset_loader.py` | YAML parsing; env-var name resolution; missing env raises with preset name; user override (whole-record replacement); unknown `wire_format` raises; malformed YAML raises with file path. |
| `test_profile_loader.py` | Profile loading; `default:` fallback; user override; unknown role lookup raises with available-roles hint; CLI flag > env var > yaml default precedence. |
| `test_openai_chat_client.py` | Mock the `openai` SDK. Verify request shape (tools serialised correctly, system message in first slot). Parse response into `CompletionResponse`. Retry on `RemoteProtocolError` (port the existing `test_openai_chat_client_retries_*` cases). `tool_calls[i].arguments` is a JSON string in the wire response; after construction, `Message.tool_calls[i].args` is a dict. |
| `test_anthropic_messages_client.py` | Mock the `anthropic` SDK. Verify: (a) system message is lifted to `system=` kwarg; (b) consecutive `role:"tool"` messages are collapsed into the following user message as `tool_result` content blocks; (c) `tool_use.input` arrives as dict; `Message.tool_calls[i].args` is the same dict by value (no double-stringification); (d) streaming `input_json_delta` events accumulate correctly into `StreamDelta(type="tool_input", arg_delta=...)`. |
| `test_message_conversion.py` | Bidirectional round-trips for both wire formats. **Key case**: an Edit tool call with `old_str = "\\frac{1}{2}"` and `new_str = "\\frac{2}{3}"` round-trips correctly through both wires. Assert that the LaTeX string survives as one backslash in `tool_calls[i].args["old_str"]`. |
| `test_factory.py` | `make_client` dispatches on `wire_format`. `make_client_factory(profile, presets)` returns a callable that, given a `GeneralAgentConfig` with `role="verifier"` and a profile mapping `verifier → deepseek-pro`, constructs a `ChatClient` for `deepseek-pro`. Unknown role / unknown preset / unknown wire_format each raise with the right message. |

### 8.2 Existing tests requiring mechanical update

- `tests/test_general_agent.py` (63 symbols). Replace all dict-shaped message constructions and assertions with `Message`. Replace any reference to `*_CONFIG` constants with `Preset` constructed inline or loaded from a test YAML fixture.
- `tests/test_agent_team.py` (108 symbols). Same as above. `make_demo_client_factory()` must return a callable producing objects satisfying the new `ChatClient` Protocol (returning `CompletionResponse`, not dicts).
- `tests/test_agent_debug.py`, `tests/test_bug_investigation.py`, `tests/test_worker_manager_status.py`, `tests/test_verifier_citation.py`: mechanical `dict → Message` conversion.
- `tests/test_dashboard_renderer.py`, `tests/test_search_and_replace.py`: likely unaffected; verify after commit 4.

### 8.3 Smoke tests (manual)

Run from a known-good problem fixture (e.g. an existing test problem in `tests/fixtures/`):

1. `alphasolve run <fixture> --profile balanced`. Verify the run completes and the trace matches the pre-refactor baseline trace for the same fixture line-by-line (modulo timing). This is the core regression test.
2. `alphasolve run <fixture> --profile cheap`. Verify the run completes; verify each agent's preset resolution by inspecting logs.
3. `alphasolve run <fixture> --profile strategic`. Verify the orchestrator uses `qwen-3.7-max` and the Anthropic-format providers (if used in the profile after URL/model_id confirmation) work end-to-end.

### 8.4 Not tested

- The `openai` and `anthropic` SDK internals — assumed correct.
- Live provider endpoints — all tests mock the SDK boundary.
- Performance / token-cost regression — separate evaluation, not part of this milestone.

---

## 9. Open Implementation TODOs

These are placeholders in `presets.yaml` and `profiles.yaml`. The user has agreed to confirm them during implementation, before commit 6:

1. **DeepSeek Anthropic endpoint URL** — `https://api.deepseek.com/anthropic` is a guess; confirm in DeepSeek docs.
2. **Moonshot Anthropic endpoint URL** — `https://api.moonshot.cn/anthropic` is a guess; confirm in Moonshot docs.
3. **Qwen 3.7 Max model_id** — `qwen3.7-max` is a guess; confirm with Dashscope (or whichever endpoint is chosen).

If any of these turn out to be unavailable or named differently, the relevant preset is renamed / removed and the affected profile entry updated. None of this affects the rest of the design.

---

## 10. Future Milestones (out of scope for this spec)

These items were identified during brainstorming as valuable but deliberately excluded from this milestone. Listed here so future planning has a reference.

- **M2: Agent layer extraction**. Lift `agents/general/` into a sub-package with a dependency rule mirroring §2.2 (no inward imports from `agents/team/`). Solve the `team/tools.py → general_agent.py` reverse-import. Possibly per-agent tool description / per-agent tool schema customisation.
- **M3: Compaction**. Implement automatic context summarisation modelled after pi-agent-core's `harness/compaction/`. Wire prompt caching as a first-class abstraction field at this point (the caching investment only pays off in conjunction with compaction). Add `cache_control` to `Preset` and `Message`.
- **M4: Skills wiring**. Implement a skill registry analogous to pi's `harness/skills.ts`. Use `GeneralAgentConfig.skills` to load skill definitions and render them into system prompts. Add `when_to_use` rendering.
- **M5: Additional native providers**. Add Gemini native, Bedrock native, Mistral native, Azure OpenAI Responses, etc., if specific provider features (e.g. Gemini's structured grounding, Bedrock's IAM auth) become necessary.
- **M6: Persistent / interactive profile UX**. If profile switching becomes a frequent operation, add `alphasolve switch` as a stateful command (writing a local active-profile file consumed by subsequent `alphasolve run` invocations).
