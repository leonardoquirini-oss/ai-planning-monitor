# Agent Check — Monitor Autonomo Pianificazione Trasporti

> **Questo documento è una specifica completa e autosufficiente.**
> Può essere copiato in una directory vuota e dato a Claude per implementare il progetto da zero.
> Il monitor è un progetto **totalmente indipendente** che comunica con il Planning Agent esistente esclusivamente **via HTTP** (API REST sulla porta 8602) e con BERLink (API REST sulla porta 9095).

---

## 1. Obiettivo

Creare un progetto Python standalone che implementa un **agent monitor** per la pianificazione trasporti, esposto come **tool HTTP invocabile dall'esterno**.

Il monitor:

1. Espone un **server FastAPI** (porta 8610) con endpoint tool nel formato OpenAI function calling — stesso pattern del Planning Agent
2. Ad ogni invocazione esegue **check deterministici** sui viaggi pianificati del giorno (**one-shot**)
3. Quando rileva anomalie, le passa a un **LLM Agent** che ragiona sulla gravità e propone soluzioni
4. Opzionalmente invia **notifiche intelligenti** a BERLink con l'analisi dell'LLM
5. Restituisce risultato JSON completo al chiamante

**Modalità standard**: one-shot (singola invocazione → risultato). L'esecuzione periodica è demandata a **cron** o scheduler esterno che chiama l'endpoint HTTP.

