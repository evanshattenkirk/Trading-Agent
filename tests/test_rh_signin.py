"""Robinhood session robustness (review 2026-10-06, M1/M3/L10/M2): a browser sign-in can finish inside the connect
wait, a retry never binds the OAuth callback port twice, start() is safe after a dropped session, the token file is
written atomically at 0600, and a call cut off by close() fails fast without a retry."""
import asyncio
import logging
import os
import socket
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdesk.brokers import robinhood as R
from agentdesk.brokers.robinhood import FileTokenStorage, RobinhoodMCP, _Callback
from agentdesk.config import load_config

CFG = load_config()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Transport:
    def __init__(self, on_enter=None):
        self.on_enter = on_enter

    async def __aenter__(self):
        if self.on_enter:
            await self.on_enter()
        return ("r", "w", None)

    async def __aexit__(self, *a):
        return False


class Session:
    made: list["Session"] = []

    def __init__(self, behaviour="ok"):
        self.behaviour, self.calls = behaviour, 0
        self.gone = asyncio.Event()
        Session.made.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        self.gone.set()                    # like the SDK: closing the session breaks a call still in flight
        return False

    async def initialize(self):
        pass

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name="get_accounts", inputSchema={}),
                                      SimpleNamespace(name="get_option_quotes", inputSchema={})])

    async def call_tool(self, tool, args):
        self.calls += 1
        if self.behaviour == "dies_on_close":
            await self.gone.wait()
            raise anyio.ClosedResourceError()
        return SimpleNamespace(isError=False, structuredContent={"data": {"accounts": [], "n": len(Session.made)}},
                               content=[])


def client(tmp_path, transport=None, behaviour="ok") -> RobinhoodMCP:
    Session.made = []
    rh = RobinhoodMCP({**CFG, "robinhood": {**CFG["robinhood"], "call_budget": None, "token_dir": str(tmp_path),
                                            "mcp_url": "http://127.0.0.1:9/mcp", "redirect_port": free_port()}})
    rh.RECONNECT_GAP_S = 0
    rh._connect = (transport or (lambda: Transport()), lambda r, w: Session(behaviour))
    return rh


