# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A WebSocket bridge between Twilio Media Streams and Deepgram's Speech-to-Speech (STS) Voice Agent API (`wss://sts.deepgram.com/v1/agent/converse`). The single entry point in `src/main.py` runs a local WebSocket server that Twilio's `<Stream>` TwiML verb connects to; the server then opens a second WebSocket to Deepgram and shuttles audio between them.

## Commands

This project uses `uv` for dependency and environment management (Python 3.12, see `.python-version`).

```powershell
uv sync                          # install deps from uv.lock into .venv
uv run python src/main.py        # start the bridge server on ws://localhost:5000
```

There are no tests, lint config, or build steps in the repo.

`DEEPGRAM_API_KEY` must be set (read from `.env` via python-dotenv).

## Architecture

`src/main.py` is the entire application. `twilio_handler` is invoked per inbound Twilio WebSocket connection and spins up three concurrent coroutines coordinated by two `asyncio.Queue`s:

- `twilio_receiver` — reads Twilio frames, extracts `streamSid` from the `start` event (pushed once into `streamsid_queue`), base64-decodes inbound `media` events, and batches mulaw audio into `BUFFER_SIZE = 20 * 160` byte chunks before pushing to `audio_queue`. The 20×160 chunking matches 8kHz mulaw frame timing expected by Deepgram.
- `sts_sender` — drains `audio_queue` and forwards raw audio bytes to Deepgram.
- `sts_receiver` — receives from Deepgram; binary messages are mulaw audio that get base64-re-encoded and wrapped in a Twilio `media` event, text messages are JSON control events. `UserStartedSpeaking` triggers a Twilio `clear` event to flush queued playback (barge-in handling).

The Deepgram agent is configured by sending the contents of `config.json` as the first message on the STS WebSocket. `config.json` declares mulaw 8kHz audio I/O (required for Twilio compatibility), the listen/think/speak providers, system prompt, and greeting. **Audio encoding must remain `mulaw` at `8000` Hz** — changing it breaks the Twilio path.

The system prompt in `config.json` references `get_drug_info`, `place_order`, and `lookup_order` functions, but **no function-calling handlers are implemented** in `main.py`. The agent will hallucinate these calls unless function definitions are added to `config.json` and corresponding `FunctionCallRequest` handling is added to `sts_receiver`/`handle_text_message`.

## Notes

- The server binds to `localhost:5000`; exposing it to Twilio typically requires an `ngrok` tunnel.
- `handle_text_message` currently only dispatches to `handle_barge_in`. New Deepgram event types (e.g. `FunctionCallRequest`, `AgentAudioDone`) should be added there.
- `streamsid_queue` is consumed once by `sts_receiver` before its `async for` loop, so the `start` event must arrive before any STS audio responses — this works because Twilio always sends `start` first.