**Approccio ibrido**: check deterministici (veloci, gratis, affidabili) + LLM agent (solo quando servono, per analisi e proposta d'azione).

---

## 2. Architettura

```
                    cron / BERLink scheduler / curl / altro agent
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────┐
│          Planning Monitor (progetto separato) :8610          │
│                                                              │
│  FastAPI server                                              │
│  ├─ GET  /api/monitor/tools     → schema OpenAI FC          │
│  ├─ POST /api/monitor/execute   → invoca tool per nome      │
│  ├─ POST /api/monitor/check     → endpoint diretto          │
│  ├─ GET  /api/monitor/health    → health check              │
│  └─ GET  /api/monitor/checks    → lista check disponibili   │
│                                                              │
│  ┌─────────────┐    anomalie    ┌──────────────────┐        │
│  │   Check     │──────────────> │  Monitor LLM     │        │
│  │   Engine    │  (lista alert) │  Agent           │        │
│  │ (determin.) │                │  (ragiona,       │        │
│  └─────────────┘                │   propone azioni)│        │
│        │                        └────────┬─────────┘        │
│        │ one-shot                        │                  │
│        │ via HTTP                        │ via HTTP         │
│  ┌─────┴──────────┐            ┌─────────┴────────┐        │
│  │ Planning Agent │            │ BERLink          │        │
│  │ API :8602      │            │ API :9095        │        │
│  │ (tool via REST)│            │ POST /notify     │        │
│  └────────────────┘            └──────────────────┘        │
└──────────────────────────────────────────────────────────────┘
```

Il monitor **non importa** nessun modulo Python dal planning agent. Tutte le interazioni avvengono via HTTP:

- **Planning Monitor API** (porta 8610): espone i propri tool nel formato OpenAI function calling, invocabili dall'esterno
- **Planning Agent API** (porta 8602): fornisce tutti i tool (ETA, localizzazione, orari sede, distanze, ecc.) come endpoint REST
- **BERLink API** (porta 9095): fornisce dati planning + invio notifiche
- **OpenRouter API**: per l'LLM agent

---

## 3. Struttura progetto

```
planning-monitor/
├── run.py                       # Entry point CLI (typer): check, serve, daemon
├── server.py                    # FastAPI server (porta 8610): tool HTTP
├── config/
│   └── settings.yaml            # Configurazione completa
├── .env                         # Chiavi API (OPENROUTER_API_KEY)
├── requirements.txt             # Dipendenze Python
├── logs/                        # Directory log
│
├── monitor/
│   ├── __init__.py              # Export run_check
│   ├── engine.py                # run_check() — entry point one-shot
│   ├── monitor_agent.py         # LLM Agent (OpenAI-compatible via OpenRouter)
│   ├── registry.py              # BaseCheck ABC + CheckAlert + registry
│   ├── notifier.py              # Invio notifiche BERLink + dedup opzionale
│   ├── planner_client.py        # Client HTTP per Planning Agent API :8602
│   ├── berlink_client.py        # Client HTTP per BERLink API :9095
│   └── checks/
│       ├── __init__.py          # Auto-import check modules via pkgutil
│       └── eta_orari_check.py   # Check 1: ETA vs orari apertura sede
│
└── models/
    └── __init__.py              # Dataclass CheckAlert, ETAResult, ecc.
```

---

## 4. API del Planning Agent (porta 8602)

Il planning agent espone un server REST FastAPI su `http://192.168.0.14:8602`. Il monitor usa questi endpoint.

### 4.1 Endpoint generico — esegue qualsiasi tool

**POST `/api/planning/execute`**

Endpoint universale: esegue qualsiasi tool del planning agent per nome. Tutti i tool restituiscono JSON.

```
POST http://192.168.0.14:8602/api/planning/execute
Content-Type: application/json

{"tool": "nome_tool", "args": {"param1": "valore1", "param2": "valore2"}}
```

**Response**: il JSON restituito dal tool (struttura variabile per tool).

### 4.2 Tool disponibili (usati dal monitor)

L'elenco completo dei tool con schema è disponibile su `GET /api/planning/tools` (ritorna array OpenAI function calling format). I tool usati dal monitor:

#### `localizza_entita` — Posizione GPS/planning di autista o semirimorchio

```json
{"tool": "localizza_entita", "args": {"tipo": "semirimorchio", "identificativo": "XA 821 YL"}}
```

Response:
```json
{
  "trovato": true,
  "tipo": "semirimorchio",
  "posizione": "Terni, Via dell'Industria",
  "coordinate": {"lat": 42.5604, "lon": 12.6471},
  "fonte": "waytracker_gps",
  "aggiornamento": "2026-04-27T10:30:00",
  "gps_age_min": 15,
  "velocita": 65,
  "stato": null,
  "semirimorchio_targa": "XA 821 YL",
  "planning_date": "2026-04-27",
  "evento_eta": "2026-04-27T14:30:00",
  "missione": {
    "mission_id": 12345,
    "reference_number": "TO-2026-001",
    "status": "IN_TRANSIT",
    "aperta": true,
    "eta": "2026-04-27T14:30:00",
    "container": "GBTU028123.5",
    "autista": "Mario Rossi"
  },
  "eta": "2026-04-27T14:30:00",
  "eta_fonte": "missione"
}
```

Cascata fonti: Planning BERLink → GPS WayTracker → evt_unit_events → TFP Mission.

#### `calcola_eta_autista` — ETA con cascata completa

```json
{"tool": "calcola_eta_autista", "args": {"bg": "26A02612", "targa": "XA 821 YL", "luogo_scarico": "Milano", "data_scarico": "2026-04-27"}}
```

Response:
```json
{
  "bg": "26A02612",
  "targa": "XA 821 YL",
  "eta": "2026-04-27T14:30:00",
  "eta_orario": "14:30",
  "disponibile_da": "2026-04-27T16:30:00",
  "metodo": "gps",
  "affidabilita": 0.9,
  "distanza_residua_km": 320.5,
  "tempo_viaggio_ore": 5.2,
  "posizione_gps": "Terni, Via dell'Industria",
  "coordinate_gps": "42.5604,12.6471",
  "dettagli": "GPS → scarico (Milano): 320km, 5.2h guida"
}
```

Cascata: missione TFP → evento GPS → GPS + routing → DataS fallback.

#### `get_eta_per_autista` — ETA cercando per nome autista

```json
{"tool": "get_eta_per_autista", "args": {"nome_autista": "Rossi Mario", "data": "2026-04-27"}}
```

Trova automaticamente BG e targa dall'autista, poi calcola ETA.

#### `check_orari_sede` — Orari apertura sede carico/scarico

```json
{"tool": "check_orari_sede", "args": {"bg": "26A02612", "data": "2026-04-27", "tipo": "SCARICO"}}
```

Response:
```json
{
  "bg": "26A02612",
  "tipo": "SCARICO",
  "data": "2026-04-27",
  "giorno": "lunedì",
  "sede": "ACME SRL - Milano",
  "orari": {
    "mattina": {"dalle": "08:00", "alle": "12:00"},
    "pomeriggio": {"dalle": "15:00", "alle": "16:30"}
  },
  "fonte": "database"
}
```

Fallback se non configurato: `08:00-12:00` + `15:00-16:30`.

#### `calcola_etoa` — ETOA (ETA + orari sede)

```json
{"tool": "calcola_etoa", "args": {"eta": "2026-04-27T14:30:00", "bg": "26A02612", "data": "2026-04-27"}}
```

Response:
```json
{
  "etoa": "2026-04-27T17:00:00",
  "etoa_orario": "17:00",
  "etoa_data": "2026-04-27",
  "dettagli": "ETA 14:30 dentro finestra pomeriggio 15:00-16:30 → ETOA = 14:30 + 2h scarico = 16:30"
}
```

Se ETA fuori finestra → ETOA slitta alla prossima finestra (pomeriggio, giorno dopo, salta weekend).

#### `trova_autista_piu_vicino` — Autista alternativo più vicino

```json
{"tool": "trova_autista_piu_vicino", "args": {"localita": "Milano", "data": "2026-04-27", "max_risultati": 5}}
```

Localizza TUTTI gli autisti disponibili e li classifica per distanza dalla località.

#### `get_autisti_disponibili` — Lista autisti disponibili

```json
{"tool": "get_autisti_disponibili", "args": {"data": "2026-04-27"}}
```

#### `calcola_distanza` — Distanza km tra due località

```json
{"tool": "calcola_distanza", "args": {"origine": "Terni", "destinazione": "Milano"}}
```

#### `cerca_bg_da_targa` — Trova BG associato a una targa

```json
{"tool": "cerca_bg_da_targa", "args": {"targa": "XA 821 YL", "data": "2026-04-27"}}
```

### 4.3 Endpoint dedicati (alternative al generico)

Alcuni tool hanno anche endpoint dedicati con request body tipizzato:

| Endpoint | Method | Body | Tool equivalente |
|----------|--------|------|-----------------|
| `/api/planning/viaggi` | POST | `{"data": "YYYY-MM-DD"}` | `get_viaggi_da_pianificare` |
| `/api/planning/autisti` | POST | `{"data": "YYYY-MM-DD"}` | `get_autisti_disponibili` |
| `/api/planning/pianificazione_corrente` | POST | `{"data": "YYYY-MM-DD"}` | `get_pianificazione_corrente` |
| `/api/planning/localizza_entita` | POST | `{"tipo": "...", "identificativo": "..."}` | `localizza_entita` |
| `/api/planning/eta` | POST | `{"nome_autista": "...", "data": "..."}` | `get_eta_per_autista` |
| `/api/planning/gps` | POST | `{"targa": "..."}` | `get_posizione_gps` |
| `/api/planning/distanza` | POST | `{"origine": "...", "destinazione": "..."}` | `calcola_distanza` |
| `/api/planning/orari-sede` | POST | `{"bg": "...", "data": "...", "tipo": "..."}` | `check_orari_sede` |
| `/api/planning/health` | GET | — | Health check |
| `/api/planning/tools` | GET | — | Schema tool (OpenAI format) |

**Nota**: `trova_autista_piu_vicino`, `calcola_eta_autista`, `calcola_etoa`, `cerca_bg_da_targa` NON hanno endpoint dedicato — usare il generico `/api/planning/execute`.

---

## 5. API BERLink (porta 9095)

### 5.1 Dati planning giornaliero

Il monitor ha bisogno delle righe planning del giorno per sapere chi è assegnato a cosa. Queste si ottengono in due modi:

**Opzione A** — Tramite Planning Agent API:
```
POST http://192.168.0.14:8602/api/planning/pianificazione_corrente
{"data": "2026-04-27"}
```

**Opzione B** — Query diretta a BERLink (preferibile per avere dati strutturati):
```
POST http://192.168.0.12:9095/api/Query/execute
X-API-Key: {berlink_api_key}
Content-Type: application/json

{
  "query": "SELECT p.id_trailer_planning, p.id_trailer, p.note, p.info_maintenance, p.id_employee, p.planning, p.planning_date, p.flag_ack, t.plate_number, t.id_trailer_type, e.name as driver_name, e.surname as driver_surname FROM pl_trailer_planning p JOIN flt_trailers t ON p.id_trailer = t.id_trailer LEFT JOIN emp_employees e ON p.id_employee = e.id_employee WHERE p.planning_date = '2026-04-27' ORDER BY t.plate_number"
}
```

Response:
```json
{
  "data": [
    {
      "id_trailer_planning": 12345,
      "id_trailer": 67,
      "plate_number": "XA 821 YL",
      "id_employee": 42,
      "driver_name": "Mario",
      "driver_surname": "Rossi",
      "planning": "#26A02612 | Carica Terni → Scarica Milano",
      "note": null,
      "info_maintenance": null,
      "flag_ack": true,
      "id_trailer_type": 1,
      "planning_date": "2026-04-27"
    }
  ],
  "rowCount": 85
}
```

**Campi chiave**:
- `id_employee`: se null → nessun autista assegnato (skip nel check)
- `planning`: **testo libero** con BG e istruzioni. I codici BG sono preceduti da `#`
- `plate_number`: targa semirimorchio

### 5.2 Estrazione BG da testo planning

Il campo `planning` è testo libero. I codici BG si estraggono con regex:

**Regex**: `r'#(\d{2}[A-Z]\d+[_\d]*)'`

Esempi:
- `"#26A02612 | Carica Terni → Scarica Milano"` → `["26A02612"]`
- `"#26A02612_01 + #26A02612_02 doppio"` → `["26A02612_01", "26A02612_02"]`
- `"Manutenzione programmata"` → `[]` (nessun BG)

### 5.3 Viaggi da pianificare (via Planning Agent)

Per avere i dettagli dei viaggi (luogo carico/scarico, date, cliente):

```
POST http://192.168.0.14:8602/api/planning/viaggi
{"data": "2026-04-27"}
```

Response: lista viaggi con `bg`, `luogo_carico`, `luogo_scarico`, `data_carico`, `data_scarico`, `cliente`, `genere`, `vettore`, `targa`, ecc.

### 5.4 Invio notifiche

**POST `http://192.168.0.12:9095/api/notifications/send`**

Auth: Header `X-API-Key: {berlink_api_key}` (con scope `cd`).

Request:
```json
{
  "notification_type": "planning_check",
  "title": "ETA fuori orario: BG 26A02612",
  "message": "L'autista Rossi Mario (targa XA 821 YL) per BG 26A02612 ha ETA alle 18:30, fuori dalla finestra di scarico (08:00-12:00, 15:00-16:30). Proposta: riassegnare a Bianchi Luigi che è a 45km dal carico.",
  "group_code": "BG_DISCORDI",
  "entity_type": "trailer",
  "entity_id": "123",
  "link": "/planning?date=2026-04-27"
}
```

Campi:
- `notification_type`: **obbligatorio** — usare sempre `"planning_check"`
- `title`: **obbligatorio** — max 200 char
- `message`: opzionale — testo dettagliato (output dell'LLM agent)
- `group_code`: broadcast a tutti i membri del gruppo (alternativa: `id_user_receiver`, `username_receiver`, `email_receiver`)
- `link`: opzionale — URL relativo per navigazione UI BERLink
- `entity_type`, `entity_id`: opzionali

Response `202 Accepted`:
```json
{
  "success": true,
  "data": {"request_id": "1776709511554-0", "id_user_receiver": 42},
  "message": "Notifica accodata"
}
```

---

## 6. LLM Agent — OpenRouter

### Configurazione

Il monitor agent usa l'API **OpenRouter** (compatibile OpenAI) per l'analisi intelligente.

```yaml
# config/settings.yaml → llm
llm:
  provider: "openrouter"
  base_url: "https://openrouter.ai/api/v1"
  model: "anthropic/claude-3-5-haiku"
  temperature: 0.1
  max_tokens: 2000
```

API key: variabile ambiente `OPENROUTER_API_KEY` (file `.env`).

### Client

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["OPENROUTER_API_KEY"],
    base_url="https://openrouter.ai/api/v1"
)
```

### System Prompt

```
Sei il Monitor Agent per la pianificazione trasporti della ditta Bernardini.

Il tuo ruolo è analizzare anomalie rilevate nei viaggi pianificati di oggi e:
1. Valutare la gravità reale del problema
2. Cercare possibili soluzioni usando i tool disponibili
3. Formulare una notifica chiara e operativa con proposta d'azione

TOOL DISPONIBILI:
- localizza_entita: localizza autista/semirimorchio (GPS, eventi, planning)
- calcola_eta_autista: calcola ETA dato BG e targa
- get_eta_per_autista: calcola ETA cercando per nome autista
- check_orari_sede: verifica orari apertura sede carico/scarico
- trova_autista_piu_vicino: trova autista alternativo più vicino a una località
- get_autisti_disponibili: lista autisti disponibili oggi
- calcola_distanza: distanza in km tra due località

DATA DI LAVORO: {data_oggi}

REGOLE:
- Sii conciso e operativo
- Includi sempre: cosa succede, perché è un problema, cosa si può fare
- Se c'è un autista disponibile più vicino, proponilo
- NON INVENTARE DATI — usa solo informazioni dai tool
- Rispondi in italiano
- Formato output: JSON con campi "titolo", "messaggio", "severita", "azione_proposta"
```

### Tool definitions per l'LLM

I tool dell'LLM agent corrispondono alle chiamate HTTP al Planning Agent. Lo schema completo si ottiene dinamicamente da `GET http://192.168.0.14:8602/api/planning/tools` (ritorna array OpenAI function calling format).

Il monitor all'avvio:
1. Chiama `GET /api/planning/tools` per ottenere lo schema completo
2. Filtra solo i tool di sola lettura (whitelist)
3. Usa quello schema per il function calling dell'LLM

**Whitelist tool** (sola lettura, sicuri per il monitor):
```python
MONITOR_TOOL_NAMES = {
    "localizza_entita",
    "calcola_eta_autista",
    "get_eta_per_autista",
    "check_orari_sede",
    "trova_autista_piu_vicino",
    "get_autisti_disponibili",
    "get_pianificazione_corrente",
    "calcola_distanza",
    "calcola_etoa",
    "cerca_bg_da_targa",
}
```

### Tool call loop

Quando l'LLM chiede di eseguire un tool, il monitor lo traduce in una chiamata HTTP:

```python
# LLM chiede: tool_call(name="localizza_entita", arguments={"tipo": "semirimorchio", "identificativo": "XA 821 YL"})
# Monitor traduce in:
response = httpx.post(
    f"{planner_base_url}/api/planning/execute",
    json={"tool": "localizza_entita", "args": {"tipo": "semirimorchio", "identificativo": "XA 821 YL"}}
)
result = response.json()
# Ritorna il result all'LLM come tool response
```

Loop standard:
```python
while True:
    response = client.chat.completions.create(model=model, messages=messages, tools=tools_schema)
    message = response.choices[0].message
    messages.append(message)

    if message.tool_calls:
        for tc in message.tool_calls:
            args = json.loads(tc.function.arguments)
            # Esegui via HTTP al Planning Agent
            result = planner_client.execute_tool(tc.function.name, args)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
        continue

    return message.content  # Risposta finale
```

---

## 7. Logica dei check

### 7.1 Check ETA vs Orari Sede (primo check)

Per ogni riga planning del giorno con autista assegnato:

1. **Estrarre BG** dal campo `planning` (regex `#(\d{2}[A-Z]\d+[_\d]*)`)
2. **Cross-reference con viaggi TIR**: match BG estratti con la lista viaggi (da `POST /api/planning/viaggi`) per avere `luogo_carico`, `luogo_scarico`, `data_scarico`
3. **Fallback cross-reference per targa**: se regex non trova BG, match `plate_number` della riga planning con campo `targa` dei viaggi TIR
4. **Calcolare ETA**: `POST /api/planning/execute` con tool `calcola_eta_autista` (bg, targa, luogo_scarico, data_scarico)
5. **Calcolare ETOA**: `POST /api/planning/execute` con tool `calcola_etoa` (eta, bg, data) — tiene conto orari sede
6. **Regole alert**:
   - **CRITICAL**: ETOA cade il giorno dopo (l'operazione non si completa in giornata)
   - **WARNING**: ETOA > ETA (arrivo fuori finestra apertura, ma si completa in giornata)

### 7.2 Flusso ciclo completo

```
1. Fetch dati (una volta per ciclo):
   a. Planning giornaliero: POST BERLink /api/Query/execute (query pl_trailer_planning)
   b. Viaggi TIR: POST Planning Agent /api/planning/viaggi

2. Per ogni check registrato e abilitato:
   a. Esegui check.run(data, planning_rows, viaggi)
   b. Il check chiama il Planning Agent via HTTP per ETA, ETOA, ecc.
   c. Ritorna lista CheckAlert

3. Filtra per dedup (non ri-notificare stessa anomalia)

4. Se alert nuovi e LLM abilitato:
   a. Passa alert + contesto al Monitor LLM Agent
   b. L'agent chiama tool via HTTP (Planning Agent) per approfondire
   c. Formula notifica intelligente con proposta d'azione

5. Invia notifiche a BERLink: POST /api/notifications/send

6. Cleanup dedup scaduti
```

---

## 8. Componenti — Design

### 8.1 Planner Client (`monitor/planner_client.py`)

Client HTTP per il Planning Agent API :8602.

```python
import httpx
import json
import logging

logger = logging.getLogger("planning-monitor.planner-client")

class PlannerClient:
    """Client HTTP per il Planning Agent API."""

    def __init__(self, base_url: str, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def execute_tool(self, tool_name: str, args: dict) -> dict:
        """Esegue un tool via POST /api/planning/execute."""
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                f"{self.base_url}/api/planning/execute",
                json={"tool": tool_name, "args": args}
            )
            resp.raise_for_status()
            return resp.json()

    def get_viaggi(self, data: str) -> dict:
        """GET viaggi da pianificare."""
        return self.execute_tool("get_viaggi_da_pianificare", {"data": data})

    def get_tools_schema(self) -> list:
        """GET schema tool (OpenAI format) per il monitor LLM agent."""
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(f"{self.base_url}/api/planning/tools")
            resp.raise_for_status()
            return resp.json()

    def health(self) -> bool:
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.get(f"{self.base_url}/api/planning/health")
                return resp.status_code == 200
        except Exception:
            return False
```

### 8.2 BERLink Client (`monitor/berlink_client.py`)

Client HTTP per BERLink :9095 (planning data + notifiche).

```python
class BERLinkClient:
    """Client HTTP per BERLink API."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self):
        return {"X-API-Key": self.api_key, "Content-Type": "application/json"}

    def get_pianificazione_giornaliera(self, data: str) -> list:
        """Query planning giornaliero da BERLink."""
        query = f"""SELECT p.id_trailer_planning, p.id_trailer, p.note,
            p.info_maintenance, p.id_employee, p.planning, p.planning_date,
            p.flag_ack, t.plate_number, t.id_trailer_type,
            e.name as driver_name, e.surname as driver_surname
            FROM pl_trailer_planning p
            JOIN flt_trailers t ON p.id_trailer = t.id_trailer
            LEFT JOIN emp_employees e ON p.id_employee = e.id_employee
            WHERE p.planning_date = '{data}'
            ORDER BY t.plate_number"""
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                f"{self.base_url}/api/Query/execute",
                headers=self._headers(), json={"query": query}
            )
            resp.raise_for_status()
            return resp.json().get("data", [])

    def send_notification(self, notification: dict) -> dict:
        """POST notifica a BERLink."""
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                f"{self.base_url}/api/notifications/send",
                headers=self._headers(), json=notification
            )
            resp.raise_for_status()
            return resp.json()
```

### 8.3 Check Registry (`monitor/registry.py`)

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

@dataclass
class CheckAlert:
    check_name: str           # "eta_orari"
    severity: str             # "warning" | "critical"
    title: str                # Titolo breve per notifica
    message: str              # Messaggio dettagliato
    context: dict = field(default_factory=dict)  # Dati strutturati per l'LLM agent
    entity_type: Optional[str] = None   # "trailer" | "trip"
    entity_id: Optional[str] = None     # targa o BG
    dedup_key: str = ""                 # Chiave univoca (es. "eta_orari:26A02612:XA821YL")

class BaseCheck(ABC):
    name: str = "unnamed"

    @abstractmethod
    def run(self, data: date, planning_rows: list, viaggi: dict,
            planner_client, berlink_client) -> List[CheckAlert]:
        """
        Esegue il check.

        Args:
            data: data di oggi
            planning_rows: righe planning da BERLink (list of dict)
            viaggi: output di get_viaggi_da_pianificare dal Planning Agent (dict/list)
            planner_client: PlannerClient per chiamare tool via HTTP
            berlink_client: BERLinkClient per query aggiuntive
        """
        ...

_checks: List[BaseCheck] = []

def register_check(check: BaseCheck):
    _checks.append(check)

def get_registered_checks() -> List[BaseCheck]:
    return list(_checks)
```

**Estendibilità**: nuovo check = nuovo file in `monitor/checks/`, subclass `BaseCheck`, chiama `register_check()`. Auto-import via `pkgutil.iter_modules` nell'`__init__.py`.

### 8.4 Notifier (`monitor/notifier.py`)

- Invio a BERLink via `BERLinkClient.send_notification()`
- Body: `notification_type: "planning_check"`, `title`, `message`, `group_code`
- **Dedup in-memory**: dict `{dedup_key: last_sent_datetime}`, TTL configurabile (default 8h)
- Un invio per ogni receiver in config
- `cleanup_expired()` ogni ciclo

### 8.5 Monitor LLM Agent (`monitor/monitor_agent.py`)

```python
class MonitorAgent:
    def __init__(self, planner_client: PlannerClient, llm_config: dict):
        self.planner = planner_client
        self.client = OpenAI(
            api_key=os.environ["OPENROUTER_API_KEY"],
            base_url=llm_config.get("base_url", "https://openrouter.ai/api/v1"),
        )
        self.model = llm_config.get("model", "anthropic/claude-3-5-haiku")
        # Fetch tool schema dal Planning Agent e filtra per whitelist
        all_tools = planner_client.get_tools_schema()
        self.tools_schema = [t for t in all_tools if t["function"]["name"] in MONITOR_TOOL_NAMES]

    def analyze(self, alerts: list, planning_context: str) -> str:
        """Analizza alert con LLM. I tool call vengono eseguiti via HTTP al Planning Agent."""
        messages = [
            {"role": "system", "content": MONITOR_SYSTEM_PROMPT.format(...)},
            {"role": "user", "content": alert_context}
        ]
        for _ in range(10):
            response = self.client.chat.completions.create(
                model=self.model, messages=messages, tools=self.tools_schema
            )
            msg = response.choices[0].message
            messages.append(msg)

            if msg.tool_calls:
                for tc in msg.tool_calls:
                    args = json.loads(tc.function.arguments)
                    result = self.planner.execute_tool(tc.function.name, args)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
                continue
            return msg.content
```

### 8.6 Engine one-shot (`monitor/engine.py`)

```python
from datetime import date
from typing import List, Optional
import logging

logger = logging.getLogger("planning-monitor.engine")

def run_check(
    data: str = None,
    notify: bool = False,
    use_llm: bool = True,
    checks: Optional[List[str]] = None,
) -> dict:
    """
    Esegue un ciclo completo di check. Entry point one-shot.

    Args:
        data:    Data YYYY-MM-DD (default: oggi)
        notify:  Se True, invia notifiche BERLink per le anomalie trovate
        use_llm: Se True, usa LLM per analisi intelligente delle anomalie
        checks:  Lista check specifici da eseguire (default: tutti)

    Returns:
        dict JSON-serializzabile con risultato completo
    """
    config = load_config()
    planner = PlannerClient(config["planner"]["base_url"])
    berlink = BERLinkClient(config["berlink"]["base_url"], config["berlink"]["api_key"])

    data_obj = date.fromisoformat(data) if data else date.today()
    data_str = data_obj.isoformat()

    # 1. Fetch dati
    planning_rows = berlink.get_pianificazione_giornaliera(data_str)
    viaggi = planner.get_viaggi(data_str)

    # 2. Check deterministici
    all_alerts = []
    checks_eseguiti = 0
    for check in get_registered_checks():
        if checks and check.name not in checks:
            continue
        alerts = check.run(data_obj, planning_rows, viaggi, planner, berlink)
        all_alerts.extend(alerts)
        checks_eseguiti += 1

    # 3. LLM analisi (opzionale)
    analisi_llm = None
    if use_llm and all_alerts:
        try:
            agent = MonitorAgent(planner, config["llm"])
            analisi_llm = agent.analyze(all_alerts, _summarize(planning_rows))
        except Exception as e:
            logger.error(f"LLM analisi fallita: {e}", exc_info=True)

    # 4. Notifiche (opzionale)
    notifiche_inviate = 0
    if notify and all_alerts:
        notifier = MonitorNotifier(berlink, config["monitor"])
        notifiche_inviate = notifier.send_batch(all_alerts, llm_message=analisi_llm)

    return {
        "success": True,
        "data": data_str,
        "checks_eseguiti": checks_eseguiti,
        "anomalie_trovate": len(all_alerts),
        "anomalie": [
            {
                "check": a.check_name,
                "severity": a.severity,
                "title": a.title,
                "message": a.message,
                "entity_type": a.entity_type,
                "entity_id": a.entity_id,
                "context": a.context,
            }
            for a in all_alerts
        ],
        "analisi_llm": analisi_llm,
        "notifiche_inviate": notifiche_inviate,
    }
```

### 8.8 Server FastAPI (`server.py`)

Il monitor espone i propri tool nello **stesso formato** del Planning Agent (OpenAI function calling), permettendo a qualsiasi sistema di scoprirli e invocarli.

```python
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional, List
from monitor.engine import run_check
from monitor.registry import get_registered_checks

app = FastAPI(title="Planning Monitor", version="1.0.0")

# --- Tool schema (stesso formato OpenAI function calling di ai-planner) ---

MONITOR_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "agent_check",
            "description": "Esegue i check di monitoraggio sulla pianificazione del giorno. "
                           "Verifica ETA vs orari sede, anomalie, e opzionalmente analizza con LLM "
                           "e invia notifiche BERLink.",
            "parameters": {
                "type": "object",
                "properties": {
                    "data": {"type": "string", "description": "Data in formato YYYY-MM-DD (default: oggi)"},
                    "notify": {"type": "boolean", "description": "Se true, invia notifiche BERLink", "default": False},
                    "use_llm": {"type": "boolean", "description": "Se true, usa LLM per analisi", "default": True},
                    "checks": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Lista check specifici da eseguire (default: tutti)"
                    },
                },
                "required": [],
            },
        },
    }
]

