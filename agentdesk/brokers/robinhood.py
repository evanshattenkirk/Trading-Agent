"""Robinhood Trading MCP client (https://agent.robinhood.com/mcp/trading).

- OAuth 2.1 + PKCE via the MCP SDK; tokens cached at ~/.agentdesk/rh_oauth.json (0600). A saved token is refreshed
  at connect once it has under a day left. Only rh-inspect and record-quotes --once may open a browser sign-in (one
  page per process); every other run fails closed with one "sign-in expired: run <cmd>" line (2026-10-07).
- Arguments follow the server's published tool schemas (verified 2026-09-27): account_number,
  legs[{option_id, side, position_effect, ratio_quantity}], quantity/price as strings,
  type 'limit', time_in_force 'gfd', ref_id idempotency key on place_option_order.
- Every order goes to review_option_order first. shadow mode stops there (and paper-fills);
  live mode then calls place_option_order and polls get_option_orders.
- The Agentic account is the one with agentic_allowed=true. Live mode also requires you to
  set robinhood.account_number yourself, and refuses to run if the account's option_level
  is empty (options not approved on that account).
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import os
import re
import stat
import uuid
import time
import webbrowser
from contextlib import AsyncExitStack
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..feeds.base import Quote, QuoteSource
from .base import Broker, OrderResult, OrderStateError, RateLimited, SignInRequired

TERMINAL = ("filled", "cancelled", "canceled", "rejected", "failed", "voided")
from .paper import PaperBroker

log = logging.getLogger("agentdesk.robinhood")

class SchemaError(Exception):
    pass


def fit_args(tool: str, schema: dict, args: dict) -> dict:
    """Drop None/unknown keys (schemas are additionalProperties:false) and check required fields."""
    props = (schema or {}).get("properties") or {}
    out = {k: v for k, v in args.items() if v is not None and (not props or k in props)}
    missing = [r for r in (schema or {}).get("required", []) if r not in out]
    if missing:
        raise SchemaError(f"{tool}: missing required {missing}; server schema props: {list(props)}")
    return out


def find_key(obj, keys, depth=0):
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] not in (None, ""):
                return obj[k]
        for v in obj.values():
            r = find_key(v, keys, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, keys, depth + 1)
            if r is not None:
                return r
    return None


def dict_items(obj) -> list[dict]:
    """Flatten a response into the list of record dicts it most likely contains."""
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        for k in ("results", "data", "items", "instruments", "options", "quotes", "orders", "accounts"):
            if isinstance(obj.get(k), list):
                return [x for x in obj[k] if isinstance(x, dict)]
        for v in obj.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        return [obj]
    return []


# --------------------------------------------------------------------------- OAuth plumbing
class FileTokenStorage:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def _write(self, d: dict) -> None:
        """Atomic and private from the first byte: a temp file created 0600 next to the token file, then renamed
        over it, so a crash never leaves half a token file and the tokens are never readable by others."""
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        try:
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)      # a leftover temp file keeps its old mode otherwise
            with os.fdopen(fd, "w") as f:
                fd = None
                f.write(json.dumps(d))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        d = self._read().get("tokens")
        return OAuthToken.model_validate(d) if d else None

    async def set_tokens(self, tokens) -> None:
        d = self._read()
        d["tokens"] = tokens.model_dump(mode="json")
        d["saved_at"] = time.time()                 # expires_in counts from here; the SDK only keeps it in memory
        self._write(d)

    def expires_at(self) -> float | None:
        """When the saved access token expires: saved_at + expires_in. A file written before saved_at existed is
        dated by its mtime. None when there is no token or it gives no lifetime."""
        try:
            d = self._read()
        except (OSError, ValueError):
            return None
        life = (d.get("tokens") or {}).get("expires_in")
        if not life:
            return None
        t0 = d.get("saved_at")
        if t0 is None:
            try:
                t0 = self.path.stat().st_mtime
            except OSError:
                return None
        return float(t0) + float(life)

    def metadata(self) -> dict | None:
        """The authorization server's metadata (token endpoint) from the last sign-in, for a refresh after a restart."""
        try:
            return self._read().get("oauth_metadata")
        except (OSError, ValueError):
            return None

    def set_metadata(self, meta: dict) -> None:
        d = self._read()
        d["oauth_metadata"] = meta
        self._write(d)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        d = self._read().get("client")
        return OAuthClientInformationFull.model_validate(d) if d else None

    async def set_client_info(self, info) -> None:
        d = self._read()
        d["client"] = info.model_dump(mode="json")
        self._write(d)


class _Callback:
    """One-shot localhost HTTP server that captures ?code=&state= from the OAuth redirect. The port is bound once:
    start() while this server is still listening keeps it (the SDK's redirect handler runs on every attempt), and
    another client in this process can't bind the same port while a sign-in is still waiting on it."""

    WAIT_S = 300.0
    _listening: dict[int, "_Callback"] = {}

    def __init__(self, port: int):
        self.port = port
        self.fut: asyncio.Future | None = None
        self.server = None
        self._loop = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        other = _Callback._listening.get(self.port)
        if other is not None and other is not self and other.server is not None and other._loop is loop:
            raise RuntimeError(f"a Robinhood sign-in is already waiting for the browser on port {self.port}; "
                               "finish it there (or wait for it to time out)")
        if self.fut is None or self.fut.done():
            self.fut = loop.create_future()
        if self.server is not None:
            return                                  # still listening from the last attempt

        async def handle(reader, writer):
            line = (await reader.readline()).decode()
            while (await reader.readline()) not in (b"\r\n", b""):
                pass
            path = line.split(" ")[1] if " " in line else "/"
            qs = parse_qs(urlparse(path).query)
            body = b"<h3>AgentDesk is connected to Robinhood. You can close this tab.</h3>"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
            writer.close()
            if "code" in qs and not self.fut.done():
                self.fut.set_result((qs["code"][0], qs.get("state", [None])[0]))

        self.server = await asyncio.start_server(handle, "127.0.0.1", self.port)
        self._loop = loop
        _Callback._listening[self.port] = self

    async def wait(self):
        await self.start() if self.server is None else None
        try:
            return await asyncio.wait_for(self.fut, timeout=self.WAIT_S)
        finally:
            self.close()

    def close(self) -> None:
        if self.server is not None:
            self.server.close()
            self.server = None
        if _Callback._listening.get(self.port) is self:
            del _Callback._listening[self.port]


