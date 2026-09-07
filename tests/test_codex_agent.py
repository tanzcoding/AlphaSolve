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


@pytest.mark.parametrize('role', ['orchestrator', 'worker', 'curator'])
def test_native_items_reach_dashboard_and_logs_before_tool_returns(monkeypatch, tmp_path, role):
    import io

    from rich.console import Console

    from alphasolve.solver.logging.event_log import EventLogWriter, compose_event_sinks
    from alphasolve.solver.logging.run_log import RunLogWriter
    from alphasolve.solver.ui.dashboard import (
        make_curator_event_sink, make_orchestrator_event_sink, make_worker_event_sink,
    )
    from alphasolve.solver.ui.team_renderer import PropositionTeamRenderer

    renderer = PropositionTeamRenderer(console=Console(file=io.StringIO(), force_terminal=False))
    if role == 'worker':
        renderer.register_worker('worker')
        ui_sink = make_worker_event_sink(renderer, worker_id='worker', role='generator')
        state = renderer._workers['worker']
    elif role == 'curator':
        ui_sink = make_curator_event_sink(renderer)
        state = renderer._curator
    else:
        ui_sink = make_orchestrator_event_sink(renderer)
        state = renderer._orchestrator
    trace_path = tmp_path / 'agent.log'
    run_path = tmp_path / 'run.log'
    events = []
    snapshots = []

    def inspect_stream(event):
        if event['type'] == 'thinking_delta':
            snapshots.append(state.thinking_text)
        events.append(event)

    def inspect_tool(_):
        # 工具执行前就应能看到已完成的说明和推理，不能等整个 turn 返回。
        assert [item.text for item in state.timeline if item.type.name == 'CONTENT'] == ['checking now']
        assert state.active_tool == 'Echo'
        detail = trace_path.read_text(encoding='utf-8')
        assert 'first step\n    boundary' in detail
        assert 'checking now' in detail
        assert '[tool] Echo' in detail
        return ToolResult('checked')

    request = ToolRequest({'tool': 'Echo', 'callId': 'echo', 'arguments': {}}, Future())
    registry = ToolRegistry()
    registry.register(name='Echo', description='Echo', parameters={'type': 'object'}, handler=inspect_tool)
    final_item = ('item/completed', {'item': {
        'type': 'agentMessage', 'id': 'm2', 'text': 'all done', 'phase': 'final_answer',
    }})
    session = Session(lambda _: [
        ('item/reasoning/summaryTextDelta', {'itemId': 'r1', 'summaryIndex': 0, 'delta': 'first'}),
        ('item/reasoning/summaryTextDelta', {'itemId': 'r1', 'summaryIndex': 0, 'delta': ' step'}),
        ('item/reasoning/summaryTextDelta', {'itemId': 'r1', 'summaryIndex': 1, 'delta': 'boundary'}),
        ('item/completed', {'item': {'type': 'reasoning', 'id': 'r1', 'summary': ['first step', 'boundary']}}),
        ('item/agentMessage/delta', {'itemId': 'm1', 'delta': 'checking'}),
        ('item/completed', {'item': {'type': 'agentMessage', 'id': 'm1', 'text': 'checking now', 'phase': 'commentary'}}),
        request,
        ('item/completed', {'item': {'type': 'reasoning', 'id': 'r2', 'summary': [{'text': 'second step'}]}}),
        final_item,
        final_item,
        ('thread/tokenUsage/updated', {'tokenUsage': {'total': {'inputTokens': 100, 'outputTokens': 20, 'cachedInputTokens': 5}}}),
        ('turn/completed', {'turn': {'status': 'completed'}}),
    ])
    run_log = RunLogWriter(run_path, flush_interval=3600)
    try:
        with EventLogWriter(trace_path, scope=role) as log:
            sink = compose_event_sinks(ui_sink, log, run_log.sink_for(role), inspect_stream)
            with build(monkeypatch, session, registry=registry, tools=('Echo',), sink=sink) as agent:
                result = agent.run('inspect events')
    finally:
        run_log.close()

    assert result.final_answer == 'all done'
    assert snapshots == ['first', 'first step', 'first step\nboundary', 'second step']
    assert [item.text for item in state.timeline if item.type.name == 'CONTENT'] == ['checking now', 'all done']
    assert len([item for item in state.timeline if item.type.name == 'THOUGHT']) == 2
    assert request.response.result()['success'] is True
    assert [event['content'] for event in result.trace if event['type'] == 'assistant_message'] == ['checking now', 'all done']
    assert [event['content'] for event in result.trace if event['type'] == 'thinking'] == ['first step\nboundary', 'second step']
    assert all(event['agent'] == 'role' for event in events)
    assert all(event['caller_context']['parent_agent_id'] == 'parent' for event in events)
    run_text = run_path.read_text(encoding='utf-8')
    assert run_text.count('AGENT TURN │') == 1
    for content in ('first step', 'boundary', 'second step', 'checking now', 'all done'):
        assert run_text.count(content) == 1
    assert 'in=100 out=20 cached=5' in run_text


