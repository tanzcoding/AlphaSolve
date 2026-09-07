"""通过官方 Python SDK 连接 Codex，保留 AlphaSolve 的工具执行边界。"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
from functools import lru_cache
import json
import os
from pathlib import Path
from queue import Queue
import re
import subprocess
from tempfile import TemporaryDirectory
import threading
from typing import Any

from alphasolve.llm import Preset, ToolDef


_EXTERNAL_CODEX_MIN_VERSION = (0, 153, 4)


class SessionFailure(RuntimeError):
    def __init__(self, message: str, failure_kind: str = "runtime") -> None:
        super().__init__(message)
        self.failure_kind = failure_kind


@lru_cache(maxsize=8)
def _external_codex_version(path: str) -> tuple[int, int, int]:
    """读取独立 Codex CLI 的稳定版三段版本号。"""
    try:
        completed = subprocess.run(
            [path, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"无法执行 {path!r} --version：{exc}") from exc
    match = re.search(
        r"\bcodex-cli\s+(\d+)\.(\d+)\.(\d+)(?P<suffix>[-+][0-9A-Za-z.-]+)?(?=\s|$)",
        completed.stdout,
    )
    if completed.returncode != 0 or match is None:
        output = completed.stdout.strip() or f"退出码 {completed.returncode}"
        raise ValueError(f"无法识别 {path!r} 的 Codex CLI 版本：{output}")
    suffix = match.group("suffix")
    if suffix and suffix.startswith("-"):
        raise ValueError(f"{path!r} 使用 Codex CLI 预发布版本 {match.group(0).split()[-1]}，请安装稳定版。")
    return tuple(int(match.group(index)) for index in range(1, 4))


def _compatible_external_codex(path: Path, *, explicit: bool) -> str | None:
    """验证外部 CLI；自动发现失败时允许 SDK 回退，显式配置则报错。"""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as exc:
        message = f"无法解析 Codex CLI 路径 {str(path)!r}：{exc}"
        if explicit:
            raise SessionFailure(message, "configuration") from exc
        return None
    invalid = not resolved.is_file() or (
        os.name != "nt" and not os.access(resolved, os.X_OK)
    )
    if invalid:
        message = f"Codex CLI 路径不是可执行文件：{resolved}"
        if explicit:
            raise SessionFailure(message, "configuration")
        return None
    try:
        version = _external_codex_version(str(resolved))
    except ValueError as exc:
        if explicit:
            raise SessionFailure(str(exc), "configuration") from exc
        return None
    if version < _EXTERNAL_CODEX_MIN_VERSION:
        required = ".".join(str(part) for part in _EXTERNAL_CODEX_MIN_VERSION)
        found = ".".join(str(part) for part in version)
        if explicit:
            raise SessionFailure(
                f"ALPHASOLVE_CODEX_BIN 的 Codex CLI 版本为 {found}，需要 {required} 或更高版本。",
                "configuration",
            )
        return None
    return str(resolved)


def _codex_on_path() -> Path | None:
    """只在 PATH 的可信绝对目录中查找 Codex，避免 Windows 隐式搜索当前目录。"""
    try:
        current_dir = Path.cwd().resolve()
    except (OSError, RuntimeError):
        return None
    if os.name == "nt":
        raw_extensions = os.getenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        extensions = tuple(
            extension if extension.startswith(".") else f".{extension}"
            for value in raw_extensions.split(os.pathsep)
            if (extension := value.strip())
        )
    else:
        extensions = ("",)

    for value in os.getenv("PATH", os.defpath).split(os.pathsep):
        entry = value.strip().strip('"')
        if not entry:
            continue
        try:
            directory = Path(os.path.expandvars(entry)).expanduser()
            if not directory.is_absolute():
                continue
            directory = directory.resolve()
        except (OSError, RuntimeError):
            continue
        if directory == current_dir:
            continue
        for extension in extensions:
            candidate = directory / f"codex{extension}"
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return candidate
    return None


def _preferred_codex_bin() -> str | None:
    """优先使用兼容的独立 CLI；不可用时交给 SDK 选择随包运行时。"""
    override = os.getenv("ALPHASOLVE_CODEX_BIN")
    if override:
        try:
            path = Path(os.path.expandvars(override)).expanduser()
        except (OSError, RuntimeError) as exc:
            raise SessionFailure(f"无法解析 ALPHASOLVE_CODEX_BIN：{exc}", "configuration") from exc
        return _compatible_external_codex(path, explicit=True)

    discovered = _codex_on_path()
    return _compatible_external_codex(discovered, explicit=False) if discovered else None


def classify_failure(error: Any) -> str:
    """保留上层分类，只识别需要明确停止运行的认证、额度和配置错误。"""
    existing = getattr(error, "failure_kind", None)
    if existing:
        return existing
    text = str(error).lower()
    if any(part in text for part in ("usagelimitexceeded", "usage_limit", "usage limit", "quota", "credit balance")):
        return "quota"
    if any(part in text for part in ("401", "unauthorized", "authentication", "not authenticated", "login", "auth token")):
        return "auth"
    if any(part in text for part in ("configuration", "config error", "invalid model", "model_not_found", "unknown model", "invalid params")):
        return "configuration"
    return "runtime"


@dataclass
class ToolRequest:
    params: dict[str, Any]
    response: Future[dict[str, Any]]


class CodexSession:
    """一个角色对应一个原生会话；模型事件与工具请求交给调用线程消费。"""

    def __init__(self, preset: Preset, system_prompt: str, tools: list[ToolDef]) -> None:
        self.preset = preset
        self.system_prompt = system_prompt
        self.tools = tools
        self.events: Queue = Queue()
        self.thread_id: str | None = None
        self._sdk = None
        self._temp: TemporaryDirectory | None = None
        self._closed = threading.Event()
        self._connect_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: set[Future] = set()

    def start_turn(self, task: str) -> None:
        # 服务端可能先请求工具、再返回 turn/start；调用方必须立即开始消费事件。
        threading.Thread(target=self._start_turn, args=(task,), daemon=True).start()

    def _start_turn(self, task: str) -> None:
        try:
            self._connect()
            if self._closed.is_set():
                return
            self._request("turn/start", {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": task}],
            })
        except BaseException as exc:
            self._fail(exc)

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        from pydantic import BaseModel, ConfigDict

        # 低层 RPC 保留实验性 dynamicTools、environments 等字段。
        class _Reply(BaseModel):
            model_config = ConfigDict(extra="allow")

        return self._sdk.request(method, params, response_model=_Reply).model_dump(by_alias=True)

    def _connect(self) -> None:
        with self._connect_lock:
            if self.thread_id is not None or self._closed.is_set():
                return
            from openai_codex.client import CodexClient, CodexConfig

            codex_bin = _preferred_codex_bin()
            config = self._config()
            overrides = tuple(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in config.items())
            env = {"OPENAI_API_KEY": "", "CODEX_API_KEY": ""} if self.preset.provider == "chatgpt" else {}
            # 只锁住进程的创建和启动，关闭操作不等待初始化或网络请求。
            with self._lifecycle_lock:
                if self._closed.is_set():
                    return
                self._temp = TemporaryDirectory(prefix="alphasolve-codex-")
                self._sdk = CodexClient(
                    CodexConfig(codex_bin=codex_bin, cwd=self._temp.name,
                                env=env, config_overrides=overrides,
                                client_name="alphasolve", client_title="AlphaSolve"),
                    approval_handler=self._handle_request,
                )
                # 固定 SDK 0.147.0 的私有挂载点：在唯一读线程中按原始顺序入队。
                # 否则独立的通知泵可能晚于工具回调，导致 curator 收到乱序 trace。
                self._sdk._router.route_notification = self._forward_notification
                self._sdk.start()
            self._sdk.initialize()
            threading.Thread(target=self._watch_transport_failure, daemon=True).start()

            effective = self._request("config/read", {"includeLayers": False})
            thread_config: dict[str, Any] = {}
            inherited_mcp = effective.get("config", {}).get("mcp_servers") or {}
            if inherited_mcp:
                thread_config["mcp_servers"] = {name: {"enabled": False} for name in inherited_mcp}

            model = self.preset.model
            if self.preset.provider == "chatgpt":
                account = self._request("account/read", {"refreshToken": False}).get("account")
                if not account or account.get("type") not in {"chatgpt", "chatgptAuthTokens"}:
                    raise SessionFailure("请先运行 codex login 并使用 ChatGPT 账号登录。", "auth")
                if model is None:
                    models = self._request("model/list", {"includeHidden": False}).get("data", [])
                    default = next((item for item in models if item.get("isDefault")), None)
                    chosen = default or next(iter(models), None)
                    if not chosen:
                        raise SessionFailure("Codex 未返回可用的 ChatGPT 模型，请检查登录或在 preset 中指定 model。", "configuration")
                    model = chosen["model"]

            params: dict[str, Any] = {
                "cwd": self._temp.name,
                "baseInstructions": self.system_prompt,
                "developerInstructions": "Use only the tools supplied by AlphaSolve for this role. Follow its task and tool permissions.",
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": True,
                "environments": [],
                "selectedCapabilityRoots": [],
                "dynamicTools": [
                    {"name": tool.name, "description": tool.description, "inputSchema": tool.parameters}
                    for tool in self.tools
                ],
                "config": thread_config,
            }
            if model is not None:
                params["model"] = model
            started = self._request("thread/start", params)
            self.thread_id = started["thread"]["id"]

    def _config(self) -> dict[str, Any]:
        config: dict[str, Any] = {
            "web_search": "disabled",
            "agents.enabled": False,
            "tools.update_plan.enabled": False,
            "tools.experimental_request_user_input.enabled": False,
            "skills.include_instructions": False,
            "skills.bundled.enabled": False,
            "orchestrator.skills.enabled": False,
            "orchestrator.mcp.enabled": False,
            "project_doc_max_bytes": 0,
            "include_environment_context": False,
        }
        for feature in (
            "shell_tool", "unified_exec", "multi_agent", "multi_agent_v2", "apps", "plugins", "hooks",
            "web_search", "web_search_request", "web_search_cached", "tool_search", "tool_suggest",
            "code_mode", "js_repl", "js_repl_tools_only", "image_generation", "artifact_tools", "memory_tool",
            "deferred_executor", "goals", "token_budget", "current_time_reminder",
            "default_mode_request_user_input", "request_permissions_tool", "standalone_web_search", "search_tool",
            "multi_agent_mode", "plugin_hooks", "remote_plugin", "recommended_plugins", "code_mode_only",
            "computer_use", "browser_use", "imagegenext", "view_image", "memories", "skill_search",
            "skill_mcp_dependency_install", "workspace_dependencies",
        ):
            config[f"features.{feature}"] = False
        if self.preset.reasoning_effort:
            config["model_reasoning_effort"] = self.preset.reasoning_effort
        if self.preset.provider == "chatgpt":
            config["model_provider"] = "openai"
            config["forced_login_method"] = "chatgpt"
        else:
            if not self.preset.api_key_env or not os.getenv(self.preset.api_key_env):
                raise SessionFailure(f"未设置 preset {self.preset.name!r} 的环境变量 {self.preset.api_key_env!r}。", "auth")
            config["model_provider"] = "alphasolve_provider"
            prefix = "model_providers.alphasolve_provider"
            config.update({
                f"{prefix}.name": self.preset.provider,
                f"{prefix}.base_url": self.preset.base_url,
                f"{prefix}.env_key": self.preset.api_key_env,
                f"{prefix}.wire_api": "responses",
                f"{prefix}.requires_openai_auth": False,
            })
        return config

    def _handle_request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        if method != "item/tool/call":
            error = SessionFailure(f"Codex 请求了角色工具之外的交互：{method}", "configuration")
            self._fail(error)
            raise error
        pending: Future[dict[str, Any]] = Future()
        with self._pending_lock:
            if self._closed.is_set():
                raise SessionFailure("Codex 会话已关闭。")
            self._pending.add(pending)
        self.events.put(ToolRequest(params=params or {}, response=pending))
        try:
            return pending.result()
        finally:
            with self._pending_lock:
                self._pending.discard(pending)

    def _watch_transport_failure(self) -> None:
        try:
            # 正常通知直接走读线程；这里仅等待 SDK router.fail_all 唤醒关闭或异常。
            self._sdk.next_notification()
        except BaseException as exc:
            self._fail(exc)

    def _forward_notification(self, notification: Any) -> None:
        payload = notification.payload
        if hasattr(payload, "model_dump"):
            data = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
        else:
            data = payload.params
        self.events.put((notification.method, data))

    def _fail(self, error: BaseException) -> None:
        if not self._closed.is_set():
            self.events.put(("_failure", {"error": error}))

    def close(self) -> None:
        self._closed.set()
        with self._pending_lock:
            for pending in tuple(self._pending):
                pending.cancel()
        with self._lifecycle_lock:
            if self._sdk is not None:
                self._sdk.close()
            if self._temp is not None:
                self._temp.cleanup()
                self._temp = None
