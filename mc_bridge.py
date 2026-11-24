async def on_minecraft(websocket):
    global _last_talk_at
    remote = getattr(websocket, "remote_address", None)
    print(f"[ws] CONNECTED from {remote}", flush=True)

    websocket._verity_pending = {}  # type: ignore[attr-defined]
    websocket._verity_futures = {}  # type: ignore[attr-defined]
    # Отслеживаем активных игроков в этой сессии (для игры вдвоём через Rainway/хост)
    session_busy_players: set[str] = set()
    session_lock = asyncio.Lock()

    talk_poll = None
    hurt_poll = None

    async def _run_player_task(coro, player_key: str) -> None:
        try:
            await coro
        except Exception as exc:  # noqa: BLE001
            print(f"[task] error for {player_key}: {type(exc).__name__}: {exc}", flush=True)
        finally:
            async with session_lock:
                session_busy_players.discard(player_key)

    try:
        await send_cmd(websocket, f"tag @a add {BRIDGE_ON_TAG}")
        await send_cmd(
            websocket,
            'tellraw @a {"rawtext":[{"text":"§7[Verity] §aConnected. §fChat§a to Verity (2P mode)."}]}',
        )

        await websocket.send(
            json.dumps(
                {
                    "header": {
                        "version": 1,
                        "requestId": str(uuid4()),
                        "messageType": "commandRequest",
                        "messagePurpose": "subscribe",
                    },
                    "body": {"eventName": "PlayerMessage"},
                }
            )
        )

        if USE_ADDON_PIPELINE:
            talk_poll = asyncio.create_task(poll_want_talk(websocket))
            hurt_poll = asyncio.create_task(poll_hurt_speak(websocket))

        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[ws] non-json: {raw[:120]!r}", flush=True)
                continue

            header = msg.get("header") or {}
            body = msg.get("body") or {}
            purpose = header.get("messagePurpose")
            req_id = header.get("requestId")

            if purpose == "commandResponse":
                futures = getattr(websocket, "_verity_futures", {})
                if isinstance(futures, dict):
                    waiter = futures.pop(req_id, None)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(body)
                        continue

                pending = getattr(websocket, "_verity_pending", {})
                cmd = pending.pop(req_id, None) if isinstance(pending, dict) else None
                status = body.get("statusCode")
                status_msg = str(body.get("statusMessage") or "")[:160]
                ok = status in (0, None) or int(body.get("successCount") or 0) > 0
                if not ok or (status is not None and int(status) < 0):
                    print(
                        f"[cmd] FAIL status={status} msg={status_msg!r} cmd={cmd!r}",
                        flush=True,
                    )
                continue

            if purpose != "event":
                continue

            props = body.get("properties") or {}
            msg_type = str(body.get("type") or props.get("Type") or "").lower()
            if msg_type and msg_type not in ("chat", "say", ""):
                continue

            text = str(props.get("Message") or body.get("message") or "").strip()
            raw_sender = props.get("Sender") or body.get("sender")
            if not text:
                continue

            player_name = str(raw_sender or "Player").strip() or "Player"
            player_key = player_name.lower()

            low = text.lower()
            # Игнорируем сообщения самого бота/системы, чтобы избежать петель
            if (
                low.startswith("[verity")
                or low.startswith("verity:")
                or text.startswith("<§eVerity")
                or text.startswith("<Verity")
                or player_key == "verity"
            ):
                continue

            print(f"[chat] {player_name}: {text}", flush=True)

            if TALK_RE.match(text):
                await _trigger_talk(websocket, player_name)
                continue

            m = TEXT_RE.match(text)
            if m:
                prompt_text = m.group(1).strip()
                if not prompt_text:
                    continue
                async with session_lock:
                    if player_key in session_busy_players:
                        print(f"[stt] ignore text — {player_name} is busy", flush=True)
                        continue
                    session_busy_players.add(player_key)

                asyncio.create_task(
                    _run_player_task(
                        handle_text(
                            websocket,
                            prompt_text,
                            player_name,
                            echo=True,
                        ),
                        player_key,
                    )
                )
                continue

            if text.startswith("!") or text.startswith("/"):
                continue

            async with session_lock:
                if player_key in session_busy_players:
                    print(f"[stt] ignore chat — {player_name} is busy", flush=True)
                    continue
                session_busy_players.add(player_key)

            print(f"[chat] -> Groq/Fish from {player_name}", flush=True)
            asyncio.create_task(
                _run_player_task(
                    handle_text(websocket, text, player_name, echo=False),
                    player_key,
                )
            )
    except websockets.exceptions.ConnectionClosed as exc:
        print(f"[ws] disconnected: code={exc.code} reason={exc.reason!r}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[ws] connection ended: {type(exc).__name__}: {exc}", flush=True)
    finally:
        session_busy_players.clear()
        for task in (talk_poll, hurt_poll):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