def test_native_streams_keep_separate_items_and_turns(monkeypatch):
    events = []
    session = Session(lambda n: [
        ('item/agentMessage/delta', {'itemId': 'm1', 'delta': f'first {n}'}),
        ('item/agentMessage/delta', {'itemId': 'm2', 'delta': f'second {n}'}),
        ('item/completed', {'item': {'type': 'agentMessage', 'id': 'm1', 'text': f'first {n}', 'phase': 'commentary'}}),
        ('item/completed', {'item': {'type': 'agentMessage', 'id': 'm2', 'text': f'second {n}', 'phase': 'final_answer'}}),
        ('turn/completed', {'turn': {'status': 'completed'}}),
    ])
    with build(monkeypatch, session, sink=events.append) as agent:
        assert agent.run('first').final_answer == 'second 1'
        assert agent.run('second').final_answer == 'second 2'
    deltas = [event for event in events if event['type'] == 'assistant_delta']
    assert [event['content'] for event in deltas] == ['first 1', 'second 1', 'first 2', 'second 2']
    assert [event['delta'] for event in deltas] == ['first 1', 'second 1', 'first 2', 'second 2']
    assert [event['turn'] for event in deltas] == [1, 1, 2, 2]


@pytest.mark.parametrize('finish', ['stopped', 'failed'])
def test_partial_native_streams_survive_interruption(monkeypatch, tmp_path, finish):
    from alphasolve.solver.logging.run_log import RunLogWriter

    session = Session(lambda _: [
        ('item/reasoning/summaryTextDelta', {'itemId': 'r1', 'summaryIndex': 0, 'delta': 'partial thought'}),
        ('item/agentMessage/delta', {'itemId': 'm1', 'delta': 'partial answer'}),
        ('turn/completed', {'turn': {'status': 'interrupted' if finish == 'stopped' else 'failed',
                                   'error': {'message': 'network down'} if finish == 'failed' else None}}),
    ])
    path = tmp_path / 'run.log'
    writer = RunLogWriter(path, flush_interval=3600)
    try:
        with build(monkeypatch, session, sink=writer.sink_for('worker')) as agent:
            if finish == 'failed':
                with pytest.raises(AgentRunError) as caught:
                    agent.run('test partial')
                trace = caught.value.trace
            else:
                trace = agent.run('test partial').trace
    finally:
        writer.close()
    partials = [event for event in trace if event.get('partial')]
    assert [event['content'] for event in partials] == ['partial thought', 'partial answer']
    assert trace[-1]['type'] == ('run_stopped' if finish == 'stopped' else 'run_error')
    text = path.read_text(encoding='utf-8')
    assert text.count('partial thought') == 1
    assert text.count('partial answer') == 1
    assert text.count('AGENT TURN │') == 1


