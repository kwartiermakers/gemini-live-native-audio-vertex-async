"""
Minimal Gemini Live native-audio demo with non-blocking function calls (Vertex AI).

Four tools, one per scheduling mode:
  - get_current_time   : SYNCHRONOUS, awaited inline.
  - log_preference     : ASYNC, scheduling=SILENT     (model stays quiet).
  - search_flights     : ASYNC, scheduling=WHEN_IDLE  (announces during a pause).
  - urgent_alert       : ASYNC, scheduling=INTERRUPT  (cuts in immediately).

Speak Dutch or English; the model auto-matches.

Setup:
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  # macOS portaudio for sounddevice:
  #   brew install portaudio
  gcloud auth application-default login   # if not already done
  python main.py
"""

import asyncio
import sys
import time
from datetime import datetime

import numpy as np
import sounddevice as sd

from google import genai
from google.genai import types


# Replace with your own Google Cloud project / region before running.
PROJECT = "your-gcp-project-id"
LOCATION = "your-gcp-region"
MODEL = "gemini-live-2.5-flash-native-audio"

INPUT_SR = 16000
OUTPUT_SR = 24000
INPUT_CHUNK = int(INPUT_SR * 0.02)  # 20 ms


_START = time.monotonic()


def log(msg):
    """Print msg prefixed with seconds-since-start, e.g. '+  3.412s [bot] ...'."""
    elapsed = time.monotonic() - _START
    print(f"+{elapsed:7.3f}s {msg}", flush=True)


# ---------- Tool declarations ----------

TOOLS = [{
    "function_declarations": [
        {
            "name": "get_current_time",
            "description": "Return the current local time. Instant, synchronous.",
            "parameters": {"type": "OBJECT", "properties": {}},
        },
        {
            "name": "log_preference",
            "description": (
                "Silently store a user preference in the background (~3s). "
                "Do NOT verbally announce when this finishes."
            ),
            "parameters": {
                "type": "OBJECT",
                "properties": {"preference": {"type": "STRING"}},
                "required": ["preference"],
            },
        },
        {
            "name": "search_flights",
            "description": (
                "Search for flights to a destination (~5s). "
                "Result will be announced when the conversation is idle."
            ),
            "parameters": {
                "type": "OBJECT",
                "properties": {"destination": {"type": "STRING"}},
                "required": ["destination"],
            },
        },
        {
            "name": "urgent_alert",
            "description": (
                "Schedule an urgent alert that will interrupt the user. "
                "ONLY call this when the user has EXPLICITLY asked for an "
                "alert, reminder, or notification in their most recent "
                "utterance (e.g. 'set an alert', 'remind me', 'maak een "
                "waarschuwing'). NEVER call this proactively, NEVER infer "
                "it from topics like weather or news, and NEVER fire it on "
                "your own initiative. The 'message' argument must be the "
                "exact text the user asked you to alert with."
            ),
            "parameters": {
                "type": "OBJECT",
                "properties": {"message": {"type": "STRING"}},
                "required": ["message"],
            },
        },
    ]
}]