# ----------------------------------------------------------------------------- M1: browser sign-in
def test_open_keeps_waiting_while_a_browser_sign_in_is_under_way(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(R, "CONNECT_TIMEOUT_S", 0.2)
    seen = []
    rh = None

    async def sign_in():                    # the saved token was refused: the SDK opens the browser and waits
        rh._sign_in_started()
        await asyncio.sleep(0.6)            # Evan signs in after the normal connect window has passed

    rh = client(tmp_path, transport=lambda: Transport(sign_in))
    rh.on_sign_in = seen.append

    async def go():
        await rh._open()
        await rh.close()
    with caplog.at_level(logging.WARNING, logger="agentdesk.robinhood"):
        asyncio.run(go())
    lines = [r.getMessage() for r in caplog.records if "needs a sign-in" in r.getMessage()]
    assert len(lines) == 1 and "open the URL printed above (or in the log)" in lines[0]
    assert seen == lines                    # the engine puts the same line on the dashboard


def test_open_still_gives_up_when_no_sign_in_started(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "CONNECT_TIMEOUT_S", 0.1)

    async def hang():
        await asyncio.Event().wait()
    rh = client(tmp_path, transport=lambda: Transport(hang))

    async def go():
        with pytest.raises(TimeoutError):
            await rh._open()
        await asyncio.sleep(0.05)
        return rh._owner.done()
    assert asyncio.run(go())                # the abandoned attempt is stopped, not left holding the callback port


def test_callback_server_is_bound_once_per_port():
    port = free_port()

    async def go():
        a = _Callback(port)
        await a.start()
        await a.start()                     # the SDK's redirect handler runs again on a retry: same server
        b = _Callback(port)
        with pytest.raises(RuntimeError, match="already waiting"):
            await b.start()                 # a second client in this process can't take the port from a waiting one
        a.close()
        await b.start()                     # once the first gave up, the port is free again
        b.close()
    asyncio.run(go())


# ----------------------------------------------------------------------------- M3: idempotent start()
def test_start_after_a_drop_reconnects_once_through_the_call_lock(tmp_path):
    rh = client(tmp_path)

    async def go():
        await rh.start()
        assert len(Session.made) == 1
        await rh.start()                    # already connected: nothing happens
        assert len(Session.made) == 1
        rh._closing.set()                   # the connection drops
        await asyncio.wait({rh._owner}, timeout=1)
        got = await asyncio.gather(rh.start(), rh.start(), rh.call("get_option_quotes", {}))
        owners = [t for t in asyncio.all_tasks() if t.get_name() == "robinhood-mcp" and not t.done()]
        await rh.close()
        return got, owners
    got, owners = asyncio.run(go())
    assert len(Session.made) == 2 and len(owners) == 1     # one reconnect, one owner task
    assert got[2]["n"] == 2


def test_book_f1_data_starts_the_client_once_not_before_every_call(tmp_path):
    from agentdesk.feeds.f_data import RobinhoodEquityData

    class RH:
        account, starts, calls = "x", 0, 0

        async def start(self):
            RH.starts += 1

        async def call(self, tool, args):
            RH.calls += 1
            return {}
    d = RobinhoodEquityData(RH(), {"cache_dir": str(tmp_path), "max_calls_per_s": 1000})

    async def go():
        for _ in range(3):
            await d._call("get_equity_quotes", {"symbols": ["NVDA"]})
    asyncio.run(go())
    assert RH.calls == 3 and RH.starts == 1


# ----------------------------------------------------------------------------- L10: token file
def test_token_file_is_replaced_atomically_and_private_from_creation(tmp_path, monkeypatch):
    path = tmp_path / "rh_oauth.json"
    path.write_text("{}")
    os.chmod(path, 0o644)
    before = path.stat().st_ino
    created = []
    real_open = os.open

    def spy(p, flags, mode=0o777, *a, **k):
        if flags & os.O_CREAT:
            created.append((str(p), mode))
        return real_open(p, flags, mode, *a, **k)
    monkeypatch.setattr(os, "open", spy)
    old = os.umask(0)
    try:
        asyncio.run(FileTokenStorage(path).set_tokens(SimpleNamespace(model_dump=lambda mode: {"access_token": "x"})))
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stat().st_ino != before                    # written next to it, then renamed over it
    assert created and all(m == 0o600 for _, m in created)
    assert '"access_token": "x"' in path.read_text()
    assert not [p for p in tmp_path.iterdir() if p.name != path.name]


# ----------------------------------------------------------------------------- M2: closing
def test_a_call_cut_off_by_close_fails_fast_without_a_retry(tmp_path):
    rh = client(tmp_path, behaviour="dies_on_close")

    async def go():
        await rh._open()
        call = asyncio.create_task(rh.call("get_option_quotes", {}))
        await asyncio.sleep(0.01)
        await rh.close()
        with pytest.raises(RuntimeError, match="closed") as ex:
            await asyncio.wait_for(call, 1)
        return ex.value
    err = asyncio.run(go())
    assert Session.made[0].calls == 1                      # no read retry into a closed session
    assert err.__suppress_context__                        # one line in the log, not a chained traceback


class TaskGroupTransport(Transport):
    """Like streamablehttp_client: the transport runs its own anyio task group (post_writer, the GET stream) and
    sends one more request on the way out (terminate_session's DELETE), which a cancelled task group can't."""
    said_goodbye: list = []

    async def __aenter__(self):
        self.cm = anyio.create_task_group()
        tg = await self.cm.__aenter__()
        tg.start_soon(anyio.sleep_forever, name="mcp.client.streamable_http.StreamableHTTPTransport.post_writer")
        return ("r", "w", None)

    async def __aexit__(self, *exc):
        try:
            await anyio.sleep(0)
            TaskGroupTransport.said_goodbye.append(True)
        except BaseException as ex:
            self.cm.cancel_scope.cancel()
            return await self.cm.__aexit__(type(ex), ex, ex.__traceback__)
        self.cm.cancel_scope.cancel()
        return await self.cm.__aexit__(*exc)


def test_shutdown_stops_pollers_before_it_closes_the_session_they_call(tmp_path, caplog):
    from agentdesk import lifecycle
    rh = client(tmp_path, transport=lambda: TaskGroupTransport(), behaviour="dies_on_close")
    TaskGroupTransport.said_goodbye = []
    hook_log = logging.getLogger("agentdesk.books")

    async def book_hook():                  # a book hook or _guard task still inside a Robinhood call at shutdown
        try:
            await rh.call("get_option_quotes", {})
        except Exception:
            hook_log.exception("books hook failed")

    async def go():
        keep = frozenset(asyncio.all_tasks())
        await rh._open()
        hook = asyncio.create_task(book_hook())
        await asyncio.sleep(0.01)
        engine = SimpleNamespace(open=[], books=None, stop=lambda: None)
        server = SimpleNamespace(should_exit=False, force_exit=False)
        srv, eng = asyncio.create_task(asyncio.sleep(0)), asyncio.create_task(asyncio.sleep(0))
        await lifecycle.shutdown(engine, server, srv, eng, [rh.close], grace=0.5, close_timeout=1.0, keep=keep)
        return hook, rh._owner
    with caplog.at_level(logging.WARNING):
        hook, owner = asyncio.run(go())
    assert hook.cancelled()                                    # stopped before the session went away under it
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], caplog.text
    assert owner.done() and not owner.cancelled() and owner.exception() is None   # closed by close(), in its own task
    assert TaskGroupTransport.said_goodbye == [True]          # the transport's own tasks were left for that close
