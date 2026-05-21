# AlphaSolve LLM Provider Abstraction — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce `alphasolve.llm` sub-package with a typed lingua franca, OpenAI Chat Completions + Anthropic Messages wire formats, and profile-based per-role model switching (`cheap` / `balanced` / `strategic`) accessible via `--profile`.

**Architecture:** A new `src/alphasolve/llm/` sub-package owns LLM provider abstraction and config loading. The agent layer adopts typed `Message` / `ToolCall` / `ToolDef` / `CompletionResponse` instead of `dict[str, Any]`. All 11 provider constants in `agent_config.py` are deleted; per-role models flow from `profiles.yaml` via a profile selected by CLI flag. Single-direction dependency: `alphasolve.agents` may import from `alphasolve.llm`, never the reverse.

**Tech Stack:** Python 3.10+, `openai` SDK (existing), `anthropic` SDK (new dependency), PyYAML, argparse, pytest. Reference: `docs/superpowers/specs/2026-05-22-alphasolve-llm-provider-abstraction-design.md`.

---

## File Structure

### Created
- `src/alphasolve/llm/__init__.py` — public API (19 re-exports)
- `src/alphasolve/llm/types.py` — `Message`, `ToolCall`, `ToolDef`, `CompletionResponse`, `Usage`, `StreamDelta`, `Role`, `FinishReason`, `ChatClient` Protocol, `ChatDeltaSink`, `ChatCompletionError`
- `src/alphasolve/llm/config/__init__.py`
- `src/alphasolve/llm/config/preset.py` — `Preset` dataclass, `WireFormat` type
- `src/alphasolve/llm/config/profile.py` — `Profile` dataclass
- `src/alphasolve/llm/config/loader.py` — `load_presets`, `load_profile`, `load_active_profile`
- `src/alphasolve/llm/factory.py` — `make_client`, `make_client_factory`
- `src/alphasolve/llm/providers/__init__.py`
- `src/alphasolve/llm/providers/openai_chat.py` — `OpenAIChatClient` (ported from general_agent.py)
- `src/alphasolve/llm/providers/anthropic_messages.py` — `AnthropicMessagesClient` (new)
- `src/alphasolve/config/presets.yaml` — canonical preset registry
- `src/alphasolve/config/profiles.yaml` — canonical profile registry + default
- `tests/llm/__init__.py`
- `tests/llm/test_types.py`
- `tests/llm/test_preset.py`
- `tests/llm/test_profile.py`
- `tests/llm/test_preset_loader.py`
- `tests/llm/test_profile_loader.py`
- `tests/llm/test_openai_chat_client.py`
- `tests/llm/test_anthropic_messages_client.py`
- `tests/llm/test_message_conversion.py`
- `tests/llm/test_factory.py`

### Modified
- `pyproject.toml` — add `anthropic>=0.40` dependency
- `src/alphasolve/agents/general/general_agent.py:62-212` — delete `OpenAIChatClient` class (moved); use new `Message`/`CompletionResponse` types throughout `GeneralPurposeAgent.run()`
- `src/alphasolve/agents/general/general_agent.py:21,26-34` — `ChatDeltaSink` and `ChatClient` re-exported from `alphasolve.llm` (kept here as compatibility shim or deleted)
- `src/alphasolve/agents/general/config.py:13-25` — `GeneralAgentConfig`: delete `model_config`, add `role`, list→tuple for `tools`/`skills`, add `effective_role()` method
- `src/alphasolve/agents/general/config.py:197-209` — `_resolve_agent_config` updated to read `role:` instead of `model_config:`; raises clearly when `model_config:` is present
- `src/alphasolve/agents/general/tool_registry.py:68-78` — rename `openai_tools` → `tool_defs`; return `list[ToolDef]`
- `src/alphasolve/agents/general/tool_registry.py:33-44` — delete `RegisteredTool.to_openai_tool()`
- `src/alphasolve/agents/team/worker.py`, `curator.py`, `orchestrator.py`, `debug_agent.py`, `tools.py`, `demo.py`, `workflow.py` — replace dict-shaped messages with `Message` constructors
- `src/alphasolve/agents/team/workflow.py:281` — `make_openai_client_factory` either deleted (replaced by `alphasolve.llm.make_client_factory`) or rewritten to thinly wrap it
- `src/alphasolve/config/agent_config.py` — delete all 11 provider constants + 7 role aliases; reduce to Wolfram-only block
- `src/alphasolve/config/agents/*.yaml` (10 files) — remove `model_config:`, add `role:`
- `src/alphasolve/config/subagents/*.yaml` (5 files) — remove `model_config:`, add `role:`
- `src/alphasolve/cli.py:111-216` — add `--profile`, `--list-profiles`, `--list-presets`; remove unconditional `make_openai_client_factory` import path
- Existing tests under `tests/` — replace dict-shaped message constructions with `Message`; replace `*_CONFIG` constant references with `Preset` instances

---

## Branch Setup (one-time, before Task 1.1)

```bash
git checkout main
git pull
git checkout -b refactor/llm-provider-abstraction
```

The plan assumes this branch is active. Each "Commit" boundary in this plan is one git commit on this branch.

---

## Commit 1: Scaffold `llm` package + loaders

**End state:** `alphasolve.llm` package exists with typed data classes, preset/profile data classes, YAML loader, and complete `presets.yaml` + `profiles.yaml`. Nothing in `agents/` or the rest of the codebase changes. `pytest tests/llm/ -v` is fully green.

### Task 1.1: Add anthropic dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add `anthropic` to dependencies**

Edit `pyproject.toml`, replace the `dependencies = [...]` block:

```toml
dependencies = [
    "openai>=1.0",
    "anthropic>=0.40",
    "wolframclient>=1.3",
    "rich>=13.0",
    "PyYAML>=6.0",
    "httpx>=0.28.1",
]
```

- [ ] **Step 2: Re-sync the environment**

Run: `uv sync`
Expected: `anthropic` is installed; existing imports still work.

- [ ] **Step 3: Verify import**

Run: `python -c "import anthropic; print(anthropic.__version__)"`
Expected: prints a version number (e.g. `0.40.x` or newer); no `ModuleNotFoundError`.

### Task 1.2: Scaffold `llm` package directory tree

**Files:**
- Create: `src/alphasolve/llm/__init__.py` (empty initially)
- Create: `src/alphasolve/llm/types.py` (empty initially)
- Create: `src/alphasolve/llm/factory.py` (empty initially)
- Create: `src/alphasolve/llm/config/__init__.py` (empty initially)
- Create: `src/alphasolve/llm/config/preset.py` (empty initially)
- Create: `src/alphasolve/llm/config/profile.py` (empty initially)
- Create: `src/alphasolve/llm/config/loader.py` (empty initially)
- Create: `src/alphasolve/llm/providers/__init__.py` (empty initially)
- Create: `src/alphasolve/llm/providers/openai_chat.py` (empty initially)
- Create: `src/alphasolve/llm/providers/anthropic_messages.py` (empty initially)
- Create: `tests/llm/__init__.py` (empty)

- [ ] **Step 1: Create the directory tree and empty files**

Run:
```bash
mkdir -p src/alphasolve/llm/config src/alphasolve/llm/providers tests/llm
touch src/alphasolve/llm/__init__.py src/alphasolve/llm/types.py src/alphasolve/llm/factory.py
touch src/alphasolve/llm/config/__init__.py src/alphasolve/llm/config/preset.py src/alphasolve/llm/config/profile.py src/alphasolve/llm/config/loader.py
touch src/alphasolve/llm/providers/__init__.py src/alphasolve/llm/providers/openai_chat.py src/alphasolve/llm/providers/anthropic_messages.py
touch tests/llm/__init__.py
```

- [ ] **Step 2: Verify import**

Run: `python -c "import alphasolve.llm"`
Expected: no error; empty package imports cleanly.

### Task 1.3: Implement `types.py` lingua franca

**Files:**
- Create: `src/alphasolve/llm/types.py`
- Test: `tests/llm/test_types.py`

- [ ] **Step 1: Write the failing tests**

Write `tests/llm/test_types.py`:

```python
from __future__ import annotations

import pytest
import dataclasses

from alphasolve.llm.types import (
    Message, ToolCall, ToolDef, CompletionResponse, Usage, StreamDelta,
)


def test_message_is_frozen():
    msg = Message(role="user", content="hi")
    with pytest.raises(dataclasses.FrozenInstanceError):
        msg.content = "no"


def test_message_assistant_with_tool_calls():
    tc = ToolCall(id="call_1", name="Read", args={"path": "foo.md"})
    msg = Message(role="assistant", content="reading", tool_calls=(tc,))
    assert msg.tool_calls == (tc,)
    # tool_calls is a tuple — cannot append
    with pytest.raises(AttributeError):
        msg.tool_calls.append(tc)


def test_tool_call_args_is_dict_not_string():
    tc = ToolCall(id="x", name="Edit", args={"old_str": "\\frac{1}{2}"})
    assert isinstance(tc.args, dict)
    assert tc.args["old_str"] == "\\frac{1}{2}"


def test_tool_def_fields():
    td = ToolDef(name="Read", description="read a file", parameters={"type": "object"})
    assert td.name == "Read"
    assert td.parameters == {"type": "object"}


def test_completion_response_with_usage():
    msg = Message(role="assistant", content="ok")
    usage = Usage(input_tokens=5, output_tokens=2)
    resp = CompletionResponse(message=msg, finish_reason="stop", usage=usage)
    assert resp.usage.input_tokens == 5


def test_completion_response_raw_excluded_from_equality():
    msg = Message(role="assistant", content="ok")
    a = CompletionResponse(message=msg, finish_reason="stop", raw={"x": 1})
    b = CompletionResponse(message=msg, finish_reason="stop", raw={"x": 2})
    assert a == b  # raw differs but is excluded from comparison


def test_stream_delta_text():
    d = StreamDelta(type="text", text="hello")
    assert d.text == "hello"


def test_stream_delta_tool_input():
    d = StreamDelta(type="tool_input", tool_call_id="call_1", arg_delta='{"path"')
    assert d.tool_call_id == "call_1"
```

- [ ] **Step 2: Run tests, verify failure**

Run: `pytest tests/llm/test_types.py -v`
Expected: ImportError or collection error because `alphasolve.llm.types` is empty.

- [ ] **Step 3: Implement `types.py`**

Write `src/alphasolve/llm/types.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

Role = Literal["system", "user", "assistant", "tool"]
FinishReason = Literal["stop", "tool_calls", "length", "content_filter", "error"]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


@dataclass(frozen=True)
class CompletionResponse:
    message: Message
    finish_reason: FinishReason
    usage: Usage = field(default_factory=Usage)
    raw: Any = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class StreamDelta:
    type: Literal["text", "tool_input"]
    text: str = ""
    tool_call_id: str = ""
    arg_delta: str = ""


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

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/llm/test_types.py -v`
Expected: all 8 tests pass.

### Task 1.4: Implement `Preset` dataclass

**Files:**
- Create: `src/alphasolve/llm/config/preset.py`
- Test: `tests/llm/test_preset.py`

- [ ] **Step 1: Write the failing tests**

Write `tests/llm/test_preset.py`:

```python
from __future__ import annotations

import pytest

from alphasolve.llm.config.preset import Preset


def test_preset_construction():
    p = Preset(
        name="deepseek-pro",
        wire_format="openai_chat",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        model="deepseek-v4-pro",
    )
    assert p.name == "deepseek-pro"
    assert p.timeout == 3600
    assert p.params == {}


def test_preset_with_params():
    p = Preset(
        name="kimi",
        wire_format="openai_chat",
        base_url="https://api.moonshot.cn/v1",
        api_key_env="MOONSHOT_API_KEY",
        model="kimi-k2-thinking",
        params={"temperature": 1.0},
    )
    assert p.params == {"temperature": 1.0}


def test_preset_is_frozen():
    p = Preset(name="x", wire_format="openai_chat", base_url="u", api_key_env="K", model="m")
    with pytest.raises(Exception):  # FrozenInstanceError
        p.name = "y"


def test_resolve_api_key_happy(monkeypatch):
    monkeypatch.setenv("MY_SECRET", "sk-xyz")
    p = Preset(name="x", wire_format="openai_chat", base_url="u", api_key_env="MY_SECRET", model="m")
    assert p.resolve_api_key() == "sk-xyz"


def test_resolve_api_key_missing_env(monkeypatch):
    monkeypatch.delenv("NEVER_SET_VAR", raising=False)
    p = Preset(name="prov", wire_format="openai_chat", base_url="u", api_key_env="NEVER_SET_VAR", model="m")
    with pytest.raises(RuntimeError) as exc:
        p.resolve_api_key()
    assert "NEVER_SET_VAR" in str(exc.value)
    assert "prov" in str(exc.value)


def test_unknown_wire_format_type_hint_does_not_enforce_at_construction():
    # Literal types are not runtime-enforced; loader must check.
    Preset(name="x", wire_format="anthropic_messages", base_url="u", api_key_env="K", model="m")
```

- [ ] **Step 2: Run tests, verify failure**

Run: `pytest tests/llm/test_preset.py -v`
Expected: ImportError because `preset.py` is empty.

- [ ] **Step 3: Implement `preset.py`**

Write `src/alphasolve/llm/config/preset.py`:

```python
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

WireFormat = Literal["openai_chat", "anthropic_messages"]


@dataclass(frozen=True)
class Preset:
    name: str
    wire_format: WireFormat
    base_url: str
    api_key_env: str
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

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/llm/test_preset.py -v`
Expected: 6 tests pass.

### Task 1.5: Implement `Profile` dataclass

**Files:**
- Create: `src/alphasolve/llm/config/profile.py`
- Test: `tests/llm/test_profile.py`

- [ ] **Step 1: Write the failing tests**

Write `tests/llm/test_profile.py`:

```python
from __future__ import annotations

import pytest

from alphasolve.llm.config.profile import Profile


def test_profile_construction():
    p = Profile(
        name="balanced",
        role_to_preset={
            "orchestrator": "deepseek-pro",
            "verifier": "deepseek-pro",
        },
    )
    assert p.name == "balanced"
    assert p.preset_for("verifier") == "deepseek-pro"


def test_preset_for_unknown_role_raises():
    p = Profile(name="cheap", role_to_preset={"orchestrator": "deepseek-flash"})
    with pytest.raises(KeyError) as exc:
        p.preset_for("verifier")
    assert "verifier" in str(exc.value)
    assert "cheap" in str(exc.value)
    assert "orchestrator" in str(exc.value)  # available roles listed
```

- [ ] **Step 2: Run tests, verify failure**

Run: `pytest tests/llm/test_profile.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `profile.py`**

Write `src/alphasolve/llm/config/profile.py`:

```python
from __future__ import annotations

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
            ) from None
```

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/llm/test_profile.py -v`
Expected: 2 tests pass.

### Task 1.6: Write `presets.yaml`

**Files:**
- Create: `src/alphasolve/config/presets.yaml`

- [ ] **Step 1: Write the complete preset registry**

Write `src/alphasolve/config/presets.yaml`:

