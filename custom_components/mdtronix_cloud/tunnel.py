"""Tunnel client. Keeps Home Assistant connected to the MDtronix relay and forwards requests to HA's local HTTP server.

Protocol: docs/remote-access/PROTOCOL.md, section 2. This module imports nothing from Home Assistant,
so it can be tested without an HA install.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import struct
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
OP_TEXT = 0x01
OP_BINARY = 0x02
MAX_STREAMS = 64
# The relay pings every 30 seconds. Going longer than this without a ping means the link is dead.
PING_TIMEOUT_S = 90
RESPONSE_CHUNK = 64 * 1024
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 60.0
STABLE_AFTER_S = 300.0

# Close codes the relay sends. 4001 means the token was revoked or replaced, so reconnecting would
# only fight whichever connection replaced this one.
CLOSE_REVOKED = 4001

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
# Headers aiohttp sets itself, or that only apply to the tunnel's own WebSocket.
REQUEST_DROP = HOP_BY_HOP | {"host", "content-length", "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions"}


class TunnelRevoked(Exception):
    """The relay rejected or closed this token for good. Stop reconnecting."""


@dataclass
class _Stream:
    queue: asyncio.Queue[bytes | None] | None = None
    ws: aiohttp.ClientWebSocketResponse | None = None
    task: asyncio.Task[None] | None = None
    handled: set[asyncio.Task[None]] = field(default_factory=set)


def encode_data(stream_id: int, payload: bytes) -> bytes:
    return struct.pack(">I", stream_id) + payload


def decode_data(frame: bytes) -> tuple[int, bytes]:
    (stream_id,) = struct.unpack(">I", frame[:4])
    return stream_id, frame[4:]


def _forward_request_headers(headers: dict[str, Any]) -> dict[str, str | list[str]]:
    out: dict[str, str | list[str]] = {}
    for name, value in headers.items():
        key = name.lower()
        if key in REQUEST_DROP:
            continue
        out[key] = value
    return out


def _response_headers(headers: Any) -> dict[str, str | list[str]]:
    out: dict[str, str | list[str]] = {}
    for name in headers.keys():
        key = name.lower()
        if key in HOP_BY_HOP:
            continue
        values = headers.getall(name)
        out[key] = values[0] if len(values) == 1 else list(values)
    return out


class TunnelClient:
    """One outbound tunnel to the relay, reconnected with backoff for as long as it runs."""

    def __init__(
        self,
        *,
        tunnel_url: str,
        token: str,
        local_base: str,
        session: aiohttp.ClientSession,
        ha_version: str = "",
        client_version: str = "0.1.0",
        on_revoked: Callable[[], Awaitable[None]] | None = None,
        on_inactive: Callable[[], Awaitable[None]] | None = None,
        on_state_change: Callable[[bool], None] | None = None,
    ) -> None:
        self._tunnel_url = tunnel_url
        self._token = token
        self._local_base = local_base.rstrip("/")
        self._session = session
        self._ha_version = ha_version
        self._client_version = client_version
        self._on_revoked = on_revoked
        self._on_inactive = on_inactive
        self._on_state_change = on_state_change
        self._ha: aiohttp.ClientWebSocketResponse | None = None
        self._streams: dict[int, _Stream] = {}
        self._closing = False
        self._connected = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def _set_connected(self, value: bool) -> None:
        if value:
            self._connected.set()
        else:
            self._connected.clear()
        if self._on_state_change:
            self._on_state_change(value)

    async def run(self) -> None:
        """Keeps the tunnel up until close() is called or the token is revoked."""
        backoff = BACKOFF_MIN_S
        loop = asyncio.get_running_loop()
        while not self._closing:
            started = loop.time()
            try:
                await self._serve()
            except TunnelRevoked:
                _LOGGER.warning("MDtronix Cloud tunnel token was revoked; re-link to reconnect")
                if self._on_revoked:
                    await self._on_revoked()
                return
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
                _LOGGER.debug("tunnel dropped: %s", err)
            finally:
                self._set_connected(False)
            if self._closing:
                return
            if loop.time() - started > STABLE_AFTER_S:
                backoff = BACKOFF_MIN_S
            delay = backoff * random.uniform(0.8, 1.2)
            backoff = min(backoff * 2, BACKOFF_MAX_S)
            await asyncio.sleep(delay)

    async def close(self) -> None:
        self._closing = True
        if self._ha is not None:
            await self._ha.close()
        for stream in list(self._streams.values()):
            if stream.task:
                stream.task.cancel()
        self._streams.clear()

    # Connection ------------------------------------------------------------

    async def _serve(self) -> None:
        try:
            ws = await self._session.ws_connect(
                self._tunnel_url,
                headers={"Authorization": f"Bearer {self._token}", "X-MDT-Protocol": str(PROTOCOL_VERSION)},
                heartbeat=None,
                autoclose=False,
            )
        except aiohttp.WSServerHandshakeError as err:
            if err.status == 401:
                raise TunnelRevoked from err
            if err.status == 403 and self._on_inactive:
                # Subscription inactive. Keep retrying with backoff, so the tunnel comes back when access does.
                await self._on_inactive()
            raise

        self._ha = ws
        try:
            await ws.send_str(
                json.dumps(
                    {
                        "t": "hello",
                        "v": PROTOCOL_VERSION,
                        "ha_version": self._ha_version,
                        "client_version": self._client_version,
                    }
                )
            )
            self._set_connected(True)
            await self._receive_loop(ws)
        finally:
            self._ha = None
            await self._reset_streams("tunnel closed")
            if not ws.closed:
                await ws.close()
            if ws.close_code == CLOSE_REVOKED:
                raise TunnelRevoked

    async def _receive_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            msg = await asyncio.wait_for(ws.receive(), timeout=PING_TIMEOUT_S)
            if msg.type == aiohttp.WSMsgType.TEXT:
                await self._on_control(json.loads(msg.data))
            elif msg.type == aiohttp.WSMsgType.BINARY:
                self._on_data(msg.data)
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                return
            elif msg.type == aiohttp.WSMsgType.ERROR:
                raise aiohttp.ClientError(str(ws.exception()))

    async def _send_text(self, obj: dict[str, Any]) -> None:
        if self._ha is not None and not self._ha.closed:
            await self._ha.send_str(json.dumps(obj))

    async def _send_bytes(self, data: bytes) -> None:
        if self._ha is not None and not self._ha.closed:
            await self._ha.send_bytes(data)

    # Control and data ------------------------------------------------------

    async def _on_control(self, msg: dict[str, Any]) -> None:
        kind = msg.get("t")
        if kind == "ready":
            _LOGGER.debug("tunnel ready, max streams %s", msg.get("max_streams"))
        elif kind == "ping":
            await self._send_text({"t": "pong"})
        elif kind == "pong":
            return
        elif kind == "req":
            await self._start_request(msg)
        elif kind == "ws_open":
            await self._start_websocket(msg)
        elif kind == "end":
            stream = self._streams.get(msg["id"])
            if stream and stream.queue is not None:
                await stream.queue.put(None)
        elif kind == "ws_close":
            stream = self._streams.pop(msg["id"], None)
            if stream and stream.ws is not None and not stream.ws.closed:
                await stream.ws.close(code=msg.get("code", 1000), message=msg.get("reason", "").encode())
        elif kind == "reset":
            stream = self._streams.pop(msg["id"], None)
            if stream and stream.task:
                stream.task.cancel()
            if stream and stream.ws is not None and not stream.ws.closed:
                await stream.ws.close(code=1011)

    def _on_data(self, frame: bytes) -> None:
        stream_id, payload = decode_data(frame)
        stream = self._streams.get(stream_id)
        if stream is None:
            return
        if stream.queue is not None:
            stream.queue.put_nowait(payload)
            return
        if stream.ws is not None and payload:
            opcode, data = payload[0], payload[1:]
            if opcode == OP_TEXT:
                asyncio.ensure_future(stream.ws.send_str(data.decode("utf-8", "replace")))
            elif opcode == OP_BINARY:
                asyncio.ensure_future(stream.ws.send_bytes(data))

    async def _reset_streams(self, reason: str) -> None:
        for stream_id, stream in list(self._streams.items()):
            if stream.task:
                stream.task.cancel()
            if stream.ws is not None and not stream.ws.closed:
                await stream.ws.close(code=1011, message=reason.encode())
            self._streams.pop(stream_id, None)

    # HTTP ------------------------------------------------------------------

    async def _start_request(self, msg: dict[str, Any]) -> None:
        stream_id = msg["id"]
        if len(self._streams) >= MAX_STREAMS:
            await self._send_text({"t": "reset", "id": stream_id, "reason": "too many streams"})
            return
        stream = _Stream(queue=asyncio.Queue())
        self._streams[stream_id] = stream
        stream.task = asyncio.create_task(self._proxy_request(stream_id, stream, msg))

    async def _proxy_request(self, stream_id: int, stream: _Stream, msg: dict[str, Any]) -> None:
        assert stream.queue is not None
        try:
            # Wait for the first body chunk, or the end marker. That decides whether the request has a body.
            first = await stream.queue.get()
            body = None if first is None else _body_from(first, stream.queue)
            query = msg.get("query") or ""
            url = f"{self._local_base}{msg['path']}" + (f"?{query}" if query else "")
            async with self._session.request(
                msg["method"],
                url,
                headers=_forward_request_headers(msg.get("headers", {})),
                data=body,
                allow_redirects=False,
                auto_decompress=False,
                timeout=aiohttp.ClientTimeout(total=None, sock_read=300),
            ) as resp:
                await self._send_text(
                    {"t": "res", "id": stream_id, "status": resp.status, "headers": _response_headers(resp.headers)}
                )
                async for chunk in resp.content.iter_chunked(RESPONSE_CHUNK):
                    await self._send_bytes(encode_data(stream_id, chunk))
                await self._send_text({"t": "end", "id": stream_id})
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
            _LOGGER.debug("request %s to home assistant failed: %s", stream_id, err)
            await self._send_text({"t": "reset", "id": stream_id, "reason": "local request failed"})
        finally:
            self._streams.pop(stream_id, None)

    # WebSocket -------------------------------------------------------------

    async def _start_websocket(self, msg: dict[str, Any]) -> None:
        stream_id = msg["id"]
        if len(self._streams) >= MAX_STREAMS:
            await self._send_text({"t": "reset", "id": stream_id, "reason": "too many streams"})
            return
        stream = _Stream()
        self._streams[stream_id] = stream
        stream.task = asyncio.create_task(self._proxy_websocket(stream_id, stream, msg))

    async def _proxy_websocket(self, stream_id: int, stream: _Stream, msg: dict[str, Any]) -> None:
        ws_base = self._local_base.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        query = msg.get("query") or ""
        url = f"{ws_base}{msg['path']}" + (f"?{query}" if query else "")
        try:
            local = await self._session.ws_connect(
                url,
                headers=_forward_request_headers(msg.get("headers", {})),
                heartbeat=None,
                autoclose=False,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            await self._send_text({"t": "reset", "id": stream_id, "reason": "local websocket failed"})
            self._streams.pop(stream_id, None)
            return

        stream.ws = local
        await self._send_text({"t": "ws_accept", "id": stream_id, "protocol": local.protocol or ""})
        try:
            async for item in local:
                if item.type == aiohttp.WSMsgType.TEXT:
                    await self._send_bytes(encode_data(stream_id, bytes([OP_TEXT]) + item.data.encode("utf-8")))
                elif item.type == aiohttp.WSMsgType.BINARY:
                    await self._send_bytes(encode_data(stream_id, bytes([OP_BINARY]) + item.data))
                else:
                    break
            code = local.close_code or 1000
            await self._send_text({"t": "ws_close", "id": stream_id, "code": code, "reason": ""})
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, OSError) as err:
            _LOGGER.debug("websocket %s failed: %s", stream_id, err)
            await self._send_text({"t": "ws_close", "id": stream_id, "code": 1011, "reason": "local websocket failed"})
        finally:
            self._streams.pop(stream_id, None)


async def _body_from(first: bytes, queue: asyncio.Queue[bytes | None]) -> AsyncIterator[bytes]:
    yield first
    while True:
        chunk = await queue.get()
        if chunk is None:
            return
        yield chunk