_HTTP_429 = re.compile(r"(?<![\w.-])429(?![\w.-])")       # not inside an instrument id or a price like 1.429


def is_rate_limited(err) -> bool:
    e = str(err).lower()
    return "rate_limited" in e or "rate limit" in e or "too many requests" in e or bool(_HTTP_429.search(e))


ORDER_TOOLS = ("place_option_order", "cancel_option_order", "get_option_orders")
CONNECT_TIMEOUT_S = 60.0          # covers a token refresh
SIGN_IN_WAIT_S = _Callback.WAIT_S + 30.0     # once a browser sign-in started: the callback's wait plus margin
SIGN_IN_MSG = "Robinhood needs a sign-in: open the URL printed above (or in the log)"
TIMEOUTS_BEFORE_RECONNECT = 2
REFRESH_AHEAD_S = 86400.0         # refresh a saved token with under a day left, so the 08:10 CT start renews it
# What Evan runs to sign in again (Mac paths; HANDOFF section 8). The engine's token, then the recorder's.
ENGINE_SIGN_IN_CMD = "cd ~/Trading-Agent && .venv/bin/python -m agentdesk rh-inspect"
RECORDER_SIGN_IN_CMD = ("cd ~/.agentdesk/recorder-app/src && ../.venv/bin/python -m agentdesk --config config.yaml "
                        "record-quotes --once")


def _ct(ts: float) -> str:
    from datetime import datetime
    from ..clock import CT
    return datetime.fromtimestamp(ts, CT).strftime("%a %b %d %H:%M CT")


@functools.lru_cache(maxsize=None)
def _provider_class():
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthMetadata

    class Provider(OAuthClientProvider):
        """The MCP SDK (1.x) keeps a token's expiry only in memory and, on a 401, goes straight to a browser sign-in.
        So after each daily restart it sent the saved token until it expired, then opened a sign-in page: the
        refresh_token grant never ran (2026-10-07). This provider dates the saved token (saved_at + expires_in, less
        REFRESH_AHEAD_S) and keeps the token endpoint from the sign-in, so the SDK refreshes at connect instead. A
        refused refresh keeps a still-valid token (and warns with its deadline); on an expired one an unattended run
        fails closed here, before the SDK's browser flow."""
        owner = None                        # the RobinhoodMCP using this provider

        async def _initialize(self) -> None:
            await super()._initialize()
            st = self.context.storage
            exp = st.expires_at()
            if self.context.current_tokens is not None and exp is not None:
                self.context.token_expiry_time = exp - REFRESH_AHEAD_S
            meta = st.metadata()
            if meta and self.context.oauth_metadata is None:
                try:
                    self.context.oauth_metadata = OAuthMetadata.model_validate(meta)
                except ValueError:
                    log.warning("Robinhood: the saved OAuth metadata is unreadable; ignoring it")

        async def _handle_token_response(self, response) -> None:
            await super()._handle_token_response(response)
            if self.context.oauth_metadata is not None:
                self.context.storage.set_metadata(self.context.oauth_metadata.model_dump(mode="json"))

        async def _handle_refresh_response(self, response) -> bool:
            old, old_exp = self.context.current_tokens, self.context.storage.expires_at()
            if await super()._handle_refresh_response(response):
                exp = self.context.storage.expires_at()
                log.info("Robinhood sign-in renewed with the refresh token%s", f"; good until {_ct(exp)}" if exp else "")
                return True
            owner = self.owner
            if old is not None and old_exp is not None and time.time() < old_exp - 60:
                self.context.current_tokens = old           # still good: keep using it, and don't retry every call
                self.context.token_expiry_time = old_exp - 60
                if owner is not None:
                    owner._refresh_refused(response.status_code, old_exp)
                return True
            if owner is not None and not owner.interactive:
                owner._mark_signed_out()
                raise SignInRequired(owner.sign_in_msg)
            return False

    return Provider


def is_dead_session(ex: BaseException) -> bool:
    """A transport-level failure: the MCP session can't carry another call and has to be reopened."""
    import anyio
    if isinstance(ex, (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream, ConnectionError)):
        return True
    try:
        import httpx
        if isinstance(ex, httpx.TransportError):
            return True
    except ImportError:
        pass
    text = str(ex).lower()
    return any(k in text for k in ("session terminated", "session not found", "connection closed"))