SYSTEM_INSTRUCTION = """You are a bilingual voice assistant. Match the user's
language (Dutch or English). Keep replies short and natural.

You have four tools, each with a different execution behavior. Follow the rules
for each tool EXACTLY — they are not suggestions.

==============================
get_current_time  (synchronous)
==============================
Instant lookup. Just speak the time. Nothing fancy.

==============================
log_preference    (SILENT EXECUTION)
==============================
When you call log_preference:
  1. Say ONE very short acknowledgement at the moment of the call
     (e.g. "Goed." / "Got it." / "Oké."). Maximum one short sentence.
  2. After that, perform a SILENT EXECUTION: say absolutely NOTHING about
     log_preference ever again in this session. Do not confirm it was saved,
     do not recite the preference back, do not narrate the tool finishing,
     do not summarise it. The preference is classified — logged silently.
  3. When the tool's response arrives, you will see it in your context.
     Do NOT generate any audio in reaction to that response. Stay silent
     and wait for the next user turn.
  4. You MAY use the saved preference later if the user explicitly asks
     ("what did I tell you to remember?"), but never proactively.

==============================
search_flights    (WHEN_IDLE, ~5s)
==============================
When you call search_flights, briefly tell the user you are searching and
continue the conversation normally. When the result arrives during a pause,
recite the flight numbers and prices EXACTLY as returned by the tool. Do
not paraphrase, do not invent flights, do not claim to be searching again.
If the tool has not yet returned, do not pretend to have results.

==============================
urgent_alert      (INTERRUPT, ~8s)
==============================
This tool is FORBIDDEN to call unless the user's MOST RECENT utterance
literally contains one of these trigger words/phrases:
  English: "alert", "alarm", "remind me", "reminder", "notification",
           "notify me"
  Dutch:   "waarschuwing", "alarm", "herinner mij", "herinnering",
           "melding"

If NONE of these words appears in the user's last sentence, DO NOT call
urgent_alert. Period. No exceptions. Talking about weather, jokes, news,
flights, milk, lunch, or any other topic does NOT justify firing it. The
client will hard-block any unauthorized call and you will look foolish.
If in doubt, do NOT call it.

When you DO call urgent_alert (because the user asked for one), briefly
acknowledge and continue chatting. When the alert fires it will cut in
automatically — at that moment, deliver the alert message verbatim, then
stop.

==============================
General
==============================
Encourage the user to ask another question right after a slow tool starts,
so they can experience the SILENT / WHEN_IDLE / INTERRUPT behavior.
"""


# ---------- Tool implementations ----------

def get_current_time_impl():
    return {"time": datetime.now().strftime("%H:%M:%S")}


async def log_preference_impl(preference: str):
    await asyncio.sleep(3)
    log(f"[tool] log_preference DONE preference={preference!r}")
    return {"status": "logged", "preference": preference}


async def search_flights_impl(destination: str):
    await asyncio.sleep(5)
    log(f"[tool] search_flights DONE destination={destination!r}")
    return {
        "status": "complete",
        "destination": destination,
        "results": [
            {"flight": "KL1234", "price_eur": 189},
            {"flight": "AF5678", "price_eur": 215},
        ],
        "assistant_instructions": (
            "These are the FINAL flight results. Announce them now to the "
            "user, verbatim with each flight number and price. Do NOT call "
            "search_flights again — these results are authoritative."
        ),
    }


async def urgent_alert_impl(message: str):
    # Slightly longer than search_flights so the model has time to actually
    # launch into the requested follow-up monologue before the alert fires —
    # otherwise there is nothing for INTERRUPT to interrupt.
    await asyncio.sleep(8)
    log(f"[tool] urgent_alert DONE message={message!r}")
    return {
        "status": "delivered",
        "message": message,
        "assistant_instructions": (
            "The alert is now ready. Deliver the message text verbatim to "
            "the user immediately, then stop. Do NOT call urgent_alert again."
        ),
    }


async def send_response(session, call_id, name, result, scheduling=None,
                        update_recent=True):
    """Send a single FunctionResponse, logging any failure.

    update_recent=False keeps the dedup TTL anchored at the FIRST real
    response, so duplicate no-op replies don't keep extending the window.
    """
    try:
        kwargs = {"id": call_id, "name": name, "response": result}
        if scheduling is not None:
            kwargs["scheduling"] = scheduling
        fr = types.FunctionResponse(**kwargs)
        await session.send_tool_response(function_responses=[fr])
        if update_recent:
            RECENT_RESPONSE[name] = time.monotonic()
        log(f"[resp] {name} sent (scheduling={scheduling})")
    except Exception as e:
        log(f"[!!] send_tool_response({name}) failed: {e!r}")
        raise