MONITOR_TOOLS_FUNCTIONS = {
    "agent_check": run_check,
}

# --- Endpoint tool discovery (identico pattern ai-planner) ---

@app.get("/api/monitor/tools")
async def get_tools():
    """Schema tool in formato OpenAI function calling."""
    return MONITOR_TOOLS_SCHEMA

@app.post("/api/monitor/execute")
async def execute_tool(request: dict):
    """Invoca un tool per nome (identico a /api/planning/execute di ai-planner)."""
    tool_name = request.get("tool")
    args = request.get("args", {})
    func = MONITOR_TOOLS_FUNCTIONS.get(tool_name)
    if not func:
        return {"error": f"Tool '{tool_name}' non trovato",
                "available": list(MONITOR_TOOLS_FUNCTIONS.keys())}
    return func(**args)

# --- Endpoint dedicati ---

class CheckRequest(BaseModel):
    data: Optional[str] = None
    notify: bool = False
    use_llm: bool = True
    checks: Optional[List[str]] = None

@app.post("/api/monitor/check")
async def api_check(req: CheckRequest):
    """Esegue un ciclo completo di check (endpoint diretto)."""
    return run_check(data=req.data, notify=req.notify,
                     use_llm=req.use_llm, checks=req.checks)

@app.get("/api/monitor/health")
async def health():
    """Health check del monitor."""
    checks = get_registered_checks()
    return {
        "status": "ok",
        "tools": len(MONITOR_TOOLS_SCHEMA),
        "tool_names": list(MONITOR_TOOLS_FUNCTIONS.keys()),
        "checks_registrati": [c.name for c in checks],
    }

