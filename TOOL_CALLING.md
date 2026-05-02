# TOOL_CALLING.md — Async function calling on Gemini Live (native audio, Vertex AI)

A field guide for any engineer (human or AI) reproducing the patterns in
`main.py`. This doc is **not** about how the SDK is documented to work. It is
about what we learned by *running* it end-to-end with native audio and four
tools across the four scheduling modes.

If you only read one section, read [§3 The five layers](#3-the-five-layers-of-defence)
and [§9 Final tuning constants](#9-final-tuning-constants).

---

## 1. The contract you're implementing

| Tool                | Execution     | `scheduling`  | What the user should hear                                                                |
|---------------------|---------------|---------------|------------------------------------------------------------------------------------------|
| `get_current_time`  | sync          | *(omitted)*   | Time, instantly.                                                                          |
| `log_preference`    | async ~3 s    | `SILENT`      | One short ack ("Got it."), then **dead silence** until the next user turn.                |
| `search_flights`    | async ~3 s    | `WHEN_IDLE`   | Ack + monologue, then flight numbers/prices announced **at the next natural pause**.      |
| `urgent_alert`      | async ~8 s    | `INTERRUPT`   | Ack + monologue, then the alert message **cuts in mid-sentence** when the tool returns.   |

The "trick" is that *the same tool/response API* drives all four behaviours;
the only knob is `scheduling` on the outgoing `FunctionResponse`.

---

## 2. SDK ground truth (`google-genai` ≥ 1.0)

These are the load-bearing facts about the SDK surface. Get them wrong and
nothing else in this document matters.

### 2.1 `scheduling` lives on the `FunctionResponse`, not on the declaration

There is **no `behavior` field** on the tool declaration in the new SDK. You
set scheduling per-response:

```python
fr = types.FunctionResponse(
    id=fc.id,                  # echo the call's id
    name=fc.name,
    response=result,           # JSON-serializable dict
    scheduling="WHEN_IDLE",    # "SILENT" | "WHEN_IDLE" | "INTERRUPT"
)
await session.send_tool_response(function_responses=[fr])
```

For **synchronous** tools, **omit** `scheduling` from the kwargs entirely —
do not pass `scheduling=None`. Build the kwargs dict and conditionally add
the key (see `send_response` in `main.py:217–235`).

### 2.2 `session.receive()` is per-turn

The async iterator returned by `session.receive()` terminates on
`turn_complete`. The websocket stays open. To keep listening across turns
you **must** wrap it:

```python
async def receive_loop(session, out_queue):
    while True:
        async for response in session.receive():
            await handle_response(session, response, out_queue)
```

If your second user message is being ignored, this is almost certainly why.

### 2.3 The two ways to push content to the model

| Direction       | Method                       | When                                                        |
|-----------------|------------------------------|-------------------------------------------------------------|
| Audio in        | `session.send_realtime_input(audio=Blob(...))` | streaming PCM from mic                                |
| FunctionResponse | `session.send_tool_response(function_responses=[FunctionResponse(...)])` | replying to a `tool_call` |
| Text turn       | `session.send_client_content(turns=[Content(role="user", parts=[Part(text=...)])], turn_complete=True)` | nudges (see §4)         |

`send_client_content` synthesises a "user turn" the model responds to. It is
how we keep the model talking during async tools.

### 2.4 Audio I/O contract

- **Mic in:** PCM `int16` little-endian, mono, **16 000 Hz**, mime
  `audio/pcm;rate=16000`. We use 20 ms blocks (`INPUT_CHUNK = 320`).
- **Speaker out:** PCM `int16` mono, **24 000 Hz**. Yes, the rates differ.
  Hard-coded by the API.
- The `sounddevice` callback runs on a non-asyncio thread; hand bytes to
  the loop with `loop.call_soon_threadsafe(queue.put_nowait, pcm)`.
- Wrap blocking `stream.write()` in `await asyncio.to_thread(...)` so the
  event loop stays responsive.

### 2.5 Server-side events you actually consume

From `response.server_content`:
- `input_transcription.text` — what the user said (lags audio by hundreds of ms).
- `output_transcription.text` — what the model said (must enable in `LiveConnectConfig`).
- `interrupted` — barge-in / INTERRUPT-driven cut. **You** must drain your local playback queue.
- `turn_complete` — end-of-turn marker; the iterator returns after this.

From `response`:
- `data` — raw 24 kHz PCM bytes for playback.
- `tool_call.function_calls[]` — the model wants to call tools.
- `go_away.time_left` — server is about to disconnect; reconnect logic goes here.

---

## 3. The five layers of defence

Native audio + async tools is a **collaboration with a flaky partner**. The
model will: re-call tools after seeing responses, fire forbidden tools, go
silent during async work, and ignore `assistant_instructions` in your
response payload. No single mitigation is enough. Stack all five:

1. **Hard system instruction** (`SYSTEM_INSTRUCTION` in `main.py:106–168`) —
   per-tool rules with explicit forbiddings ("FORBIDDEN unless trigger words
   appear", "say absolutely NOTHING about log_preference ever again").
2. **`assistant_instructions` in the tool result payload** — embedded next
   to the data the model needs to recite ("These are the FINAL flight
   results. Do NOT call search_flights again."). Stronger than the system
   prompt because it's adjacent to the tokens the model is reading.
3. **Pre-result nudge** (`NUDGES`, fired immediately after `tool_call`) —
   forces the model to acknowledge verbally and continue talking through
   the wait. Without this the model goes silent for 3–8 seconds.
4. **Post-result announce nudge** (`ANNOUNCE_NUDGES`, fired conditionally
   after the FunctionResponse) — for `WHEN_IDLE`/`INTERRUPT` only, when the
   model failed to announce on its own.
5. **Client-side hard guards** — dedup window, in-flight counter, trigger-word
   check for `urgent_alert`. The prompt cannot be trusted.

Skip any of these and a specific failure mode comes back. Symptoms in §6.

---

## 4. Nudges — prompt engineering that worked

Two nudge dictionaries in `main.py`. They are not polite suggestions to the
model; they are imperative micro-scripts.

### 4.1 Pre-result nudges (`NUDGES`)

Fired by `nudge_model()` (`main.py:501–523`) **immediately** after we accept
a `tool_call`. Three things matter:

1. **Quote the exact sentence to say**, in both languages:
   > *"Repeat this sentence in the user's language right now: NL: 'Ik zoek de
   > vluchten even voor je op, een momentje.' EN: 'Let me look those flights
   > up for you, one moment.'"*

   Free-form instructions like "acknowledge politely" produce silence.
   Quoted sentences produce reliable audio.

2. **Demand a minimum monologue length.** `search_flights` says *"Talk
   continuously for at least 10 seconds"*; `urgent_alert` says *"keep talking
   continuously for at least 15 seconds without pausing"*. Without a number
   the model produces one sentence and hands the turn back.

3. **Echo the user's recent transcript** verbatim into the nudge. The nudge
   is structurally a new "user turn" — the model responds to *it*, not to
   the original audio turn. Without echoing, a request like "search flights
   AND tell me a joke" loses the joke. We append the last 500 chars of
   `RECENT_USER_TEXT`:

   > *"For reference, the user just said (verbatim): "<...>". Make sure you
   > fulfill ALL parts of that request, not just the part that triggered
   > the tool call."*

### 4.2 Post-result announce nudge (`ANNOUNCE_NUDGES`)

Fired by `announce_after_response()` (`main.py:426–498`) **only when the
model fails to announce on its own**. This is a fallback; if the model
already announced you must not nudge or it will announce a second time.

The conditions before firing the fallback:

```
A) MODEL_LAST_SPOKE quiet for ≥ required_quiet (1.5 s)
B) playback queue drained (out_queue.empty())
C) no tool-specific keyword spoken since the response was sent
D) grace window of 2.5 s passes without (A) or (C) flipping
```

Why this combination:
- (A) alone fails when the model finished generating audio fast but the
  *playback* of that audio is still in progress (Gemini Live often generates
  faster than real-time).
- (C) alone fails when trailing pre-response chatter ("...also a great
  city") looks like an announcement; we filter by **content keywords** —
  flight codes for `search_flights`, message words for `urgent_alert` — so
  unrelated speech doesn't fool us. See `_announce_keywords()`.

---

## 5. Deduplication — two layers, one anchor

The model **will** re-issue function calls. Two distinct patterns:

| Pattern                        | What it looks like                                                              | Defence                                  |
|--------------------------------|----------------------------------------------------------------------------------|------------------------------------------|
| In-flight overlap              | Second call arrives while first is still running.                                | `PENDING[name] > 0` → skip.              |
| Post-response re-issue         | Second call arrives 0.3–8 s **after** we sent the first FunctionResponse.        | `RECENT_RESPONSE[name]` TTL window.      |

Critical detail: **dropped duplicates need a reply.** The model is waiting
on a FunctionResponse for that `fc.id`. If you silently drop the call, you
leak an open call slot and the model may eventually time out or re-call
again. The fix is to send a SILENT no-op:

```python
asyncio.create_task(send_response(
    session, fc.id, fc.name,
    {"status": "duplicate_call_ignored",
     "assistant_instructions": (
         "Previous result for this tool is already in your context — use "
         "that. Do not announce anything new for this duplicate call."
     )},
    scheduling="SILENT",
    update_recent=False,    # ← anchor TTL to the FIRST real response
))
```

The `update_recent=False` flag is load-bearing. Without it, every blocked
duplicate refreshes the TTL window indefinitely — the model stays in
"every call is a duplicate" mode forever. We anchor the dedup window to
the **first real** response, not to subsequent no-ops.

`DEDUP_TTL_SEC = 10.0` — long enough to catch model re-issues after seeing
the first response in context (we observed ~0.3 s and ~6 s gaps), short
enough not to block a legitimate second user request.

---

## 6. The `urgent_alert` guard — prompts are not enough

**The bug:** asked for "flights to Tokyo and tell me a joke", the model
spontaneously fired `urgent_alert(message="Severe weather warning for
Tokyo area.")`. Tightening the system prompt with explicit forbiddings
reduced but did not eliminate this. Native audio models will hallucinate
tool calls inferred from topics ("weather" → "alert", "lunch" → "reminder").

**The fix is two-layered:**

### 6.1 Prompt-level trigger words

The system instruction enumerates literal trigger phrases the user must
have said:

```
English: "alert", "alarm", "remind me", "reminder", "notification", "notify"
Dutch:   "waarschuwing", "alarm", "herinner", "herinnering", "melding"
```

Phrased so the model knows *we will check* (`"The client will hard-block
any unauthorized call and you will look foolish."`). Hostile-sounding, but
gets the lowest false-fire rate.

### 6.2 Client-side guard (`guarded_urgent_alert`)

```python
URGENT_ALERT_TRIGGERS = (
    "alert", "alarm",
    "remind me", "reminder", "notification", "notify",
    "waarschuwing", "herinner", "herinnering", "melding",
)
```

Substring check against the rolling `RECENT_USER_TEXT[0]` buffer (last 500
chars of `input_transcription`).

**Race condition you must handle:** the model can call `urgent_alert`
**before** the input transcription pipeline has finalised the user's
words. The model understands audio faster than the transcript stream
delivers tokens. If you check the buffer immediately, you'll false-block
legitimate alerts.

The fix is a poll loop with `GUARD_WAIT_SEC = 2.0` ceiling, checking every
100 ms. Two seconds was tuned to comfortably exceed worst-case
transcription lag without making the user wait perceptibly:

```python
deadline = time.monotonic() + GUARD_WAIT_SEC
while time.monotonic() < deadline and not user_recently_requested_alert():
    await asyncio.sleep(0.1)

if not user_recently_requested_alert():
    # send SILENT denial response and return
```

Denied calls still get a FunctionResponse (`{"status": "denied", "reason":
"user did not request an alert"}`, scheduling=SILENT) so the model isn't
left dangling.

---

## 7. INTERRUPT pipeline — what actually interrupts

`scheduling="INTERRUPT"` doesn't *itself* drop your buffered audio. The
server signals via `response.server_content.interrupted = True`, and **you
drain your local playback queue**:

```python
if sc.interrupted:
    drained = 0
    while not out_queue.empty():
        out_queue.get_nowait()
        drained += 1
    log(f"[bot ] <interrupted; dropped {drained} chunks>")
```

A "real" INTERRUPT in this demo drops ~60–96 chunks (multi-second of
buffered speech). Single-digit drops are barge-in echoes and harmless.

**`urgent_alert_impl` sleeps 8 seconds on purpose.** It must outlast
`search_flights` (3 s) so the model has time to actually launch into the
follow-up monologue. INTERRUPT with nothing to interrupt is a non-event —
the alert just plays into silence and looks no different from `WHEN_IDLE`.

Pair this with the `urgent_alert` nudge demanding "talk continuously for
at least 15 seconds without pausing" — together they guarantee the model
is mid-sentence when the alert lands.

---

## 8. Concurrency model — don't await tool replies inline

The receive loop must **never** block on an outbound websocket write. The
SDK's per-turn iterator can stall if `send_tool_response` does, and the
whole pipeline freezes. Every reply goes through `asyncio.create_task`:

```python
if fc.name == "get_current_time":                       # sync tool
    result = get_current_time_impl()
    asyncio.create_task(send_response(session, fc.id, fc.name, result))

elif fc.name == "search_flights":                       # async tool
    asyncio.create_task(run_async_tool(
        session, fc.id, fc.name,
        search_flights_impl(args.get("destination", "")),
        scheduling="WHEN_IDLE",
    ))
    asyncio.create_task(nudge_model(session, fc.name))   # parallel pre-nudge
```

Even synchronous results are dispatched on a task. Even nudges are
dispatched on a task. The receive loop's only job is to demux events.

`run_async_tool()` increments/decrements `PENDING[name]` and, after
sending the response, kicks off `announce_after_response()` for non-SILENT
tools. All of this is fire-and-forget; the response handler returns
immediately.

---

## 9. Final tuning constants

Every constant is in `main.py`. Don't change them blindly — most were
arrived at by walking back from a specific failure trace.

| Constant                  | Value     | Rationale                                                                                                          |
|---------------------------|-----------|--------------------------------------------------------------------------------------------------------------------|
| `INPUT_SR`                | 16 000 Hz | API contract.                                                                                                       |
| `OUTPUT_SR`               | 24 000 Hz | API contract.                                                                                                       |
| `INPUT_CHUNK`             | 320       | 20 ms at 16 kHz; standard for streaming VAD.                                                                        |
| `DEDUP_TTL_SEC`           | 10.0      | Long enough to catch re-issued calls (we see 0.3–6 s gaps); short enough to allow a real second request.            |
| `GUARD_WAIT_SEC`          | 2.0       | Worst-case input-transcription lag; <1 s caused false blocks.                                                       |
| `required_quiet`          | 1.5 s     | Longer than within-monologue pauses (~1 s) but short enough to not feel like dead air.                              |
| `grace`                   | 2.5 s     | Final window for the model to announce on its own before the fallback nudge fires.                                  |
| `max_wait_quiet`          | 30.0 s    | Safety bound on stage-1 wait so a stuck pipeline doesn't park forever.                                              |
| `RECENT_USER_TEXT` size   | 500 chars | Spans the most recent user utterance; long enough for trigger-word checks.                                          |
| `log_preference_impl` sleep | 3 s     | Short enough that SILENT silence test stays interactive.                                                             |
| `search_flights_impl` sleep | 3 s     | Long enough for monologue + WHEN_IDLE pause behaviour to be observable.                                              |
| `urgent_alert_impl` sleep | 8 s       | Must exceed the model's monologue ramp-up + a few sentences, otherwise INTERRUPT has nothing to cut.                |
| Monologue minimum (search) | 10 s    | Tool sleeps 3 s; the extra 7 s of demanded monologue gives WHEN_IDLE a real "find a pause" opportunity.             |
| Monologue minimum (alert) | 15 s    | Tool sleeps 8 s; the extra 7 s ensures the model is still talking when INTERRUPT fires.                              |

---

## 10. Logging — non-negotiable for debugging this

Every log line is prefixed with seconds-since-start: `+ 12.345s ...`.
Without timestamps, "the model went silent" is unprovable; with them, you
read latencies straight off the trace:

```
+12.524s [resp] search_flights sent (scheduling=WHEN_IDLE)
+13.100s [bot ] Vlucht KL1234 kost...
        ^^^^^^^ 576 ms model latency to announce
```

Standard tag taxonomy:

| Tag         | Meaning                                                       |
|-------------|---------------------------------------------------------------|
| `[you ]`    | Input transcription chunk.                                    |
| `[bot ]`    | Output transcription chunk.                                   |
| `[call]`    | A `tool_call` was received.                                   |
| `[bg]`      | An async tool started running.                                |
| `[tool]`    | A tool's `*_impl` finished.                                   |
| `[resp]`    | A FunctionResponse went out.                                  |
| `[nudge]`   | A pre-result nudge was sent.                                  |
| `[announce]`| A post-result announce nudge fired (or was skipped).          |
| `[guard]`   | `urgent_alert` was allowed or blocked.                        |
| `[skip]`    | A duplicate call was deduped.                                 |
| `[!!]`      | Exception in any of the above.                                |
| `[srv ]`    | Server-side event (e.g. `go_away`).                           |

A clean SILENT trace is `[call] log_preference → [nudge] → [bot ] <ack> →
[bot ] <turn complete> → [bg] started → [tool] DONE → [resp] sent` — and
**no further `[bot ]` lines** until the next user turn. That dead-air gap
is the SILENT acceptance test.

---

## 11. Failure-mode → fix lookup

Quick reference. Match the symptom you see in your trace, jump to the fix.

| Symptom                                                                            | Cause                                                            | Fix                                              |
|------------------------------------------------------------------------------------|------------------------------------------------------------------|--------------------------------------------------|
| Second user message is ignored.                                                    | `session.receive()` returned on first `turn_complete`.            | Wrap in `while True:` (§2.2).                    |
| Pipeline freezes after first tool call.                                            | Awaited `send_tool_response` inline in receive loop.              | `asyncio.create_task(send_response(...))` (§8). |
| Model goes silent for 3–8 s after issuing a tool call.                             | No pre-result nudge.                                              | `nudge_model()` after every async `tool_call`.   |
| Model only addresses part of the user's request.                                   | Nudge replaced the user turn in the model's context.              | Echo `RECENT_USER_TEXT` into the nudge (§4.1).   |
| `WHEN_IDLE` response arrives but nothing is announced.                             | Native audio sometimes ignores response payloads.                 | `ANNOUNCE_NUDGES` fallback (§4.2).                |
| Flights are announced **twice**.                                                   | Fallback fired while playback was still draining.                 | `playback_drained()` check on `out_queue`.        |
| Flights are announced twice in a different way.                                    | "Spoke after response" matched trailing pre-response chatter.     | `_announce_keywords()` content match (§4.2).      |
| Same tool fires twice for one user request.                                        | In-flight overlap.                                                | `PENDING[name] > 0` guard.                        |
| Same tool fires again 0.3–6 s after first response.                                | Model re-issues call after seeing response in context.            | `RECENT_RESPONSE[name]` TTL of 10 s.              |
| Dedup TTL never expires.                                                           | Each blocked no-op refreshed `RECENT_RESPONSE`.                    | `update_recent=False` for no-op replies (§5).     |
| `urgent_alert` fires when user asked for flights/weather/jokes.                    | Model infers alerts from topics.                                  | Trigger-word guard + SILENT denial response (§6). |
| `urgent_alert` is blocked when the user *did* ask for one.                         | Input transcription lagged the model's audio comprehension.       | Poll the buffer for up to `GUARD_WAIT_SEC=2 s`.   |
| INTERRUPT lands but nothing audible was interrupted.                               | Model finished its turn before tool returned.                     | Tool sleeps 8 s + nudge demands ≥15 s monologue.  |
| INTERRUPT fires and `<interrupted; dropped 0 chunks>` logs.                         | Local playback was already empty.                                 | Only meaningful when the queue was full; check timing. |

---

## 12. What we deliberately did **not** build

These came up during iteration and were decided against:

- **Reconnect on `go_away`.** Logged but not handled. A 30-min single-session
  demo doesn't need it. Production code should reconnect and re-establish
  config + tools.
- **Persistent memory of `log_preference`.** SILENT proves the *response* is
  in context (the model can recall it within the session). Nothing is
  written to disk. Adding persistence is orthogonal.
- **A no-`FunctionResponse` ("fire-and-forget") `log_preference`.** This is
  the only way to *guarantee* SILENT silence — if you never reply, the
  model has no event to react to. We chose to send a SILENT response
  instead because (a) it lets the model recall the preference later (Step 3
  of the test playbook) and (b) the layered defences usually keep it
  quiet. If you need a hard SILENT guarantee and don't need recall, just
  don't send a `FunctionResponse` at all.
- **Local VAD / mic energy detection for barge-in.** The server tells us
  via `server_content.interrupted`. Don't duplicate it client-side.

---

## 13. Reproducing the results

The test playbooks in `ENGLISH_STEPS.md` and `DUTCH_STEPS.md` are
acceptance tests. Run them in order; for each step, the expected log lines
and audible behaviour are documented. If a step fails, find the matching
row in §11 above.

The **single most diagnostic step** is Step 2 (SILENT). If you see any
`[bot ]` line **after** `[resp] log_preference sent (scheduling=SILENT)`
and before the next user turn, your defence stack has a leak — usually a
missing or weak system-prompt rule, or an `assistant_instructions` field
the model is paraphrasing instead of obeying.
