#!/usr/bin/env python3
"""Connection relay between k6 and the shared Chrome (used by run-report.sh).

Why: when many virtual users start at the same moment, they all connect to
Chrome's DevTools port at once. Chrome only queues a few new connections, so
the rest fail with "connection reset" / "broken pipe" and the run dies.

This relay listens with a large queue, accepts every connection immediately,
and opens the matching connection to Chrome a few at a time (retrying if
Chrome is momentarily busy). Data is then passed through unchanged, so k6
still runs exactly the requested number of users, all starting together.

Usage: python3 cdp-relay.py <chrome-port>
Prints the relay's own port on the first line of stdout, then serves until
killed. Standard library only.
"""

import asyncio
import sys

UPSTREAM_HOST = "127.0.0.1"
CONNECT_AT_ONCE = 4        # new connections opened to Chrome in parallel
CONNECT_RETRIES = 100      # x RETRY_DELAY = up to ~10 s per connection
RETRY_DELAY = 0.1


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def connect_upstream(port, gate):
    async with gate:
        for _ in range(CONNECT_RETRIES):
            try:
                return await asyncio.open_connection(UPSTREAM_HOST, port)
            except (ConnectionError, OSError):
                await asyncio.sleep(RETRY_DELAY)
    return None, None


async def handle(client_reader, client_writer, port, gate):
    up_reader, up_writer = await connect_upstream(port, gate)
    if up_writer is None:
        client_writer.close()
        return
    await asyncio.gather(
        pipe(client_reader, up_writer),
        pipe(up_reader, client_writer),
    )


async def main(port):
    gate = asyncio.Semaphore(CONNECT_AT_ONCE)
    server = await asyncio.start_server(
        lambda r, w: handle(r, w, port, gate),
        host=UPSTREAM_HOST, port=0, backlog=1024,
    )
    print(server.sockets[0].getsockname()[1], flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        sys.exit("Usage: python3 cdp-relay.py <chrome-port>")
    try:
        asyncio.run(main(int(sys.argv[1])))
    except KeyboardInterrupt:
        pass