@app.get("/api/monitor/checks")
async def list_checks():
    """Lista check disponibili."""
    return [{"name": c.name, "description": c.__doc__ or ""} for c in get_registered_checks()]
```

**Porta**: 8610 (configurabile in settings.yaml)
**Avvio**: `python run.py serve` oppure `uvicorn server:app --host 0.0.0.0 --port 8610`

**Invocazione dall'esterno** — esempi:

```bash
# Scopri tool disponibili (come farebbe un LLM o un altro agent)
curl http://192.168.0.14:8610/api/monitor/tools

# Invoca via protocollo generico (identico a ai-planner)
curl -X POST http://192.168.0.14:8610/api/monitor/execute \
  -H 'Content-Type: application/json' \
  -d '{"tool": "agent_check", "args": {"data": "2026-04-28", "notify": true}}'

# Invoca via endpoint diretto
curl -X POST http://192.168.0.14:8610/api/monitor/check \
  -H 'Content-Type: application/json' \
  -d '{"data": "2026-04-28", "notify": true}'

# Scheduling via cron (ogni 5 min, lun-ven 6-20)
*/5 6-19 * * 1-5 curl -s -X POST http://192.168.0.14:8610/api/monitor/check \
  -H 'Content-Type: application/json' \
  -d '{"data":"'$(date +\%Y-\%m-\%d)'","notify":true}' \
  >> /var/log/planning-monitor.log
```

### 8.7 Check ETA Orari (`monitor/checks/eta_orari_check.py`)

```python
import re
from datetime import date
from typing import List
from ..registry import BaseCheck, CheckAlert, register_check

BG_REGEX = re.compile(r'#(\d{2}[A-Z]\d+[_\d]*)')

class ETAOrariCheck(BaseCheck):
    name = "eta_orari"

    def run(self, data, planning_rows, viaggi, planner_client, berlink_client):
        alerts = []
        data_str = data.isoformat()

        # Indicizza viaggi per BG
        viaggi_list = viaggi if isinstance(viaggi, list) else viaggi.get("viaggi", [])
        viaggi_by_bg = {}
        for v in viaggi_list:
            bg = v.get("bg") or v.get("BG", "")
            if bg:
                viaggi_by_bg[bg.upper()] = v

        for row in planning_rows:
            if not row.get("id_employee"):
                continue  # Skip righe senza autista

            plate = row.get("plate_number", "")
            driver = f"{row.get('driver_name', '')} {row.get('driver_surname', '')}".strip()
            planning_text = row.get("planning") or ""

            # Estrai BG da testo
            bg_codes = BG_REGEX.findall(planning_text)

            # Fallback: match per targa
            if not bg_codes:
                for bg, v in viaggi_by_bg.items():
                    if v.get("targa", "").replace(" ", "") == plate.replace(" ", ""):
                        bg_codes.append(bg)

            for bg in bg_codes:
                viaggio = viaggi_by_bg.get(bg.upper())
                if not viaggio:
                    continue

                # Calcola ETA via Planning Agent HTTP
                eta_result = planner_client.execute_tool("calcola_eta_autista", {
                    "bg": bg, "targa": plate,
                    "luogo_scarico": viaggio.get("luogo_scarico", ""),
                    "data_scarico": viaggio.get("data_scarico", ""),
                })

                eta_str = eta_result.get("eta")
                if not eta_str:
                    continue

                # Calcola ETOA via Planning Agent HTTP
                etoa_result = planner_client.execute_tool("calcola_etoa", {
                    "eta": eta_str, "bg": bg, "data": data_str,
                })

                etoa_str = etoa_result.get("etoa")
                etoa_data = etoa_result.get("etoa_data", data_str)
                etoa_detail = etoa_result.get("dettagli", "")

                # CRITICAL: ETOA giorno dopo
                if etoa_data and etoa_data > data_str:
                    alerts.append(CheckAlert(
                        check_name=self.name, severity="critical",
                        title=f"ETA fuori orario: {bg}",
                        message=f"Autista {driver} ({plate}) per BG {bg}: "
                                f"ETA {eta_result.get('eta_orario', eta_str)}, "
                                f"disponibilità spostata a {etoa_str}. {etoa_detail}",
                        context={"bg": bg, "targa": plate, "eta": eta_str,
                                 "etoa": etoa_str, "autista": driver,
                                 "luogo_scarico": viaggio.get("luogo_scarico", ""),
                                 "luogo_carico": viaggio.get("luogo_carico", "")},
                        entity_type="trailer", entity_id=plate,
                        dedup_key=f"eta_orari:{bg}:{plate}",
                    ))

                # WARNING: arrivo fuori finestra stesso giorno
                elif etoa_str and eta_str and etoa_str > eta_str:
                    alerts.append(CheckAlert(
                        check_name=self.name, severity="warning",
                        title=f"Arrivo fuori finestra: {bg}",
                        message=f"Autista {driver} ({plate}) BG {bg}: "
                                f"ETA {eta_result.get('eta_orario', eta_str)} fuori finestra apertura. {etoa_detail}",
                        context={"bg": bg, "targa": plate, "eta": eta_str,
                                 "etoa": etoa_str, "autista": driver},
                        entity_type="trailer", entity_id=plate,
                        dedup_key=f"eta_orari:{bg}:{plate}",
                    ))

        return alerts

