import asyncio
import json
import os
from uuid import uuid4
import websockets


async def send_cmd(websocket, command: str) -> dict | None:
    req_id = str(uuid4())
    payload = {
        "header": {
            "version": 1,
            "requestId": req_id,
            "messageType": "commandRequest",
            "messagePurpose": "commandOrigin",
        },
        "body": {
            "version": 1,
            "commandLine": command,
            "origin": {
                "type": "player",
            },
        },
    }
    await websocket.send(json.dumps(payload))
    return None


async def subscribe_chat(websocket) -> None:
    payload = {
        "header": {
            "version": 1,
            "requestId": str(uuid4()),
            "messageType": "commandRequest",
            "messagePurpose": "subscribe",
        },
        "body": {
            "eventName": "PlayerMessage",
        },
    }
    await websocket.send(json.dumps(payload))


async def on_minecraft(websocket):
    remote = getattr(websocket, "remote_address", None)
    print(f"[ws] CONNECTED from {remote}", flush=True)

    try:
        await subscribe_chat(websocket)
        await send_cmd(websocket, 'tellraw @a {"rawtext":[{"text":"§aBridge connected!"}]}')

        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[ws] non-json: {str(raw)[:120]}", flush=True)
                continue

            header = msg.get("header") or {}
            body = msg.get("body") or {}
            purpose = header.get("messagePurpose")

            if purpose == "event":
                props = body.get("properties") or {}
                msg_type = str(body.get("type") or props.get("MessageType") or "")
                text = str(props.get("Message") or body.get("message") or "")
                sender = str(props.get("Sender") or body.get("sender") or "Player")

                if msg_type == "chat" and text:
                    print(f"[chat] {sender}: {text}", flush=True)

    except websockets.exceptions.ConnectionClosed as exc:
        print(f"[ws] disconnected: code={exc.code} reason={exc.reason!r}", flush=True)
    except Exception as exc:
        print(f"[ws] connection ended: {type(exc).__name__}: {exc}", flush=True)
    finally:
        print("[ws] session closed", flush=True)


async def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    print(f"[ws] starting server on {host}:{port}", flush=True)

    async with websockets.serve(on_minecraft, host, port):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