```yaml
# OpenAI Chat Completions presets (1:1 migration from agent_config.py)

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

# Anthropic Messages presets (new). All URLs/model_ids confirmed by user
# before implementation.

deepseek-pro-anthropic:
  wire_format: anthropic_messages
  base_url: https://api.deepseek.com/anthropic
  api_key_env: DEEPSEEK_API_KEY
  model: deepseek-v4-pro
  timeout: 3600

moonshot-kimi-anthropic:
  wire_format: anthropic_messages
  base_url: https://api.moonshot.cn/anthropic
  api_key_env: MOONSHOT_API_KEY
  model: kimi-k2-thinking
  timeout: 3600

# Strategic-tier orchestrator model

qwen-3.7-max:
  wire_format: openai_chat
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  api_key_env: DASHSCOPE_API_KEY
  model: qwen3.7-max
  timeout: 3600
```

- [ ] **Step 2: Verify YAML parses**

Run: `python -c "import yaml; print(len(yaml.safe_load(open('src/alphasolve/config/presets.yaml'))))"`
Expected: prints `13` (10 OpenAI + 2 Anthropic + 1 qwen).

### Task 1.7: Write `profiles.yaml`

**Files:**
- Create: `src/alphasolve/config/profiles.yaml`

- [ ] **Step 1: Write the complete profile registry**

Write `src/alphasolve/config/profiles.yaml`:

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

- [ ] **Step 2: Verify YAML parses**

Run: `python -c "import yaml; data = yaml.safe_load(open('src/alphasolve/config/profiles.yaml')); assert data['default'] == 'balanced'; assert set(data) - {'default'} == {'cheap', 'balanced', 'strategic'}; print('ok')"`
Expected: prints `ok`.

### Task 1.8: Implement `load_presets`

**Files:**
- Create: `src/alphasolve/llm/config/loader.py`
- Test: `tests/llm/test_preset_loader.py`

- [ ] **Step 1: Write the failing tests**

Write `tests/llm/test_preset_loader.py`:

```python
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from alphasolve.llm.config.loader import load_presets
from alphasolve.llm.config.preset import Preset


def _write(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def test_loads_simple_preset(tmp_path):
    repo_yaml = tmp_path / "presets.yaml"
    _write(repo_yaml, """
        deepseek-pro:
          wire_format: openai_chat
          base_url: https://api.deepseek.com
          api_key_env: DEEPSEEK_API_KEY
          model: deepseek-v4-pro
    """)
    presets = load_presets(repo_path=repo_yaml, user_path=None)
    assert "deepseek-pro" in presets
    p = presets["deepseek-pro"]
    assert isinstance(p, Preset)
    assert p.model == "deepseek-v4-pro"
    assert p.timeout == 3600  # default
    assert p.params == {}     # default


def test_loads_preset_with_params(tmp_path):
    repo_yaml = tmp_path / "presets.yaml"
    _write(repo_yaml, """
        kimi:
          wire_format: openai_chat
          base_url: https://api.moonshot.cn/v1
          api_key_env: MOONSHOT_API_KEY
          model: kimi-k2-thinking
          timeout: 1800
          params:
            temperature: 1.0
            extra_body: { reasoning: { effort: max } }
    """)
    presets = load_presets(repo_path=repo_yaml, user_path=None)
    p = presets["kimi"]
    assert p.timeout == 1800
    assert p.params["temperature"] == 1.0
    assert p.params["extra_body"]["reasoning"]["effort"] == "max"


def test_unknown_wire_format_raises(tmp_path):
    repo_yaml = tmp_path / "presets.yaml"
    _write(repo_yaml, """
        bad:
          wire_format: gemini_native
          base_url: https://example.com
          api_key_env: X
          model: y
    """)
    with pytest.raises(ValueError) as exc:
        load_presets(repo_path=repo_yaml, user_path=None)
    assert "gemini_native" in str(exc.value)
    assert "bad" in str(exc.value)


def test_missing_required_field_raises(tmp_path):
    repo_yaml = tmp_path / "presets.yaml"
    _write(repo_yaml, """
        broken:
          wire_format: openai_chat
          base_url: https://example.com
          # missing api_key_env and model
    """)
    with pytest.raises(ValueError) as exc:
        load_presets(repo_path=repo_yaml, user_path=None)
    assert "broken" in str(exc.value)


def test_user_override_replaces_whole_record(tmp_path):
    repo_yaml = tmp_path / "presets.yaml"
    user_yaml = tmp_path / "user_presets.yaml"
    _write(repo_yaml, """
        deepseek-pro:
          wire_format: openai_chat
          base_url: https://api.deepseek.com
          api_key_env: DEEPSEEK_API_KEY
          model: deepseek-v4-pro
          params: { extra_body: { reasoning: { effort: max } } }
    """)
    _write(user_yaml, """
        deepseek-pro:
          wire_format: openai_chat
          base_url: https://my-proxy.internal/deepseek
          api_key_env: MY_PROXY_KEY
          model: deepseek-v4-pro
          timeout: 7200
        # note: params absent → user wins, no merge from repo
    """)
    presets = load_presets(repo_path=repo_yaml, user_path=user_yaml)
    p = presets["deepseek-pro"]
    assert p.base_url == "https://my-proxy.internal/deepseek"
    assert p.api_key_env == "MY_PROXY_KEY"
    assert p.timeout == 7200
    assert p.params == {}, "user-provided preset replaces wholesale, no field merge"


def test_user_adds_new_preset(tmp_path):
    repo_yaml = tmp_path / "presets.yaml"
    user_yaml = tmp_path / "user_presets.yaml"
    _write(repo_yaml, """
        deepseek-pro:
          wire_format: openai_chat
          base_url: https://api.deepseek.com
          api_key_env: DEEPSEEK_API_KEY
          model: deepseek-v4-pro
    """)
    _write(user_yaml, """
        my-claude:
          wire_format: anthropic_messages
          base_url: https://api.anthropic.com
          api_key_env: ANTHROPIC_API_KEY
          model: claude-opus-4
    """)
    presets = load_presets(repo_path=repo_yaml, user_path=user_yaml)
    assert "deepseek-pro" in presets
    assert "my-claude" in presets
    assert presets["my-claude"].wire_format == "anthropic_messages"


def test_user_path_nonexistent_is_ok(tmp_path):
    repo_yaml = tmp_path / "presets.yaml"
    _write(repo_yaml, """
        x:
          wire_format: openai_chat
          base_url: u
          api_key_env: K
          model: m
    """)
    user_yaml = tmp_path / "does_not_exist.yaml"
    presets = load_presets(repo_path=repo_yaml, user_path=user_yaml)
    assert "x" in presets
```

- [ ] **Step 2: Run tests, verify failure**

Run: `pytest tests/llm/test_preset_loader.py -v`
Expected: ImportError because `loader.py` is empty.

- [ ] **Step 3: Implement `load_presets` in `loader.py`**

Write `src/alphasolve/llm/config/loader.py`:

```python
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

import yaml

from .preset import Preset
from .profile import Profile

_VALID_WIRE_FORMATS = {"openai_chat", "anthropic_messages"}
_REQUIRED_PRESET_FIELDS = ("wire_format", "base_url", "api_key_env", "model")


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, Mapping):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    return dict(data)


def _build_preset(name: str, raw: Mapping[str, Any]) -> Preset:
    if not isinstance(raw, Mapping):
        raise ValueError(f"preset {name!r}: value must be a mapping")
    missing = [field for field in _REQUIRED_PRESET_FIELDS if field not in raw]
    if missing:
        raise ValueError(f"preset {name!r}: missing required fields: {missing}")
    wire = raw["wire_format"]
    if wire not in _VALID_WIRE_FORMATS:
        raise ValueError(
            f"preset {name!r}: unknown wire_format {wire!r}; "
            f"expected one of {sorted(_VALID_WIRE_FORMATS)}"
        )
    return Preset(
        name=name,
        wire_format=wire,
        base_url=str(raw["base_url"]),
        api_key_env=str(raw["api_key_env"]),
        model=str(raw["model"]),
        timeout=float(raw.get("timeout", 3600)),
        params=dict(raw.get("params") or {}),
    )


def load_presets(*, repo_path: Path, user_path: Path | None) -> dict[str, Preset]:
    repo_raw = _read_yaml(repo_path)
    user_raw = _read_yaml(user_path) if user_path is not None else {}

    merged: dict[str, Mapping[str, Any]] = {}
    merged.update(repo_raw)
    merged.update(user_raw)  # whole-record replacement

    return {name: _build_preset(name, raw) for name, raw in merged.items()}
```

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/llm/test_preset_loader.py -v`
Expected: 7 tests pass.

### Task 1.9: Implement `load_profile` and `load_active_profile`

**Files:**
- Modify: `src/alphasolve/llm/config/loader.py`
- Test: `tests/llm/test_profile_loader.py`

- [ ] **Step 1: Write the failing tests**

Write `tests/llm/test_profile_loader.py`:

```python
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from alphasolve.llm.config.loader import load_profile, load_active_profile