# Track in-flight async tool calls per tool name so we can drop duplicate
# function_calls the model sometimes emits while waiting for the first response.
# Pattern recommended by the Live API "Handle duplicate function calls" docs.
PENDING: dict[str, int] = {}

# Also track when the most recent response was sent, so we can drop duplicate
# calls that arrive shortly AFTER a response (model occasionally re-issues the
# same call once the first response lands in its context).
RECENT_RESPONSE: dict[str, float] = {}
DEDUP_TTL_SEC = 10.0


# Rolling buffer of recent user input transcript chunks. Used as a client-side
# guardrail: urgent_alert is only allowed when the user actually asked for one.
RECENT_USER_TEXT: list[str] = [""]


# Timestamp of the last time the model produced audio (or output transcription).
# Used by the announce-follow-up nudge so we don't barge in mid-sentence.
MODEL_LAST_SPOKE: list[float] = [0.0]


URGENT_ALERT_TRIGGERS = (
    "alert", "alarm",
    "remind me", "reminder", "notification", "notify",
    "waarschuwing", "herinner", "herinnering", "melding",
)


def user_recently_requested_alert() -> bool:
    txt = RECENT_USER_TEXT[0].lower()
    return any(t in txt for t in URGENT_ALERT_TRIGGERS)


GUARD_WAIT_SEC = 2.0  # how long to wait for input transcription to catch up


async def guarded_urgent_alert(session, fc, args):
    """Dispatch urgent_alert only if the user's recent transcript contains a
    trigger word. The model can call this tool BEFORE the input transcription
    finalises (it understands audio faster than the transcription pipeline),
    so we briefly poll the transcript buffer before deciding to block."""
    deadline = time.monotonic() + GUARD_WAIT_SEC
    while time.monotonic() < deadline and not user_recently_requested_alert():
        await asyncio.sleep(0.1)

    if not user_recently_requested_alert():
        log(
            f"[guard] urgent_alert BLOCKED — no trigger word within "
            f"{GUARD_WAIT_SEC:.1f}s of call: "
            f"{RECENT_USER_TEXT[0][-120:]!r}"
        )
        asyncio.create_task(send_response(
            session, fc.id, fc.name,
            {"status": "denied",
             "reason": "user did not request an alert"},
            scheduling="SILENT",
        ))
        return

    log(f"[guard] urgent_alert ALLOWED — trigger found")
    asyncio.create_task(nudge_model(session, fc.name))
    asyncio.create_task(run_async_tool(
        session, fc.id, fc.name,
        urgent_alert_impl(args.get("message", "")),
        scheduling="INTERRUPT",
    ))


async def run_async_tool(session, call_id, name, coro, scheduling):
    """Await the tool, then return the FunctionResponse with a scheduling policy."""
    PENDING[name] = PENDING.get(name, 0) + 1
    log(f"[bg] {name} started (scheduling={scheduling}, pending={PENDING[name]})")
    try:
        try:
            result = await coro
        except Exception as e:
            log(f"[!!] tool {name} raised: {e!r}")
            return
        await send_response(session, call_id, name, result, scheduling)
        # For tools where we want the model to verbally announce the result
        # (WHEN_IDLE, INTERRUPT), send a follow-up text nudge that explicitly
        # tells it to speak — relying on the response payload alone is not
        # reliable with native audio.
        if scheduling != "SILENT":
            asyncio.create_task(announce_after_response(session, name))
    finally:
        PENDING[name] = max(0, PENDING.get(name, 0) - 1)


