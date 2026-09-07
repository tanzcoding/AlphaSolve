"""Codex 事件适配层的会话、权限与停止行为。"""
from concurrent.futures import Future
import os
from queue import Queue
import stat
import threading
from types import SimpleNamespace

import pytest

import alphasolve.agent.codex_session as codex_session_module
from alphasolve.agent import Agent, AgentConfig, AgentRunError, ToolRegistry, ToolResult
from alphasolve.agent.codex_session import CodexSession, SessionFailure, ToolRequest
from alphasolve.llm import CodexClient, Preset


class Session:
    def __init__(self, script):
        self.events = Queue()
        self.script = script
        self.tasks = []
        self.closed = False
        self.thread_id = 'native-session'

    def start_turn(self, task):
        self.tasks.append(task)
        for event in self.script(len(self.tasks)):
            self.events.put(event)

    def close(self):
        self.closed = True


def completed(text='done', status='completed', error=None):
    return [('item/completed', {'item': {'type': 'agentMessage', 'text': text, 'phase': 'final_answer'}}),
            ('turn/completed', {'turn': {'status': status, 'error': error}})]


def test_session_forwards_preset_reasoning_effort_to_codex_config():
    session = CodexSession(Preset('sol', model='gpt-5.6-sol', reasoning_effort='max'), 'test', [])
    assert session._config()['model_reasoning_effort'] == 'max'


def make_executable(path):
    path.touch()
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def codex_executable_name():
    return 'codex.exe' if os.name == 'nt' else 'codex'


def test_prefers_explicit_codex_binary(tmp_path, monkeypatch):
    executable = make_executable(tmp_path / 'custom-codex')
    monkeypatch.setenv('ALPHASOLVE_CODEX_BIN', str(executable))
    monkeypatch.setattr(codex_session_module, '_codex_on_path', lambda: None)
    monkeypatch.setattr(codex_session_module, '_external_codex_version', lambda _path: (0, 153, 4))
    assert codex_session_module._preferred_codex_bin() == str(executable.resolve())


def test_discovers_codex_on_path_and_allows_bundled_fallback(tmp_path, monkeypatch):
    executable = make_executable(tmp_path / 'codex')
    monkeypatch.delenv('ALPHASOLVE_CODEX_BIN', raising=False)
    monkeypatch.setattr(codex_session_module, '_codex_on_path', lambda: executable)
    monkeypatch.setattr(codex_session_module, '_external_codex_version', lambda _path: (0, 153, 4))
    assert codex_session_module._preferred_codex_bin() == str(executable.resolve())
    monkeypatch.setattr(codex_session_module, '_external_codex_version', lambda _path: (0, 153, 3))
    assert codex_session_module._preferred_codex_bin() is None
    monkeypatch.setattr(codex_session_module, '_codex_on_path', lambda: None)
    assert codex_session_module._preferred_codex_bin() is None


def test_path_lookup_skips_current_and_relative_directories(tmp_path, monkeypatch):
    current = tmp_path / 'problem'
    safe_bin = tmp_path / 'safe-bin'
    current.mkdir()
    safe_bin.mkdir()
    make_executable(current / codex_executable_name())
    expected = make_executable(safe_bin / codex_executable_name())
    monkeypatch.chdir(current)
    monkeypatch.setenv('PATH', os.pathsep.join(['', '.', str(current), str(safe_bin)]))
    assert codex_session_module._codex_on_path() == expected.resolve()


def test_path_lookup_does_not_implicitly_execute_codex_from_cwd(tmp_path, monkeypatch):
    make_executable(tmp_path / codex_executable_name())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('PATH', '')
    assert codex_session_module._codex_on_path() is None


def test_rejects_missing_explicit_codex_binary(tmp_path, monkeypatch):
    monkeypatch.setenv('ALPHASOLVE_CODEX_BIN', str(tmp_path / 'missing-codex'))
    with pytest.raises(SessionFailure) as caught:
        codex_session_module._preferred_codex_bin()
    assert caught.value.failure_kind == 'configuration'


def test_rejects_old_explicit_codex_binary(tmp_path, monkeypatch):
    executable = make_executable(tmp_path / 'old-codex')
    monkeypatch.setenv('ALPHASOLVE_CODEX_BIN', str(executable))
    monkeypatch.setattr(codex_session_module, '_external_codex_version', lambda _path: (0, 153, 3))
    with pytest.raises(SessionFailure, match='0.153.4') as caught:
        codex_session_module._preferred_codex_bin()
    assert caught.value.failure_kind == 'configuration'