def _write(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def test_load_named_profile(tmp_path):
    repo = tmp_path / "profiles.yaml"
    _write(repo, """
        balanced:
          orchestrator: deepseek-pro
          verifier: deepseek-pro
        cheap:
          orchestrator: deepseek-flash
          verifier: deepseek-flash
        default: balanced
    """)
    p = load_profile("cheap", repo_path=repo, user_path=None)
    assert p.name == "cheap"
    assert p.preset_for("orchestrator") == "deepseek-flash"


def test_unknown_profile_raises(tmp_path):
    repo = tmp_path / "profiles.yaml"
    _write(repo, """
        balanced:
          orchestrator: deepseek-pro
        default: balanced
    """)
    with pytest.raises(KeyError) as exc:
        load_profile("aggressive", repo_path=repo, user_path=None)
    msg = str(exc.value)
    assert "aggressive" in msg
    assert "balanced" in msg


def test_default_profile_via_active_loader(tmp_path):
    repo = tmp_path / "profiles.yaml"
    _write(repo, """
        balanced:
          orchestrator: deepseek-pro
        cheap:
          orchestrator: deepseek-flash
        default: balanced
    """)
    p = load_active_profile(name=None, repo_path=repo, user_path=None)
    assert p.name == "balanced"


def test_explicit_name_overrides_default(tmp_path):
    repo = tmp_path / "profiles.yaml"
    _write(repo, """
        balanced:
          orchestrator: deepseek-pro
        cheap:
          orchestrator: deepseek-flash
        default: balanced
    """)
    p = load_active_profile(name="cheap", repo_path=repo, user_path=None)
    assert p.name == "cheap"


def test_user_overrides_whole_profile(tmp_path):
    repo = tmp_path / "profiles.yaml"
    user = tmp_path / "user_profiles.yaml"
    _write(repo, """
        strategic:
          orchestrator: qwen-3.7-max
          verifier: deepseek-pro
          generator: deepseek-pro
        default: strategic
    """)
    _write(user, """
        strategic:
          orchestrator: internal-claude
          verifier: deepseek-pro-anthropic
          generator: deepseek-pro
    """)
    p = load_profile("strategic", repo_path=repo, user_path=user)
    assert p.preset_for("orchestrator") == "internal-claude"
    assert p.preset_for("verifier") == "deepseek-pro-anthropic"


def test_user_adds_new_profile(tmp_path):
    repo = tmp_path / "profiles.yaml"
    user = tmp_path / "user_profiles.yaml"
    _write(repo, """
        balanced:
          orchestrator: deepseek-pro
        default: balanced
    """)
    _write(user, """
        myteam:
          orchestrator: my-claude
    """)
    p = load_profile("myteam", repo_path=repo, user_path=user)
    assert p.preset_for("orchestrator") == "my-claude"


def test_no_default_and_no_name_raises(tmp_path):
    repo = tmp_path / "profiles.yaml"
    _write(repo, """
        balanced:
          orchestrator: deepseek-pro
        cheap:
          orchestrator: deepseek-flash
    """)
    with pytest.raises(ValueError) as exc:
        load_active_profile(name=None, repo_path=repo, user_path=None)
    assert "default" in str(exc.value)
```

- [ ] **Step 2: Run tests, verify failure**

Run: `pytest tests/llm/test_profile_loader.py -v`
Expected: AttributeError (functions not yet in `loader.py`).

- [ ] **Step 3: Extend `loader.py` with profile loaders**

Append to `src/alphasolve/llm/config/loader.py`:

```python
def _load_profiles_raw(repo_path: Path, user_path: Path | None) -> tuple[dict[str, Any], str | None]:
    repo_raw = _read_yaml(repo_path)
    user_raw = _read_yaml(user_path) if user_path is not None else {}

    default_name = user_raw.pop("default", None) or repo_raw.pop("default", None)

    merged: dict[str, Any] = {}
    merged.update(repo_raw)
    merged.update(user_raw)
    return merged, default_name


def _build_profile(name: str, raw: Mapping[str, Any]) -> Profile:
    if not isinstance(raw, Mapping):
        raise ValueError(f"profile {name!r}: value must be a mapping (role → preset)")
    role_to_preset = {str(role): str(preset) for role, preset in raw.items()}
    return Profile(name=name, role_to_preset=role_to_preset)


def load_profile(name: str, *, repo_path: Path, user_path: Path | None) -> Profile:
    merged, _ = _load_profiles_raw(repo_path, user_path)
    if name not in merged:
        raise KeyError(
            f"profile {name!r} not defined; "
            f"available profiles: {sorted(merged)}"
        )
    return _build_profile(name, merged[name])


def load_active_profile(*, name: str | None, repo_path: Path, user_path: Path | None) -> Profile:
    merged, default_name = _load_profiles_raw(repo_path, user_path)
    effective = name or default_name
    if effective is None:
        raise ValueError(
            f"no profile name given and no 'default:' key in profiles.yaml at {repo_path}"
        )
    if effective not in merged:
        raise KeyError(
            f"profile {effective!r} not defined; "
            f"available profiles: {sorted(merged)}"
        )
    return _build_profile(effective, merged[effective])
```

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/llm/test_profile_loader.py -v`
Expected: 7 tests pass.

### Task 1.10: Add path-resolution helpers in `loader.py`

**Files:**
- Modify: `src/alphasolve/llm/config/loader.py`
- Test: extend `tests/llm/test_preset_loader.py`

- [ ] **Step 1: Add test for $ALPHASOLVE_CONFIG_DIR resolution**

Append to `tests/llm/test_preset_loader.py`:

```python
def test_user_config_dir_resolves_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHASOLVE_CONFIG_DIR", str(tmp_path))
    from alphasolve.llm.config.loader import _user_config_dir
    assert _user_config_dir() == tmp_path


def test_user_config_dir_falls_back_to_home(monkeypatch):
    monkeypatch.delenv("ALPHASOLVE_CONFIG_DIR", raising=False)
    from alphasolve.llm.config.loader import _user_config_dir
    assert _user_config_dir() == Path.home() / ".alphasolve"
```

- [ ] **Step 2: Run tests, verify failure**

Run: `pytest tests/llm/test_preset_loader.py::test_user_config_dir_resolves_env_var tests/llm/test_preset_loader.py::test_user_config_dir_falls_back_to_home -v`
Expected: ImportError on `_user_config_dir`.

- [ ] **Step 3: Add `_user_config_dir` to `loader.py`**

Append to `src/alphasolve/llm/config/loader.py`:

```python
def _user_config_dir() -> Path:
    override = os.getenv("ALPHASOLVE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".alphasolve"
```

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/llm/test_preset_loader.py tests/llm/test_profile_loader.py -v`
Expected: all 16 tests pass (7 + 7 + 2 new).

### Task 1.11: Write `__init__.py` public API

**Files:**
- Modify: `src/alphasolve/llm/__init__.py`
- Modify: `src/alphasolve/llm/config/__init__.py`

- [ ] **Step 1: Add test that all 19 names are importable**

Create `tests/llm/test_public_api.py`:

```python
from __future__ import annotations


def test_public_api_exports_exactly_19_names():
    import alphasolve.llm as mod
    expected = {
        # Types
        "ChatClient", "ChatDeltaSink", "ChatCompletionError",
        "Message", "Role", "ToolCall", "ToolDef",
        "CompletionResponse", "Usage", "StreamDelta", "FinishReason",
        # Config
        "Preset", "Profile", "WireFormat",
        # Loaders + factory
        "load_presets", "load_profile", "load_active_profile",
        "make_client", "make_client_factory",
    }
    actual = set(mod.__all__)
    assert actual == expected, f"missing: {expected - actual}, extra: {actual - expected}"


def test_public_api_names_are_importable():
    import alphasolve.llm as mod
    for name in mod.__all__:
        assert hasattr(mod, name), f"{name} listed in __all__ but not actually exported"
```

- [ ] **Step 2: Run test, verify failure**

Run: `pytest tests/llm/test_public_api.py -v`
Expected: ImportError or AssertionError; `__all__` not defined yet.

- [ ] **Step 3: Implement `__init__.py`**

`make_client` and `make_client_factory` come from `factory.py` which is still empty — but we need them in `__all__` now. Create stub implementations that will be filled in Commit 2.

Write `src/alphasolve/llm/factory.py`:

```python
from __future__ import annotations

from typing import Callable, TYPE_CHECKING

from .config.preset import Preset
from .config.profile import Profile
from .types import ChatClient

if TYPE_CHECKING:
    from alphasolve.agents.general.config import GeneralAgentConfig


def make_client(preset: Preset) -> ChatClient:
    """Construct a ChatClient for the given preset. Dispatched on wire_format."""
    if preset.wire_format == "openai_chat":
        from .providers.openai_chat import OpenAIChatClient
        return OpenAIChatClient(preset)
    if preset.wire_format == "anthropic_messages":
        from .providers.anthropic_messages import AnthropicMessagesClient
        return AnthropicMessagesClient(preset)
    raise ValueError(f"unknown wire_format: {preset.wire_format!r}")


def make_client_factory(
    profile: Profile,
    presets: dict[str, Preset],
) -> Callable[["GeneralAgentConfig"], ChatClient]:
    """Returns a ClientFactory: agent_config → ChatClient via profile role lookup."""
    def factory(agent_config: "GeneralAgentConfig") -> ChatClient:
        role = agent_config.effective_role() if hasattr(agent_config, "effective_role") else (
            getattr(agent_config, "role", None) or agent_config.name
        )
        preset_name = profile.preset_for(role)
        if preset_name not in presets:
            raise KeyError(
                f"profile {profile.name!r} maps role {role!r} to preset {preset_name!r}, "
                f"but no such preset is defined; available presets: {sorted(presets)}"
            )
        return make_client(presets[preset_name])
    return factory
```

> The `hasattr(agent_config, "effective_role")` guard is a temporary compatibility shim used until Commit 4 lands. After Commit 4 it can be simplified to `agent_config.effective_role()`.

Write `src/alphasolve/llm/config/__init__.py`:

```python
# Re-exports for `from alphasolve.llm.config import Preset` etc., though
# canonical imports use `alphasolve.llm.Preset` (re-exported one level up).

from .preset import Preset, WireFormat
from .profile import Profile
from .loader import load_presets, load_profile, load_active_profile

__all__ = [
    "Preset", "WireFormat", "Profile",
    "load_presets", "load_profile", "load_active_profile",
]
```

Write `src/alphasolve/llm/__init__.py`:

```python
"""Public API of the LLM provider abstraction layer.

External callers (agent layer, CLI, workflow) import from this module.
The factory and loaders live underneath; consumers do not reach into
`alphasolve.llm.config.*` or `alphasolve.llm.providers.*` directly.
"""

from .types import (
    ChatClient,
    ChatDeltaSink,
    ChatCompletionError,
    Message,
    Role,
    ToolCall,
    ToolDef,
    CompletionResponse,
    Usage,
    StreamDelta,
    FinishReason,
)
from .config.preset import Preset, WireFormat
from .config.profile import Profile
from .config.loader import load_presets, load_profile, load_active_profile
from .factory import make_client, make_client_factory

__all__ = [
    "ChatClient",
    "ChatDeltaSink",
    "ChatCompletionError",
    "Message",
    "Role",
    "ToolCall",
    "ToolDef",
    "CompletionResponse",
    "Usage",
    "StreamDelta",
    "FinishReason",
    "Preset",
    "Profile",
    "WireFormat",
    "load_presets",
    "load_profile",
    "load_active_profile",
    "make_client",
    "make_client_factory",
]
```

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/llm/ -v`
Expected: all tests under `tests/llm/` pass (~18 total).

### Task 1.12: Smoke test the shipped YAML files

**Files:**
- Test: `tests/llm/test_shipped_yamls.py`

- [ ] **Step 1: Write smoke tests against the actual `presets.yaml` + `profiles.yaml`**

Write `tests/llm/test_shipped_yamls.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from alphasolve.llm.config.loader import load_presets, load_active_profile

PRESETS_PATH = Path(__file__).parent.parent.parent / "src" / "alphasolve" / "config" / "presets.yaml"
PROFILES_PATH = Path(__file__).parent.parent.parent / "src" / "alphasolve" / "config" / "profiles.yaml"


def test_shipped_presets_load():
    presets = load_presets(repo_path=PRESETS_PATH, user_path=None)
    expected_openai = {
        "deepseek-flash", "deepseek-pro", "parasail-deepseek", "longcat",
        "moonshot-kimi", "volcano-doubao", "volcano-deepseek",
        "dashscope-deepseek", "mimo", "openrouter-gemini",
        "qwen-3.7-max",
    }
    expected_anthropic = {"deepseek-pro-anthropic", "moonshot-kimi-anthropic"}
    assert set(presets) == expected_openai | expected_anthropic
    for name in expected_openai:
        assert presets[name].wire_format == "openai_chat", name
    for name in expected_anthropic:
        assert presets[name].wire_format == "anthropic_messages", name


def test_balanced_profile_role_coverage():
    p = load_active_profile(name="balanced", repo_path=PROFILES_PATH, user_path=None)
    required = {"orchestrator", "generator", "verifier", "reviser",
                "curator", "compute_subagent", "proof_subagent"}
    assert set(p.role_to_preset) == required


def test_each_profile_role_resolves_to_a_real_preset():
    presets = load_presets(repo_path=PRESETS_PATH, user_path=None)
    for profile_name in ("cheap", "balanced", "strategic"):
        p = load_active_profile(name=profile_name, repo_path=PROFILES_PATH, user_path=None)
        for role, preset_name in p.role_to_preset.items():
            assert preset_name in presets, (
                f"profile {profile_name!r} role {role!r} → preset {preset_name!r} not found"
            )


def test_default_profile_is_balanced():
    p = load_active_profile(name=None, repo_path=PROFILES_PATH, user_path=None)
    assert p.name == "balanced"
```

- [ ] **Step 2: Run tests, verify pass**

Run: `pytest tests/llm/test_shipped_yamls.py -v`
Expected: 4 tests pass.

### Task 1.13: Commit 1

- [ ] **Step 1: Stage and verify diff**

Run: `git status`
Expected: shows new files under `src/alphasolve/llm/`, `src/alphasolve/config/{presets,profiles}.yaml`, `tests/llm/`; modified `pyproject.toml`.

- [ ] **Step 2: Run full test suite**

Run: `pytest -v`
Expected: all pre-existing tests still pass; new `tests/llm/` tests pass.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml src/alphasolve/llm/ src/alphasolve/config/presets.yaml src/alphasolve/config/profiles.yaml tests/llm/
git commit -m "feat(llm): scaffold llm package + Preset/Profile loaders

Introduces the alphasolve.llm sub-package with typed lingua-franca
dataclasses (Message, ToolCall, ToolDef, CompletionResponse,
StreamDelta, Usage), Preset/Profile config types, YAML loaders with
user-override support, and the full presets.yaml + profiles.yaml
content.

No callers consume these yet — agent layer untouched.

Part of refactor/llm-provider-abstraction (Commit 1 of 6).
See docs/superpowers/specs/2026-05-22-alphasolve-llm-provider-abstraction-design.md"
```

---

## Commit 2: Provider implementations + factory

**End state:** `OpenAIChatClient` lifted from `general_agent.py` into `llm/providers/openai_chat.py`, refactored to take a `Preset` and to return `CompletionResponse`. New `AnthropicMessagesClient` in `llm/providers/anthropic_messages.py`. Factory in `llm/factory.py` fully wired. `general_agent.py` still uses its own inline `OpenAIChatClient` — those two co-exist for this commit. Boundary conversion (lingua-franca ↔ wire) tested exhaustively, including the LaTeX-Edit round-trip case.

### Task 2.1: Port `OpenAIChatClient` shell to new file

**Files:**
- Create: `src/alphasolve/llm/providers/openai_chat.py`
- Test: `tests/llm/test_openai_chat_client.py`

The strategy: build the new client greenfield against the typed `ChatClient` Protocol, with logic ported from the existing class (general_agent.py:62-212). We do NOT delete the existing class yet — Commit 3 deletes it after the new one is proven.

- [ ] **Step 1: Write the failing test for construction from Preset**

Write `tests/llm/test_openai_chat_client.py`:

```python
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from alphasolve.llm.config.preset import Preset
from alphasolve.llm.providers.openai_chat import OpenAIChatClient


def _preset(**overrides) -> Preset:
    defaults = dict(
        name="test-preset",
        wire_format="openai_chat",
        base_url="https://api.test.example/v1",
        api_key_env="TEST_KEY",
        model="test-model-1",
        timeout=120,
        params={},
    )
    defaults.update(overrides)
    return Preset(**defaults)


def test_constructs_with_preset(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    with patch("alphasolve.llm.providers.openai_chat.OpenAI") as MockOpenAI:
        client = OpenAIChatClient(_preset())
        MockOpenAI.assert_called_once()
        kwargs = MockOpenAI.call_args.kwargs
        assert kwargs["api_key"] == "sk-test"
        assert kwargs["base_url"] == "https://api.test.example/v1"
        assert kwargs["timeout"] == 120


def test_missing_api_key_raises_at_construction(monkeypatch):
    monkeypatch.delenv("TEST_KEY", raising=False)
    with pytest.raises(RuntimeError) as exc:
        OpenAIChatClient(_preset())
    assert "TEST_KEY" in str(exc.value)
```

- [ ] **Step 2: Run tests, verify failure**

Run: `pytest tests/llm/test_openai_chat_client.py -v`
Expected: ImportError on `OpenAIChatClient`.

- [ ] **Step 3: Write the new `openai_chat.py` skeleton**

Write `src/alphasolve/llm/providers/openai_chat.py`:

```python
from __future__ import annotations

import json
import random
import time
import traceback
from typing import Any

import httpx
import openai
from openai import OpenAI

from ..config.preset import Preset
from ..types import (
    ChatClient,
    ChatCompletionError,
    ChatDeltaSink,
    CompletionResponse,
    FinishReason,
    Message,
    StreamDelta,
    ToolCall,
    ToolDef,
    Usage,
)

_REASONING_KEYS = ("reasoning_content", "reasoning", "reasoning_text", "thinking")

_RETRYABLE_EXCEPTIONS = (
    openai.InternalServerError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.RateLimitError,
    httpx.RemoteProtocolError,
)


class OpenAIChatClient:
    _STREAMING_MAX_RETRIES = 3

    def __init__(self, preset: Preset, *, http_client: httpx.Client | None = None) -> None:
        self.preset = preset
        self.model = preset.model
        self.timeout = preset.timeout
        self.params = dict(preset.params)
        self.temperature = self.params.pop("temperature", 1.0)
        self.thinking_mode = _config_enables_thinking(self.params)
        self.client = OpenAI(
            api_key=preset.resolve_api_key(),
            base_url=preset.base_url,
            timeout=self.timeout,
            max_retries=6,
            http_client=http_client,
        )

    def complete(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDef],
        delta_sink: ChatDeltaSink | None = None,
    ) -> CompletionResponse:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": _messages_to_openai(messages, thinking_mode=self.thinking_mode),
            "temperature": self.temperature,
            **self.params,
        }
        if tools:
            request["tools"] = [_tool_to_openai(t) for t in tools]

        max_retries = 8
        delay = 5.0
        streaming_failures = 0
        use_streaming = delta_sink is not None

        for attempt in range(max_retries + 1):
            try:
                if use_streaming:
                    raw_message = self._complete_streaming(request, delta_sink=delta_sink)
                else:
                    raw_message = self._complete_non_streaming(request, delta_sink=delta_sink)
                return _openai_response_to_completion(raw_message)
            except _RETRYABLE_EXCEPTIONS as exc:
                if attempt == max_retries:
                    raise ChatCompletionError(
                        f"OpenAI request failed after {max_retries + 1} attempts: {exc}"
                    ) from exc
                if use_streaming:
                    streaming_failures += 1
                    if streaming_failures >= self._STREAMING_MAX_RETRIES:
                        use_streaming = False
                        if delta_sink is not None:
                            # Surface fallback as a synthetic text delta with a leading marker.
                            delta_sink(StreamDelta(type="text", text=f"[fallback to non-streaming after {streaming_failures} streaming failures]"))
                        delay = 5.0
                        continue
                time.sleep(delay + random.uniform(0, delay * 0.5))
                delay = min(delay * 2, 300.0)

        raise ChatCompletionError("unreachable retry state")

    def _complete_non_streaming(
        self, request: dict[str, Any], *, delta_sink: ChatDeltaSink | None
    ) -> dict[str, Any]:
        response = self.client.chat.completions.create(**request)
        message = _object_to_dict(response.choices[0].message)
        finish_reason = str(response.choices[0].finish_reason or "stop")
        message["_finish_reason"] = finish_reason
        message["_usage"] = _object_to_dict(getattr(response, "usage", None) or {})
        message["_raw"] = response
        if delta_sink is not None:
            content = str(message.get("content") or "")
            if content:
                delta_sink(StreamDelta(type="text", text=content))
        return message

    def _complete_streaming(
        self, request: dict[str, Any], *, delta_sink: ChatDeltaSink
    ) -> dict[str, Any]:
        stream_request = dict(request)
        stream_request["stream"] = True

        role = "assistant"
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_call_parts: dict[int, dict[str, Any]] = {}
        finish_reason: str = "stop"

        for chunk in self.client.chat.completions.create(**stream_request):
            chunk_dict = _object_to_dict(chunk)
            choices = chunk_dict.get("choices") or []
            if not choices:
                continue
            choice = _object_to_dict(choices[0])
            if choice.get("finish_reason"):
                finish_reason = str(choice["finish_reason"])
            delta = _object_to_dict(choice.get("delta") or {})
            if not delta:
                continue

            role = str(delta.get("role") or role)
            content_delta = _first_text_delta(delta, ("content",))
            if content_delta:
                content_parts.append(content_delta)
                delta_sink(StreamDelta(type="text", text=content_delta))
            reasoning_delta = _first_text_delta(delta, _REASONING_KEYS)
            if reasoning_delta:
                reasoning_parts.append(reasoning_delta)

            for raw_tool_delta in delta.get("tool_calls") or []:
                tool_delta = _object_to_dict(raw_tool_delta)
                index = int(tool_delta.get("index") or 0)
                current = tool_call_parts.setdefault(
                    index,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if tool_delta.get("id"):
                    current["id"] = str(tool_delta["id"])

                function_delta = _object_to_dict(tool_delta.get("function") or {})
                function = current.setdefault("function", {"name": "", "arguments": ""})
                if function_delta.get("name"):
                    function["name"] = str(function.get("name") or "") + str(function_delta["name"])
                if function_delta.get("arguments"):
                    arg_chunk = str(function_delta["arguments"])
                    function["arguments"] = str(function.get("arguments") or "") + arg_chunk
                    delta_sink(StreamDelta(type="tool_input", tool_call_id=current["id"], arg_delta=arg_chunk))

        message: dict[str, Any] = {"role": role, "content": "".join(content_parts)}
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if tool_call_parts:
            message["tool_calls"] = [tool_call_parts[i] for i in sorted(tool_call_parts)]
        message["_finish_reason"] = finish_reason
        message["_usage"] = {}
        message["_raw"] = None
        return message


# ----- Conversion helpers (boundary code; not part of the public API) -----

def _config_enables_thinking(params: dict[str, Any]) -> bool:
    extra_body = params.get("extra_body") if isinstance(params, dict) else None
    if not isinstance(extra_body, dict):
        return False
    if extra_body.get("enable_thinking") is True:
        return True
    reasoning = extra_body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning:
        return True
    thinking = extra_body.get("thinking")
    if isinstance(thinking, dict):
        thinking_type = str(thinking.get("type") or "").lower()
        return thinking_type in {"enabled", "enable", "on", "true"} or bool(thinking.get("enabled"))
    return False


def _first_text_delta(delta: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = delta.get(key)
        if value:
            return str(value)
    return ""


def _object_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=False)
    if hasattr(value, "dict"):
        return value.dict()
    return dict(value) if hasattr(value, "__iter__") else {"value": value}


def _messages_to_openai(messages: list[Message], *, thinking_mode: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "tool":
            out.append({
                "role": "tool",
                "content": m.content,
                "tool_call_id": m.tool_call_id or "",
                "name": m.name or "",
            })
            continue
        d: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.args, ensure_ascii=False),
                    },
                }
                for tc in m.tool_calls
            ]
        out.append(d)
    return out


