"""Small in-memory stand-ins for the Supabase async client.

These are deliberately hand-written fakes, not mocks of private SDK internals:
they implement only the surface nyoanime.party uses. Real network behaviour is
covered by the optional remote tests in test_supabase_remote.py.
"""
import asyncio
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional


class FakeSession:
    def __init__(
        self,
        user_id: str,
        access_token: str = "test-access-token",
        expires_at: Optional[int] = None,
    ):
        self.user = SimpleNamespace(id=user_id)
        self.access_token = access_token
        self.refresh_token = "test-refresh-token"
        self.expires_at = expires_at


class FakeAuth:
    def __init__(self, session: Optional[FakeSession] = None, user_id: str = "user-1"):
        self.session = session
        self.user_id = user_id
        self.sign_in_calls = 0
        self.sign_out_calls = 0
        self.refresh_calls = 0
        self.fail_refresh = False

    async def get_session(self):
        return self.session

    async def sign_in_anonymously(self):
        self.sign_in_calls += 1
        self.session = FakeSession(self.user_id)
        return SimpleNamespace(user=self.session.user, session=self.session)

    async def refresh_session(self, refresh_token=None):
        self.refresh_calls += 1
        if self.fail_refresh or self.session is None:
            raise RuntimeError("invalid refresh token")
        self.session.access_token = "rotated-access-token"
        return SimpleNamespace(user=self.session.user, session=self.session)

    async def sign_out(self, options=None):
        self.sign_out_calls += 1
        self.session = None


class FakeRealtime:
    def __init__(self):
        self.token: Optional[str] = None
        self.tokens: List[str] = []
        self.closed = False

    async def set_auth(self, token: str) -> None:
        self.token = token
        self.tokens.append(token)


class FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]]):
        self.rows = rows

    def select(self, _columns: str):
        return self

    def eq(self, field: str, value: Any):
        self.rows = [row for row in self.rows if str(row.get(field)) == str(value)]
        return self

    def limit(self, _count: int):
        self.rows = self.rows[:_count]
        return self

    async def execute(self):
        return SimpleNamespace(data=[dict(row) for row in self.rows])


class FakeRPCQuery:
    def __init__(self, fake: "FakeSupabase", name: str, args: Optional[Dict[str, Any]]):
        self.fake = fake
        self.name = name
        self.args = args or {}

    async def execute(self):
        self.fake.rpc_calls.append((self.name, dict(self.args)))
        response = self.fake.rpc_responses.get(self.name)
        if callable(response):
            response = response(self.args)
        if asyncio.iscoroutine(response):
            response = await response
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(data=response if response is not None else [])


class FakeFunctions:
    def __init__(self, responses: Optional[Dict[str, Any]] = None):
        self.responses = responses or {}
        self.calls: List[tuple] = []

    async def invoke(self, name: str, options: Dict[str, Any]):
        self.calls.append((name, options))
        response = self.responses.get(name)
        if callable(response):
            response = response(options)
        if asyncio.iscoroutine(response):
            response = await response
        if isinstance(response, Exception):
            raise response
        return response


class FakeChannel:
    def __init__(self, topic: str, params: Dict[str, Any], fail_message: Optional[str] = None):
        self.topic = topic
        self.params = params
        self.fail_message = fail_message
        self.is_joined = False
        self.broadcast_handlers: List[tuple] = []
        self.presence_callback: Optional[Callable[[], None]] = None
        self.sent: List[tuple] = []
        self.tracked: Optional[Dict[str, Any]] = None
        self.presence: Dict[str, Any] = {}
        self.unsubscribed = False
        self.subscribe_callback: Optional[Callable] = None

    def on_broadcast(self, event: str, callback: Callable):
        self.broadcast_handlers.append((event, callback))
        return self

    def on_presence_sync(self, callback: Callable):
        self.presence_callback = callback
        return self

    async def subscribe(self, callback=None):
        self.subscribe_callback = callback
        if self.fail_message is not None:
            self.is_joined = False
            if callback:
                callback(FakeSubscribeState.CHANNEL_ERROR, RuntimeError(self.fail_message))
            return self
        self.is_joined = True
        if callback:
            callback(FakeSubscribeState.SUBSCRIBED, None)
        return self

    def drop(self, message: str = "token has expired", state: str = "CHANNEL_ERROR"):
        """Simulate a channel failing after a successful join."""
        self.is_joined = False
        if self.subscribe_callback:
            self.subscribe_callback(state, RuntimeError(message))

    async def send_broadcast(self, event: str, data: Any):
        self.sent.append((event, data))

    async def track(self, data: Dict[str, Any]):
        self.tracked = dict(data)

    def presence_state(self):
        return self.presence

    async def unsubscribe(self):
        self.is_joined = False
        self.unsubscribed = True

    def receive(self, event: str, payload: Dict[str, Any]):
        """Deliver a simulated broadcast to the registered callbacks."""
        message = {"event": event, "payload": payload}
        for registered_event, callback in self.broadcast_handlers:
            if registered_event in ("*", event):
                callback(message)


class FakeSubscribeState:
    SUBSCRIBED = "SUBSCRIBED"
    CHANNEL_ERROR = "CHANNEL_ERROR"
    TIMED_OUT = "TIMED_OUT"
    CLOSED = "CLOSED"


class FakeSupabase:
    def __init__(
        self,
        session: Optional[FakeSession] = None,
        responses: Optional[Dict[str, Any]] = None,
        user_id: str = "user-1",
        rows: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        fail_topics: Optional[Dict[str, str]] = None,
        supabase_key: str = "test-publishable-key",
        rpc_responses: Optional[Dict[str, Any]] = None,
    ):
        self.auth = FakeAuth(session=session, user_id=user_id)
        self.realtime = FakeRealtime()
        self.functions = FakeFunctions(responses)
        self.rows = rows or {}
        self.fail_topics = fail_topics or {}
        self.supabase_key = supabase_key
        self.channels: List[FakeChannel] = []
        self.removed: List[FakeChannel] = []
        self.rpc_responses = rpc_responses or {}
        self.rpc_calls: List[tuple] = []

    def channel(self, topic: str, params: Dict[str, Any]):
        fail_message = None
        for fragment, message in self.fail_topics.items():
            if fragment in topic:
                fail_message = message
                break
        channel = FakeChannel(topic, params, fail_message=fail_message)
        self.channels.append(channel)
        return channel

    async def remove_channel(self, channel: FakeChannel):
        self.removed.append(channel)
        channel.is_joined = False

    def table(self, name: str):
        return FakeQuery(self.rows.get(name, []))

    def rpc(self, name: str, args: Optional[Dict[str, Any]] = None):
        return FakeRPCQuery(self, name, args)


class FakeFunctionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.message = {"code": code, "message": message}
