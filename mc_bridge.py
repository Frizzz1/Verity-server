import asyncio
import os
import websockets


async def handler(websocket):
    remote = getattr(websocket, "remote_address", None)
    path = getattr(websocket, "request", None)
    req_path = getattr(path, "path", getattr(websocket, "path", ""))
    print(f"[MC] CONNECTED from {remote} path={req_path}", flush=True)

    try:
        async for message in websocket:
            print(f"[MC] MSG: {message}", flush=True)
    except websockets.exceptions.ConnectionClosed as exc:
        print(f"[MC] CLOSED code={exc.code} reason={exc.reason!r}", flush=True)
    except Exception as exc:
        print(f"[MC] ERROR {type(exc).__name__}: {exc}", flush=True)


async def main():
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", "8080"))
    print(f"[MC] LISTEN on {host}:{port}", flush=True)

    async with websockets.serve(handler, host, port):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