def _tool_to_openai(td: ToolDef) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": td.name,
            "description": td.description,
            "parameters": td.parameters,
        },
    }


def _openai_response_to_completion(raw_message: dict[str, Any]) -> CompletionResponse:
    finish_reason = raw_message.pop("_finish_reason", "stop")
    usage_dict = raw_message.pop("_usage", {})
    raw_obj = raw_message.pop("_raw", None)

    tool_calls: list[ToolCall] = []
    for tc_raw in raw_message.get("tool_calls") or []:
        fn = tc_raw.get("function") or {}
        args_str = fn.get("arguments") or "{}"
        try:
            args = json.loads(args_str) if isinstance(args_str, str) else dict(args_str)
        except json.JSONDecodeError as exc:
            raise ChatCompletionError(
                f"tool call {tc_raw.get('id', '?')} returned invalid JSON args: {exc}; raw: {args_str!r}"
            ) from exc
        tool_calls.append(ToolCall(id=str(tc_raw.get("id") or ""), name=str(fn.get("name") or ""), args=args))

    msg = Message(
        role="assistant",
        content=str(raw_message.get("content") or ""),
        tool_calls=tuple(tool_calls),
    )
    usage = Usage(
        input_tokens=int(usage_dict.get("prompt_tokens") or usage_dict.get("input_tokens") or 0),
        output_tokens=int(usage_dict.get("completion_tokens") or usage_dict.get("output_tokens") or 0),
        cached_tokens=int(usage_dict.get("cache_read_input_tokens") or usage_dict.get("cached_tokens") or 0),
    )
    fr: FinishReason = finish_reason if finish_reason in {"stop", "tool_calls", "length", "content_filter", "error"} else "stop"
    return CompletionResponse(message=msg, finish_reason=fr, usage=usage, raw=raw_obj)
```

- [ ] **Step 4: Run construction tests, verify pass**

Run: `pytest tests/llm/test_openai_chat_client.py -v`
Expected: the 2 construction tests pass.

### Task 2.2: Test `OpenAIChatClient.complete` end-to-end (mocked SDK)

**Files:**
- Modify: `tests/llm/test_openai_chat_client.py`

- [ ] **Step 1: Add tests for `complete()` happy-path and tool-call parsing**

Append to `tests/llm/test_openai_chat_client.py`:

```python
from alphasolve.llm.types import Message, ToolDef


def _mock_response(content: str = "", tool_calls=None, finish_reason: str = "stop"):
    msg = MagicMock()
    msg.model_dump = MagicMock(return_value={
        "role": "assistant",
        "content": content,
        "tool_calls": tool_calls or [],
    })
    msg.dict = msg.model_dump
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = finish_reason
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = None
    return resp


def test_complete_text_response(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    with patch("alphasolve.llm.providers.openai_chat.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mock_client.chat.completions.create.return_value = _mock_response(content="hello")

        client = OpenAIChatClient(_preset())
        resp = client.complete(messages=[Message(role="user", content="hi")], tools=[])
        assert resp.message.role == "assistant"
        assert resp.message.content == "hello"
        assert resp.finish_reason == "stop"
        assert resp.message.tool_calls == ()


def test_complete_tool_call_response(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    with patch("alphasolve.llm.providers.openai_chat.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mock_client.chat.completions.create.return_value = _mock_response(
            content="",
            tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "Edit",
                    "arguments": '{"old_str": "\\\\frac{1}{2}", "new_str": "\\\\frac{2}{3}"}',
                },
            }],
            finish_reason="tool_calls",
        )

        client = OpenAIChatClient(_preset())
        resp = client.complete(
            messages=[Message(role="user", content="edit")],
            tools=[ToolDef(name="Edit", description="", parameters={})],
        )
        assert resp.finish_reason == "tool_calls"
        assert len(resp.message.tool_calls) == 1
        tc = resp.message.tool_calls[0]
        assert tc.id == "call_1"
        assert tc.name == "Edit"
        # CRITICAL: args is a dict, and the LaTeX backslashes are preserved as single backslash strings
        assert isinstance(tc.args, dict)
        assert tc.args["old_str"] == "\\frac{1}{2}"
        assert tc.args["new_str"] == "\\frac{2}{3}"