# Nudges sent to the model right after we receive a function_call, so the model
# verbally acknowledges instead of going silent for several seconds while the
# background task runs. Pattern recommended by the Live API "Manage user
# expectations" docs.
NUDGES = {
    "log_preference": (
        "(system) The preference is now being logged silently in the background. "
        "Say ONE very short acknowledgement in the user's language "
        "(NL: 'Oké, genoteerd.' / EN: 'Got it.') and then STAY SILENT. "
        "Do NOT name or repeat what was logged. Do not say anything else."
    ),
    "search_flights": (
        "(system) The flight search is now running in the background. Repeat "
        "this sentence in the user's language right now: "
        "NL: 'Ik zoek de vluchten even voor je op, een momentje.' "
        "EN: 'Let me look those flights up for you, one moment.' "
        "Then IMMEDIATELY launch into a long, multi-sentence monologue to "
        "fill the time — anything relevant works (a story, an interesting "
        "fact about the destination, a tangent the user touched on, etc.). "
        "Talk continuously for at least 10 seconds. Do NOT ask clarifying "
        "questions, do NOT hand the turn back to the user. When the search "
        "result arrives later, recite the flight numbers and prices exactly "
        "as returned by the tool."
    ),
    "urgent_alert": (
        "(system) The alert is now being scheduled in the background. "
        "Acknowledge in ONE very short sentence in the user's language "
        "(NL: 'Oké, ingesteld.' / EN: 'OK, queued.') and then IMMEDIATELY "
        "fulfill the OTHER request the user made in their PREVIOUS audio turn "
        "(typically: a long detailed monologue about a topic like weather, "
        "news, or a story). Be VERY long-winded — keep talking continuously "
        "for at least 15 seconds without pausing. Do NOT ask clarifying "
        "questions, do NOT hand the turn back to the user, do NOT go silent. "
        "The alert will fire on its own in a few seconds and cut you off "
        "mid-sentence."
    ),
}


# Follow-up nudges sent AFTER the FunctionResponse for tools where we want the
# model to verbally announce the result (WHEN_IDLE, INTERRUPT). The model
# sometimes ignores the response or re-calls the tool; an explicit text turn
# saying "announce now" reliably forces it to speak.
ANNOUNCE_NUDGES = {
    "search_flights": (
        "(system) Your search_flights tool just returned its FINAL result "
        "and the flight numbers and prices are now in your context. "
        "Announce them to the user RIGHT NOW, verbatim, in this format: "
        "NL: 'Vlucht <code> kost <prijs> euro, en vlucht <code> kost "
        "<prijs> euro.' / EN: 'Flight <code> costs €<price>, and flight "
        "<code> costs €<price>.' Do NOT call search_flights again. Do NOT "
        "ask clarifying questions. Just announce the results now."
    ),
    "urgent_alert": (
        "(system) Your urgent_alert tool just returned. Deliver the alert "
        "message text to the user RIGHT NOW, verbatim, then stop. Do NOT "
        "call urgent_alert again. Do NOT add commentary."
    ),
}


async def announce_after_response(session, name):
    """Fallback nudge — only fires if the model FAILED to announce the
    FunctionResponse on its own. Two-stage wait:
      1. Wait for the model to be quiet for `required_quiet` seconds (don't
         barge in mid-monologue from a previous turn).
      2. Then wait `grace` seconds for the model to start a new turn on its
         own (responding to the FunctionResponse). If it does, we skip the
         nudge — it's announcing naturally, no need to duplicate.
    """
    text = ANNOUNCE_NUDGES.get(name)
    if not text:
        return

    required_quiet = 1.5
    grace = 2.5
    max_wait_quiet = 15.0

    # Stage 1: wait until the model has been quiet for `required_quiet`.
    deadline = time.monotonic() + max_wait_quiet
    while time.monotonic() < deadline:
        if time.monotonic() - MODEL_LAST_SPOKE[0] >= required_quiet:
            break
        await asyncio.sleep(0.1)
    else:
        log(f"[!!] announce timeout for {name} (model never went quiet)")
        return

    # Stage 2: wait `grace` seconds to see if the model speaks on its own.
    last_quiet_marker = MODEL_LAST_SPOKE[0]
    grace_deadline = time.monotonic() + grace
    while time.monotonic() < grace_deadline:
        await asyncio.sleep(0.1)
        if MODEL_LAST_SPOKE[0] > last_quiet_marker:
            log(f"[announce] model started speaking on its own — "
                f"skipping nudge for {name}")
            return

    try:
        await session.send_client_content(
            turns=[types.Content(role="user", parts=[types.Part(text=text)])],
            turn_complete=True,
        )
        log(f"[announce] fallback nudge sent for {name} "
            f"(model stayed silent for {grace}s after response)")
    except Exception as e:
        log(f"[!!] announce nudge for {name} failed: {e!r}")


async def nudge_model(session, tool_name):
    """Send a short instruction to the model so it acknowledges the tool call
    verbally instead of staying silent during the background task. We append
    the user's recent words so the model has the original request right next
    to the instruction to fulfill it."""
    text = NUDGES.get(tool_name)
    if not text:
        return
    user_context = RECENT_USER_TEXT[0].strip()
    if user_context:
        text += (
            f"\n\nFor reference, the user just said (verbatim): "
            f"\"{user_context}\". Make sure you fulfill ALL parts of that "
            f"request, not just the part that triggered the tool call."
        )
    try:
        await session.send_client_content(
            turns=[types.Content(role="user", parts=[types.Part(text=text)])],
            turn_complete=True,
        )
        log(f"[nudge] sent for {tool_name}")
    except Exception as e:
        log(f"[!!] nudge for {tool_name} failed: {e!r}")


# ---------- Audio I/O ----------

def make_input_stream(loop, mic_queue):
    def cb(indata, frames, time_info, status):
        if status:
            log(f"[mic] {status}")
        pcm = (indata[:, 0] * 32767).astype(np.int16).tobytes()
        loop.call_soon_threadsafe(mic_queue.put_nowait, pcm)
    return sd.InputStream(
        samplerate=INPUT_SR,
        channels=1,
        dtype="float32",
        blocksize=INPUT_CHUNK,
        callback=cb,
    )


async def mic_to_session(session, mic_queue):
    while True:
        chunk = await mic_queue.get()
        await session.send_realtime_input(
            audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
        )


async def play_audio(out_queue):
    stream = sd.OutputStream(
        samplerate=OUTPUT_SR, channels=1, dtype="int16"
    )
    stream.start()
    try:
        while True:
            chunk = await out_queue.get()
            if chunk is None:
                continue
            audio = np.frombuffer(chunk, dtype=np.int16).reshape(-1, 1)
            await asyncio.to_thread(stream.write, audio)
    finally:
        stream.stop()
        stream.close()


# ---------- Main receive loop ----------

async def handle_response(session, response, out_queue):
    if response.data:
        await out_queue.put(response.data)
        MODEL_LAST_SPOKE[0] = time.monotonic()

    sc = response.server_content
    if sc:
        if sc.input_transcription and sc.input_transcription.text:
            log(f"[you ] {sc.input_transcription.text}")
            RECENT_USER_TEXT[0] = (RECENT_USER_TEXT[0] + " " + sc.input_transcription.text)[-500:]
        if sc.output_transcription and sc.output_transcription.text:
            log(f"[bot ] {sc.output_transcription.text}")
            MODEL_LAST_SPOKE[0] = time.monotonic()
        if sc.interrupted:
            drained = 0
            while not out_queue.empty():
                out_queue.get_nowait()
                drained += 1
            log(f"[bot ] <interrupted; dropped {drained} chunks>")
        if sc.turn_complete:
            log("[bot ] <turn complete>")

    if response.tool_call:
        for fc in response.tool_call.function_calls:
            args = dict(fc.args) if fc.args else {}
            log(f"[call] id={fc.id} name={fc.name} args={args}")

            # Drop duplicates: in-flight, OR within the dedup TTL of the
            # last response. Async tools often get re-issued by the model
            # right after it sees the first response in its context.
            if PENDING.get(fc.name, 0) > 0:
                log(f"[skip] duplicate {fc.name} — already pending; ignoring")
                continue
            since_resp = time.monotonic() - RECENT_RESPONSE.get(fc.name, 0.0)
            if since_resp < DEDUP_TTL_SEC:
                log(f"[skip] duplicate {fc.name} — only {since_resp:.1f}s since "
                    f"last response (TTL {DEDUP_TTL_SEC}s); SILENT no-op back")
                # Close the call with a SILENT no-op so the model isn't left
                # waiting on a response it's silently expecting. Don't refresh
                # the dedup TTL — anchor it to the FIRST response.
                asyncio.create_task(send_response(
                    session, fc.id, fc.name,
                    {"status": "duplicate_call_ignored",
                     "assistant_instructions": (
                         "Previous result for this tool is already in your "
                         "context — use that. Do not announce anything new "
                         "for this duplicate call."
                     )},
                    scheduling="SILENT",
                    update_recent=False,
                ))
                continue

            # Dispatch every tool reply on a background task so the receive
            # loop is never blocked on an outbound websocket write.
            if fc.name == "get_current_time":
                result = get_current_time_impl()
                asyncio.create_task(
                    send_response(session, fc.id, fc.name, result)
                )
            elif fc.name == "log_preference":
                asyncio.create_task(run_async_tool(
                    session, fc.id, fc.name,
                    log_preference_impl(args.get("preference", "")),
                    scheduling="SILENT",
                ))
                asyncio.create_task(nudge_model(session, fc.name))
            elif fc.name == "search_flights":
                asyncio.create_task(run_async_tool(
                    session, fc.id, fc.name,
                    search_flights_impl(args.get("destination", "")),
                    scheduling="WHEN_IDLE",
                ))
                asyncio.create_task(nudge_model(session, fc.name))
            elif fc.name == "urgent_alert":
                # Guard runs in background so it can wait for the input
                # transcription to catch up to the model's tool call.
                asyncio.create_task(guarded_urgent_alert(session, fc, args))
            else:
                log(f"[!!] unknown tool: {fc.name}")

    if response.go_away is not None:
        log(f"[srv ] go_away time_left={response.go_away.time_left}")


async def receive_loop(session, out_queue):
    # session.receive() yields one turn at a time and returns on turn_complete.
    # Re-enter it to listen for the next turn (the websocket stays open).
    while True:
        async for response in session.receive():
            try:
                await handle_response(session, response, out_queue)
            except Exception as e:
                log(f"[!!] receive iteration error: {e!r}")


async def main():
    client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=types.Content(parts=[types.Part(text=SYSTEM_INSTRUCTION)]),
        tools=TOOLS,
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )

    print(f"Connecting to {MODEL} via Vertex ({PROJECT} / {LOCATION})…")
    async with client.aio.live.connect(model=MODEL, config=config) as session:
        print("Connected. Speak Dutch or English. Ctrl+C to quit.\n"
              "Try: 'Wat is het nu voor tijd?', 'Onthoud dat ik van koffie hou',\n"
              "     'Search flights to Tokyo, then tell me a joke',\n"
              "     'Schedule an urgent alert that says lunch is ready,'\n"
              "     'then talk about the weather for a while.'")

        loop = asyncio.get_running_loop()
        mic_queue: asyncio.Queue = asyncio.Queue()
        out_queue: asyncio.Queue = asyncio.Queue()

        async def supervised(name, coro):
            try:
                await coro
            except Exception as e:
                log(f"[!!] task {name!r} died: {e!r}")
                raise

        with make_input_stream(loop, mic_queue):
            await asyncio.gather(
                supervised("mic", mic_to_session(session, mic_queue)),
                supervised("recv", receive_loop(session, out_queue)),
                supervised("play", play_audio(out_queue)),
            )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye")
