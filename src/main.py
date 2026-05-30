import asyncio
import os
import json
import base64
import websockets
from dotenv import load_dotenv
load_dotenv()

def load_config():
    with open("config.json", "r") as f:
        config = json.load(f)
    return config

def sts_connect():
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        raise Exception("DEEPGRAM_API_KEY not set")
    print('deepgram api key:', f"{api_key[0:10]}..." )

    sts_ws = websockets.connect(
        "wss://agent.deepgram.com/v1/agent/converse",
        subprotocols=["token", api_key]
    )
    return sts_ws

async def handle_barge_in(decoded, twilio_ws, streamsid):
    if decoded["type"] == "UserStartedSpeaking":
        clear_message = {
            "event": "clear",
            "streamSid": streamsid
        }
        await twilio_ws.send(json.dumps(clear_message))

async def handle_text_message(decoded, twilio_ws, sts_ws, streamsid):
    await handle_barge_in(decoded, twilio_ws, streamsid)

async def sts_sender(sts_ws, audio_queue):
    print("sts_sender started")
    while True:
        chunk = await audio_queue.get()
        await sts_ws.send(chunk)

async def sts_receiver(sts_ws, twilio_ws,streamsid_queue):
    print("sts_receiver started")
    streamsid = await streamsid_queue.get()
    async for message in sts_ws:
        if type(message) is str:
            print(message)
            decoded = json.loads(message)
            await handle_text_message(decoded, twilio_ws, sts_ws, streamsid)
            continue

        raw_mulaw = message
        media_message = {
            "event":"media",
            "streamSid": streamsid,
            "media":{"payload":base64.b64encode(raw_mulaw).decode("ascii")}
        }

        await twilio_ws.send(json.dumps(media_message))

async def twilio_receiver(twilio_ws, audio_queue, streamsid_queue):
    BUFFER_SIZE = 20 * 160
    inbuffer = bytearray(b"")
    async for message in twilio_ws:
        try:
            data = json.loads(message)
            event = data["event"]
            if event == "start":
                print('get our streamsid')
                start = data["start"]
                streamsid = data["streamSid"]
                streamsid_queue.put_nowait(streamsid)
            elif event == "connect":
                continue
            elif event == "media":
                media = data["media"]
                chunk = base64.b64decode(media["payload"])
                if media["track"] == "inbound":
                    inbuffer.extend(chunk)
            elif event == "stop":
                break

            while len(inbuffer) > BUFFER_SIZE:
                chunk = inbuffer[:BUFFER_SIZE]
                audio_queue.put_nowait(chunk)
                inbuffer = inbuffer[BUFFER_SIZE:]
        except Exception as e:
            print(e)

async def twilio_handler(twilio_ws):
    audio_queue = asyncio.Queue()
    streamsid_queue = asyncio.Queue()

    async with sts_connect() as sts_ws:
        config = load_config()
        await sts_ws.send(json.dumps(config))
        await asyncio.wait([
            asyncio.ensure_future(sts_sender(sts_ws, audio_queue)),
            asyncio.ensure_future(sts_receiver(sts_ws, twilio_ws, streamsid_queue)),
            asyncio.ensure_future(twilio_receiver(twilio_ws, audio_queue, streamsid_queue))
        ])

    await twilio_ws.close()

async def main():
    await websockets.serve(twilio_handler, host="localhost", port=5000)
    print('started server')
    await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