def test_complete_invalid_tool_args_raises(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    with patch("alphasolve.llm.providers.openai_chat.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mock_client.chat.completions.create.return_value = _mock_response(
            content="",
            tool_calls=[{
                "id": "call_bad",
                "type": "function",
                "function": {"name": "Edit", "arguments": '{"path": "broken'},
            }],
            finish_reason="tool_calls",
        )

        from alphasolve.llm.types import ChatCompletionError
        client = OpenAIChatClient(_preset())
        with pytest.raises(ChatCompletionError) as exc:
            client.complete(messages=[Message(role="user", content="x")], tools=[])
        assert "call_bad" in str(exc.value)


def test_messages_serialize_tool_calls_to_openai_dict(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    with patch("alphasolve.llm.providers.openai_chat.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mock_client.chat.completions.create.return_value = _mock_response(content="ack")

        from alphasolve.llm.types import ToolCall
        client = OpenAIChatClient(_preset())
        msgs = [
            Message(role="user", content="please edit"),
            Message(
                role="assistant",
                content="",
                tool_calls=(ToolCall(id="t1", name="Edit", args={"old_str": "a", "new_str": "b"}),),
            ),
            Message(role="tool", content="ok", tool_call_id="t1", name="Edit"),
        ]
        client.complete(messages=msgs, tools=[])
        # Inspect the kwargs the OpenAI SDK was called with
        sent = mock_client.chat.completions.create.call_args.kwargs["messages"]
        assert len(sent) == 3
        assert sent[1]["tool_calls"][0]["function"]["name"] == "Edit"
        # arguments is a JSON string at the wire layer
        assert sent[1]["tool_calls"][0]["function"]["arguments"] == '{"old_str": "a", "new_str": "b"}'
        assert sent[2]["role"] == "tool"
        assert sent[2]["tool_call_id"] == "t1"
```

- [ ] **Step 2: Run tests, verify pass**

Run: `pytest tests/llm/test_openai_chat_client.py -v`
Expected: 6 tests pass.

### Task 2.3: Implement `AnthropicMessagesClient`

**Files:**
- Modify: `src/alphasolve/llm/providers/anthropic_messages.py`
- Test: `tests/llm/test_anthropic_messages_client.py`

- [ ] **Step 1: Write the failing tests**

Write `tests/llm/test_anthropic_messages_client.py`:

```python
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from alphasolve.llm.config.preset import Preset
from alphasolve.llm.providers.anthropic_messages import AnthropicMessagesClient
from alphasolve.llm.types import Message, ToolCall, ToolDef


def _preset(**overrides) -> Preset:
    defaults = dict(
        name="test-anthropic",
        wire_format="anthropic_messages",
        base_url="https://api.anthropic.example",
        api_key_env="ANTHROPIC_TEST_KEY",
        model="claude-test-1",
        timeout=120,
        params={},
    )
    defaults.update(overrides)
    return Preset(**defaults)


def _mock_anthropic_response(*, text: str = "", tool_uses=None, stop_reason: str = "end_turn"):
    """Mimic anthropic.types.Message shape."""
    content_blocks = []
    if text:
        b = MagicMock()
        b.type = "text"
        b.text = text
        content_blocks.append(b)
    for tu in tool_uses or []:
        b = MagicMock()
        b.type = "tool_use"
        b.id = tu["id"]
        b.name = tu["name"]
        b.input = tu["input"]   # dict, NOT string
        content_blocks.append(b)
    resp = MagicMock()
    resp.content = content_blocks
    resp.stop_reason = stop_reason
    resp.usage = MagicMock(input_tokens=10, output_tokens=5, cache_read_input_tokens=0)
    resp.model_dump = MagicMock(return_value={"content": "[mock]"})
    return resp


def test_constructs_with_preset(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "sk-anthropic")
    with patch("alphasolve.llm.providers.anthropic_messages.Anthropic") as MockAnthropic:
        AnthropicMessagesClient(_preset())
        kwargs = MockAnthropic.call_args.kwargs
        assert kwargs["api_key"] == "sk-anthropic"
        assert kwargs["base_url"] == "https://api.anthropic.example"


def test_system_message_lifted_to_kwarg(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "sk-anthropic")
    with patch("alphasolve.llm.providers.anthropic_messages.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client
        mock_client.messages.create.return_value = _mock_anthropic_response(text="hello")

        client = AnthropicMessagesClient(_preset())
        client.complete(
            messages=[
                Message(role="system", content="you are helpful"),
                Message(role="user", content="hi"),
            ],
            tools=[],
        )
        kwargs = mock_client.messages.create.call_args.kwargs
        assert kwargs["system"] == "you are helpful"
        # the user message goes in messages=, with NO system entry there
        assert kwargs["messages"][0]["role"] == "user"
        assert all(m["role"] != "system" for m in kwargs["messages"])


def test_tool_messages_collapse_into_user_tool_result(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "sk-anthropic")
    with patch("alphasolve.llm.providers.anthropic_messages.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client
        mock_client.messages.create.return_value = _mock_anthropic_response(text="ok")

        client = AnthropicMessagesClient(_preset())
        client.complete(
            messages=[
                Message(role="user", content="edit foo"),
                Message(
                    role="assistant",
                    content="",
                    tool_calls=(ToolCall(id="t1", name="Edit", args={"old_str": "a", "new_str": "b"}),),
                ),
                Message(role="tool", content="ok", tool_call_id="t1", name="Edit"),
            ],
            tools=[],
        )
        sent = mock_client.messages.create.call_args.kwargs["messages"]
        # Expected shape:
        #   [0] user: text
        #   [1] assistant: tool_use block (Anthropic native form)
        #   [2] user: tool_result block (the role="tool" message folded back into user)
        assert sent[0]["role"] == "user"
        assert sent[1]["role"] == "assistant"
        assert any(blk["type"] == "tool_use" for blk in sent[1]["content"])
        assert sent[2]["role"] == "user"
        assert sent[2]["content"][0]["type"] == "tool_result"
        assert sent[2]["content"][0]["tool_use_id"] == "t1"


def test_tool_use_input_is_dict_not_stringified(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "sk-anthropic")
    with patch("alphasolve.llm.providers.anthropic_messages.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client
        mock_client.messages.create.return_value = _mock_anthropic_response(
            text="",
            tool_uses=[{
                "id": "tu_1",
                "name": "Edit",
                "input": {"old_str": "\\frac{1}{2}", "new_str": "\\frac{2}{3}"},
            }],
            stop_reason="tool_use",
        )

        client = AnthropicMessagesClient(_preset())
        resp = client.complete(messages=[Message(role="user", content="edit")], tools=[])
        assert resp.finish_reason == "tool_calls"
        tc = resp.message.tool_calls[0]
        # KEY ASSERTION: protocol advantage preserved — args is a dict, no double-stringification
        assert isinstance(tc.args, dict)
        assert tc.args["old_str"] == "\\frac{1}{2}"
        assert tc.args["new_str"] == "\\frac{2}{3}"


def test_tool_def_converts_to_anthropic_schema(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "sk-anthropic")
    with patch("alphasolve.llm.providers.anthropic_messages.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client
        mock_client.messages.create.return_value = _mock_anthropic_response(text="ok")

        client = AnthropicMessagesClient(_preset())
        td = ToolDef(name="Read", description="read a file", parameters={"type": "object", "properties": {"path": {"type": "string"}}})
        client.complete(messages=[Message(role="user", content="x")], tools=[td])
        sent_tools = mock_client.messages.create.call_args.kwargs["tools"]
        assert len(sent_tools) == 1
        assert sent_tools[0]["name"] == "Read"
        assert sent_tools[0]["description"] == "read a file"
        # Anthropic calls it input_schema, not parameters
        assert sent_tools[0]["input_schema"] == {"type": "object", "properties": {"path": {"type": "string"}}}
        # ...and there is no `parameters` key
        assert "parameters" not in sent_tools[0]


def test_usage_extracted(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "sk-anthropic")
    with patch("alphasolve.llm.providers.anthropic_messages.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client
        mock_client.messages.create.return_value = _mock_anthropic_response(text="ok")

        client = AnthropicMessagesClient(_preset())
        resp = client.complete(messages=[Message(role="user", content="x")], tools=[])
        assert resp.usage.input_tokens == 10
        assert resp.usage.output_tokens == 5
```

- [ ] **Step 2: Run tests, verify failure**

Run: `pytest tests/llm/test_anthropic_messages_client.py -v`
Expected: ImportError on `AnthropicMessagesClient`.

- [ ] **Step 3: Implement `anthropic_messages.py`**

Write `src/alphasolve/llm/providers/anthropic_messages.py`:

```python
from __future__ import annotations

import random
import time
from typing import Any

import anthropic
from anthropic import Anthropic

from ..config.preset import Preset
from ..types import (
    ChatClient,
    ChatCompletionError,
    ChatDeltaSink,
    CompletionResponse,
    FinishReason,
    Message,
    StreamDelta,
    ToolCall,
    ToolDef,
    Usage,
)

_RETRYABLE_EXCEPTIONS = (
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.InternalServerError,
    anthropic.RateLimitError,
)


class AnthropicMessagesClient:
    def __init__(self, preset: Preset) -> None:
        self.preset = preset
        self.model = preset.model
        self.timeout = preset.timeout
        self.params = dict(preset.params)
        self.client = Anthropic(
            api_key=preset.resolve_api_key(),
            base_url=preset.base_url,
            timeout=self.timeout,
            max_retries=6,
        )

    def complete(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDef],
        delta_sink: ChatDeltaSink | None = None,
    ) -> CompletionResponse:
        system_text, wire_messages = _messages_to_anthropic(messages)
        wire_tools = [_tool_to_anthropic(t) for t in tools] if tools else []

        request: dict[str, Any] = {
            "model": self.model,
            "messages": wire_messages,
            "max_tokens": int(self.params.get("max_tokens") or 8192),
            **{k: v for k, v in self.params.items() if k != "max_tokens"},
        }
        if system_text:
            request["system"] = system_text
        if wire_tools:
            request["tools"] = wire_tools

        max_retries = 8
        delay = 5.0
        for attempt in range(max_retries + 1):
            try:
                if delta_sink is not None:
                    return self._complete_streaming(request, delta_sink=delta_sink)
                return self._complete_non_streaming(request)
            except _RETRYABLE_EXCEPTIONS as exc:
                if attempt == max_retries:
                    raise ChatCompletionError(
                        f"Anthropic request failed after {max_retries + 1} attempts: {exc}"
                    ) from exc
                time.sleep(delay + random.uniform(0, delay * 0.5))
                delay = min(delay * 2, 300.0)

        raise ChatCompletionError("unreachable retry state")

    def _complete_non_streaming(self, request: dict[str, Any]) -> CompletionResponse:
        response = self.client.messages.create(**request)
        return _anthropic_response_to_completion(response)

    def _complete_streaming(
        self, request: dict[str, Any], *, delta_sink: ChatDeltaSink
    ) -> CompletionResponse:
        text_parts: list[str] = []
        tool_blocks: dict[int, dict[str, Any]] = {}  # index → {"id","name","input_str"}
        stop_reason = "end_turn"
        usage_in = 0
        usage_out = 0
        usage_cached = 0

        with self.client.messages.stream(**request) as stream:
            for event in stream:
                etype = getattr(event, "type", "")
                if etype == "message_delta":
                    delta = getattr(event, "delta", None)
                    if delta is not None:
                        sr = getattr(delta, "stop_reason", None)
                        if sr:
                            stop_reason = str(sr)
                    usage = getattr(event, "usage", None)
                    if usage is not None:
                        usage_out += int(getattr(usage, "output_tokens", 0) or 0)
                elif etype == "content_block_start":
                    block = getattr(event, "content_block", None)
                    index = int(getattr(event, "index", 0))
                    if block is not None and getattr(block, "type", "") == "tool_use":
                        tool_blocks[index] = {
                            "id": str(getattr(block, "id", "")),
                            "name": str(getattr(block, "name", "")),
                            "input_str": "",
                        }
                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if delta is None:
                        continue
                    dtype = getattr(delta, "type", "")
                    index = int(getattr(event, "index", 0))
                    if dtype == "text_delta":
                        text = str(getattr(delta, "text", "") or "")
                        if text:
                            text_parts.append(text)
                            delta_sink(StreamDelta(type="text", text=text))
                    elif dtype == "input_json_delta":
                        partial = str(getattr(delta, "partial_json", "") or "")
                        if partial and index in tool_blocks:
                            tool_blocks[index]["input_str"] += partial
                            delta_sink(StreamDelta(
                                type="tool_input",
                                tool_call_id=tool_blocks[index]["id"],
                                arg_delta=partial,
                            ))
                elif etype == "message_start":
                    msg_obj = getattr(event, "message", None)
                    if msg_obj is not None:
                        usage = getattr(msg_obj, "usage", None)
                        if usage is not None:
                            usage_in = int(getattr(usage, "input_tokens", 0) or 0)
                            usage_cached = int(getattr(usage, "cache_read_input_tokens", 0) or 0)

        # Build the final message
        tool_calls: list[ToolCall] = []
        for idx in sorted(tool_blocks):
            block = tool_blocks[idx]
            try:
                args = _parse_json_strict(block["input_str"] or "{}", context=f"tool_call {block['id']}")
            except ChatCompletionError:
                raise
            tool_calls.append(ToolCall(id=block["id"], name=block["name"], args=args))

        msg = Message(role="assistant", content="".join(text_parts), tool_calls=tuple(tool_calls))
        return CompletionResponse(
            message=msg,
            finish_reason=_normalize_stop_reason(stop_reason, has_tool_calls=bool(tool_calls)),
            usage=Usage(input_tokens=usage_in, output_tokens=usage_out, cached_tokens=usage_cached),
            raw=None,
        )


# ----- Conversion helpers -----

def _messages_to_anthropic(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    """Lift system to a string, fold role='tool' into the following user as tool_result."""
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    def flush_pending_into_user():
        nonlocal pending_tool_results
        if not pending_tool_results:
            return
        out.append({"role": "user", "content": pending_tool_results})
        pending_tool_results = []

    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
            continue
        if m.role == "tool":
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": m.tool_call_id or "",
                "content": m.content,
            })
            continue
        # Before emitting a non-tool message, flush any accumulated tool results as a user message.
        flush_pending_into_user()
        if m.role == "user":
            out.append({"role": "user", "content": [{"type": "text", "text": m.content}]})
        elif m.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                blocks.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.args,
                })
            out.append({"role": "assistant", "content": blocks})

    flush_pending_into_user()

    system_text = "\n\n".join(s for s in system_parts if s)
    return system_text, out


def _tool_to_anthropic(td: ToolDef) -> dict[str, Any]:
    return {
        "name": td.name,
        "description": td.description,
        "input_schema": td.parameters,
    }


def _anthropic_response_to_completion(response: Any) -> CompletionResponse:
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in getattr(response, "content", None) or []:
        btype = getattr(block, "type", "")
        if btype == "text":
            text_parts.append(str(getattr(block, "text", "") or ""))
        elif btype == "tool_use":
            tool_calls.append(ToolCall(
                id=str(getattr(block, "id", "")),
                name=str(getattr(block, "name", "")),
                args=dict(getattr(block, "input", {}) or {}),
            ))

    stop_reason = str(getattr(response, "stop_reason", "") or "end_turn")
    usage_obj = getattr(response, "usage", None)
    usage = Usage(
        input_tokens=int(getattr(usage_obj, "input_tokens", 0) or 0) if usage_obj else 0,
        output_tokens=int(getattr(usage_obj, "output_tokens", 0) or 0) if usage_obj else 0,
        cached_tokens=int(getattr(usage_obj, "cache_read_input_tokens", 0) or 0) if usage_obj else 0,
    )
    msg = Message(role="assistant", content="".join(text_parts), tool_calls=tuple(tool_calls))
    return CompletionResponse(
        message=msg,
        finish_reason=_normalize_stop_reason(stop_reason, has_tool_calls=bool(tool_calls)),
        usage=usage,
        raw=response,
    )


def _normalize_stop_reason(stop: str, *, has_tool_calls: bool) -> FinishReason:
    # Anthropic uses: end_turn / max_tokens / stop_sequence / tool_use
    if stop == "tool_use" or has_tool_calls:
        return "tool_calls"
    if stop == "max_tokens":
        return "length"
    if stop in ("end_turn", "stop_sequence"):
        return "stop"
    return "stop"


def _parse_json_strict(s: str, *, context: str) -> dict[str, Any]:
    import json as _json
    try:
        return _json.loads(s)
    except _json.JSONDecodeError as exc:
        raise ChatCompletionError(f"{context}: invalid JSON input from stream: {exc}; raw: {s!r}") from exc
```

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/llm/test_anthropic_messages_client.py -v`
Expected: 6 tests pass.

### Task 2.4: Cross-provider round-trip test (the LaTeX assertion)

**Files:**
- Create: `tests/llm/test_message_conversion.py`

- [ ] **Step 1: Write the round-trip test**

Write `tests/llm/test_message_conversion.py`:

```python
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from alphasolve.llm.config.preset import Preset
from alphasolve.llm.providers.openai_chat import OpenAIChatClient
from alphasolve.llm.providers.anthropic_messages import AnthropicMessagesClient
from alphasolve.llm.types import Message, ToolCall, ToolDef

LATEX_OLD = "\\frac{1}{2}"
LATEX_NEW = "\\frac{2}{3}"


def _openai_preset():
    return Preset(name="t", wire_format="openai_chat", base_url="u", api_key_env="OAI_K", model="m")


def _anthropic_preset():
    return Preset(name="t", wire_format="anthropic_messages", base_url="u", api_key_env="ANT_K", model="m")


def test_openai_latex_roundtrip(monkeypatch):
    """Send a tool-call assistant message containing LaTeX; assert it survives the wire."""
    monkeypatch.setenv("OAI_K", "sk-x")

    captured_wire = {}

    def _capture(**kwargs):
        captured_wire.update(kwargs)
        # Return a minimal response so .complete() finishes
        msg = MagicMock()
        msg.model_dump = MagicMock(return_value={"role": "assistant", "content": "ack"})
        msg.dict = msg.model_dump
        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = "stop"
        resp = MagicMock()
        resp.choices = [choice]
        resp.usage = None
        return resp

    with patch("alphasolve.llm.providers.openai_chat.OpenAI") as MockOpenAI:
        mock = MagicMock()
        MockOpenAI.return_value = mock
        mock.chat.completions.create.side_effect = _capture

        client = OpenAIChatClient(_openai_preset())
        client.complete(
            messages=[Message(
                role="assistant",
                content="",
                tool_calls=(ToolCall(id="call_1", name="Edit", args={"old_str": LATEX_OLD, "new_str": LATEX_NEW}),),
            )],
            tools=[],
        )
        sent_args_str = captured_wire["messages"][0]["tool_calls"][0]["function"]["arguments"]
        # Wire is JSON string. Parse it back and verify LaTeX survives.
        import json as _json
        parsed = _json.loads(sent_args_str)
        assert parsed["old_str"] == LATEX_OLD
        assert parsed["new_str"] == LATEX_NEW


def test_anthropic_latex_roundtrip(monkeypatch):
    """Anthropic delivers tool_use input as dict — no JSON-escape gymnastics."""
    monkeypatch.setenv("ANT_K", "sk-x")

    captured_wire = {}

    def _capture(**kwargs):
        captured_wire.update(kwargs)
        b = MagicMock()
        b.type = "text"
        b.text = "ack"
        resp = MagicMock()
        resp.content = [b]
        resp.stop_reason = "end_turn"
        resp.usage = MagicMock(input_tokens=0, output_tokens=0, cache_read_input_tokens=0)
        return resp

    with patch("alphasolve.llm.providers.anthropic_messages.Anthropic") as MockAnthropic:
        mock = MagicMock()
        MockAnthropic.return_value = mock
        mock.messages.create.side_effect = _capture

        client = AnthropicMessagesClient(_anthropic_preset())
        client.complete(
            messages=[Message(
                role="assistant",
                content="",
                tool_calls=(ToolCall(id="t1", name="Edit", args={"old_str": LATEX_OLD, "new_str": LATEX_NEW}),),
            )],
            tools=[],
        )
        # Anthropic native form: tool_use block in assistant content with input as DICT.
        assistant_block = captured_wire["messages"][0]["content"]
        tool_use = next(b for b in assistant_block if b["type"] == "tool_use")
        assert isinstance(tool_use["input"], dict)
        assert tool_use["input"]["old_str"] == LATEX_OLD
        assert tool_use["input"]["new_str"] == LATEX_NEW


def test_openai_response_args_parse_back_to_dict(monkeypatch):
    """The reverse path: when OpenAI returns a tool_call with stringified args, the lingua-franca exposes them as dict."""
    monkeypatch.setenv("OAI_K", "sk-x")
    with patch("alphasolve.llm.providers.openai_chat.OpenAI") as MockOpenAI:
        mock = MagicMock()
        MockOpenAI.return_value = mock
        msg = MagicMock()
        # IMPORTANT: arguments uses JSON-string-encoded LaTeX (2 escaping levels)
        msg.model_dump = MagicMock(return_value={
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "Edit",
                    "arguments": '{"old_str": "\\\\frac{1}{2}", "new_str": "\\\\frac{2}{3}"}',
                },
            }],
        })
        msg.dict = msg.model_dump
        choice = MagicMock(); choice.message = msg; choice.finish_reason = "tool_calls"
        resp = MagicMock(); resp.choices = [choice]; resp.usage = None
        mock.chat.completions.create.return_value = resp

        client = OpenAIChatClient(_openai_preset())
        out = client.complete(messages=[Message(role="user", content="x")], tools=[])
        tc = out.message.tool_calls[0]
        assert isinstance(tc.args, dict)
        assert tc.args["old_str"] == LATEX_OLD
        assert tc.args["new_str"] == LATEX_NEW
```

- [ ] **Step 2: Run tests, verify pass**

Run: `pytest tests/llm/test_message_conversion.py -v`
Expected: 3 tests pass.

### Task 2.5: Wire `factory.py` tests

**Files:**
- Create: `tests/llm/test_factory.py`

- [ ] **Step 1: Write factory tests**

Write `tests/llm/test_factory.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

import pytest

from alphasolve.llm import make_client, make_client_factory
from alphasolve.llm.config.preset import Preset
from alphasolve.llm.config.profile import Profile
from alphasolve.llm.providers.openai_chat import OpenAIChatClient
from alphasolve.llm.providers.anthropic_messages import AnthropicMessagesClient


def _preset(name: str, wire: str = "openai_chat") -> Preset:
    return Preset(
        name=name, wire_format=wire,
        base_url="https://example.com", api_key_env="X", model="m",
    )


@dataclass
class _FakeAgentConfig:
    name: str
    role: str | None = None

    def effective_role(self) -> str:
        return self.role or self.name


def test_make_client_dispatches_openai(monkeypatch):
    monkeypatch.setenv("X", "sk")
    c = make_client(_preset("a", "openai_chat"))
    assert isinstance(c, OpenAIChatClient)


def test_make_client_dispatches_anthropic(monkeypatch):
    monkeypatch.setenv("X", "sk")
    c = make_client(_preset("a", "anthropic_messages"))
    assert isinstance(c, AnthropicMessagesClient)


def test_make_client_unknown_wire_format(monkeypatch):
    monkeypatch.setenv("X", "sk")
    # Construct manually bypassing Literal type checking
    p = Preset.__new__(Preset)
    object.__setattr__(p, "name", "x")
    object.__setattr__(p, "wire_format", "gemini_native")
    object.__setattr__(p, "base_url", "u")
    object.__setattr__(p, "api_key_env", "X")
    object.__setattr__(p, "model", "m")
    object.__setattr__(p, "timeout", 3600.0)
    object.__setattr__(p, "params", {})
    with pytest.raises(ValueError) as exc:
        make_client(p)
    assert "gemini_native" in str(exc.value)


def test_make_client_factory_resolves_role(monkeypatch):
    monkeypatch.setenv("X", "sk")
    presets = {"p1": _preset("p1"), "p2": _preset("p2")}
    profile = Profile(name="t", role_to_preset={"verifier": "p1", "curator": "p2"})

    factory = make_client_factory(profile, presets)
    client_v = factory(_FakeAgentConfig(name="verifier_adversarial", role="verifier"))
    client_c = factory(_FakeAgentConfig(name="curator"))  # role falls back to name
    assert isinstance(client_v, OpenAIChatClient)
    assert isinstance(client_c, OpenAIChatClient)
    # Different presets selected
    assert client_v.preset.name == "p1"
    assert client_c.preset.name == "p2"


def test_make_client_factory_unknown_role(monkeypatch):
    monkeypatch.setenv("X", "sk")
    presets = {"p1": _preset("p1")}
    profile = Profile(name="t", role_to_preset={"verifier": "p1"})
    factory = make_client_factory(profile, presets)
    with pytest.raises(KeyError):
        factory(_FakeAgentConfig(name="orchestrator"))


def test_make_client_factory_role_maps_to_missing_preset(monkeypatch):
    monkeypatch.setenv("X", "sk")
    presets = {"p1": _preset("p1")}
    profile = Profile(name="t", role_to_preset={"verifier": "p_ghost"})
    factory = make_client_factory(profile, presets)
    with pytest.raises(KeyError) as exc:
        factory(_FakeAgentConfig(name="verifier"))
    assert "p_ghost" in str(exc.value)
```

- [ ] **Step 2: Run tests, verify pass**

Run: `pytest tests/llm/test_factory.py -v`
Expected: 6 tests pass.

### Task 2.6: Commit 2

- [ ] **Step 1: Run all llm tests**

Run: `pytest tests/llm/ -v`
Expected: all ~37 llm tests pass.

- [ ] **Step 2: Run the full pre-existing test suite to confirm no regressions**

Run: `pytest -v --ignore=tests/llm`
Expected: pre-existing tests still pass (agent layer untouched).

- [ ] **Step 3: Commit**

```bash
git add src/alphasolve/llm/providers/ src/alphasolve/llm/factory.py tests/llm/
git commit -m "feat(llm): port OpenAIChatClient + add AnthropicMessagesClient

OpenAIChatClient is lifted from general_agent.py into
alphasolve.llm.providers.openai_chat, refactored to take a Preset and
return a typed CompletionResponse. AnthropicMessagesClient is new,
built against the anthropic SDK with: system message lifted to system=
kwarg, role='tool' messages folded into the following user message as
tool_result blocks, tool_use.input arriving as a dict (no double-string
escaping for LaTeX-heavy edits).

Factory dispatches on Preset.wire_format. The existing
general_agent.py's inline OpenAIChatClient is still present and used;
Commit 3 removes it.

Part of refactor/llm-provider-abstraction (Commit 2 of 6)."
```

---

## Commit 3: Wire new `ChatClient` through `general_agent.py` (with shim)

**End state:** `general_agent.py`'s inline `OpenAIChatClient` is deleted. The new `alphasolve.llm.providers.openai_chat.OpenAIChatClient` is the one in use. A small dict↔Message adapter inside `general_agent.py` lets the rest of the agent layer keep using dict-shaped messages temporarily (Commit 4 removes the adapter).

### Task 3.1: Delete the inline `OpenAIChatClient` class

**Files:**
- Modify: `src/alphasolve/agents/general/general_agent.py:62-212`

- [ ] **Step 1: Delete the class definition and related private helpers**

In `src/alphasolve/agents/general/general_agent.py`, delete lines 53-212 (the `_RETRYABLE_EXCEPTIONS` tuple, the entire `OpenAIChatClient` class, and `_config_enables_thinking`, `_first_text_delta`, `_format_exception_detail`, `_object_to_dict`, `_prepare_messages_for_request` if they exist only for OpenAIChatClient — re-add what `GeneralPurposeAgent` itself still uses).

The minimum remaining around the same region: keep `ChatClient` Protocol (line 26-34) and `ChatDeltaSink` (line 21) — these become re-exports from `alphasolve.llm` later in this commit but stay locally for now.

- [ ] **Step 2: Run pre-existing tests to find consumers**

Run: `pytest -v --ignore=tests/llm`
Expected: `tests/test_general_agent.py` and `tests/test_agent_team.py` may fail with `ImportError: cannot import name 'OpenAIChatClient'`.

Catalogue every test/module that previously imported `OpenAIChatClient` from `general_agent`. Tests will be updated in the next step.

### Task 3.2: Update consumers to import from the new location

**Files:**
- Modify: `src/alphasolve/agents/team/workflow.py:281` (`make_openai_client_factory`)
- Modify: any test in `tests/` that imports `OpenAIChatClient`

- [ ] **Step 1: Update `make_openai_client_factory`**

Open `src/alphasolve/agents/team/workflow.py`. Find `make_openai_client_factory` at line 281. Currently it constructs `OpenAIChatClient(config_dict)` where `config_dict` comes from `agent_config.py` constants. Refactor to use the new client + Preset, but for now keep `agent_config.py` constants as the source (Commit 4 deletes them).

Replace the function with a temporary shim:

```python
def make_openai_client_factory(suite) -> ClientFactory:
    """Temporary shim: builds the legacy ClientFactory using new OpenAIChatClient.

    Reads agent_config.py constants like before, but instantiates the relocated
    OpenAIChatClient. Will be replaced in Commit 6 by alphasolve.llm.make_client_factory.
    """
    from alphasolve.llm.providers.openai_chat import OpenAIChatClient
    from alphasolve.llm.config.preset import Preset
    from alphasolve.config.agent_config import AlphaSolveConfig

    def _config_dict_to_preset(name: str, cfg: Mapping[str, Any]) -> Preset:
        # Resolve callables (api_key: lambda) — agent_config.py still uses them.
        api_key_env = cfg.get("api_key_env")
        if not api_key_env:
            # Synthesize a one-shot env var. Set it now from the lambda result.
            value = cfg["api_key"]() if callable(cfg["api_key"]) else cfg["api_key"]
            import os
            synth_env = f"_ALPHASOLVE_LEGACY_KEY_{name.upper().replace('-', '_')}"
            os.environ[synth_env] = value or ""
            api_key_env = synth_env
        return Preset(
            name=name,
            wire_format="openai_chat",
            base_url=cfg["base_url"],
            api_key_env=api_key_env,
            model=cfg["model"],
            timeout=float(cfg.get("timeout", 3600)),
            params=dict(cfg.get("params") or {}),
        )

    def factory(agent_config: GeneralAgentConfig) -> ChatClient:
        model_config_name = agent_config.model_config or "GENERATOR_CONFIG"
        cfg = getattr(AlphaSolveConfig, model_config_name)
        preset = _config_dict_to_preset(model_config_name, cfg)
        return OpenAIChatClient(preset)

    return factory
```

This shim is **temporary**: it bridges the old `agent_config.py` constants to the new `OpenAIChatClient` while leaving callers undisturbed. Commit 6 replaces it with `alphasolve.llm.make_client_factory`.

- [ ] **Step 2: Update any test that imports `OpenAIChatClient` from `general_agent`**

Run: `grep -rn "from alphasolve.agents.general.general_agent import.*OpenAIChatClient\|from .general_agent import.*OpenAIChatClient" tests/ src/`
Expected: 0 hits after step 2 cleanup; if hits remain, edit those imports to:
```python
from alphasolve.llm.providers.openai_chat import OpenAIChatClient
```

- [ ] **Step 3: Run all tests**

Run: `pytest -v`
Expected: all tests pass. If `OpenAIChatClient` was previously constructed with `OpenAIChatClient(config_dict)` (the old signature), update those call sites to construct a `Preset` first.

### Task 3.3: Commit 3

- [ ] **Step 1: Confirm clean state**

Run: `git status && pytest -v` (both pass)

- [ ] **Step 2: Commit**

```bash
git add -A
git commit -m "refactor(llm): use new OpenAIChatClient from alphasolve.llm

Removes the inline OpenAIChatClient class from general_agent.py and
wires GeneralPurposeAgent through alphasolve.llm.providers.openai_chat
instead. make_openai_client_factory is a temporary shim that converts
agent_config.py's legacy config dicts into Preset objects; this shim
disappears in Commit 6 once profile-driven model resolution is in
place.

ChatClient Protocol and ChatDeltaSink remain in general_agent.py as
local re-exports for now — Commit 4 unifies them with alphasolve.llm.types.

Part of refactor/llm-provider-abstraction (Commit 3 of 6)."
```

---

## Commit 4: Adopt typed messages across the agent layer

**End state:** `GeneralAgentConfig` has `role` (no `model_config`), tuples for `tools`/`skills`. `ToolRegistry.openai_tools` is renamed to `tool_defs` and returns `list[ToolDef]`. `GeneralPurposeAgent.run()` operates on `list[Message]` and returns `CompletionResponse`. `team/*.py` files build `Message` instead of dict literals. `agent_config.py`'s 11 provider constants and 7 role aliases are deleted. The dict↔Message shim in `general_agent.py` is gone.

> This is the largest commit. If it grows unwieldy in practice, split into 4a (`GeneralAgentConfig` + `ToolRegistry`), 4b (`GeneralPurposeAgent.run()`), 4c (`team/*.py`), 4d (`agent_config.py` deletion).

### Task 4.1: Update `GeneralAgentConfig` shape and YAML loader

**Files:**
- Modify: `src/alphasolve/agents/general/config.py:13-25` (the dataclass)
- Modify: `src/alphasolve/agents/general/config.py:197-209` (the yaml loader)

- [ ] **Step 1: Add a failing test**

Append to `tests/test_general_agent.py` (or create `tests/test_general_agent_config.py`):

```python
def test_general_agent_config_has_role_field():
    from alphasolve.agents.general.config import GeneralAgentConfig
    cfg = GeneralAgentConfig(name="verifier_adversarial", system_prompt="...", role="verifier")
    assert cfg.role == "verifier"
    assert cfg.effective_role() == "verifier"


def test_effective_role_falls_back_to_name():
    from alphasolve.agents.general.config import GeneralAgentConfig
    cfg = GeneralAgentConfig(name="curator", system_prompt="...")
    assert cfg.role is None
    assert cfg.effective_role() == "curator"


def test_loading_yaml_with_model_config_raises(tmp_path):
    """The migration is one-way; YAML with model_config: must fail loudly."""
    from alphasolve.agents.general.config import load_general_agent_config
    yaml_path = tmp_path / "broken.yaml"
    yaml_path.write_text(
        "version: 1\n"
        "agent:\n"
        "  name: x\n"
        "  system_prompt: hi\n"
        "  model_config: VERIFIER_CONFIG\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as exc:
        load_general_agent_config(yaml_path)
    assert "model_config" in str(exc.value)
    assert "role" in str(exc.value)
```

- [ ] **Step 2: Run tests, verify failure**

Run: `pytest tests/test_general_agent.py -k "role" -v` (or equivalent)
Expected: AttributeError on `role` and `effective_role`.

- [ ] **Step 3: Update the dataclass and loader**

Edit `src/alphasolve/agents/general/config.py` lines 13-25:

```python
@dataclass(frozen=True)
class GeneralAgentConfig:
    name: str
    system_prompt: str
    role: str | None = None
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

Edit `src/alphasolve/agents/general/config.py:197-209` (inside `_resolve_agent_config`):

Replace:
```python
        model_config=raw.get("model_config") or raw.get("model") or (base.model_config if base else None),
```

With:
```python
        # `model_config:` is removed; raise loudly to force migration.
        # We check `raw` and `base_raw` keys above; here just thread `role` through.
```

Earlier in `_resolve_agent_config`, near the top (after the `extend` block), add:

```python
    if "model_config" in raw:
        raise ValueError(
            f"{config_path}: field 'model_config' is no longer supported; "
            f"replace with 'role: <role-name>' (see "
            f"docs/superpowers/specs/2026-05-22-alphasolve-llm-provider-abstraction-design.md §5.5)"
        )
```

Adjust the final `GeneralAgentConfig(...)` call to use `role`:

```python
    return GeneralAgentConfig(
        name=name,
        system_prompt=prompt_text,
        role=raw.get("role") or (base.role if base else None),
        tools=tuple(tools),
        tool_parameters=tool_parameters,
        max_turns=int(raw.get("max_turns", base.max_turns if base else 80)),
        skills=tuple(skills),
        when_to_use=str(raw.get("when_to_use") or (base.when_to_use if base else "")),
        system_prompt_template=prompt_template,
        system_prompt_args=prompt_args,
        metadata=metadata,
    )
```

- [ ] **Step 4: Run targeted tests, verify pass**

Run: `pytest tests/test_general_agent.py -k "role or model_config" -v`
Expected: tests pass.

- [ ] **Step 5: Run the full suite**

Run: `pytest -v`
Expected: tests under `tests/test_agent_team.py` etc. may fail due to YAML files still containing `model_config:` (those are migrated in Commit 5). For now, expect some workflow-level tests to error with the migration message. Mark the affected tests with `@pytest.mark.xfail(reason="awaiting Commit 5 YAML migration")` OR run with `--deselect` on those tests until Commit 5 completes.

To find affected tests:
```bash
pytest -v 2>&1 | grep -E "FAILED.*model_config" | sort -u
```

Tests directly using `GeneralAgentConfig(model_config=...)` (without YAML) must be updated to `GeneralAgentConfig(role=...)` in this commit. Tests loading YAML fixtures must wait for Commit 5 — temporarily skip them with xfail.

### Task 4.2: Update `ToolRegistry.openai_tools` → `tool_defs`

**Files:**
- Modify: `src/alphasolve/agents/general/tool_registry.py:33-44, 68-78`

- [ ] **Step 1: Add a failing test**

Append to `tests/test_general_agent.py`:

```python
def test_tool_registry_returns_tool_defs():
    from alphasolve.agents.general.tool_registry import ToolRegistry, ToolResult
    from alphasolve.llm.types import ToolDef

    reg = ToolRegistry()
    reg.register(
        name="Read",
        description="read a file",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        handler=lambda args: ToolResult(content="..."),
    )
    defs = reg.tool_defs(enabled=["Read"])
    assert len(defs) == 1
    assert isinstance(defs[0], ToolDef)
    assert defs[0].name == "Read"
    assert defs[0].description == "read a file"
    assert defs[0].parameters["required"] == ["path"]
```

- [ ] **Step 2: Run test, verify failure**

Run: `pytest tests/test_general_agent.py::test_tool_registry_returns_tool_defs -v`
Expected: AttributeError on `tool_defs`.

- [ ] **Step 3: Implement `tool_defs`, delete `to_openai_tool` and `openai_tools`**

Edit `src/alphasolve/agents/general/tool_registry.py`:

Delete `RegisteredTool.to_openai_tool` (lines 33-44).

Replace the `openai_tools` method (lines 68-78) with:

```python
    def tool_defs(
        self,
        enabled: tuple[str, ...] | list[str] | None = None,
        tool_parameters: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> list["ToolDef"]:
        from alphasolve.llm.types import ToolDef
        names = list(enabled) if enabled is not None else list(self._tools)
        missing = [name for name in names if name not in self._tools]
        if missing:
            raise KeyError(f"unknown tools: {missing}")
        constraints = tool_parameters or {}
        out: list[ToolDef] = []
        for name in names:
            t = self._tools[name]
            params = deepcopy(t.parameters)
            if name in constraints:
                _apply_parameter_constraints(params, constraints[name])
            out.append(ToolDef(name=t.name, description=t.description, parameters=params))
        return out
```

- [ ] **Step 4: Update callers of `openai_tools`**

Find callers: `grep -rn "openai_tools" src/ tests/`

For each caller, replace `.openai_tools(...)` with `.tool_defs(...)`. The return type changes from `list[dict]` to `list[ToolDef]` — callers in `GeneralPurposeAgent.run()` are updated in Task 4.3; other callers (likely tests + `team/debug_agent.py`) update inline here.

- [ ] **Step 5: Run targeted tests, verify pass**

Run: `pytest tests/test_general_agent.py::test_tool_registry_returns_tool_defs -v`
Expected: pass.

### Task 4.3: Rewire `GeneralPurposeAgent.run()` to use typed messages

**Files:**
- Modify: `src/alphasolve/agents/general/general_agent.py:232+` (the `run` method)

- [ ] **Step 1: Update `run` to use `Message` / `ToolDef` / `CompletionResponse`**

Open `src/alphasolve/agents/general/general_agent.py`. Find `def run(self, task: str, ...)`. Convert message construction from dict literals to `Message(...)`:

```python
from alphasolve.llm.types import Message, ToolDef, CompletionResponse


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
        enabled=list(self.config.tools),
        tool_parameters=self.config.tool_parameters,
    )

    final_answer = ""
    trace: list[dict[str, Any]] = [{
        "type": "run_start",
        "agent": self.config.name,
        "task": task,
        "description": description,
        "enabled_tools": list(self.config.tools),
        "tool_parameters": dict(self.config.tool_parameters),
    }]
    self.last_trace = trace
    self._emit(trace[-1])

    for turn in range(1, self.config.max_turns + 1):
        if self.stop_event is not None and self.stop_event.is_set():
            trace.append({"type": "run_stopped", "turn": turn, "reason": "stop_event set"})
            self._emit(trace[-1])
            return AgentRunResult(final_answer="", messages=messages, trace=trace, turns=turn - 1)

        delta_sink = self._make_delta_sink(turn=turn, trace=trace)
        response: CompletionResponse = self.client.complete(
            messages=messages,
            tools=tools,
            delta_sink=delta_sink,
        )
        assistant_msg = response.message
        messages.append(assistant_msg)
        self._emit({"type": "message", "turn": turn, "message": _message_to_log(assistant_msg)})

        if response.finish_reason == "stop" and not assistant_msg.tool_calls:
            final_answer = assistant_msg.content
            trace.append({"type": "stop", "turn": turn, "final_answer": final_answer})
            self._emit(trace[-1])
            return AgentRunResult(final_answer=final_answer, messages=messages, trace=trace, turns=turn)

        if assistant_msg.tool_calls:
            for tc in assistant_msg.tool_calls:
                result = self.tool_registry.execute(
                    tc.name, tc.args,
                    enabled=list(self.config.tools),
                    tool_parameters=self.config.tool_parameters,
                )
                tool_msg = Message(
                    role="tool",
                    content=result.content,
                    tool_call_id=tc.id,
                    name=tc.name,
                )
                messages.append(tool_msg)
                trace.append({
                    "type": "tool_result",
                    "turn": turn,
                    "tool": tc.name,
                    "args": tc.args,
                    "content": result.content,
                    "is_error": result.is_error,
                })
                self._emit(trace[-1])
                if result.stop_agent:
                    final_answer = result.stop_answer or ""
                    return AgentRunResult(final_answer=final_answer, messages=messages, trace=trace, turns=turn)
            continue

        # No tool calls, finish_reason != stop — loop again.

    # Loop exhausted
    trace.append({"type": "max_turns_reached", "max_turns": self.config.max_turns})
    self._emit(trace[-1])
    return AgentRunResult(final_answer=final_answer, messages=messages, trace=trace, turns=self.config.max_turns)


def _message_to_log(msg: Message) -> dict[str, Any]:
    """Serialize Message for the trace/event log."""
    import dataclasses
    return dataclasses.asdict(msg)
```

- [ ] **Step 2: Update `AgentRunResult.messages` type annotation**

Change the existing `AgentRunResult` dataclass (currently at the top of `general_agent.py`) to:

```python
@dataclass(frozen=True)
class AgentRunResult:
    final_answer: str
    messages: list[Message]
    trace: list[dict[str, Any]]
    turns: int
```

- [ ] **Step 3: Run targeted tests**

Run: `pytest tests/test_general_agent.py -v`
Expected: many pass; some may fail because callers construct/assert on dict-shaped messages. Update each failing test mechanically: replace `{"role": "user", "content": "x"}` literals with `Message(role="user", content="x")`, and `msg["content"]` access with `msg.content`.

### Task 4.4: Update `team/*.py` consumers

**Files:**
- Modify: `src/alphasolve/agents/team/worker.py`
- Modify: `src/alphasolve/agents/team/curator.py`
- Modify: `src/alphasolve/agents/team/orchestrator.py`
- Modify: `src/alphasolve/agents/team/debug_agent.py`
- Modify: `src/alphasolve/agents/team/tools.py`
- Modify: `src/alphasolve/agents/team/demo.py`
- Modify: `src/alphasolve/agents/team/workflow.py`

- [ ] **Step 1: Find all dict-message construction sites**

Run: `grep -rn 'role.*:.*"\(user\|assistant\|system\|tool\)"\|"role".*:' src/alphasolve/agents/team/`
This locates dict literals like `{"role": "user", "content": ...}`.

- [ ] **Step 2: Replace each dict literal with `Message(...)`**

For each match:

Before:
```python
messages = [
    {"role": "user", "content": prompt},
]
```

After:
```python
from alphasolve.llm.types import Message
messages = [
    Message(role="user", content=prompt),
]
```

- [ ] **Step 3: Update message reads**

For each `msg["content"]` / `msg["tool_calls"]` / `msg["role"]` access, change to attribute access (`msg.content`, `msg.tool_calls`, `msg.role`).

For tool_call iteration, replace:
```python
for tc in msg["tool_calls"]:
    name = tc["function"]["name"]
    args = json.loads(tc["function"]["arguments"])
```
with:
```python
for tc in msg.tool_calls:
    name = tc.name
    args = tc.args
```

- [ ] **Step 4: Update `make_demo_client_factory`**

Open `src/alphasolve/agents/team/demo.py`. Find `make_demo_client_factory` at line 107. The function returns a fake client whose `complete()` method must now return `CompletionResponse`, not a dict.

Rewrite the fake client to comply with the `ChatClient` Protocol:

```python
from alphasolve.llm.types import (
    ChatClient, ChatDeltaSink, CompletionResponse, Message, ToolCall, ToolDef, Usage,
)


class _DemoChatClient:
    """Deterministic local client used by --demo mode and offline tests."""
    def complete(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDef],
        delta_sink: ChatDeltaSink | None = None,
    ) -> CompletionResponse:
        # ... preserve existing demo logic, but construct Message/CompletionResponse ...
        # (the exact body depends on the existing demo logic; faithfully port it.)
```

Read the existing `make_demo_client_factory` body and port its logic 1:1 into the new shape.

- [ ] **Step 5: Run the agent team tests**

Run: `pytest tests/test_agent_team.py -v`
Expected: most pass; mechanical failures should be fixable in the same way (`msg["x"]` → `msg.x`).

### Task 4.5: Remove the temporary shim in `general_agent.py`

**Files:**
- Modify: `src/alphasolve/agents/general/general_agent.py`

- [ ] **Step 1: Verify nothing imports the local `ChatClient` Protocol from `general_agent`**

Run: `grep -rn "from alphasolve.agents.general.general_agent import.*ChatClient\|from .general_agent import.*ChatClient" src/ tests/`
If hits exist, update them to `from alphasolve.llm import ChatClient`.

- [ ] **Step 2: Delete local `ChatDeltaSink` and `ChatClient` from `general_agent.py`**

At the top of `src/alphasolve/agents/general/general_agent.py`, replace the local definitions with re-exports:

```python
from alphasolve.llm.types import ChatClient, ChatDeltaSink  # noqa: F401  (re-export for compatibility)
```

Delete the lines that previously defined them inline (around the original lines 21, 26-34).

- [ ] **Step 3: Run tests**

Run: `pytest -v`
Expected: all tests pass except those skipped/xfail'd for the YAML migration (Commit 5).

### Task 4.6: Delete `agent_config.py` provider constants

**Files:**
- Modify: `src/alphasolve/config/agent_config.py`

- [ ] **Step 1: Find all references to the doomed constants**

Run: `grep -rn "DEEPSEEK_CONFIG\|DEEPSEEK_PRO_CONFIG\|PARASAIL_CONFIG\|LONGCAT_CONFIG\|MOONSHOT_CONFIG\|VOLCANO_CONFIG\|VOLCANO_DS_CONFIG\|DASHSCOPE_CONFIG\|MIMO_CONFIG\|OPENROUTER_CONFIG\|GENERATOR_CONFIG\|VERIFIER_CONFIG\|REVISER_CONFIG\|COMPUTE_SUBAGENT_CONFIG\|PROOF_SUBAGENT_CONFIG\|ORCHESTRATOR_CONFIG\|CURATOR_CONFIG" src/ tests/`

Expected references:
- `src/alphasolve/agents/team/workflow.py` — the shim from Commit 3 (will be replaced in Commit 6 — see below)
- `src/alphasolve/config/agent_config.py` — the definitions themselves
- Various tests — update to construct `Preset` directly or import from a fixture

- [ ] **Step 2: Update the workflow shim to not depend on the constants**

In `src/alphasolve/agents/team/workflow.py`, the `make_openai_client_factory` shim from Commit 3 still reads `AlphaSolveConfig` constants. Before deleting the constants, switch the shim to read from `presets.yaml`. Since Commit 6 will replace the whole CLI flow anyway, the simplest move is to have `make_openai_client_factory` immediately call `alphasolve.llm.make_client_factory` with the `balanced` profile and the shipped presets:

```python
def make_openai_client_factory(suite) -> ClientFactory:
    """Legacy adapter: delegates to alphasolve.llm.make_client_factory with the balanced profile."""
    from pathlib import Path
    from alphasolve.llm import load_presets, load_active_profile, make_client_factory

    PACKAGE_CONFIG = Path(__file__).parent.parent.parent / "config"
    presets = load_presets(repo_path=PACKAGE_CONFIG / "presets.yaml", user_path=None)
    profile = load_active_profile(name="balanced", repo_path=PACKAGE_CONFIG / "profiles.yaml", user_path=None)
    return make_client_factory(profile, presets)
```

The whole "synth env var" complexity from Commit 3's shim disappears.

- [ ] **Step 3: Replace `agent_config.py` with the Wolfram-only version**

Rewrite `src/alphasolve/config/agent_config.py` to:

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

- [ ] **Step 4: Update test fixtures**

For each test file that referenced a `*_CONFIG` constant, change to constructing a `Preset` directly:

Before:
```python
from alphasolve.config.agent_config import AlphaSolveConfig
client = OpenAIChatClient(AlphaSolveConfig.DEEPSEEK_PRO_CONFIG)
```

After:
```python
from alphasolve.llm.config.preset import Preset
from alphasolve.llm.providers.openai_chat import OpenAIChatClient

preset = Preset(
    name="deepseek-pro",
    wire_format="openai_chat",
    base_url="https://api.deepseek.com",
    api_key_env="DEEPSEEK_API_KEY",
    model="deepseek-v4-pro",
    params={"extra_body": {"reasoning": {"effort": "max"}}},
)
client = OpenAIChatClient(preset)
```

- [ ] **Step 5: Run all tests except those awaiting Commit 5 YAML migration**

Run: `pytest -v`
Expected: all tests pass except YAML-loading tests that still see `model_config:` in the live yaml files.

### Task 4.7: Commit 4

- [ ] **Step 1: Commit**

```bash
git add -A
git commit -m "refactor(agent): adopt typed messages in GeneralPurposeAgent

GeneralAgentConfig: remove model_config field, add role field, change
list to tuple for tools/skills, add effective_role() helper.
load_general_agent_config raises ValueError on legacy 'model_config:'
fields with a migration message.

ToolRegistry: rename openai_tools() to tool_defs() returning
list[ToolDef]; delete RegisteredTool.to_openai_tool().

GeneralPurposeAgent.run() now operates on list[Message] and consumes
CompletionResponse. Team-layer files (worker, curator, orchestrator,
debug_agent, tools, demo, workflow) construct Message objects instead
of dict literals.

agent_config.py reduced to its Wolfram block; the 11 provider constants
and 7 role aliases are deleted. The legacy make_openai_client_factory
in workflow.py is rewritten to delegate to alphasolve.llm.make_client_factory
with the balanced profile.

Part of refactor/llm-provider-abstraction (Commit 4 of 6)."
```

---

## Commit 5: Migrate agent YAML files to `role:` schema

**End state:** Every YAML file under `src/alphasolve/config/agents/` and `subagents/` has its `model_config:` field replaced with `role:`. Loading any YAML still containing `model_config:` raises with a clear migration message.

### Task 5.1: Migrate agent YAML files

**Files:**
- Modify: `src/alphasolve/config/agents/generator.yaml`
- Modify: `src/alphasolve/config/agents/orchestrator.yaml`
- Modify: `src/alphasolve/config/agents/reviser.yaml`
- Modify: `src/alphasolve/config/agents/theorem_checker.yaml`
- Modify: `src/alphasolve/config/agents/verifier.yaml`
- Modify: `src/alphasolve/config/agents/verifier_adversarial.yaml`
- Modify: `src/alphasolve/config/agents/verifier_citation.yaml`
- Modify: `src/alphasolve/config/agents/verifier_failure_modes.yaml`
- Modify: `src/alphasolve/config/agents/verifier_premise_chain.yaml`
- Modify: `src/alphasolve/config/agents/verifier_stepwise.yaml`

For each file, apply the mapping per spec §5.5:

| File | Old `model_config:` | New `role:` |
|---|---|---|
| `generator.yaml`              | `GENERATOR_CONFIG`    | `generator`    |
| `orchestrator.yaml`           | `ORCHESTRATOR_CONFIG` | `orchestrator` |
| `reviser.yaml`                | `REVISER_CONFIG`      | `reviser`      |
| `theorem_checker.yaml`        | `VERIFIER_CONFIG`     | `verifier`     |
| `verifier.yaml`               | `VERIFIER_CONFIG`     | `verifier`     |
| `verifier_adversarial.yaml`   | `VERIFIER_CONFIG`     | `verifier`     |
| `verifier_citation.yaml`      | `VERIFIER_CONFIG`     | `verifier`     |
| `verifier_failure_modes.yaml` | `VERIFIER_CONFIG`     | `verifier`     |
| `verifier_premise_chain.yaml` | `VERIFIER_CONFIG`     | `verifier`     |
| `verifier_stepwise.yaml`      | `VERIFIER_CONFIG`     | `verifier`     |

- [ ] **Step 1: For each file, delete the `model_config:` line and add a `role:` line**

Example for `theorem_checker.yaml`:

Before:
```yaml
version: 1
agent:
  name: theorem_checker
  system_prompt_path: ../../prompts/theorem_checker.md
  model_config: VERIFIER_CONFIG
  max_turns: 60
  ...
```

After:
```yaml
version: 1
agent:
  name: theorem_checker
  system_prompt_path: ../../prompts/theorem_checker.md
  role: verifier
  max_turns: 60
  ...
```

Apply to all 10 files above.

- [ ] **Step 2: Verify each file parses and produces the right role**

Run:
```bash
python -c "
import pathlib, yaml
from alphasolve.agents.general.config import load_general_agent_config
for p in sorted(pathlib.Path('src/alphasolve/config/agents').glob('*.yaml')):
    cfg = load_general_agent_config(p)
    print(f'{p.name}: role={cfg.role}')"
```
Expected: prints each file with its assigned role; no exception.

### Task 5.2: Migrate subagent YAML files

**Files:**
- Modify: `src/alphasolve/config/subagents/compute_subagent.yaml`
- Modify: `src/alphasolve/config/subagents/curator.yaml`
- Modify: `src/alphasolve/config/subagents/numerical_experiment_subagent.yaml`
- Modify: `src/alphasolve/config/subagents/reasoning_subagent.yaml`
- Modify: `src/alphasolve/config/subagents/research_reviewer.yaml`

| File | Old `model_config:` | New `role:` |
|---|---|---|
| `compute_subagent.yaml`              | `COMPUTE_SUBAGENT_CONFIG` | `compute_subagent` |
| `curator.yaml`                       | `CURATOR_CONFIG`          | `curator`          |
| `numerical_experiment_subagent.yaml` | `COMPUTE_SUBAGENT_CONFIG` | `compute_subagent` |
| `reasoning_subagent.yaml`            | `PROOF_SUBAGENT_CONFIG`   | `proof_subagent`   |
| `research_reviewer.yaml`             | `ORCHESTRATOR_CONFIG`     | `orchestrator`     |

- [ ] **Step 1: Apply the same line replacement as Task 5.1 to all 5 files**

- [ ] **Step 2: Verify each file parses**

Run:
```bash
python -c "
import pathlib
from alphasolve.agents.general.config import load_general_agent_config
for p in sorted(pathlib.Path('src/alphasolve/config/subagents').glob('*.yaml')):
    cfg = load_general_agent_config(p)
    print(f'{p.name}: role={cfg.role}')"
```
Expected: prints each file with its assigned role; no exception.

### Task 5.3: Verify no `model_config:` remains anywhere

- [ ] **Step 1: Search for `model_config:` in YAML files**

Run: `grep -rn "model_config:" src/alphasolve/config/`
Expected: zero hits.

- [ ] **Step 2: Add a test that catches a future regression**

Append to `tests/llm/test_shipped_yamls.py`:

```python
def test_no_yaml_uses_model_config_field():
    """A regression test: model_config: must never reappear in shipped configs."""
    for yaml_path in (Path(__file__).parent.parent.parent / "src" / "alphasolve" / "config").rglob("*.yaml"):
        content = yaml_path.read_text(encoding="utf-8")
        assert "model_config:" not in content, f"{yaml_path}: contains model_config:"
```

Run: `pytest tests/llm/test_shipped_yamls.py::test_no_yaml_uses_model_config_field -v`
Expected: passes.

### Task 5.4: Un-skip / un-xfail tests blocked by YAML migration

- [ ] **Step 1: Find xfail markers added in Commit 4**

Run: `grep -rn 'awaiting Commit 5 YAML migration' tests/`

- [ ] **Step 2: Remove those markers**

For each match, delete the `@pytest.mark.xfail(...)` line.

- [ ] **Step 3: Run the full suite**

Run: `pytest -v`
Expected: all tests pass.

### Task 5.5: Commit 5

- [ ] **Step 1: Commit**

```bash
git add src/alphasolve/config/agents/ src/alphasolve/config/subagents/ tests/llm/test_shipped_yamls.py tests/
git commit -m "refactor(config): migrate agent yaml files to role: schema

All 15 agent/subagent yaml files now declare role: <role-name> instead
of model_config: *_CONFIG. The loader raises ValueError with a clear
migration message if it encounters the legacy field.

theorem_checker (was VERIFIER_CONFIG) → role: verifier
research_reviewer (was ORCHESTRATOR_CONFIG) → role: orchestrator
numerical_experiment_subagent (was COMPUTE_SUBAGENT_CONFIG) → role: compute_subagent

A test_shipped_yamls regression check fails the build if any future
yaml reintroduces 'model_config:'.

Part of refactor/llm-provider-abstraction (Commit 5 of 6)."
```

---

## Commit 6: Wire `--profile` flag end-to-end

**End state:** `cli.py` exposes `--profile`, `--list-profiles`, `--list-presets`. `workflow.AlphaSolve.__init__`'s `client_factory` becomes a required argument. The legacy `make_openai_client_factory` becomes a thin wrapper around `alphasolve.llm.make_client_factory`. Three manual smoke runs validate the three profiles.

### Task 6.1: Add `--profile`, `--list-profiles`, `--list-presets` to `cli.py`

**Files:**
- Modify: `src/alphasolve/cli.py`

- [ ] **Step 1: Replace the CLI argument block**

In `src/alphasolve/cli.py`, find the `parser.add_argument(...)` block (lines 112-141). After the existing args, add:

```python
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="Model profile (cheap | balanced | strategic | <user-defined>). "
             "Default: ALPHASOLVE_PROFILE env var, else 'default:' from profiles.yaml.",
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="List available profiles and exit",
    )
    parser.add_argument(
        "--list-presets",
        action="store_true",
        help="List available presets and exit",
    )
```

- [ ] **Step 2: Add `--list-*` handling and profile resolution**

After `args = parser.parse_args()` (line 143), before the agent-debug branch, add:

```python
    from pathlib import Path as _Path
    from alphasolve.llm import load_presets, load_profile, load_active_profile, make_client_factory
    from alphasolve.config.agent_config import PACKAGE_ROOT as _PKG_ROOT
    _PRESETS_PATH = _Path(_PKG_ROOT) / "config" / "presets.yaml"
    _PROFILES_PATH = _Path(_PKG_ROOT) / "config" / "profiles.yaml"
    _USER_DIR = _Path(os.getenv("ALPHASOLVE_CONFIG_DIR") or (_Path.home() / ".alphasolve"))
    _USER_PRESETS = _USER_DIR / "presets.yaml" if (_USER_DIR / "presets.yaml").is_file() else None
    _USER_PROFILES = _USER_DIR / "profiles.yaml" if (_USER_DIR / "profiles.yaml").is_file() else None

    if args.list_presets:
        presets = load_presets(repo_path=_PRESETS_PATH, user_path=_USER_PRESETS)
        for name in sorted(presets):
            p = presets[name]
            print(f"  {name:<28} {p.wire_format:<22} {p.model}")
        return

    if args.list_profiles:
        # We read profiles.yaml directly so we can show role mappings, not just names.
        import yaml as _yaml
        merged: dict = {}
        for path in (_PROFILES_PATH, _USER_PROFILES):
            if path is None or not path.is_file():
                continue
            with path.open("r", encoding="utf-8") as f:
                data = _yaml.safe_load(f) or {}
            data.pop("default", None)
            merged.update(data)
        for name in sorted(merged):
            print(f"  {name}")
            for role, preset_name in (merged[name] or {}).items():
                print(f"    {role:<22} → {preset_name}")
        return

    profile_name = args.profile or os.getenv("ALPHASOLVE_PROFILE")
    active_profile = load_active_profile(name=profile_name, repo_path=_PROFILES_PATH, user_path=_USER_PROFILES)
    presets = load_presets(repo_path=_PRESETS_PATH, user_path=_USER_PRESETS)
    client_factory = make_client_factory(active_profile, presets) if not args.demo else make_demo_client_factory()
```

- [ ] **Step 3: Update the `AlphaSolve(...)` construction**

Find the `_app = AlphaSolve(...)` call (around line 195). Replace `client_factory=make_demo_client_factory() if args.demo else None` with `client_factory=client_factory` (using the resolved factory from Step 2).

- [ ] **Step 4: Update the `--agent-debug` branch similarly**

Around lines 150-178, the `--agent-debug` branch chooses between `make_demo_client_factory()` and `make_openai_client_factory(suite)`. Replace `make_openai_client_factory(suite)` with the `client_factory` constructed in Step 2. Delete the `from alphasolve.agents.team.workflow import make_openai_client_factory` import.

### Task 6.2: Make `workflow.AlphaSolve.client_factory` required

**Files:**
- Modify: `src/alphasolve/agents/team/workflow.py:34-44` (the `__init__` signature)

- [ ] **Step 1: Change the parameter type**

Open `src/alphasolve/agents/team/workflow.py`. Find `AlphaSolve.__init__`. Change:

```python
client_factory: ClientFactory | None = None,
```

to:

```python
client_factory: ClientFactory,
```

Move it earlier in the parameter list if Python complains about non-default-after-default ordering.

- [ ] **Step 2: Remove the auto-construction code in `__init__` body**

If the body has anything like `if client_factory is None: client_factory = make_openai_client_factory(...)`, delete it. The CLI is now responsible for constructing.

- [ ] **Step 3: Delete the legacy `make_openai_client_factory`**

Since `cli.py` no longer imports it and `AlphaSolve.__init__` no longer constructs it, delete the function in `workflow.py` (originally line 281).

If any test still imports it, update the test to construct the factory via `alphasolve.llm.make_client_factory` instead.

- [ ] **Step 4: Run the test suite**

Run: `pytest -v`
Expected: all tests pass.

### Task 6.3: Smoke runs (manual)

> These require valid API keys in the environment and live network. Run from a known small fixture problem.

- [ ] **Step 1: Run with `--profile balanced` (default)**

Pre-conditions: `DEEPSEEK_API_KEY` is set in the environment. A fixture problem directory exists.

Run:
```bash
DEEPSEEK_API_KEY=... alphasolve --config tests/fixtures/small_problem --profile balanced
```
Expected: orchestrator starts, agents resolve to `deepseek-pro` / `deepseek-flash` per the balanced profile, run completes (or makes progress equivalent to pre-refactor behavior on the same fixture).

- [ ] **Step 2: Run with `--profile cheap`**

Run:
```bash
DEEPSEEK_API_KEY=... alphasolve --config tests/fixtures/small_problem --profile cheap
```
Expected: every agent uses `deepseek-flash`; run completes.

- [ ] **Step 3: Run with `--profile strategic`**

Pre-conditions: `DASHSCOPE_API_KEY` is set (for qwen orchestrator). The `qwen-3.7-max` preset's `model:` field has been confirmed against Dashscope's actual model identifier (TODO from spec §9).

Run:
```bash
DEEPSEEK_API_KEY=... DASHSCOPE_API_KEY=... alphasolve --config tests/fixtures/small_problem --profile strategic
```
Expected: orchestrator uses `qwen-3.7-max`, other agents use `deepseek-pro`/`deepseek-flash`; run completes.

- [ ] **Step 4: Run `--list-profiles` and `--list-presets`**

Run: `alphasolve --list-profiles`
Expected: prints `balanced`, `cheap`, `strategic` with role mappings.

Run: `alphasolve --list-presets`
Expected: prints all 13 presets with wire_format and model.

- [ ] **Step 5: Confirm `--profile unknown` errors gracefully**

Run: `alphasolve --profile nonexistent-profile`
Expected: exits non-zero with a clear `KeyError`-derived message listing available profile names.

### Task 6.4: Placeholders already resolved

All three "TODO" placeholders were confirmed by the user before implementation, and the values baked into the `presets.yaml` content in Task 1.6 already reflect them:

- DeepSeek Anthropic endpoint URL: `https://api.deepseek.com/anthropic` ✓
- Moonshot Anthropic endpoint URL: `https://api.moonshot.cn/anthropic` ✓
- Qwen 3.7 Max model_id: `qwen3.7-max` ✓

- [ ] **Step 1: Verify the shipped `presets.yaml` has no `# TODO` comments**

Run: `grep -n "TODO" src/alphasolve/config/presets.yaml`
Expected: zero hits.

### Task 6.5: Commit 6

- [ ] **Step 1: Commit**

```bash
git add src/alphasolve/cli.py src/alphasolve/agents/team/workflow.py src/alphasolve/config/presets.yaml
git commit -m "feat(cli): wire --profile flag end-to-end + smoke runs

Adds --profile, --list-profiles, --list-presets to cli.py. Profile is
resolved from --profile flag > ALPHASOLVE_PROFILE env var > profiles.yaml
'default:' key (in that order).

AlphaSolve.__init__'s client_factory becomes a required argument; the
legacy make_openai_client_factory in workflow.py is deleted in favor
of alphasolve.llm.make_client_factory.

Smoke-tested: balanced, cheap, strategic profiles each run end-to-end
on the small fixture problem; --list-* commands and bad-profile error
behavior verified.

Closes refactor/llm-provider-abstraction (Commit 6 of 6).
See docs/superpowers/specs/2026-05-22-alphasolve-llm-provider-abstraction-design.md"
```

---

## Done

After Commit 6, the branch is ready to merge. Do **not** squash — preserve the 6-commit boundary for bisecting and review.

```bash
git push -u origin refactor/llm-provider-abstraction
gh pr create --title "Refactor: LLM provider abstraction (Milestone 1)" --body "$(cat <<'EOF'
## Summary

- New `alphasolve.llm` sub-package with typed lingua franca, OpenAI Chat Completions + Anthropic Messages providers, and `--profile` switching (cheap / balanced / strategic).
- All 11 `agent_config.py` provider constants deleted in favor of `presets.yaml` + `profiles.yaml`.
- See `docs/superpowers/specs/2026-05-22-alphasolve-llm-provider-abstraction-design.md` for full design rationale.

## Test plan

- [x] `pytest tests/llm/` — full unit coverage of the new package (loaders, providers, conversion, factory).
- [x] `pytest` — full suite passes; existing agent/team tests adapted to typed messages.
- [x] Smoke: `alphasolve --profile balanced` reproduces pre-refactor behaviour.
- [x] Smoke: `alphasolve --profile cheap` runs end-to-end.
- [x] Smoke: `alphasolve --profile strategic` runs end-to-end (qwen orchestrator).
- [x] `alphasolve --list-profiles` and `--list-presets` work.
EOF
)"
```

---

## Self-Review Checklist

Run through this before declaring the plan complete:

- [ ] **Spec coverage:** Every section of `docs/superpowers/specs/2026-05-22-alphasolve-llm-provider-abstraction-design.md` is implemented by at least one task. The 19-name public API, 13 presets, 3 profiles, role schema, CLI flags, 6-commit structure, test plan, and TODO placeholders are all addressed.
- [ ] **No placeholders in tasks:** Every step contains either code, an exact shell command, or a documented manual action. Search the file for "TODO", "TBD", "fill in", "similar to" — only the three intentional TODO placeholders in `presets.yaml` (and the Task 6.4 confirmations) remain.
- [ ] **Type consistency:** `Message`, `ToolCall`, `ToolDef`, `CompletionResponse`, `Preset`, `Profile`, `ChatClient`, `effective_role()`, `tool_defs()`, `role`, `wire_format`, `api_key_env`, `params` — used consistently across all tasks.
- [ ] **TDD discipline:** Each new component has a failing-test step before the implementation step.
- [ ] **Commit boundaries map to spec §7:** Six commits, each ending with a `git commit` step and a passing full test suite.