register_check(ETAOrariCheck())
```

---

## 9. Configurazione — `config/settings.yaml`

```yaml
# Planning Agent API (ai-planner esistente)
planner:
  base_url: "http://192.168.0.14:8602"
  timeout: 120.0

# BERLink API
berlink:
  base_url: "http://192.168.0.12:9095"
  api_key: "0e223634-4c2a-44f2-8139-960f03964933"
  timeout: 60.0

# LLM (OpenRouter)
llm:
  provider: "openrouter"
  base_url: "https://openrouter.ai/api/v1"
  model: "anthropic/claude-3-5-haiku"
  temperature: 0.1
  max_tokens: 2000

# Server HTTP (tool endpoint)
server:
  host: "0.0.0.0"
  port: 8610

# Monitor
monitor:
  check_interval_seconds: 300       # 5 minuti (solo per comando daemon)
  notification_receivers:
    - group_code: "BG_DISCORDI"    # Broadcast al gruppo
  dedup_ttl_hours: 8                # Non ri-notificare stessa anomalia entro 8h (solo daemon)
  checks:
    eta_orari:
      enabled: true
```

### File `.env`

```bash
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

---

## 10. Dipendenze — `requirements.txt`

```
httpx>=0.26.0
openai>=1.0.0
typer>=0.9.0
rich>=13.0.0
pyyaml>=6.0
python-dotenv>=1.0.0
fastapi>=0.110.0
uvicorn>=0.27.0
```

Nessuna dipendenza pesante: il monitor è un thin client HTTP + LLM. Tutta la logica pesante (geocoding, GPS, routing, ETA) gira nel Planning Agent. Il server FastAPI espone i tool nello stesso formato del Planning Agent.

---

## 11. CLI — `run.py`

```python
import typer
from typing import Optional
from rich.console import Console
from rich import print_json
import json

app = typer.Typer(name="planning-monitor", help="Monitor autonomo pianificazione trasporti")
console = Console()

@app.command()
def check(
    data: Optional[str] = typer.Argument(None, help="Data YYYY-MM-DD (default: oggi)"),
    notify: bool = typer.Option(False, "--notify", "-n", help="Invia notifiche BERLink"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Solo check deterministici, senza LLM"),
    checks: Optional[str] = typer.Option(None, "--checks", "-c", help="Check specifici (comma-separated)"),
):
    """Esegue un ciclo di check sulla pianificazione (one-shot, modalità standard)."""
    from monitor.engine import run_check

    checks_list = checks.split(",") if checks else None
    result = run_check(data=data, notify=notify, use_llm=not no_llm, checks=checks_list)

    print_json(json.dumps(result, ensure_ascii=False, indent=2))

    n = result.get("anomalie_trovate", 0)
    if n > 0:
        console.print(f"\n[yellow]{n} anomalie trovate[/yellow]")
    else:
        console.print("\n[green]Nessuna anomalia[/green]")

@app.command()
def serve(
    port: int = typer.Option(8610, "--port", "-p", help="Porta HTTP"),
    host: str = typer.Option("0.0.0.0", "--host", help="Host binding"),
):
    """Avvia il server HTTP del monitor (espone tool come ai-planner)."""
    import uvicorn
    console.print(f"[bold blue]Planning Monitor Server[/bold blue] su {host}:{port}")
    console.print(f"  Tools:   GET  http://{host}:{port}/api/monitor/tools")
    console.print(f"  Execute: POST http://{host}:{port}/api/monitor/execute")
    console.print(f"  Check:   POST http://{host}:{port}/api/monitor/check")
    console.print(f"  Health:  GET  http://{host}:{port}/api/monitor/health")
    uvicorn.run("server:app", host=host, port=port)

@app.command()
def daemon(
    interval: int = typer.Option(300, "--interval", "-i", help="Secondi tra cicli"),
    notify: bool = typer.Option(True, "--notify", "-n"),
    no_llm: bool = typer.Option(False, "--no-llm"),
):
    """Loop continuo (alternativa a cron + endpoint HTTP)."""
    import time, signal
    from monitor.engine import run_check

    running = True
    def _stop(*_): nonlocal running; running = False
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    console.print(f"[bold blue]Planning Monitor Daemon[/bold blue] — ogni {interval}s")
    while running:
        try:
            result = run_check(notify=notify, use_llm=not no_llm)
            n = result.get("anomalie_trovate", 0)
            console.print(f"[dim]{result['data']}[/dim] — {n} anomalie")
        except Exception as e:
            console.print(f"[red]Errore: {e}[/red]")
        time.sleep(interval)

if __name__ == "__main__":
    app()
```

Comandi:
- `python run.py check` → one-shot oggi (**modalità standard**)
- `python run.py check 2026-04-28` → one-shot data specifica
- `python run.py check --notify` → con invio notifiche BERLink
- `python run.py check --no-llm` → solo check deterministici
- `python run.py check --checks eta_orari` → solo check specifici
- `python run.py serve` → avvia server HTTP su :8610 (tool invocabili dall'esterno)
- `python run.py daemon` → loop continuo (per chi non vuole cron)

---

## 12. Check futuri (esempi di estensione)

| Check | Descrizione |
|-------|-------------|
| `driver_rest_check` | Verifica rispetto ore guida EU 561 (9h/giorno, 56h/settimana) |
| `unassigned_trip_check` | Viaggi vicini alla scadenza senza autista assegnato |
| `gps_stale_check` | Autista assegnato ma GPS fermo da troppo tempo |
| `driver_proximity_check` | Autista assegnato ma troppo lontano dal punto di carico |
| `maintenance_conflict_check` | Semirimorchio in manutenzione ma assegnato a viaggio |

Ogni check = 1 file in `monitor/checks/`, subclass `BaseCheck`, nessuna modifica al resto.

---

## 13. Prerequisiti

1. **Planning Agent** (ai-planner) in esecuzione su porta 8602 — verifica con `GET http://192.168.0.14:8602/api/planning/health`
2. **BERLink** accessibile su porta 9095 con API key con scope `cd`
3. **notification_type `planning_check`** deve esistere nella tabella `c_ntf_notification_types` di BERLink
4. **Utenti receiver** (es. `b.guido`) devono esistere in `emp_employees`
5. **`OPENROUTER_API_KEY`** nel file `.env`

---

## 14. Verifica

### CLI one-shot (modalità standard)

1. `python run.py check --no-llm` — check deterministici senza LLM (oggi)
2. `python run.py check 2026-04-28` — check con analisi LLM su data specifica
3. `python run.py check --notify` — check + invio notifiche BERLink
4. Verificare output JSON con anomalie trovate

### Server HTTP (tool invocabili dall'esterno)

5. `python run.py serve` — avvia server su :8610
6. `curl http://localhost:8610/api/monitor/health` — health check
7. `curl http://localhost:8610/api/monitor/tools` — schema tool (OpenAI format)
8. `curl -X POST http://localhost:8610/api/monitor/execute -H 'Content-Type: application/json' -d '{"tool":"agent_check","args":{"data":"2026-04-28"}}'` — invocazione tool generica
9. `curl -X POST http://localhost:8610/api/monitor/check -H 'Content-Type: application/json' -d '{"data":"2026-04-28","notify":true}'` — endpoint diretto
10. Verificare notifica su BERLink (campanella, notification_type: planning_check)

### Daemon (opzionale)

11. `python run.py daemon --interval 60` — loop ogni 60s per test
12. Verificare log ciclico con anomalie trovate
