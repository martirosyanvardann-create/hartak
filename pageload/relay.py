"""Connection relay between k6 and the shared Chrome.

When many virtual users start at the same moment they all connect to
Chrome's DevTools port at once. Chrome only queues a few new connections,
so the rest fail with "connection reset" / "broken pipe" and the run dies.

The relay listens with a large queue, accepts every connection immediately
and opens the matching connection to Chrome a few at a time, retrying while
Chrome is busy. Data then passes through unchanged, so k6 still runs exactly
the requested number of users, all starting together.

It runs an asyncio server in a background thread of the app itself.
"""

from __future__ import annotations

import asyncio
import threading


class ConnectionRelay:
    # One upstream connect at a time: Chrome's queue can then never overflow.
    # (With several at once, an overflowing queue sometimes accepts a
    # connection and resets it right after, which cannot be retried because
    # the client's data is already on its way.) A local connect takes about
    # a millisecond, so even 200 users are connected within a fraction of a second.
    def __init__(self, upstream_port: int, host: str = "127.0.0.1", *,
                 connect_at_once: int = 1, retries: int = 200, retry_delay: float = 0.05):
        self.upstream_port = upstream_port
        self.host = host
        self.connect_at_once = connect_at_once
        self.retries = retries
        self.retry_delay = retry_delay
        self.port: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: asyncio.base_events.Server | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None

    # -- lifecycle -----------------------------------------------------------
    def start(self, timeout: float = 5) -> int:
        """Start listening; returns the relay's port."""
        self._thread = threading.Thread(target=self._serve, name="cdp-relay", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout) or self.port is None:
            raise RuntimeError(f"Connection relay did not start: {self._error}")
        return self.port

    def stop(self) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result(5)
        except Exception:  # noqa: BLE001 - shutting down anyway
            pass
        loop.call_soon_threadsafe(loop.stop)
        if self._thread:
            self._thread.join(5)

    def __enter__(self) -> "ConnectionRelay":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- internals -----------------------------------------------------------
    def _serve(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            gate = asyncio.Semaphore(self.connect_at_once)
            self._server = loop.run_until_complete(asyncio.start_server(
                lambda r, w: self._handle(r, w, gate), host=self.host, port=0, backlog=1024,
            ))
            self.port = self._server.sockets[0].getsockname()[1]
        except BaseException as error:  # noqa: BLE001 - reported by start()
            self._error = error
            self._ready.set()
            loop.close()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            loop.close()

    async def _shutdown(self) -> None:
        if self._server:
            self._server.close()
        current = asyncio.current_task()
        tasks = [task for task in asyncio.all_tasks() if task is not current]
        for task in tasks:
            task.cancel()
        # Let the cancelled connections finish before the loop stops.
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _connect_upstream(self, gate: asyncio.Semaphore):
        async with gate:
            for _ in range(self.retries):
                try:
                    return await asyncio.open_connection(self.host, self.upstream_port)
                except (ConnectionError, OSError):
                    await asyncio.sleep(self.retry_delay)
        return None, None

    async def _handle(self, client_reader, client_writer, gate) -> None:
        up_reader, up_writer = await self._connect_upstream(gate)
        if up_writer is None:
            client_writer.close()
            return
        await asyncio.gather(_pipe(client_reader, up_writer), _pipe(up_reader, client_writer),
                             return_exceptions=True)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass
