# Planning Monitor

Monitor autonomo per la pianificazione trasporti della ditta Bernardini.
Progetto Python standalone che esegue check deterministici sui viaggi pianificati del giorno, con analisi LLM opzionale e notifiche BERLink.

## Architettura

Progetto **totalmente indipendente** — comunica solo via HTTP:
- **Planning Agent** (:8602) — tool ETA, GPS, orari sede, distanze
- **BERLink** (:9095) — dati planning giornaliero + invio notifiche
- **OpenRouter** — LLM agent per analisi anomalie

Approccio ibrido: check deterministici (veloci, gratis) + LLM agent (solo su anomalie rilevate).

## Struttura progetto

```
planning-monitor/
├── run.py                       # CLI (typer): check, serve, daemon
├── server.py                    # FastAPI server :8610
├── config/settings.yaml         # Configurazione
├── .env                         # API keys (NON committare)
├── monitor/
│   ├── engine.py                # run_check() — entry point one-shot
│   ├── monitor_agent.py         # LLM Agent via OpenRouter
│   ├── registry.py              # BaseCheck ABC + registry
│   ├── notifier.py              # Notifiche BERLink + dedup
│   ├── planner_client.py        # Client HTTP → Planning Agent :8602
│   ├── berlink_client.py        # Client HTTP → BERLink :9095
│   └── checks/                  # Check auto-registrati (pkgutil)
│       └── eta_orari_check.py   # Check ETA vs orari apertura sede
├── models/__init__.py           # Dataclass CheckAlert, ETAResult
└── docs/CHECKS.md               # Reference check implementati e formato alert
```

## Comandi

```bash
# Attivare il venv
source .venv/bin/activate

# One-shot (modalita standard)
python run.py check                    # oggi, con LLM
python run.py check 2026-04-28         # data specifica
python run.py check --no-llm           # solo deterministici
python run.py check --notify           # con notifiche BERLink
python run.py check -c eta_orari       # solo check specifici

# Server HTTP
python run.py serve                    # :8610
python run.py serve --port 8611

# Daemon (loop continuo)
python run.py daemon --interval 300
```

## Endpoint API (:8610)

```
GET  /api/monitor/tools      — schema OpenAI function calling
POST /api/monitor/execute     — invoca tool per nome {"tool": "agent_check", "args": {...}}
POST /api/monitor/check       — endpoint diretto {"data": "...", "notify": true}
GET  /api/monitor/health      — health check
GET  /api/monitor/checks      — lista check registrati
```

## Dipendenze

Runtime: httpx, openai, typer, rich, pyyaml, python-dotenv, fastapi, uvicorn.
Installate nel venv `.venv/`. Nessuna dipendenza pesante: il monitor e un thin client HTTP + LLM.

## Convenzioni di sviluppo

- **Lingua**: codice e variabili in inglese, commenti e messaggi utente in italiano
- **Nuovi check**: un file per check in `monitor/checks/`, subclass `BaseCheck`, chiama `register_check()` a fine modulo. Auto-import via pkgutil. Documentare in `docs/CHECKS.md`
- **Severity**: solo `"warning"` o `"critical"` nei CheckAlert
- **Dedup key**: formato `"{check_name}:{chiavi_univoche}"`
- **Errori HTTP**: catturare con try/except, loggare, continuare. Un errore su un BG non blocca l'intero check
- **Config**: tutto in `config/settings.yaml`, secrets in `.env`
- **Nessun import dal planning agent**: tutte le interazioni via HTTP REST

## API esterne usate

### Planning Agent (porta 8602)
- `POST /api/planning/execute` — esegue qualsiasi tool per nome
- `POST /api/planning/viaggi` — viaggi da pianificare
- `GET /api/planning/tools` — schema tool (usato dal LLM agent)
- `GET /api/planning/health` — health check

Tool usati dal monitor (whitelist sola lettura):
`localizza_entita`, `calcola_eta_autista`, `get_eta_per_autista`, `check_orari_sede`,
`trova_autista_piu_vicino`, `get_autisti_disponibili`, `get_pianificazione_corrente`,
`calcola_distanza`, `calcola_etoa`, `cerca_bg_da_targa`, `get_info_bg`

### BERLink (porta 9095)
- `POST /api/Query/execute` — query SQL (planning giornaliero). Header: `X-API-Key`
- `POST /api/notifications/send` — invio notifiche. `notification_type: "planning_check"`

### OpenRouter
- Client OpenAI-compatible, base_url `https://openrouter.ai/api/v1`
- Modello: `anthropic/claude-3-5-haiku`
- Key: `OPENROUTER_API_KEY` in `.env`

## Specifica completa

Il file `PLANNING_MONITOR_REQS.md` nella root contiene la specifica completa e autosufficiente del progetto, con dettagli su tutte le API, i formati di risposta, e gli esempi.


---

## For Claude Code

## Workflow
- Start complex tasks in Plan mode
- Get plan approval before implementation
- Break large changes into reviewable chunks

### When Creating New Features
1. Seguire i pattern esistenti 
2. Cerca di mantenere le funzioni piccole: <= 100 righe di codice. Se una funzione fa troppe cose, spezzala in funzioni helper piu' piccole
3. Applica principio DRY e NON DUPLICARE CODICE. Se una logica esiste in due posti, rifattorizzala in una funzione comune (o chiarisci perche' servono due implementazioni differenti se esiste un motivo valido)
4. Implementa un mini-agile cycle: proponi -> ottieni feedback -> implementa -> review

### When encountering a bug or failing test
1. First explain possible causes step-by-step. 
2. Check assumptions, inputs, and relevant code paths.

### When Fixing Bugs
1. Verificare i log 
2. Con bug critici aggiungi log per isolare la issue
3. No Silent Failures: Do not swallow exceptions silently. Always surface errors either by throwing or logging them.

### When Refactoring
1. Mantenere backward compatibility API

### When adding / updating API
1. Aggiorna il file PLANNING_MONITOR_REQS.md.md
2. Non rimuovere endpoint ma rendili deprecati (per backward compatibility)
3. For API changes, test with `curl` or Postman

### Questions to Ask Human
- Business logic requirements non chiari
- Requisiti di performance
- Considerazioni di sicurezza

---

## Main Rules

1. Don’t assume. Don’t hide confusion. Surface tradeoffs.
2. Minimum code that solves the problem. Nothing speculative.
3. Touch only what you must. Clean up only your own mess.
4. Define success criteria. Loop until verified.

---

## Keep This Updated

**When to update this file:**
- Dopo aggiunta di nuove dipendenze major
- Dopo modifiche architetturali
- Dopo cambio convenzioni di codice
- Quando emergono nuovi pattern