@pytest.mark.parametrize('role', ['orchestrator', 'worker', 'curator'])
@pytest.mark.parametrize('snapshot', ['corrected answer', ''])
def test_completed_snapshot_preserves_other_items_and_empty_streams(monkeypatch, role, snapshot):
    import io

    from rich.console import Console

    from alphasolve.solver.logging.event_log import compose_event_sinks
    from alphasolve.solver.ui.dashboard import (
        make_curator_event_sink, make_orchestrator_event_sink, make_worker_event_sink,
    )
    from alphasolve.solver.ui.team_renderer import PropositionTeamRenderer

    renderer = PropositionTeamRenderer(console=Console(file=io.StringIO(), force_terminal=False))
    if role == 'worker':
        renderer.register_worker('worker')
        sink = make_worker_event_sink(renderer, worker_id='worker', role='generator')
        state = renderer._workers['worker']
    elif role == 'curator':
        sink = make_curator_event_sink(renderer)
        state = renderer._curator
    else:
        sink = make_orchestrator_event_sink(renderer)
        state = renderer._orchestrator
    renderer.update_orchestrator_tool_start(module='parent', name='Agent', arg_preview='waiting')
    snapshots = []

    def observe(event):
        if event['type'] == 'assistant_message' and event.get('item_id') == 'current':
            snapshots.append((state.output_buffer, state.thinking_text, state.active_tool,
                              [item.text for item in state.timeline if item.type.name == 'CONTENT']))

    session = Session(lambda _: [
        ('item/completed', {'item': {'type': 'agentMessage', 'id': 'previous', 'text': 'earlier', 'phase': 'commentary'}}),
        ('item/reasoning/summaryTextDelta', {'itemId': 'r1', 'summaryIndex': 0, 'delta': 'kept thought'}),
        ('item/completed', {'item': {'type': 'reasoning', 'id': 'r1', 'summary': []}}),
        ('item/reasoning/summaryTextDelta', {'itemId': 'r2', 'summaryIndex': 0, 'delta': 'still thinking'}),
        ('item/agentMessage/delta', {'itemId': 'current', 'delta': 'old answer'}),
        ('item/agentMessage/delta', {'itemId': 'neighbor', 'delta': 'neighbor'}),
        ('item/completed', {'item': {'type': 'agentMessage', 'id': 'current', 'text': snapshot, 'phase': 'commentary'}}),
        ('item/completed', {'item': {'type': 'agentMessage', 'id': 'neighbor', 'text': '', 'phase': 'final_answer'}}),
        ('item/completed', {'item': {'type': 'reasoning', 'id': 'r2', 'summary': []}}),
        ('turn/completed', {'turn': {'status': 'completed'}}),
    ])
    with build(monkeypatch, session, sink=compose_event_sinks(sink, observe)) as agent:
        result = agent.run('test snapshots')

    answer = snapshot or 'old answer'
    assert len(snapshots) == 1
    buffer, thought, active_tool, timeline = snapshots[0]
    assert buffer == 'neighbor'
    assert thought == 'still thinking'
    assert timeline == ['earlier', answer]
    if role == 'orchestrator':
        assert active_tool == 'Agent'
    else:
        assert renderer._orchestrator.active_tool == 'Agent'
        assert renderer._orchestrator.status == 'tool'
    assert [item.text for item in state.timeline if item.type.name == 'CONTENT'] == ['earlier', answer, 'neighbor']
    assert state.output_buffer == ''
    assert state.thinking_text == ''
    assert [event['content'] for event in result.trace if event['type'] == 'thinking'] == ['kept thought', 'still thinking']
    assert [event['content'] for event in result.trace if event['type'] == 'codex_message'] == ['earlier', answer, 'neighbor']
    assert [message.content for message in result.messages if message.role == 'assistant'] == ['earlier', answer, 'neighbor']
    assert result.final_answer == 'neighbor'


@pytest.mark.parametrize('completed_text', ['partial answer', 'corrected answer'])
def test_anonymous_deltas_match_named_completion_without_repeating(monkeypatch, completed_text):
    import io

    from rich.console import Console

    from alphasolve.solver.ui.dashboard import make_worker_event_sink
    from alphasolve.solver.ui.team_renderer import PropositionTeamRenderer

    renderer = PropositionTeamRenderer(console=Console(file=io.StringIO(), force_terminal=False))
    sink = make_worker_event_sink(renderer, worker_id='worker', role='generator')
    session = Session(lambda _: [
        ('item/agentMessage/delta', {'delta': 'partial'}),
        ('item/completed', {'item': {'type': 'agentMessage', 'id': 'named', 'text': completed_text, 'phase': 'final_answer'}}),
        ('turn/completed', {'turn': {'status': 'completed'}}),
    ])
    with build(monkeypatch, session, sink=sink) as agent:
        result = agent.run('anonymous source')
    state = renderer._workers['worker']
    assert [item.text for item in state.timeline if item.type.name == 'CONTENT'] == [completed_text]
    assert state.output_buffer == ''
    assert result.final_answer == completed_text
