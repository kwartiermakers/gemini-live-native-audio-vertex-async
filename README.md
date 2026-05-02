<p align="center">
  <a href="https://sainer.nl">
    <img src="https://press.sainer.nl/sainer/email/Logo-Sainer-Purple.png" alt="Sainer" width="240">
  </a>
</p>

# non-blocking-test

> Built by the team behind **[Sainer](https://sainer.nl)**.
> If this repo helped you, please follow us on
> **[LinkedIn](https://www.linkedin.com/company/sainer-nl)** and give us a
> mention — much appreciated.

Minimal demo of **Gemini Live native-audio non-blocking function calling** via
Vertex AI. One Python file, four tools, one for each scheduling mode:

| Tool                | Execution    | `scheduling` |
|---------------------|--------------|--------------|
| `get_current_time`  | synchronous  | n/a          |
| `log_preference`    | async        | `SILENT`     |
| `search_flights`    | async        | `WHEN_IDLE`  |
| `urgent_alert`      | async        | `INTERRUPT`  |

You speak (Dutch or English), the model picks a tool, the tool runs in the
background while the conversation keeps flowing, and the result is delivered
back according to its `scheduling` policy.

## Setup

```bash
brew install portaudio                       # macOS, for sounddevice
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
gcloud auth application-default login        # if not already done
```

## Run

```bash
python main.py
```

Speak naturally; press **Ctrl+C** to quit.

## TL;DR test playbook (Nederlands)

Start `python main.py`, dan in volgorde:

- *"Hoor je me goed?"* — wacht ~5 s *(warm-up)*
- *"Hoe laat is het nu?"* — wacht ~5 s *(sync)*
- *"Wil je alsjeblieft onthouden dat ik van sterke espresso hou?"* — wacht ~10 s *(SILENT)*
- *"Wat heb ik je net gevraagd om te onthouden?"* — wacht ~5 s *(recall)*
- *"Zoek vluchten naar Tokio en blijf ondertussen praten."* — wacht ~15 s *(WHEN_IDLE)*
- *"Maak een dringende waarschuwing aan met de tekst 'lunch is klaar', en vertel me daarna een uitgebreid verhaal over het weer in Rotterdam."* — wacht ~15 s *(INTERRUPT)*
- **Ctrl+C** *(exit)*

## TL;DR test playbook (English)

Start `python main.py`, then in order:

- *"Hi, can you hear me clearly?"* — wait ~5 s *(warm-up)*
- *"What time is it right now?"* — wait ~5 s *(sync)*
- *"Please remember that I like strong espresso."* — wait ~10 s *(SILENT)*
- *"What did I just ask you to remember?"* — wait ~5 s *(recall)*
- *"Search flights to Tokyo and keep talking meanwhile."* — wait ~15 s *(WHEN_IDLE)*
- *"Schedule an urgent alert that says 'lunch is ready', and then tell me a long, very detailed story about the weather in Rotterdam."* — wait ~15 s *(INTERRUPT)*
- **Ctrl+C** *(exit)*

For expected terminal output per step and a checklist, see `DUTCH_STEPS.md` /
`ENGLISH_STEPS.md`.

## Configuration

Before running, edit the constants at the top of `main.py` and set them to
your own Google Cloud project and region:

```python
PROJECT  = "your-gcp-project-id"   # replace
LOCATION = "your-gcp-region"        # replace, e.g. "europe-west4" or "us-central1"
MODEL    = "gemini-live-2.5-flash-native-audio"
```

Auth uses Application Default Credentials (`gcloud auth application-default
login`). The principal you authenticate as needs the **Vertex AI User** role
(or equivalent) on the chosen project.

## Architecture notes

What this demo learned the hard way (most of which the
[Live API async function calling docs][docs] warn about):

- **`scheduling` lives on the `FunctionResponse`**, not on the tool declaration
  or model config. There is no `behavior` parameter in the new SDK.
- **`session.receive()` is per-turn**; wrap it in `while True:` to keep
  receiving across turns.
- **Always dispatch tool work on a background task.** The receive loop must
  never `await` something slow.
- **Manage user expectations with a "nudge".** After receiving a `tool_call`,
  send a `client_content` text turn instructing the model to verbally
  acknowledge — otherwise the model just goes silent for the duration of the
  background task. Pattern straight from the docs' "Manage user expectations"
  section. We additionally append the user's recent transcript to the nudge so
  the model fulfils any *other* pending request from the same audio turn.
- **Send a follow-up "announce" nudge after the FunctionResponse** for
  `WHEN_IDLE` and `INTERRUPT`. Native audio sometimes ignores the response or
  re-calls the tool; an explicit text turn telling it to "announce now" fixes
  this.
- **Deduplicate function calls** within a 15 s TTL window — the model
  occasionally re-issues the same call right after seeing the first response.
  Blocked duplicates get a `SILENT` no-op response so they don't dangle.
- **Hard-block unauthorised tool calls** in the client. Prompts alone are not
  enough to keep `urgent_alert` from firing on its own initiative; we also
  scan the recent user transcript for keywords ("alarm", "waarschuwing",
  "remind me", …) and reject the call with a `SILENT` `denied` response if
  none are present.
- **The "Silent" caveat.** Even with `scheduling="SILENT"`, the model can
  occasionally narrate a tool. Strong guardrails in the system prompt help; a
  fire-and-forget pattern (no `FunctionResponse` at all) is the only hard
  guarantee.

[docs]: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/asynchronous-function-calling

## Files

```
main.py             single-file app
requirements.txt    google-genai, sounddevice, numpy
README.md           this file (includes TL;DR playbooks)
DUTCH_STEPS.md      detailed test walkthrough (NL)
ENGLISH_STEPS.md    detailed test walkthrough (EN)
```