class CallBudget:
    """One Robinhood account's call budget for this process. Robinhood throttles near 240 calls a minute per account
    (recorder probe, 2026-09-28) and the standalone quote recorder shares that account, so the engine keeps to
    `per_min` (and `per_s` in bursts). A RATE_LIMITED answer pauses every call for FIRST_PAUSE_S, doubling up to
    MAX_PAUSE_S while it repeats; a success resets it. One pause stays under the 10 s quote watchdog, but pauses in a
    row add up (2+4+8 s), so the engine's watchdog doesn't count the time holding() is true (a pause, or this
    minute's budget used up) toward a quote's age: a throttle alone can't trip a safety halt, an outage still does.
    Order calls (live only) never wait, but they count."""

    FIRST_PAUSE_S = 2.0
    MAX_PAUSE_S = 8.0

    def __init__(self, per_min: int = 120, per_s: int = 3, clock=time.monotonic, sleep=asyncio.sleep):
        self.per_min, self.per_s = int(per_min), int(per_s)
        self.clock, self.sleep = clock, sleep
        self.recent: list[float] = []
        self.paused_until = 0.0
        self.pause_s = self.FIRST_PAUSE_S
        self._minute, self._count = None, 0

    @classmethod
    def from_cfg(cls, rh_cfg: dict) -> "CallBudget | None":
        c = (rh_cfg or {}).get("call_budget")
        if not c:
            return None
        return cls(per_min=c.get("per_min", 120), per_s=c.get("per_s", 3))

    def _prune(self, now: float) -> None:
        cut = now - 60.0
        i = 0
        while i < len(self.recent) and self.recent[i] <= cut:
            i += 1
        if i:
            del self.recent[:i]

    def last_minute(self) -> int:
        self._prune(self.clock())
        return len(self.recent)

    async def acquire(self, urgent: bool = False) -> None:
        while not urgent:
            now = self.clock()
            self._prune(now)
            wait = self.paused_until - now
            if len(self.recent) >= self.per_min:
                wait = max(wait, self.recent[0] + 60.0 - now)
            last_s = [t for t in self.recent[-self.per_s:] if t > now - 1.0] if self.per_s else []
            if self.per_s and len(last_s) >= self.per_s:
                wait = max(wait, last_s[0] + 1.0 - now)
            if wait <= 0:
                break
            await self.sleep(wait + 0.001)
        now = self.clock()
        self.recent.append(now)
        m = int(time.time() // 60)
        if m != self._minute:
            if self._minute is not None:
                log.info("Robinhood: %d calls in the last minute (budget %d)", self._count, self.per_min)
            self._minute, self._count = m, 0
        self._count += 1

    def throttled(self) -> float:
        """Robinhood said RATE_LIMITED: pause every call. Returns the pause in seconds."""
        pause = self.pause_s
        self.paused_until = max(self.paused_until, self.clock() + pause)
        self.pause_s = min(self.pause_s * 2, self.MAX_PAUSE_S)
        log.warning("Robinhood RATE_LIMITED: pausing calls for %.0fs (%d calls in the last minute)", pause,
                    self.last_minute())
        return pause

    def ok(self) -> None:
        self.pause_s = self.FIRST_PAUSE_S

    def holding(self) -> bool:
        """True while no call can go out: a RATE_LIMITED pause, or this minute's budget is used up."""
        now = self.clock()
        if self.paused_until > now:
            return True
        self._prune(now)
        return len(self.recent) >= self.per_min


class RobinhoodMCP:
    RECONNECT_GAP_S = 5.0                 # at most one reconnect try per this many seconds
    _refused: dict[str, float | None] = {}  # token file -> its mtime when Robinhood refused it (this process)
    _browser_opened = False                 # interactive commands open one sign-in page per process

    def __init__(self, cfg, interactive: bool = False, sign_in_cmd: str = ENGINE_SIGN_IN_CMD,
                 after: str = ", then restart the engine"):
        self.cfg = cfg["robinhood"]
        self.url = self.cfg["mcp_url"]
        self.stack: AsyncExitStack | None = None
        self.session = None
        self.tools: dict[str, dict] = {}
        self.account: str | None = str(self.cfg["account_number"]) if self.cfg.get("account_number") else None
        self.account_info: dict = {}
        self._lock = asyncio.Lock()
        self._started = False
        self.chain_symbol = cfg.get("symbol", "SPY")
        self.budget = CallBudget.from_cfg(self.cfg)     # None: no pacing here (the recorder paces itself)
        self.call_timeout = float(self.cfg.get("call_timeout_sec", 20))
        self._connect = None                # (transport factory, session class), kept so a dropped session reconnects
        self._owner = None
        self._closing = asyncio.Event()
        self._closed = self._dead = False
        self._timeouts, self._last_connect = 0, -1e18
        self._start_lock = asyncio.Lock()
        self._accounts_loaded = False
        self._cb: _Callback | None = None
        self._provider = None
        self._sign_in_at: float | None = None       # when the current connect attempt started a browser sign-in
        self.on_sign_in = None                      # optional callable(msg): the engine puts the line on the dashboard
        # Unattended runs (the launchd engine, the recorder daemon) never open a browser (2026-10-07: an expired token
        # opened ~67 sign-in tabs, one per reconnect). Only rh-inspect and record-quotes --once are interactive.
        self.interactive = bool(interactive)
        self.sign_in_cmd = sign_in_cmd
        self.sign_in_msg = f"Robinhood sign-in expired: run `{sign_in_cmd}` in Terminal{after}"
        self.on_signed_out = None                   # callable(msg), once per client: the engine halts
        self.on_token_warning = None                # callable(msg): a refused refresh, while the token still works
        self._told = False
        self._refresh_warning: str | None = None

    def _session_down(self) -> bool:
        owner = self._owner
        return self._connect is None or self._dead or owner is None or owner.done()

    async def start(self) -> None:
        """Connects once (OAuth, session, account). Calling it again is cheap and safe: a dropped session is reopened
        through the same locked reconnect path every call uses (_ensure_session), so there is never a second owner
        task and RECONNECT_GAP_S holds."""
        self._check_signed_out()
        if self._accounts_loaded and not self._session_down():
            return
        async with self._start_lock:
            if self._connect is None:
                self._connect = self._transport()
                async with self._lock:
                    await self._open()
                log.info("Robinhood MCP connected: %d tools", len(self.tools))
            elif self._session_down():
                async with self._lock:
                    await self._ensure_session()
            if not self._accounts_loaded:
                await self._load_accounts()
                self._accounts_loaded = True

    def _transport(self):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        from mcp.shared.auth import OAuthClientMetadata

        port = self.cfg["redirect_port"]
        if self._cb is not None:
            self._cb.close()
        self._cb = _Callback(port)
        provider = _provider_class()(
            server_url=self.url,
            client_metadata=OAuthClientMetadata(
                client_name="AgentDesk", redirect_uris=[f"http://localhost:{port}/callback"],
                grant_types=["authorization_code", "refresh_token"], response_types=["code"],
                token_endpoint_auth_method="none"),
            storage=FileTokenStorage(self._token_file()),
            redirect_handler=self._redirect, callback_handler=self._wait_callback)
        provider.owner = self
        self._provider = provider
        # The MCP client's anyio task groups must be entered and exited by the same task, so one task owns the
        # session for its whole life. Closing it from anywhere else (shutdown, a finished engine) is then safe,
        # and a dropped connection can't cancel whichever task happened to open it.
        return (lambda: streamablehttp_client(self.url, auth=provider, timeout=30), ClientSession)

    async def _redirect(self, url: str) -> None:
        """The OAuth redirect handler: the saved token was refused, so a sign-in is needed. Unattended, that fails
        closed (no browser, no callback server). Interactive, the URL is printed and the browser opens once."""
        if not self.interactive:
            self._mark_signed_out()
            raise SignInRequired(self.sign_in_msg)
        if self._cb is None:
            self._cb = _Callback(self.cfg["redirect_port"])
        await self._cb.start()
        print(f"\nOpen this URL to authorize AgentDesk with Robinhood (desktop browser):\n{url}\n", flush=True)
        log.info("Robinhood sign-in URL: %s", url)
        self._sign_in_started()
        if not RobinhoodMCP._browser_opened:
            RobinhoodMCP._browser_opened = True
            webbrowser.open(url)

    async def _wait_callback(self):
        return await self._cb.wait()

    # ------------------------------------------------------------------ sign-in state
    def _token_file(self) -> Path:
        return Path(os.path.expanduser(self.cfg["token_dir"])) / "rh_oauth.json"

    def _token_mtime(self) -> float | None:
        try:
            return self._token_file().stat().st_mtime
        except OSError:
            return None

    def _is_refused(self) -> bool:
        return str(self._token_file()) in RobinhoodMCP._refused

    def _mark_signed_out(self) -> None:
        """Robinhood refused the saved sign-in: one ERROR line per process and token file, then every call fails fast
        until a new sign-in rewrites the token file."""
        key = str(self._token_file())
        if key not in RobinhoodMCP._refused:
            log.error(self.sign_in_msg)
        RobinhoodMCP._refused[key] = self._token_mtime()
        self._tell()

    def _tell(self) -> None:
        if self._told:
            return
        self._told = True
        if self.on_signed_out is not None:
            try:
                self.on_signed_out(self.sign_in_msg)
            except Exception:
                log.exception("on_signed_out failed")

    def signed_out(self) -> str | None:
        """The sign-in-expired line while the saved token is the one Robinhood refused; None once a new sign-in
        (rh-inspect, record-quotes --once) rewrote the token file, and the next connect uses it."""
        key = str(self._token_file())
        if key not in RobinhoodMCP._refused:
            return None
        if self._token_mtime() == RobinhoodMCP._refused[key]:
            return self.sign_in_msg
        del RobinhoodMCP._refused[key]
        log.warning("Robinhood: a new sign-in was saved; connecting with it")
        self._told = False
        if self._connect is not None:
            self._connect, self._dead = self._transport(), True
        return None

    def _check_signed_out(self) -> None:
        msg = self.signed_out()
        if msg:
            self._tell()
            raise SignInRequired(msg)

    def _refresh_refused(self, status: int, expires: float) -> None:
        if self._refresh_warning is not None:
            return
        self._refresh_warning = (f"Robinhood token refresh failed (HTTP {status}); the sign-in ends {_ct(expires)}: "
                                 f"run `{self.sign_in_cmd}` in Terminal before then")
        log.warning(self._refresh_warning)
        if self.on_token_warning is not None:
            try:
                self.on_token_warning(self._refresh_warning)
            except Exception:
                log.exception("on_token_warning failed")

    def sign_in_status(self) -> tuple[bool, str]:
        """For the Ops desk: red when signed out, when a refresh was refused, or when the token has under a day left
        (the connect should have renewed it); green with the deadline otherwise."""
        out = self.signed_out()
        if out:
            return False, out
        if self._refresh_warning:
            return False, self._refresh_warning
        exp = FileTokenStorage(self._token_file()).expires_at()
        if exp is None:
            return True, "signed in (token lifetime unknown)"
        if exp - time.time() < REFRESH_AHEAD_S:
            return False, f"the sign-in ends {_ct(exp)} and did not renew: run `{self.sign_in_cmd}` in Terminal before then"
        return True, f"signed in until {_ct(exp)}"

    def _sign_in_started(self) -> None:
        """From here _open waits for the browser callback (SIGN_IN_WAIT_S), not just CONNECT_TIMEOUT_S."""
        first = self._sign_in_at is None
        self._sign_in_at = time.monotonic()
        if not first:
            return
        log.warning(SIGN_IN_MSG)
        if self.on_sign_in is not None:
            try:
                self.on_sign_in(SIGN_IN_MSG)
            except Exception:
                log.debug("on_sign_in failed", exc_info=True)

    async def _open(self) -> None:
        transport, session_cls = self._connect
        self._last_connect = time.monotonic()
        ready = asyncio.get_running_loop().create_future()
        self._closing = asyncio.Event()
        self._sign_in_at = None
        self._owner = owner = asyncio.create_task(self._own_session(transport, session_cls, ready), name="robinhood-mcp")
        try:
            await self._wait_ready(ready)
        except BaseException as ex:
            if not ready.done():
                ready.cancel()                      # nobody reads it now
            if not owner.done():
                owner.cancel()                      # an abandoned attempt must not keep a sign-in (and its port) open
            if self._cb is not None:
                self._cb.close()
            if self._is_refused() and isinstance(ex, Exception) and not isinstance(ex, SignInRequired):
                raise SignInRequired(self.sign_in_msg) from ex      # however the SDK's task group wrapped it
            raise
        finally:
            self._sign_in_at = None
        self._started, self._dead, self._timeouts = True, False, 0

    async def _wait_ready(self, ready: asyncio.Future) -> None:
        """CONNECT_TIMEOUT_S covers a token refresh. Once the redirect handler started a browser sign-in, keep
        waiting for the callback (SIGN_IN_WAIT_S from then), so Evan can finish signing in."""
        t0 = time.monotonic()
        while not ready.done():
            if self._is_refused():                  # the SDK may swallow the refusal and wait; don't wait with it
                raise SignInRequired(self.sign_in_msg)
            limit = t0 + CONNECT_TIMEOUT_S
            if self._sign_in_at is not None:
                limit = max(limit, self._sign_in_at + SIGN_IN_WAIT_S)
            left = limit - time.monotonic()
            if left <= 0:
                what = "the browser sign-in was not completed" if self._sign_in_at is not None else "no session"
                raise TimeoutError(f"Robinhood MCP: {what} after {time.monotonic() - t0:.0f}s")
            await asyncio.wait({ready}, timeout=min(left, 0.5))
        ready.result()

    async def _ensure_session(self) -> None:
        """Reconnects when the session ended or looks dead (a connection error, or TIMEOUTS_BEFORE_RECONNECT calls
        in a row timed out). Runs under the call lock, so only one reconnect happens at a time."""
        if self._closed:
            raise RuntimeError("Robinhood MCP is closed")
        self._check_signed_out()
        owner = self._owner
        if self._connect is None or not (self._dead or owner is None or owner.done()):
            return
        if time.monotonic() - self._last_connect < self.RECONNECT_GAP_S:
            raise ConnectionError("Robinhood MCP session dropped; reconnecting shortly")
        if owner is not None and not owner.done():
            self._closing.set()
            await asyncio.wait({owner}, timeout=2.0)
            if not owner.done():
                owner.cancel()
        log.warning("Robinhood MCP session dropped; reconnecting")
        try:
            await self._open()
        except SignInRequired:
            self._dead = True
            raise
        except Exception as ex:
            self._dead = True
            raise ConnectionError(f"Robinhood MCP reconnect failed: {ex!r}") from ex
        log.warning("Robinhood MCP reconnected: %d tools", len(self.tools))

    async def _own_session(self, transport, session_cls, ready: asyncio.Future) -> None:
        try:
            async with AsyncExitStack() as stack:
                self.stack = stack
                read, write, _ = await stack.enter_async_context(transport())
                self.session = await stack.enter_async_context(session_cls(read, write))
                await self.session.initialize()
                listed = await self.session.list_tools()
                self.tools = {t.name: (t.inputSchema or {}) for t in listed.tools}
                ready.set_result(None)
                await self._closing.wait()
        except BaseException as ex:
            if not ready.done():
                ready.set_exception(ex if isinstance(ex, Exception) else RuntimeError(f"Robinhood MCP: {ex!r}"))
            elif not isinstance(ex, asyncio.CancelledError):
                log.warning("Robinhood MCP session ended: %r", ex)
            if isinstance(ex, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            if self._owner is None or self._owner is asyncio.current_task():     # not a replaced, older session
                self.stack, self._started = None, False

    def owned_tasks(self) -> list:
        """The session's owner task: shutdown leaves it to close() instead of cancelling it with the pollers."""
        return [self._owner] if self._owner is not None else []

    async def close(self, timeout: float = 3.0) -> None:
        self._closed = True                         # from here every call fails fast (call())
        owner = self._owner
        if owner is None or owner.done():
            return
        self._closing.set()
        await asyncio.wait({owner}, timeout=timeout)    # never raises, even if the owner ended cancelled
        if not owner.done():
            log.warning("Robinhood MCP session did not close within %.0fs; cancelling it", timeout)
            owner.cancel()
            await asyncio.wait({owner}, timeout=1.0)

    async def call(self, tool: str, args: dict):
        if self._closed:
            raise RuntimeError(f"{tool}: Robinhood MCP is closed")
        self._check_signed_out()
        if tool not in self.tools:
            raise SchemaError(f"tool {tool} not offered by the server (have: {sorted(self.tools)})")
        args = fit_args(tool, self.tools[tool], args)
        async with self._lock:
            try:
                res = await self._send(tool, args)
            except Exception as ex:
                if self._closed:                    # close() began while this call was in flight: no retry
                    raise RuntimeError(f"{tool}: Robinhood MCP is closed") from None
                if isinstance(ex, SignInRequired):
                    raise
                if self._is_refused():              # the sign-in was refused under this call
                    self._tell()
                    raise SignInRequired(self.sign_in_msg) from ex
                if is_rate_limited(ex):
                    self._throttled()
                    raise RateLimited(f"{tool} error: {ex}") from ex
                if tool.startswith(("get_", "review_")):          # read-only: one retry on a transient failure
                    await asyncio.sleep(0.3)
                    res = await self._send(tool, args)
                else:
                    raise
            if getattr(res, "isError", False) and tool.startswith(("get_", "review_")) and "500" in _text(res) \
                    and not is_rate_limited(_text(res)):
                await asyncio.sleep(0.3)
                res = await self._send(tool, args)
            if getattr(res, "isError", False) and is_rate_limited(_text(res)):
                self._throttled()
                raise RateLimited(f"{tool} error: {_text(res)[:400]}")
            if self.budget is not None:
                self.budget.ok()
        if getattr(res, "isError", False):
            raise RuntimeError(f"{tool} error: {_text(res)[:400]}")
        data = getattr(res, "structuredContent", None)
        if not data:
            txt = _text(res)
            try:
                data = json.loads(txt)
            except json.JSONDecodeError:
                return {"text": txt}
        return data.get("data", data) if isinstance(data, dict) else data

    async def _send(self, tool: str, args: dict):
        await self._ensure_session()
        if self.budget is not None:
            await self.budget.acquire(urgent=tool in ORDER_TOOLS)
        try:
            res = await asyncio.wait_for(self.session.call_tool(tool, args), self.call_timeout)
        except asyncio.TimeoutError:
            self._timeouts += 1
            self._dead = self._dead or self._timeouts >= TIMEOUTS_BEFORE_RECONNECT
            raise TimeoutError(f"{tool} got no reply in {self.call_timeout:g}s") from None
        except Exception as ex:
            if is_dead_session(ex):
                self._dead = True
            raise
        self._timeouts = 0
        return res

    def _throttled(self) -> None:
        if self.budget is not None:
            self.budget.throttled()

    async def _load_accounts(self) -> None:
        data = await self.call("get_accounts", {})
        accts = data.get("accounts", []) if isinstance(data, dict) else []
        agentic = [a for a in accts if a.get("agentic_allowed")]
        if self.account:
            self.account_info = next((a for a in accts if str(a.get("account_number")) == self.account), {})
            if not self.account_info.get("agentic_allowed"):
                raise SystemExit(f"robinhood.account_number ...{self.account[-4:]} is not the agent-tradable account")
        elif agentic:
            self.account_info = agentic[0]
            self.account = str(agentic[0]["account_number"])
        lvl = self.account_info.get("option_level") or "none"
        log.info("Agentic account ...%s type=%s option_level=%s", (self.account or "????")[-4:],
                 self.account_info.get("type"), lvl)

    @property
    def option_level(self) -> str:
        return self.account_info.get("option_level") or ""

    async def instrument_id(self, contract) -> str | None:
        if contract.broker_id:
            return contract.broker_id
        data = await self.call("get_option_instruments", {
            "chain_symbol": contract.symbol, "expiration_dates": contract.expiry,
            "strike_price": f"{float(contract.strike):.4f}", "type": contract.right,
            "state": getattr(contract, "state", None) or "active"})
        for it in dict_items(data):
            sp = find_key(it, ["strike_price", "strike"])
            ty = str(find_key(it, ["type", "option_type"]) or "").lower()
            ex = str(find_key(it, ["expiration_date", "expiration"]) or "")
            if sp is not None and abs(float(sp) - contract.strike) < 1e-6 and (not ty or ty.startswith(contract.right[0])) \
                    and (not ex or ex.startswith(contract.expiry)):
                contract.broker_id = str(it.get("id") or find_key(it, ["id", "option_id", "instrument_id"]))
                so = it.get("sellout_datetime")
                if so:        # Robinhood force-closes at this time (14:45 CT for SPY 0DTE when checked)
                    from datetime import datetime as _dt
                    contract.sellout_ts = _dt.fromisoformat(so.replace("Z", "+00:00")).timestamp()
                return contract.broker_id
        return None

    async def price_book(self, symbol: str) -> dict | None:
        data = await self.call("get_equity_price_book", {"symbols": [symbol]})
        books = data.get("books", []) if isinstance(data, dict) else []
        return books[0] if books else None

    async def equity_bars(self, symbol: str, start_iso: str, end_iso: str, interval: str = "minute") -> list[dict]:
        data = await self.call("get_equity_historicals", {"symbols": [symbol], "start_time": start_iso,
                                                         "end_time": end_iso, "interval": interval, "bounds": "regular"})
        res = data.get("results", []) if isinstance(data, dict) else []
        return [b for b in (res[0].get("bars", []) if res else []) if not b.get("interpolated")]

    async def option_bars(self, instrument_id: str, start_iso: str, end_iso: str, interval: str = "minute") -> list[dict]:
        data = await self.call("get_option_historicals", {"instrument_ids": [instrument_id], "start_time": start_iso,
                                                         "end_time": end_iso, "interval": interval})
        res = data.get("results", []) if isinstance(data, dict) else []
        return [b for b in (res[0].get("bars", []) if res else []) if not b.get("interpolated")]


def _text(res) -> str:
    return "".join(getattr(c, "text", "") for c in (res.content or []))


# --------------------------------------------------------------------------- quotes + broker
class OptionQuoteRecorder:
    """Records real 0DTE SPY quotes into the journal: calls and puts ATM-10..ATM+10 (covers B's +-$5 wings and D's
    +-0.9 EM shorts + $2 wings).
    Calls settle naked-vs-debit-spread on real prices; the ATM call+put pair measures what the straddle
    really costs vs the move that follows (the variance-risk-premium check behind the iron-fly idea).

    The day's 0DTE chain is listed once (paged get_option_instruments) and cached, so each poll is one
    get_option_quotes call. The engine's copy stands down while the standalone recorder
    (python -m agentdesk record-quotes) is recording, so rows aren't written twice."""

    CHAIN_RETRY_SEC = 300          # an empty chain (holiday, listing error) is retried at most this often
    CHAIN_REFRESH_SEC = 900        # missing strikes near the money trigger a re-list at most this often

    def __init__(self, rh: RobinhoodMCP, journal, every: float = 10.0, width: int = 10, symbol: str = "SPY",
                 standalone: bool = False):
        self.rh, self.journal, self.every, self.width, self.symbol = rh, journal, every, width, symbol
        self.standalone = standalone
        self.chain: dict[tuple[float, str], str] = {}
        self.chain_day: str | None = None
        self.chain_ts = 0.0

    async def load_chain(self, exp: str) -> int:
        chain: dict[tuple[float, str], str] = {}
        cursor = None
        for _ in range(40):
            args = {"chain_symbol": self.symbol, "expiration_dates": exp, "state": "active"}
            if cursor:
                args["cursor"] = cursor
            data = await self.rh.call("get_option_instruments", args)
            for it in dict_items(data):
                if str(it.get("expiration_date") or exp) != exp or not it.get("id"):
                    continue
                try:
                    chain[(float(it["strike_price"]), str(it["type"]).lower())] = str(it["id"])
                except (KeyError, TypeError, ValueError):
                    continue
            cursor = data.get("next") if isinstance(data, dict) else None
            if not cursor:
                break
        self.chain, self.chain_day, self.chain_ts = chain, exp, time.time()
        log.info("quote recorder: %s 0DTE chain for %s has %d contracts", self.symbol, exp, len(chain))
        return len(chain)

    def wanted(self, px: float) -> dict[str, tuple[float, str]]:
        atm = round(px)
        ids = {}
        for r in ("call", "put"):
            for k in range(-self.width, self.width + 1):
                oid = self.chain.get((float(atm + k), r))
                if oid:
                    ids[oid] = (float(atm + k), r)
        return ids

    async def poll_once(self, px: float, now: float) -> int:
        """One snapshot: quotes for ATM-width..ATM+width calls and puts at spot px. Returns rows written."""
        from ..clock import session_date
        exp = str(session_date(now))
        age = time.time() - self.chain_ts
        if self.chain_day != exp or (not self.chain and age > self.CHAIN_RETRY_SEC):
            await self.load_chain(exp)
        ids = self.wanted(px)
        if len(ids) < 2 * (2 * self.width + 1) and self.chain and age > self.CHAIN_REFRESH_SEC:
            await self.load_chain(exp)
            ids = self.wanted(px)
        if not ids:
            return 0
        data = await self.rh.call("get_option_quotes", {"instrument_ids": list(ids)})
        rows = []
        for q in dict_items(data.get("quotes", data) if isinstance(data, dict) else data):
            q = q.get("quote", q) if isinstance(q.get("quote"), dict) else q
            oid = str(q.get("instrument_id") or find_key(q, ["instrument_id", "id"]) or "")
            if oid in ids and q.get("bid_price") is not None and q.get("ask_price") is not None:
                rows.append((now, exp, ids[oid][0], ids[oid][1], float(q["bid_price"]), float(q["ask_price"]), px))
        self.journal.record_quotes(rows)
        return len(rows)

    async def run(self, price_fn, now_fn) -> None:
        from ..clock import is_rth
        from ..recorder import standalone_active
        while True:
            try:
                now, px = now_fn(), price_fn()
                if px and is_rth(now) and (self.standalone or not standalone_active()):
                    await self.poll_once(px, now)
            except Exception as ex:
                log.debug("quote recorder: %s", ex)
            await asyncio.sleep(self.every)


class RobinhoodQuotes(QuoteSource):
    name = "robinhood"

    def __init__(self, rh: RobinhoodMCP, max_age: float = 0.5):
        self.rh = rh
        self.cache: dict[str, Quote] = {}
        self.max_age = max_age

    async def start(self) -> None:
        await self.rh.start()

    async def quote(self, contract) -> Quote | None:
        oid = await self.rh.instrument_id(contract)
        if not oid:
            return None
        q = self.cache.get(oid)
        if q and time.time() - q.ts < self.max_age:
            return q
        data = await self.rh.call("get_option_quotes", {"instrument_ids": [oid]})
        bid = find_key(data, ["bid_price", "bid"])
        ask = find_key(data, ["ask_price", "ask"])
        if bid is None or ask is None:
            return None
        q = Quote(float(bid), float(ask), time.time())
        self.cache[oid] = q
        return q


def order_args(account: str, legs: list[dict], qty: int, price: float, review: bool, symbol: str = "SPY",
               direction: str | None = None) -> dict:
    """`direction` ("debit"/"credit") should come from the position's credit flag; without it a multi-leg order
    falls back to the first leg's side, which is wrong for a debit calendar that lists its sold leg first."""
    a = {"account_number": account, "legs": legs, "quantity": str(int(qty)), "price": f"{price:.2f}",
         "type": "limit", "time_in_force": "gfd"}
    if len(legs) > 1:
        a["direction"] = direction or ("debit" if legs[0]["side"] == "buy" else "credit")
    if review:
        a.update({"chain_symbol": symbol, "underlying_type": "equity"})
    return a


class RobinhoodBroker(Broker):
    def __init__(self, rh: RobinhoodMCP, quotes, live: bool, fill_timeout: float = 2.5):
        self.rh, self.quotes, self.live = rh, quotes, live
        self.name = "robinhood-live" if live else "robinhood-shadow"
        self.paper = PaperBroker(quotes)
        self.fill_timeout = fill_timeout
        self.open_orders: set[str] = set()
        self.confirm_tries = 6

    async def start(self) -> None:
        await self.rh.start()
        if not self.rh.account:
            raise SystemExit("No agent-tradable Robinhood account found.")
        if self.rh.option_level in ("", "option_level_0"):
            raise SystemExit("Options are not approved on the Agentic account "
                             f"••••{self.rh.account[-4:]}. Apply in the Robinhood app: Account > Investing > Options.")
        if self.live and not self.rh.cfg.get("account_number"):
            raise SystemExit("Live mode: set robinhood.account_number in config.yaml to your Agentic account number.")

    async def resolve(self, contract):
        await self.rh.instrument_id(contract)
        return contract

    async def submit(self, contract, side, qty, limit, now) -> OrderResult:
        legs = [{"option_id": contract.broker_id, "side": side, "position_effect": "open" if side == "buy" else "close"}]
        acct = self.rh.account
        try:
            review = await self.rh.call("review_option_order", order_args(self.rh.account, legs, qty, limit, True, contract.symbol))
        except Exception as ex:
            return OrderResult("rejected", message=redact_account(f"review failed: {ex}", acct))
        if not self.live:
            res = await self.paper.submit(contract, side, qty, limit, now)
            res.review = _short(review, account=acct)
            return res
        args = order_args(self.rh.account, legs, qty, limit, False)
        args["ref_id"] = str(uuid.uuid4())
        try:
            placed = await self.rh.call("place_option_order", args)
        except Exception as first:
            if not can_retry_place(self.rh, "place_option_order"):
                log.error("place_option_order failed and its schema has no ref_id, so a retry could place a second "
                          "order: not retrying (%s)", redact_account(str(first), acct))
                raise OrderStateError(redact_account(f"place_option_order failed, order state unknown (not retried: "
                                                     f"no ref_id in the tool schema): {first}", acct)) from first
            try:
                placed = await self.rh.call("place_option_order", args)   # one retry, same ref_id = no duplicate
            except Exception as ex:
                # the first call may have reached Robinhood; we can't know whether an order exists
                raise OrderStateError(redact_account(f"place_option_order failed twice, order state unknown: {ex}",
                                                     acct)) from ex
        oid = str(find_key(placed, ["id", "order_id"]) or "")
        if not oid:
            raise OrderStateError(f"place_option_order returned no order id: {_short(placed, 200, account=acct)}")
        self.open_orders.add(oid)
        deadline = time.time() + self.fill_timeout
        rec = None
        while time.time() < deadline:
            await asyncio.sleep(0.4)
            rec = await self._read_order(oid) or rec
            if rec and rec[0] in TERMINAL:
                break
        if not rec or rec[0] not in TERMINAL:
            try:
                await self.rh.call("cancel_option_order", {"account_number": self.rh.account, "order_id": oid})
            except Exception as ex:
                log.warning("cancel %s failed: %s", oid, ex)
            # a fill can land between the last poll and the cancel: re-read until the order is final
            for _ in range(self.confirm_tries):
                await asyncio.sleep(0.5)
                rec = await self._read_order(oid) or rec
                if rec and rec[0] in TERMINAL:
                    break
        if not rec or rec[0] not in TERMINAL:
            filled, avg = (rec[1], _per_share(rec[2], limit)) if rec else (0, 0.0)
            raise OrderStateError(f"order {oid} not confirmed final after cancel (state {rec[0] if rec else 'unknown'}, "
                                  f"filled {filled})", oid, filled, avg)
        self.open_orders.discard(oid)
        state, filled, avg = rec
        avg = _per_share(avg, limit) or limit
        status = "filled" if filled >= qty else "partial" if filled else ("rejected" if state in ("rejected", "failed") else "unfilled")
        return OrderResult(status, filled, avg, oid, state, _short(review, account=acct),
                           {"placed": _short(placed, account=acct)})

    async def _read_order(self, oid: str) -> tuple[str, int, float] | None:
        """(state, filled qty, avg price) or None if the read failed."""
        try:
            data = await self.rh.call("get_option_orders", {"account_number": self.rh.account, "order_id": oid})
        except Exception as ex:
            log.warning("get_option_orders %s failed: %s", oid, ex)
            return None
        rec = next(iter(dict_items(data.get("orders", data) if isinstance(data, dict) else data)), None)
        if rec is None:
            return None
        state = str(rec.get("state") or "").lower()
        filled = int(float(rec.get("processed_quantity") or rec.get("filled_quantity") or 0))
        avg = float(rec.get("average_price") or rec.get("processed_premium_per_contract") or rec.get("price") or 0)
        return state, filled, avg

    async def buying_power(self) -> float | None:
        try:
            v = find_key(await self.rh.call("get_portfolio", {"account_number": self.rh.account}),
                         ["option_buying_power", "buying_power", "cash_available_for_trading"])
            return float(v) if v is not None else None
        except Exception:
            return None

    async def open_positions(self) -> list:
        data = await self.rh.call("get_option_positions", {"account_number": self.rh.account, "nonzero": True})
        return [p for p in dict_items(data.get("positions", data) if isinstance(data, dict) else data)
                if float(find_key(p, ["quantity", "qty"]) or 0) > 0]

    async def position_qty(self) -> int | None:
        if not self.live:
            return None                 # shadow fills are simulated; the account holds nothing
        return int(sum(float(find_key(p, ["quantity", "qty"]) or 0) for p in await self.open_positions()))

    async def cancel_all(self) -> None:
        for oid in list(self.open_orders):
            try:
                await self.rh.call("cancel_option_order", {"account_number": self.rh.account, "order_id": oid})
            except Exception:
                pass
        self.open_orders.clear()


def _per_share(avg: float, limit: float) -> float:
    """Some order fields report the premium per contract (100 shares), others per share. A fill is never 10x away
    from its own limit, so take whichever reading is closer to the limit (a $0.15 fill reported as 15 reads 0.15)."""
    if avg <= 0:
        return avg
    if limit <= 0:
        return avg / 100 if avg > 20 else avg
    return min((avg, avg / 100), key=lambda v: abs(math.log(v / limit)))


def _short(d, n=600, account: str | None = None):
    """A JSON-safe copy of d, cut to n characters. With `account`, the account number (which review and order
    replies echo) shows only its last 4 digits, as everywhere else this is stored or shown."""
    s = json.dumps(d, default=str)
    if account:
        s = json.dumps(_redacted(json.loads(s), str(account)), ensure_ascii=False)
    return json.loads(s) if len(s) <= n else {"truncated": s[:n]}


def _redacted(obj, acct: str):
    if isinstance(obj, str):
        return redact_account(obj, acct)
    if isinstance(obj, dict):
        return {_redacted(k, acct): _redacted(v, acct) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redacted(v, acct) for v in obj]
    if isinstance(obj, (int, float)) and not isinstance(obj, bool) and str(obj) == acct:
        return redact_account(acct, acct)
    return obj


def can_retry_place(rh, tool: str) -> bool:
    """A failed place_*_order may be retried only when the live schema takes ref_id: fit_args drops keys the schema
    doesn't list, and without ref_id a retry could place a second order."""
    props = (((getattr(rh, "tools", None) or {}).get(tool) or {}).get("properties") or {})
    return "ref_id" in props


# --------------------------------------------------------------------------- rh-inspect (read-only)
def redact_account(text: str, acct: str) -> str:
    """Anything rh-inspect prints shows only the account's last 4 digits (Robinhood echoes it in responses)."""
    return text.replace(acct, "••••" + acct[-4:]) if acct else text


async def inspect_main(cfg, out: str) -> None:
    from datetime import datetime
    from ..clock import CT
    from ..exits import Contract

    rh = RobinhoodMCP(cfg, interactive=True)            # run by hand: the one command that may open a sign-in page
    await rh.start()
    Path(out).write_text(json.dumps(rh.tools, indent=2))
    print(f"\nConnected. {len(rh.tools)} tools. Schemas written to {out}")
    acct = rh.account or ""
    print(f"Agentic account: ...{acct[-4:] if acct else 'NOT FOUND'}  type={rh.account_info.get('type')}  "
          f"option_level={rh.option_level or 'NONE (apply in the Robinhood app: Account > Investing > Options)'}")
    try:
        bp = find_key(await rh.call("get_portfolio", {"account_number": acct}), ["option_buying_power", "buying_power"])
        print(redact_account(f"Buying power: {bp}", acct))
    except Exception as ex:
        print(f"portfolio failed: {ex}")
    q = await rh.call("get_equity_quotes", {"symbols": ["SPY"]})
    px = float(find_key(q, ["last_trade_price", "last_extended_hours_trade_price", "ask_price"]))
    print(f"SPY {px:.2f}")
    book = await rh.price_book("SPY")
    print(f"Level 2: {len(book.get('bids', [])) if book else 0} bid levels / {len(book.get('asks', [])) if book else 0} ask levels "
          f"(empty outside market hours)")
    today = datetime.now(CT).date().isoformat()
    c = Contract("SPY", today, float(int(px) + 1), "call")
    oid = await rh.instrument_id(c)
    print(f"Instrument {c.label}: {oid or 'none (no expiry today?)'}")
    if oid and rh.option_level:
        qt = await RobinhoodQuotes(rh).quote(c)
        print(f"Quote: {qt}")
        if qt:
            legs = [{"option_id": oid, "side": "buy", "position_effect": "open"}]
            print("review_option_order (simulation, nothing placed):")
            rev = await rh.call("review_option_order", order_args(acct, legs, 1, max(0.01, qt.bid), True))
            print(redact_account(json.dumps(rev, indent=2, default=str), acct)[:3000])
    from .robinhood_equity import inspect_equity
    await inspect_equity(rh, acct)
    await rh.close()
