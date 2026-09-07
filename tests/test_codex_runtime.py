"""不访问外网：用真实 SDK 和本地 Responses 服务验证工具白名单与消息往返。"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from queue import Queue
import threading
from types import SimpleNamespace

import pytest

from alphasolve.agent import Agent, AgentConfig, ToolRegistry, ToolResult
from alphasolve.llm import CodexClient, Preset


def test_bundled_codex_roundtrip_exposes_only_role_tools(tmp_path, monkeypatch):
    requests = []
    executed = []
    visible_events = []
    owner = threading.get_ident()
    tool_executing = threading.Event()

    from openai_codex.client import CodexClient as SdkClient
    next_turn_notification = SdkClient.next_turn_notification

    def delayed_notification(client, turn_id):
        notification = next_turn_notification(client, turn_id)
        # 故意让旧通知泵落后于工具执行，避免顺序回归依赖线程调度运气。
        if notification.method == 'item/completed':
            tool_executing.wait(timeout=1)
        return notification

    monkeypatch.setattr(SdkClient, 'next_turn_notification', delayed_notification)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            number = len(requests)
            response_id = f'resp_{number}'
            if number == 1:
                item = {'type': 'function_call', 'id': 'fc_test', 'call_id': 'call_test',
                        'name': 'Echo', 'arguments': '{"text":"原样工具结果"}', 'status': 'completed'}
            else:
                item = {'type': 'message', 'id': f'msg_{number}', 'role': 'assistant',
                        'status': 'completed', 'phase': 'final_answer',
                        'content': [{'type': 'output_text', 'text': f'done {number}', 'annotations': []}]}
            items = [item]
            if number == 1:
                # 模型先解释、再调工具；实际执行工具前，说明必须已交给事件订阅者。
                items.insert(0, {'type': 'message', 'id': 'msg_commentary', 'role': 'assistant',
                                 'status': 'completed', 'phase': 'commentary',
                                 'content': [{'type': 'output_text', 'text': 'I will check with Echo.', 'annotations': []}]})
            response = {'id': response_id, 'object': 'response', 'created_at': 1,
                        'status': 'completed', 'model': 'gpt-5.5', 'output': items,
                        'usage': {'input_tokens': 10, 'output_tokens': 4, 'total_tokens': 14,
                                  'input_tokens_details': {'cached_tokens': 2}}}
            frames = [{'type': 'response.created', 'response': {**response, 'output': [], 'status': 'in_progress'}}]
            for index, response_item in enumerate(items):
                frames.extend([
                    {'type': 'response.output_item.added', 'output_index': index, 'item': response_item},
                    {'type': 'response.output_item.done', 'output_index': index, 'item': response_item},
                ])
            frames.append({'type': 'response.completed', 'response': response})
            encoded = ''.join(f'event: {frame["type"]}\ndata: {json.dumps(frame, ensure_ascii=False)}\n\n' for frame in frames).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    home = tmp_path / 'codex-home'
    home.mkdir()
    # 故意配置额外工具与个人指令，验证角色配置能覆盖继承值。
    (home / 'config.toml').write_text(
        'developer_instructions = "PERSONAL_INSTRUCTIONS_MUST_NOT_APPEAR"\n'
        '[features]\nshell_tool = true\nmulti_agent = true\n'
        'current_time_reminder = true\ndeferred_executor = true\n'
        '[mcp_servers.unwanted]\ncommand = "must-not-start-this-program"\n', encoding='utf-8')
    monkeypatch.setenv('CODEX_HOME', str(home))
    monkeypatch.setenv('ALPHASOLVE_TEST_TOKEN', 'local-test-only')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    registry = ToolRegistry()

    def echo(args):
        tool_executing.set()
        assert any(event['type'] == 'codex_message' and event['content'] == 'I will check with Echo.'
                   for event in visible_events), visible_events
        executed.append((threading.get_ident(), args))
        return ToolResult(args['text'])

    registry.register(name='Echo', description='Echo the supplied text unchanged.',
                      parameters={'type': 'object', 'properties': {'text': {'type': 'string'}},
                                  'required': ['text'], 'additionalProperties': False}, handler=echo)
    registry.register(name='Forbidden', description='Never expose this tool.',
                      parameters={'type': 'object', 'properties': {}}, handler=lambda _: ToolResult('bad'))
    agent = Agent(config=AgentConfig(name='test', system_prompt='Call Echo, then answer.', tools=('Echo',)),
                  client=CodexClient(Preset(name='local', provider='test', model='gpt-5.5',
                                           base_url=f'http://127.0.0.1:{server.server_port}/v1',
                                           api_key_env='ALPHASOLVE_TEST_TOKEN')),
                  tool_registry=registry, event_sink=visible_events.append)
    # 出错时及时结束测试，避免 SDK 的网络重试使测试永久等待。
    timer = threading.Timer(40, lambda: stop.set())
    stop = threading.Event()
    agent.stop_event = stop
    timer.start()
    try:
        first = agent.run('Use Echo once.')
        assert first.final_answer == 'done 2', first.trace
        thread_id = agent._session.thread_id
        second = agent.run('Continue without another tool.')
        assert second.final_answer == 'done 3', second.trace
        assert agent._session.thread_id == thread_id
    finally:
        timer.cancel()
        agent.close()
        server.shutdown()
        server.server_close()
    assert executed == [(owner, {'text': '原样工具结果'})]
    assert len(requests) == 3
    for request in requests:
        assert [tool['name'] for tool in request['tools']] == ['Echo'], request['tools']
        assert 'PERSONAL_INSTRUCTIONS_MUST_NOT_APPEAR' not in json.dumps(request)
    outputs = [item for item in requests[1]['input'] if item['type'] == 'function_call_output']
    assert '原样工具结果' in json.dumps(outputs, ensure_ascii=False)
    results = [event for event in first.trace if event['type'] == 'tool_result']
    json.dumps(first.trace, ensure_ascii=False)
    assert results[0]['content'] == '原样工具结果'
    usages = [event for event in second.trace if event['type'] == 'usage']
    assert usages[0]['input_tokens'] == 10
    assert usages[0]['output_tokens'] == 4


def test_subscription_rejects_api_key_login_before_starting_model_turn(monkeypatch):
    from alphasolve.agent import AgentRunError

    calls = []
    closed = threading.Event()

    class ApiKeySdk:
        def __init__(self, config, approval_handler):
            self.notifications = Queue()
            self._router = SimpleNamespace(route_notification=None)

        def start(self):
            pass

        def initialize(self):
            pass

        def request(self, method, params, *, response_model):
            calls.append(method)
            if method == 'config/read':
                return response_model.model_validate({'config': {}})
            if method == 'account/read':
                return response_model.model_validate({'account': {'type': 'apiKey'}})
            raise AssertionError(f'Unexpected request after API-key login: {method}')

        def turn_start(self, *args):
            calls.append('turn/start')
            raise AssertionError('Subscription preset must not make a paid API request')

        def next_notification(self):
            self.notifications.get()
            raise RuntimeError('closed')

        def close(self):
            closed.set()
            self.notifications.put(None)

    monkeypatch.setattr('openai_codex.client.CodexClient', ApiKeySdk)
    agent = Agent(config=AgentConfig(name='subscription', system_prompt='Answer the task.'),
                  client=CodexClient(Preset(name='subscription')), tool_registry=ToolRegistry())
    with pytest.raises(AgentRunError) as caught:
        agent.run('Do not fall back to the API.')
    assert caught.value.failure_kind == 'auth'
    assert calls == ['config/read', 'account/read']
    assert closed.is_set()