def test_parses_external_codex_version(monkeypatch):
    completed = SimpleNamespace(returncode=0, stdout='codex-cli 0.153.4\n')
    monkeypatch.setattr(codex_session_module.subprocess, 'run', lambda *args, **kwargs: completed)
    codex_session_module._external_codex_version.cache_clear()
    try:
        assert codex_session_module._external_codex_version('codex') == (0, 153, 4)
    finally:
        codex_session_module._external_codex_version.cache_clear()


@pytest.mark.parametrize('suffix', ['-alpha.1', '-rc.2', '-dev'])
def test_rejects_external_codex_prerelease(monkeypatch, suffix):
    completed = SimpleNamespace(returncode=0, stdout=f'codex-cli 0.153.4{suffix}\n')
    monkeypatch.setattr(codex_session_module.subprocess, 'run', lambda *args, **kwargs: completed)
    codex_session_module._external_codex_version.cache_clear()
    try:
        with pytest.raises(ValueError, match='预发布版本'):
            codex_session_module._external_codex_version('codex')
    finally:
        codex_session_module._external_codex_version.cache_clear()


def test_explicit_codex_path_resolution_error_is_configuration_failure(monkeypatch):
    monkeypatch.setenv('ALPHASOLVE_CODEX_BIN', '~invalid/codex')

    def fail_expanduser(_path):
        raise RuntimeError('unknown user')

    monkeypatch.setattr(codex_session_module.Path, 'expanduser', fail_expanduser)
    with pytest.raises(SessionFailure, match='ALPHASOLVE_CODEX_BIN') as caught:
        codex_session_module._preferred_codex_bin()
    assert caught.value.failure_kind == 'configuration'


def test_invalid_path_entry_allows_bundled_fallback(tmp_path, monkeypatch):
    broken = tmp_path / 'broken'
    original_resolve = codex_session_module.Path.resolve

    def fail_broken(path, *args, **kwargs):
        if path == broken:
            raise OSError('unreadable path')
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setenv('PATH', str(broken))
    monkeypatch.setattr(codex_session_module.Path, 'resolve', fail_broken)
    assert codex_session_module._codex_on_path() is None


def test_close_does_not_wait_for_codex_probe_or_start_sdk(monkeypatch):
    probe_started = threading.Event()
    release_probe = threading.Event()
    sdk_created = threading.Event()

    def blocking_probe():
        probe_started.set()
        assert release_probe.wait(5)
        return None

    class UnexpectedSdk:
        def __init__(self, *_args, **_kwargs):
            sdk_created.set()

    monkeypatch.setattr(codex_session_module, '_preferred_codex_bin', blocking_probe)
    monkeypatch.setattr('openai_codex.client.CodexClient', UnexpectedSdk)
    session = CodexSession(Preset('luna', model='gpt-5.6-luna'), 'test', [])
    connector = threading.Thread(target=session._connect)
    connector.start()
    assert probe_started.wait(1)

    closer = threading.Thread(target=session.close)
    closer.start()
    closer.join(1)
    try:
        assert not closer.is_alive()
        assert not sdk_created.is_set()
    finally:
        release_probe.set()
        connector.join(1)
    assert not connector.is_alive()
    assert not sdk_created.is_set()


def build(monkeypatch, session, *, registry=None, tools=(), stop=None, sink=None):
    monkeypatch.setattr(Agent, '_make_session', lambda self: session)
    return Agent(config=AgentConfig(name='role', system_prompt='role instructions', tools=tools),
                 client=CodexClient(Preset('subscription')), tool_registry=registry or ToolRegistry(),
                 stop_event=stop, event_sink=sink, caller_context={'parent_agent_id': 'parent'})


def test_reuses_native_session_and_counts_incremental_usage(monkeypatch):
    session = Session(lambda n: [
        ('thread/tokenUsage/updated', {'tokenUsage': {'total': {'inputTokens': 10*n, 'outputTokens': 4*n, 'cachedInputTokens': 2*n}}}),
        ('item/completed', {'item': {'type': 'reasoning', 'id': 'r', 'summary': ['visible summary']}}),
        ('item/completed', {'item': {'type': 'contextCompaction'}}),
        *completed(f'answer {n}')])
    events = []
    with build(monkeypatch, session, sink=events.append) as agent:
        first = agent.run('first')
        second = agent.run('second')
        assert not session.closed
    assert session.closed
    assert session.tasks == ['first', 'second']
    assert (first.final_answer, second.final_answer) == ('answer 1', 'answer 2')
    assert first.turns == second.turns == 1
    assert [event['input_tokens'] for event in events if event['type'] == 'usage'] == [10, 10]
    assert all(event['caller_context']['parent_agent_id'] == 'parent' for event in events)
    assert any(event['type'] == 'context_compacted' for event in second.trace)
    assert any(event['type'] == 'visible_reasoning' for event in second.trace)


