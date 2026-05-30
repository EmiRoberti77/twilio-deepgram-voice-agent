# OAIX Voice Agent

A WebSocket bridge that connects [Twilio Media Streams](https://www.twilio.com/docs/voice/media-streams) to [Deepgram's Voice Agent API](https://developers.deepgram.com/docs/voice-agent) (Speech-to-Speech). It lets a caller on a regular phone number have a live, spoken conversation with an AI agent.

```
 ☎️  Caller ──PSTN──▶ Twilio ──WebSocket(mulaw)──▶  this bridge  ──WebSocket──▶  Deepgram Voice Agent
                                                  (src/main.py)                 (STT → LLM → TTS)
```

The bridge owns no AI logic of its own — it shuttles audio in both directions and relays a few control events. All speech recognition, reasoning, and speech synthesis happen inside Deepgram, configured by `config.json`.

## Requirements

- Python 3.12 (pinned in `.python-version`)
- [`uv`](https://docs.astral.sh/uv/) for dependency/environment management
- A Deepgram API key with Voice Agent access
- A way to expose `localhost:5000` to the public internet for Twilio (e.g. [`ngrok`](https://ngrok.com/))

## Setup

1. Install dependencies into a local `.venv`:

   ```powershell
   uv sync
   ```

2. Create a `.env` file in the project root with your Deepgram key:

   ```
   DEEPGRAM_API_KEY=your_key_here
   ```

   It is loaded at startup via `python-dotenv`.

## Running

```powershell
uv run python src/main.py
```

This starts a WebSocket server on `ws://localhost:5000` and prints `started server`.

### Connecting Twilio

Twilio cannot reach `localhost`, so expose the port with a tunnel:

```powershell
ngrok http 5000
```

Then point your Twilio number's voice webhook at TwiML that streams to the tunnel's `wss://` URL:

```xml
<Response>
  <Connect>
    <Stream url="wss://<your-ngrok-id>.ngrok.io" />
  </Connect>
</Response>
```

Call the number and the bridge will open a Deepgram session and play the configured greeting.

## How the code works

The entire application is `src/main.py`. The server (`main`) registers `twilio_handler` as the handler for every inbound Twilio WebSocket connection — i.e. once per phone call.

### Per-call setup (`twilio_handler`)

For each call it:

1. Creates two `asyncio.Queue`s:
   - `audio_queue` — inbound caller audio waiting to be sent to Deepgram.
   - `streamsid_queue` — carries the Twilio `streamSid` (sent once) so the receiver knows where to send audio back.
2. Opens the outbound WebSocket to Deepgram (`sts_connect`) at `wss://agent.deepgram.com/v1/agent/converse`, authenticating with the API key via the `token` subprotocol.
3. Sends the contents of `config.json` as the first message — this is the Deepgram `Settings` payload.
4. Runs three coroutines concurrently for the lifetime of the call:

| Coroutine          | Direction          | Responsibility |
|--------------------|--------------------|----------------|
| `twilio_receiver`  | Twilio → bridge    | Reads Twilio frames, captures `streamSid` from the `start` event, base64-decodes inbound `media`, buffers it into fixed-size chunks, and pushes them to `audio_queue`. |
| `sts_sender`       | bridge → Deepgram  | Drains `audio_queue` and forwards raw mulaw bytes to Deepgram. |
| `sts_receiver`     | Deepgram → bridge → Twilio | Reads Deepgram messages: **binary** = agent speech (re-encoded and wrapped in a Twilio `media` event), **text** = JSON control events handled by `handle_text_message`. |

### Audio format and chunking

Twilio Media Streams use **8 kHz μ-law (mulaw)** mono audio, base64-encoded inside JSON `media` events. `config.json` declares the same encoding for both Deepgram input and output:

```json
"audio": {
  "input":  { "encoding": "mulaw", "sample_rate": 8000 },
  "output": { "encoding": "mulaw", "sample_rate": 8000, "container": "none" }
}
```

> ⚠️ **Do not change the audio encoding or sample rate.** Twilio only speaks 8 kHz mulaw; any other format breaks the audio path in both directions.

`twilio_receiver` accumulates inbound audio and only forwards it in `BUFFER_SIZE = 20 * 160 = 3200`-byte chunks. At 8 kHz mulaw (1 byte/sample) that's 20 frames of 20 ms = ~400 ms of audio per chunk, which matches the frame timing Deepgram expects.

### Control events (`handle_text_message`)

Text messages from Deepgram are JSON events. They are printed for visibility and dispatched by `handle_text_message`, which currently only handles barge-in:

- **`UserStartedSpeaking`** → the bridge sends Twilio a `clear` event (`handle_barge_in`), flushing any already-queued agent audio so the caller can interrupt the agent naturally.

Other events Deepgram emits during a healthy call (visible in stdout) include `Welcome`, `SettingsApplied`, `ConversationText`, `History`, `AgentAudioDone`, and `Error`. `handle_text_message` receives `sts_ws` as an argument so future event handlers (e.g. function calling) can reply to Deepgram.

### Typical message flow for one call

```
get our streamsid                         ← Twilio "start" event
{"type":"Welcome", ...}                    ← Deepgram session opened
{"type":"SettingsApplied"}                 ← config.json accepted
{"type":"ConversationText","role":"assistant", ...}   ← greeting text
(binary audio frames)                      ← greeting spoken to caller
{"type":"AgentAudioDone"}                  ← agent finished speaking
{"type":"UserStartedSpeaking"}             ← caller speaks → barge-in clear
{"type":"ConversationText","role":"user", ...}        ← transcribed speech
... conversation continues ...
```

### The agent configuration (`config.json`)

`config.json` is the Deepgram `Settings` message. It defines:

- **`audio`** — mulaw 8 kHz I/O (see above).
- **`agent.listen`** — STT provider (`deepgram` `nova-3`).
- **`agent.think`** — the LLM (`open_ai` `gpt-4o-mini`). This is a Deepgram-managed model, so no extra API key is required.
- **`agent.speak`** — TTS voice (`deepgram` `aura-2-thalia-en`).
- **`agent.prompt`** — the system prompt (a pharmacy assistant).
- **`agent.greeting`** — the first thing the agent says when the call connects.

## Known limitations

- **Function calling is not implemented.** The system prompt references `get_drug_info`, `place_order`, and `lookup_order`, but there are no function definitions in `config.json` and no `FunctionCallRequest` handler in `sts_receiver`. The agent will *narrate* actions ("I'll place your order now…") without anything actually happening. To make these real, add `functions` to the agent config and handle `FunctionCallRequest` in `handle_text_message`, replying with a `FunctionCallResponse`.
- **No KeepAlive / graceful shutdown.** When the caller stops sending audio (e.g. hangs up, or the agent says "please hold" and goes quiet), Deepgram emits `Error … CLIENT_MESSAGE_TIMEOUT` because it received no audio within its window. The bridge does not send `KeepAlive` during silence or proactively close the Deepgram socket when the Twilio `stop` event arrives.
- **Single file, no tests.** There is no lint config, test suite, or build step.

## Project layout

```
src/main.py     Entire application (server + per-call coroutines)
config.json     Deepgram Voice Agent "Settings" payload sent on connect
.env            DEEPGRAM_API_KEY (not committed)
pyproject.toml  Project metadata and dependencies (python-dotenv, websockets)
uv.lock         Locked dependency versions
```
