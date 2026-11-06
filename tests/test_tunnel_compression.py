"""Compressed responses through the tunnel, against a fake relay and a fake Home Assistant.

A body stays compressed only when the relay states it can pass one on. Run from the repo root:
python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "mdtronix_cloud"))

from tunnel import CAP_CONTENT_ENCODING, TunnelClient, decode_data  # noqa: E402

BUNDLE = b"function card(){return 'hello'}\n" * 20000
JSON_BODY = b'{"state": "on"}'


class TunnelCompressionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.relay_caps: list[str] | None = [CAP_CONTENT_ENCODING]
        self.relay: web.WebSocketResponse | None = None
        self.relay_ready = asyncio.Event()
        self.control: asyncio.Queue[dict] = asyncio.Queue()
        self.data: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue()
        self.ha_saw: list[str | None] = []
        self.next_id = 1

        self.assets = tempfile.TemporaryDirectory()
        folder = Path(self.assets.name)
        (folder / "app.js").write_bytes(BUNDLE)
        (folder / "app.js.gz").write_bytes(gzip.compress(BUNDLE))

        app = web.Application()
        app.router.add_get("/v1/tunnel", self._fake_relay)
        app.router.add_get("/frontend_latest/app.js", self._file)
        app.router.add_get("/api/plain", self._plain)
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
        self.assets.cleanup()

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
                if self.relay_caps is not None:
                    ready["caps"] = self.relay_caps
                await ws.send_str(json.dumps(ready))
                self.relay_ready.set()
            else:
                self.control.put_nowait(body)
        return ws

    async def _file(self, request: web.Request) -> web.FileResponse:
        self.ha_saw.append(request.headers.get("Accept-Encoding"))
        return web.FileResponse(Path(self.assets.name) / "app.js")

    async def _plain(self, request: web.Request) -> web.Response:
        self.ha_saw.append(request.headers.get("Accept-Encoding"))
        return web.Response(body=JSON_BODY, content_type="application/json")

    async def _connect(self) -> None:
        self.run_task = asyncio.create_task(self.client.run())
        await asyncio.wait_for(self.relay_ready.wait(), 5)
        await self.relay.send_str(json.dumps({"t": "ping"}))
        self.assertEqual((await asyncio.wait_for(self.control.get(), 5))["t"], "pong")

    async def _fetch(self, path: str, accept_encoding: str | None) -> tuple[dict, bytes]:
        stream_id = self.next_id
        self.next_id += 1
        headers = {} if accept_encoding is None else {"accept-encoding": accept_encoding}
        await self.relay.send_str(
            json.dumps({"t": "req", "id": stream_id, "method": "GET", "path": path, "query": "", "headers": headers})
        )
        await self.relay.send_str(json.dumps({"t": "end", "id": stream_id}))

        head = await asyncio.wait_for(self.control.get(), 10)
        self.assertEqual((head["t"], head["id"], head["status"]), ("res", stream_id, 200))
        end = await asyncio.wait_for(self.control.get(), 10)
        self.assertEqual((end["t"], end["id"]), ("end", stream_id))
        body = b""
        while not self.data.empty():
            frame_id, payload = self.data.get_nowait()
            self.assertEqual(frame_id, stream_id)
            body += payload
        return head["headers"], body

    async def test_compressed_file_crosses_the_tunnel_unchanged(self) -> None:
        await self._connect()
        headers, body = await self._fetch("/frontend_latest/app.js", "gzip, br")

        self.assertEqual(self.ha_saw, ["gzip"])
        self.assertEqual(headers["content-encoding"], "gzip")
        self.assertNotIn("content-length", headers)
        self.assertLess(len(body), len(BUNDLE) / 3)
        self.assertEqual(gzip.decompress(body), BUNDLE)

    async def test_a_response_home_assistant_did_not_compress_is_left_alone(self) -> None:
        await self._connect()
        headers, body = await self._fetch("/api/plain", "gzip, br")

        self.assertNotIn("content-encoding", headers)
        self.assertEqual(body, JSON_BODY)

    async def test_relay_without_the_capability_gets_a_plain_body(self) -> None:
        self.relay_caps = None
        await self._connect()
        headers, body = await self._fetch("/frontend_latest/app.js", "gzip, br")

        self.assertNotIn("content-encoding", headers)
        self.assertEqual(body, BUNDLE)

    async def test_client_that_does_not_take_gzip_gets_a_plain_body(self) -> None:
        await self._connect()
        for accept_encoding in ("identity", "br", None):
            headers, body = await self._fetch("/frontend_latest/app.js", accept_encoding)
            self.assertNotIn("content-encoding", headers, accept_encoding)
            self.assertEqual(body, BUNDLE, accept_encoding)


if __name__ == "__main__":
    unittest.main()