@pytest.mark.parametrize('name,args', [('Denied', {}), ('Echo', '{bad'), ('Echo', ['not an object'])])
def test_denied_or_malformed_tools_return_error_without_execution(monkeypatch, name, args):
    request = ToolRequest({'tool': name, 'callId': 'call', 'arguments': args}, Future())
    session = Session(lambda _: [request, *completed()])
    calls = []
    registry = ToolRegistry()
    for tool in ('Echo', 'Denied'):
        registry.register(name=tool, description=tool, parameters={'type': 'object'},
                          handler=lambda args: calls.append(args) or ToolResult('bad'))
    with build(monkeypatch, session, registry=registry, tools=('Echo',)) as agent:
        result = agent.run('test')
    assert calls == []
    assert request.response.result()['success'] is False
    assert any(event.get('is_error') for event in result.trace if event['type'] == 'tool_result')


def test_tool_requested_stop_preserves_result_and_closes(monkeypatch):
    request = ToolRequest({'tool': 'Finish', 'callId': 'call', 'arguments': {}}, Future())
    session = Session(lambda _: [request])
    registry = ToolRegistry()
    registry.register(name='Finish', description='Finish', parameters={'type': 'object'},
                      handler=lambda _: ToolResult('saved artifact', stop_agent=True, stop_answer='complete'))
    agent = build(monkeypatch, session, registry=registry, tools=('Finish',))
    result = agent.run('finish')
    assert session.closed
    assert result.final_answer == 'complete'
    assert request.response.result()['contentItems'][0]['text'] == 'saved artifact'
    assert result.trace[-1]['reason'] == 'tool_requested_stop'


@pytest.mark.parametrize('error,kind', [
    ({'codexErrorInfo': 'usageLimitExceeded', 'message': 'exhausted'}, 'quota'),
    ({'message': '401 unauthorized'}, 'auth'),
    ({'message': 'invalid model'}, 'configuration'),
    ({'message': 'network down'}, 'runtime'),
])
def test_native_failure_retains_trace_and_classification(monkeypatch, error, kind):
    session = Session(lambda _: completed('partial progress', 'failed', error))
    agent = build(monkeypatch, session)
    with pytest.raises(AgentRunError) as raised:
        agent.run('task')
    assert raised.value.failure_kind == kind
    assert raised.value.fatal == (kind != 'runtime')
    assert any(event.get('content') == 'partial progress' for event in raised.value.trace)
    assert session.closed


def test_retry_notification_does_not_end_turn(monkeypatch):
    session = Session(lambda _: [('error', {'error': {'message': 'transient'}, 'willRetry': True}), *completed()])
    with build(monkeypatch, session) as agent:
        assert agent.run('retry').final_answer == 'done'


def test_stop_before_start_does_not_connect(monkeypatch):
    session = Session(lambda _: [])
    stop = threading.Event()
    stop.set()
    agent = build(monkeypatch, session, stop=stop)
    result = agent.run('stop')
    assert not session.tasks
    assert result.turns == 0
    assert result.trace[-1]['type'] == 'run_stopped'


def test_stop_arriving_with_tool_request_prevents_side_effect(monkeypatch):
    stop = threading.Event()
    request = ToolRequest({'tool': 'Echo', 'callId': 'call', 'arguments': {}}, Future())
    def script(_):
        stop.set()
        return [request]
    session = Session(script)
    calls = []
    registry = ToolRegistry()
    registry.register(name='Echo', description='Echo', parameters={'type': 'object'},
                      handler=lambda args: calls.append(args) or ToolResult('bad'))
    agent = build(monkeypatch, session, stop=stop, registry=registry, tools=('Echo',))
    assert agent.run('stop').final_answer == ''
    assert not calls
    assert session.closed

def test_native_typed_failure_is_not_mistaken_for_success(monkeypatch):
    from openai_codex.generated.v2_all import TurnCompletedNotification
    from openai_codex.models import Notification

    # SDK 的 status 是 Enum；转为 JSON 值后再判断，才能正确传播真实失败。
    payload = TurnCompletedNotification.model_validate({
        'threadId': 'thread',
        'turn': {'id': 'turn', 'status': 'failed', 'items': [], 'itemsView': 'full',
                 'error': {'message': 'usage limit reached', 'codexErrorInfo': 'usageLimitExceeded'}},
    })
    session = CodexSession(Preset('subscription'), 'test', [])
    session.start_turn = lambda _: session._forward_notification(Notification('turn/completed', payload))
    agent = build(monkeypatch, session)
    with pytest.raises(AgentRunError) as raised:
        agent.run('failed task')
    assert raised.value.failure_kind == 'quota'
    assert raised.value.fatal
    assert raised.value.trace[-1]['type'] == 'run_error'
