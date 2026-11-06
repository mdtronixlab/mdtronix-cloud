"""WebSocket size limits and close handling in the tunnel client, against a fake relay and a fake Home Assistant.

tunnel.py imports nothing from Home Assistant, so this needs only aiohttp.
Run from the repo root:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "mdtronix_cloud"))

import tunnel  # noqa: E402
from tunnel import OP_TEXT, TunnelClient, decode_data, encode_data  # noqa: E402

MiB = 1024 * 1024


class TunnelLimitsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # What the fake relay states in "ready". None leaves the field out, like a relay from before it existed.
        self.relay_limit: int | None = 24 * MiB
        self.relay: web.WebSocketResponse | None = None
        self.relay_ready = asyncio.Event()
        self.control: asyncio.Queue[dict] = asyncio.Queue()
        self.data: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue()
        self.ha_sockets: asyncio.Queue[web.WebSocketResponse] = asyncio.Queue()
        self.ha_received: asyncio.Queue[aiohttp.WSMessage] = asyncio.Queue()

        app = web.Application()
        app.router.add_get("/v1/tunnel", self._fake_relay)
        app.router.add_get("/api/websocket", self._fake_ha)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001

        self.session = aiohttp.ClientSession()
        self.client = TunnelClient(
            tunnel_url=f"ws://127.0.0.1:{port}/v1/tunnel",
            token="token",
            local_base=f"http://127.0.0.1:{port}",
            session=self.session,
        )
        self.run_task: asyncio.Task | None = None

    async def asyncTearDown(self) -> None:
        await self.client.close()
        if self.run_task:
            self.run_task.cancel()
        await self.session.close()
        await self.runner.cleanup()

    async def _fake_relay(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=0)
        await ws.prepare(request)
        self.relay = ws
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                self.data.put_nowait(decode_data(msg.data))
                continue
            body = json.loads(msg.data)
            if body["t"] == "hello":
                ready = {"t": "ready", "instance": "test", "max_streams": 64}
                if self.relay_limit is not None:
                    ready["max_ws_message_bytes"] = self.relay_limit
                await ws.send_str(json.dumps(ready))
                self.relay_ready.set()
            else:
                self.control.put_nowait(body)
        return ws

    async def _fake_ha(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=0)
        await ws.prepare(request)
        self.ha_sockets.put_nowait(ws)
        async for msg in ws:
            self.ha_received.put_nowait(msg)
        self.ha_received.put_nowait(aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, ws.close_code, ""))
        return ws

    async def _connect(self) -> None:
        self.run_task = asyncio.create_task(self.client.run())
        await asyncio.wait_for(self.relay_ready.wait(), 5)
        # "ready" is on its way; a ping's pong proves the client has read everything sent before it.
        await self.relay.send_str(json.dumps({"t": "ping"}))
        self.assertEqual((await self._next_control())["t"], "pong")

    async def _next_control(self) -> dict:
        return await asyncio.wait_for(self.control.get(), 10)

    async def _open_stream(self, stream_id: int) -> web.WebSocketResponse:
        await self.relay.send_str(
            json.dumps({"t": "ws_open", "id": stream_id, "path": "/api/websocket", "query": "", "headers": {}})
        )
        accepted = await self._next_control()
        self.assertEqual((accepted["t"], accepted["id"]), ("ws_accept", stream_id))
        return await asyncio.wait_for(self.ha_sockets.get(), 5)

    async def test_home_assistant_message_over_aiohttp_default_is_forwarded_whole(self) -> None:
        await self._connect()
        ha = await self._open_stream(1)
        text = "x✨" * (5 * MiB // 4)
        await ha.send_str(text)

        stream_id, payload = await asyncio.wait_for(self.data.get(), 10)
        self.assertEqual(stream_id, 1)
        self.assertEqual(payload[0], OP_TEXT)
        self.assertEqual(payload[1:].decode("utf-8"), text)

    async def test_message_over_the_limit_ends_only_its_own_stream(self) -> None:
        self.relay_limit = 1 * MiB
        await self._connect()
        ha = await self._open_stream(1)
        with self.assertLogs("tunnel", level="WARNING") as logs:
            await ha.send_str("x" * (2 * MiB))
            closed = await self._next_control()

        self.assertEqual((closed["t"], closed["id"], closed["code"]), ("ws_close", 1, 1009))
        self.assertIn(str(2 * MiB), closed["reason"])
        self.assertLessEqual(len(closed["reason"].encode()), 123)
        self.assertIn("hop: Home Assistant to tunnel client", logs.output[0])
        self.assertIn(str(2 * MiB), logs.output[0])

        self.assertTrue(self.client.connected)
        other = await self._open_stream(2)
        await other.send_str("still here")
        self.assertEqual((await asyncio.wait_for(self.data.get(), 5))[0], 2)

    async def test_browser_message_over_aiohttp_default_keeps_the_tunnel_up(self) -> None:
        await self._connect()
        await self._open_stream(1)
        text = "y" * (5 * MiB)
        await self.relay.send_bytes(encode_data(1, bytes([OP_TEXT]) + text.encode()))

        received = await asyncio.wait_for(self.ha_received.get(), 10)
        self.assertEqual(received.data, text)
        self.assertTrue(self.client.connected)

    async def test_home_assistant_close_code_and_reason_are_passed_on(self) -> None:
        await self._connect()
        ha = await self._open_stream(1)
        await ha.close(code=4321, message=b"reason from ha")

        closed = await self._next_control()
        self.assertEqual((closed["t"], closed["code"], closed["reason"]), ("ws_close", 4321, "reason from ha"))

    async def test_home_assistant_1009_is_logged_with_the_size_sent(self) -> None:
        await self._connect()
        ha = await self._open_stream(1)
        await self.relay.send_bytes(encode_data(1, bytes([OP_TEXT]) + b"z" * 1000))
        await asyncio.wait_for(self.ha_received.get(), 5)

        with self.assertLogs("tunnel", level="WARNING") as logs:
            await ha.close(code=1009, message=b"too big")
            closed = await self._next_control()

        self.assertEqual((closed["code"], closed["reason"]), (1009, "too big"))
        self.assertIn("hop: tunnel client to Home Assistant", logs.output[0])
        self.assertIn("1000 bytes", logs.output[0])

    async def test_browser_close_with_an_unsendable_code_still_closes_home_assistant(self) -> None:
        await self._connect()
        await self._open_stream(1)
        await self.relay.send_str(json.dumps({"t": "ws_close", "id": 1, "code": 1006, "reason": ""}))

        closed = await asyncio.wait_for(self.ha_received.get(), 5)
        self.assertEqual(closed.data, 1011)


class LimitHelpersTest(unittest.TestCase):
    def test_relay_limit_is_honoured_up_to_what_cloudflare_accepts(self) -> None:
        self.assertEqual(tunnel._ws_message_limit(8 * MiB), 8 * MiB)  # noqa: SLF001
        self.assertEqual(tunnel._ws_message_limit(64 * MiB), 32 * MiB - 5)  # noqa: SLF001

    def test_relay_without_a_usable_limit_gets_the_default(self) -> None:
        for value in (None, 0, -1, "24"):
            self.assertEqual(tunnel._ws_message_limit(value), 24 * MiB)  # noqa: SLF001

    def test_tunnel_socket_accepts_the_largest_frame_a_browser_can_cause(self) -> None:
        self.assertGreater(tunnel.MAX_TUNNEL_FRAME_BYTES, 32 * MiB + 5)

    def test_close_codes_that_cant_be_sent_are_mapped(self) -> None:
        for code, expected in ((1000, 1000), (1009, 1009), (4321, 4321), (1005, 1000), (1006, 1011), (1015, 1011)):
            self.assertEqual(tunnel._wire_close_code(code), expected)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
