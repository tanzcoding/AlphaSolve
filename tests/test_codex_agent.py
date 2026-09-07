"""Codex 事件适配层的会话、权限与停止行为。"""
from concurrent.futures import Future
from queue import Queue
import threading

import pytest

from alphasolve.agent import Agent, AgentConfig, AgentRunError, ToolRegistry, ToolResult
from alphasolve.agent.codex_session import ToolRequest
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
    from alphasolve.agent.codex_session import CodexSession

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
