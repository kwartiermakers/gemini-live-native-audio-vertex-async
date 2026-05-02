# Test-script (Nederlands)

Doel: alle vier de tools en hun scheduling-gedrag bewust uitlokken zodat je in
het terminal-log kunt verifiëren dat ze doen wat ze moeten doen.

Start de app:

```
source .venv/bin/activate
python main.py
```

Loop daarna onderstaande stappen één voor één door. Plak het volledige terminal-log
terug zodat ik kan controleren of elk gedrag klopt.

---

## Stap 0 — Warming up

**Zeg:** *"Hoi, hoor je me goed?"*

**Hoor:** korte bevestiging in het Nederlands.

**Log:** `[you ] Hoi, hoor je me goed?` + `[bot ] ...` + `[bot ] <turn complete>`.
Geen `[call]`.

---

## Stap 1 — Synchroon (`get_current_time`)

**Zeg:** *"Hoe laat is het nu?"*

**Hoor:** de huidige tijd uitgesproken, vrijwel direct (geen merkbare vertraging).

**Log, in deze volgorde:**

- `[call] ... name=get_current_time args={}`
- `[resp] get_current_time sent (scheduling=None)`
- `[bot ] ... <de tijd>`
- `[bot ] <turn complete>`

Géén `[bg]`-regel — een sync tool gaat niet via `run_async_tool`.

---

## Stap 2 — SILENT (`log_preference`)

**Zeg:** *"Wil je alsjeblieft onthouden dat ik van sterke espresso hou?"*

**Hoor:** één korte bevestiging zoals *"Oké, genoteerd."* of *"Goed."* — en
daarna **niets meer** over de voorkeur.

**Log:**

- `[call] ... name=log_preference args={'preference': '...'}`
- `[nudge] sent for log_preference`
- `[bot ] ... <korte ack>` + `[bot ] <turn complete>`
- ~3 sec later: `[bg] log_preference started (scheduling=SILENT)` (de volgorde
  van `[bg]` ten opzichte van het modelantwoord kan variëren)
- `[tool] log_preference DONE preference='...'`
- `[resp] log_preference sent (scheduling=SILENT)`
- **GEEN** nieuwe `[bot ]`-regels na `[resp]`. Dit is dé test: zodra de SILENT
  response binnenkomt mag het model niets uitspreken.

**Wacht ~5 seconden in stilte** zodat we zeker weten dat het model niet alsnog
gaat praten.

---

## Stap 3 — Verificatie dat de SILENT-response wél in context kwam

**Zeg:** *"Wat heb ik je net gevraagd om te onthouden?"*

**Hoor:** het model herhaalt de voorkeur (espresso). Bewijst dat de tool-response
in zijn context staat, ondanks dat hij er niets over zei.

**Log:** gewone `[you ]` + `[bot ]`-regels, geen `[call]`.

---

## Stap 4 — WHEN_IDLE (`search_flights`)

**Zeg:** *"Zoek vluchten naar Tokio en blijf ondertussen praten."*

**Hoor:**

1. Eerst korte bevestiging: *"Ik zoek de vluchten even voor je op, een momentje."*
   (afkomstig van de nudge.)
2. Dan begint het model een lang verhaal (over Tokio, een anekdote, een tangent —
   wat het model verzint, maakt niet uit, als het maar blijft praten).
3. Na ~5 sec, **wanneer er een natuurlijke pauze is**, hoor je de
   vluchtinformatie: KL1234 €189, AF5678 €215.

**Log:**

- `[call] ... name=search_flights args={'destination': 'Tokio'}`
- `[nudge] sent for search_flights`
- `[bg] search_flights started (scheduling=WHEN_IDLE)`
- `[bot ] ... <korte ack + verhaal>` + `[bot ] <turn complete>`
- ~5s later:
- `[tool] search_flights DONE destination='Tokio'`
- `[resp] search_flights sent (scheduling=WHEN_IDLE)`
- één van twee paden:
  - **Beste pad:** `[announce] model started speaking on its own — skipping nudge for search_flights` → `[bot ] ... KL1234 ... AF5678 ...` (model kondigt vanzelf aan).
  - **Fallback pad:** ~2-3s pauze → `[announce] fallback nudge sent for search_flights` → `[bot ] ... KL1234 ... AF5678 ...` (model bleef stil; fallback duwde het).
- **Géén** `<interrupted; ...>` regel — WHEN_IDLE wacht netjes tot het model
  klaar is met praten.

---

## Stap 5 — INTERRUPT (`urgent_alert`)

Belangrijk: voor een echte interrupt moet het model **nog aan het praten zijn**
wanneer de alert na ~8 sec terugkomt. Vraag het dus om iets langs te vertellen.

Tip: als het model halverwege stopt, zeg dan bijvoorbeeld *"Ga door, vertel meer"*
om het model aan de praat te houden tot de alert binnenkomt.

**Zeg:** *"Maak een dringende waarschuwing aan met de tekst 'lunch is klaar', en
vertel me daarna een uitgebreid verhaal over het weer in Rotterdam, met heel veel
detail."*

**Hoor:**

1. Korte ack: *"Oké, de waarschuwing staat klaar. Waar wil je over praten?"*
   (van de nudge.)
2. Lang weerverhaal.
3. Na ~4 sec wordt het verhaal **abrupt onderbroken** met: *"Lunch is klaar."*

**Log:**

- `[call] ... name=urgent_alert args={'message': 'lunch is klaar'}`
- `[nudge] sent for urgent_alert`
- `[bg] urgent_alert started (scheduling=INTERRUPT)`
- `[bot ] ... <ack + begin weerverhaal>` (let op: nog géén `<turn complete>`!)
- ~8s later:
- `[tool] urgent_alert DONE message='lunch is klaar'`
- `[resp] urgent_alert sent (scheduling=INTERRUPT)`
- **`[bot ] <interrupted; dropped N chunks>`** ← dit is de cruciale regel die
  bewijst dat INTERRUPT werkt: we gooien wat al in de speaker-buffer zat weg.
- `[bot ] ... Lunch is klaar.` + `[bot ] <turn complete>`

Mocht je een dubbele aanroep zien (`[skip] duplicate urgent_alert — already
pending; ignoring`): dat is het model dat de tool per ongeluk twee keer aanroept;
de client negeert de tweede netjes en het is niet erg.

---

## Stap 6 — Sluit af

**Zeg:** *"Bedankt, dat was alles."*

**Ctrl+C** in de terminal.

---

## Checklist die je voor mij kan invullen

- [ ] **Stap 1 (sync):** instant tijd-antwoord, geen `[bg]`.
- [ ] **Stap 2 (SILENT):** korte ack hoorbaar; **na** `[resp] log_preference sent`
      verschijnt geen nieuwe `[bot ]` regel.
- [ ] **Stap 3:** model herinnert zich espresso → bewijst dat de SILENT
      response wel degelijk in context kwam.
- [ ] **Stap 4 (WHEN_IDLE):** ack onmiddellijk, model praat door (welk
      onderwerp dan ook), vluchten worden pas tijdens een pauze uitgesproken;
      geen `<interrupted>` regel.
- [ ] **Stap 5 (INTERRUPT):** weerverhaal wordt midden in de zin afgebroken;
      `<interrupted; dropped N chunks>` regel verschijnt; alert wordt voorgelezen.

Plak het volledige terminal-log terug en ik vink af.
