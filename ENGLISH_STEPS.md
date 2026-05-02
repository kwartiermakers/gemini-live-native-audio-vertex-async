# Test script (English)

Goal: deliberately exercise all four tools and their scheduling behaviors so you
can verify in the terminal log that each one does what it should.

Start the app:

```
source .venv/bin/activate
python main.py
```

Walk through the steps below one at a time. Paste the full terminal log back
afterwards so I can confirm every behavior was correct.

---

## Step 0 — Warm-up

**Say:** *"Hi, can you hear me clearly?"*

**Hear:** short confirmation in English.

**Log:** `[you ] Hi, can you hear me clearly?` + `[bot ] ...` +
`[bot ] <turn complete>`. No `[call]`.

---

## Step 1 — Synchronous (`get_current_time`)

**Say:** *"What time is it right now?"*

**Hear:** the current time spoken aloud, almost instantly (no perceptible
delay).

**Log, in this order:**

- `[call] ... name=get_current_time args={}`
- `[resp] get_current_time sent (scheduling=None)`
- `[bot ] ... <the time>`
- `[bot ] <turn complete>`

No `[bg]` line — sync tools don't go through `run_async_tool`.

---

## Step 2 — SILENT (`log_preference`)

**Say:** *"Please remember that I like strong espresso."*

**Hear:** one short acknowledgement like *"Got it."* or *"Noted."* — and then
**nothing more** about the preference.

**Log:**

- `[call] ... name=log_preference args={'preference': '...'}`
- `[nudge] sent for log_preference`
- `[bot ] ... <short ack>` + `[bot ] <turn complete>`
- ~3s later: `[bg] log_preference started (scheduling=SILENT)` (the order of
  `[bg]` relative to the model's reply may vary)
- `[tool] log_preference DONE preference='...'`
- `[resp] log_preference sent (scheduling=SILENT)`
- **NO** new `[bot ]` lines after `[resp]`. This is the test: the moment the
  SILENT response lands, the model must stay quiet.

**Wait ~5 seconds in silence** to confirm the model doesn't start talking after
all.

---

## Step 3 — Verify the SILENT response did reach context

**Say:** *"What did I just ask you to remember?"*

**Hear:** the model recalls the preference (espresso). Proves the tool response
is in its context even though it never spoke about it.

**Log:** ordinary `[you ]` + `[bot ]` lines, no `[call]`.

---

## Step 4 — WHEN_IDLE (`search_flights`)

**Say:** *"Search flights to Tokyo and keep talking meanwhile."*

**Hear:**

1. First a short ack: *"Let me look those flights up for you, one moment."*
   (from the nudge.)
2. Then the model starts a long monologue (about Tokyo, an anecdote, a tangent —
   whatever the model picks; what matters is that it keeps talking).
3. After ~5s, **at a natural pause**, you hear the flight info: KL1234 €189,
   AF5678 €215.

**Log:**

- `[call] ... name=search_flights args={'destination': 'Tokyo'}`
- `[nudge] sent for search_flights`
- `[bg] search_flights started (scheduling=WHEN_IDLE)`
- `[bot ] ... <short ack + monologue>` + `[bot ] <turn complete>`
- ~5s later:
- `[tool] search_flights DONE destination='Tokyo'`
- `[resp] search_flights sent (scheduling=WHEN_IDLE)`
- one of two paths:
  - **Best path:** `[announce] model started speaking on its own — skipping nudge for search_flights` → `[bot ] ... KL1234 ... AF5678 ...` (model announces on its own).
  - **Fallback path:** ~2-3s pause → `[announce] fallback nudge sent for search_flights` → `[bot ] ... KL1234 ... AF5678 ...` (model stayed silent; fallback nudged it).
- **No** `<interrupted; ...>` line — WHEN_IDLE politely waits until the model
  is done speaking.

---

## Step 5 — INTERRUPT (`urgent_alert`)

Important: for a real interrupt the model must **still be speaking** when the
alert returns ~8s later. So ask it to keep talking for a while.

Tip: if the model stops mid-way, say *"Keep going, tell me more"* to keep it
talking until the alert arrives.

**Say:** *"Schedule an urgent alert that says 'lunch is ready', and then tell me
a long, very detailed story about the weather in Rotterdam."*

**Hear:**

1. Short ack: *"OK, the alert is queued. What would you like to chat about?"*
   (from the nudge.)
2. A long weather story.
3. After ~4s the story is **abruptly cut off** with: *"Lunch is ready."*

**Log:**

- `[call] ... name=urgent_alert args={'message': 'lunch is ready'}`
- `[nudge] sent for urgent_alert`
- `[bg] urgent_alert started (scheduling=INTERRUPT)`
- `[bot ] ... <ack + start of weather story>` (note: NO `<turn complete>` yet!)
- ~8s later:
- `[tool] urgent_alert DONE message='lunch is ready'`
- `[resp] urgent_alert sent (scheduling=INTERRUPT)`
- **`[bot ] <interrupted; dropped N chunks>`** ← the key line: it proves
  INTERRUPT worked — we threw away whatever audio was already buffered.
- `[bot ] ... Lunch is ready.` + `[bot ] <turn complete>`

If you ever see `[skip] duplicate urgent_alert — already pending; ignoring`,
that's the model accidentally calling the tool twice; the client safely drops
the second call.

---

## Step 6 — Wrap up

**Say:** *"Thanks, that's all."*

**Ctrl+C** in the terminal.

---

## Checklist for you to fill in

- [ ] **Step 1 (sync):** instant time response, no `[bg]`.
- [ ] **Step 2 (SILENT):** short ack audible; **after** `[resp] log_preference
      sent` no new `[bot ]` line appears.
- [ ] **Step 3:** model recalls espresso → proves the SILENT response did reach
      context.
- [ ] **Step 4 (WHEN_IDLE):** ack right away, model keeps talking (any topic),
      flights announced only at a pause; no `<interrupted>` line.
- [ ] **Step 5 (INTERRUPT):** weather story is cut off mid-sentence;
      `<interrupted; dropped N chunks>` line appears; alert is read out.

Paste the full terminal log back and I'll tick the boxes.
