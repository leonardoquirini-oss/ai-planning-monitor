# Check Reference — Planning Monitor

Documentazione di tutti i check implementati nel Planning Monitor.
Ogni check viene eseguito dall'engine (`monitor/engine.py`) e produce una lista di `CheckAlert`.

Questo file serve anche come **reference** per il formato standard degli alert e per le convenzioni da rispettare quando si implementano nuovi check.

---

## Formato comune: `CheckAlert`

Ogni check ritorna una lista di `CheckAlert` (definito in `models/__init__.py`).

### Campi

| Campo          | Tipo            | Descrizione                                                                 |
|----------------|-----------------|-----------------------------------------------------------------------------|
| `check_name`   | `str`           | Identificativo del check (es. `"eta_orari"`). Deve coincidere con `BaseCheck.name`. |
| `severity`     | `str`           | Livello di gravita: `"warning"` o `"critical"`.                             |
| `title`        | `str`           | Titolo breve dell'anomalia (max 200 char). Usato come titolo notifica BERLink. |
| `message`      | `str`           | Descrizione leggibile del problema: cosa succede, autista, BG, orari.       |
| `context`      | `dict`          | Dati strutturati passati al Monitor LLM Agent per analisi approfondita.     |
| `entity_type`  | `str` o `None`  | Tipo entita coinvolta: `"trailer"`, `"trip"`, ecc.                          |
| `entity_id`    | `str` o `None`  | Identificativo entita: targa semirimorchio o codice BG.                     |
| `dedup_key`    | `str`           | Chiave univoca per deduplicazione notifiche. Formato: `"{check}:{bg}:{targa}"`. |

### Severity

| Valore       | Significato                                                                 |
|--------------|-----------------------------------------------------------------------------|
| `"critical"` | L'operazione non si completa in giornata. Richiede intervento immediato.    |
| `"warning"`  | Anomalia rilevata ma gestibile in giornata. Attenzione consigliata.         |

### Esempio alert JSON (come restituito dall'engine)

```json
{
  "check": "eta_orari",
  "severity": "critical",
  "title": "ETA fuori orario: 26A02612",
  "message": "Autista Rossi Mario (XA 821 YL) per BG 26A02612: ETA 18:30, disponibilita spostata a 2026-04-29T08:00:00. ETA 18:30 fuori finestra pomeriggio 15:00-16:30 -> prossima apertura domani 08:00.",
  "entity_type": "trailer",
  "entity_id": "XA 821 YL",
  "context": {
    "bg": "26A02612",
    "targa": "XA 821 YL",
    "eta": "2026-04-28T18:30:00",
    "etoa": "2026-04-29T08:00:00",
    "autista": "Rossi Mario",
    "data": "2026-04-28",
    "luogo_scarico": "Milano",
    "luogo_carico": "Terni"
  }
}
```

---

## Check implementati

### 1. `eta_orari` — ETA vs Orari Apertura Sede

**File**: `monitor/checks/eta_orari_check.py`
**Classe**: `ETAOrariCheck`

#### Scopo

Verifica che ogni autista assegnato a un viaggio riesca a completare l'operazione di scarico **entro gli orari di apertura della sede destinataria**, nella giornata prevista.

Combina l'ETA di arrivo (calcolata dal Planning Agent tramite GPS, missioni, routing) con gli orari di apertura sede per ottenere l'ETOA (Estimated Time of Operation Availability) — cioe il momento in cui l'operazione di scarico puo effettivamente iniziare e concludersi.

#### Dati in ingresso

| Parametro        | Fonte                     | Descrizione                                              |
|------------------|---------------------------|----------------------------------------------------------|
| `planning_rows`  | BERLink query SQL         | Righe planning giornaliero (semirimorchio + autista)     |
| `viaggi`         | Planning Agent `/viaggi`  | Lista viaggi TIR con BG, luoghi, date, cliente           |
| `planner_client` | —                         | Client HTTP per chiamare tool del Planning Agent         |
| `data`           | Parametro engine          | Data di riferimento del check                            |

#### Logica step-by-step

**Step 1 — Indicizzazione viaggi**

Costruisce due lookup dai viaggi TIR:
- `viaggi_by_bg`: dizionario `{BG_UPPER: viaggio}` — per cross-reference con i BG estratti dal planning
- `viaggi_by_targa`: dizionario `{targa_senza_spazi: viaggio}` — fallback quando il BG non e presente nel testo

**Step 2 — Iterazione righe planning**

Per ogni riga con `id_employee != null` (autista assegnato):

1. Legge `plate_number`, nome autista (`driver_name` + `driver_surname`), testo `planning`
2. Righe senza autista vengono saltate (niente da verificare)

**Step 3 — Estrazione codici BG**

Regex applicata al campo `planning` (testo libero):

```
#(\d{2}[A-Z]\d+[_\d]*)
```

Cattura il gruppo dopo `#`. Esempi:

| Testo planning                              | BG estratti                    |
|---------------------------------------------|--------------------------------|
| `#26A02612 \| Carica Terni -> Scarica MI`   | `["26A02612"]`                 |
| `#26A02612_01 + #26A02612_02 doppio`        | `["26A02612_01", "26A02612_02"]` |
| `Manutenzione programmata`                  | `[]` (nessun BG)              |

**Fallback per targa**: se la regex non trova BG, cerca `plate_number` (senza spazi) in `viaggi_by_targa` e recupera il BG dal viaggio corrispondente.

**Step 4 — Per ogni BG: cross-reference con viaggio**

Cerca il BG (uppercased) in `viaggi_by_bg`. Se non c'e corrispondenza il BG viene saltato — non e un viaggio noto nel sistema.

**Step 5 — Calcolo ETA** (chiamata HTTP al Planning Agent)

```
POST /api/planning/execute
{
  "tool": "calcola_eta_autista",
  "args": {
    "bg": "26A02612",
    "targa": "XA 821 YL",
    "luogo_scarico": "Milano",
    "data_scarico": "2026-04-28"
  }
}
```

Il Planning Agent applica la sua cascata interna per determinare l'ETA:
1. Missione TFP (se gia presente con ETA)
2. Evento GPS (arrivo/partenza recente)
3. GPS live + routing stradale (posizione attuale -> destinazione)
4. Fallback DataS (stima dal sistema legacy)

Se il tool non restituisce `eta`, il BG viene saltato (dati insufficienti).

**Step 6 — Calcolo ETOA** (chiamata HTTP al Planning Agent)

```
POST /api/planning/execute
{
  "tool": "calcola_etoa",
  "args": {
    "eta": "2026-04-28T14:30:00",
    "bg": "26A02612",
    "data": "2026-04-28"
  }
}
```

Il tool `calcola_etoa` combina l'ETA con gli orari di apertura della sede di scarico:
- Se ETA dentro finestra aperta -> ETOA = ETA + durata operazione
- Se ETA in pausa pranzo (es. 12:00-15:00) -> ETOA slitta alla riapertura pomeridiana
- Se ETA dopo chiusura serale -> ETOA slitta al giorno lavorativo successivo
- Salta weekend e festivita se configurati

Ritorna `etoa` (datetime), `etoa_data` (solo data), `dettagli` (spiegazione testuale).

**Step 7 — Valutazione alert**

| Condizione                               | Severity     | Titolo                           | Significato                                      |
|------------------------------------------|--------------|----------------------------------|--------------------------------------------------|
| `etoa_data > data` (giorno successivo)   | **critical** | `ETA fuori orario: {bg}`        | L'operazione non si completa oggi. Scarico rimandato a domani o oltre. |
| `etoa > eta` (stesso giorno)             | **warning**  | `Arrivo fuori finestra: {bg}`   | L'autista arriva fuori finestra apertura, ma si recupera in giornata (es. dopo pausa pranzo). |
| Nessuna delle due                        | —            | —                                | Nessun problema: ETA dentro finestra, operazione regolare. |

#### Campi `context` prodotti

Il dizionario `context` dell'alert contiene i dati strutturati che il Monitor LLM Agent usa per approfondire l'analisi e proporre soluzioni:

| Campo           | Tipo   | Presente in        | Descrizione                                |
|-----------------|--------|--------------------|--------------------------------------------|
| `bg`            | `str`  | critical, warning  | Codice BG del viaggio                      |
| `targa`         | `str`  | critical, warning  | Targa semirimorchio                        |
| `eta`           | `str`  | critical, warning  | ETA calcolata (datetime ISO)               |
| `etoa`          | `str`  | critical, warning  | ETOA calcolata (datetime ISO)              |
| `autista`       | `str`  | critical, warning  | Nome completo autista                      |
| `data`          | `str`  | critical, warning  | Data di riferimento (YYYY-MM-DD)           |
| `luogo_scarico` | `str`  | critical           | Localita di scarico                        |
| `luogo_carico`  | `str`  | critical           | Localita di carico                         |

Nota: gli alert `warning` non includono `luogo_scarico` e `luogo_carico` nel context perche il problema e meno grave e non richiede analisi di autisti alternativi.

#### Dedup key

Formato: `eta_orari:{bg}:{targa}`

Esempio: `eta_orari:26A02612:XA 821 YL`

Impedisce che la stessa anomalia generi notifiche ripetute entro il TTL configurato (default 8 ore). Il TTL e gestito dal `MonitorNotifier` ed e rilevante solo in modalita daemon o con invocazioni ripetute.

#### Chiamate HTTP per iterazione

Per ogni BG con viaggio associato, il check effettua **2 chiamate** al Planning Agent:

1. `calcola_eta_autista` — ETA di arrivo
2. `calcola_etoa` — ETOA considerando orari sede

Il numero totale di chiamate dipende dal numero di righe planning con autista e BG valido.

---

## Aggiungere un nuovo check

1. Creare un file in `monitor/checks/` (es. `gps_stale_check.py`)
2. Definire una classe che estende `BaseCheck`
3. Implementare il metodo `run()` con la firma standard
4. Chiamare `register_check()` a livello di modulo

L'auto-import in `monitor/checks/__init__.py` (via `pkgutil.iter_modules`) registra automaticamente il check all'avvio.

### Template

```python
import logging
from datetime import date
from typing import List

from models import CheckAlert
from monitor.registry import BaseCheck, register_check

logger = logging.getLogger("planning-monitor.checks.nome_check")


class NomeCheck(BaseCheck):
    """Descrizione breve del check."""

    name = "nome_check"

    def run(self, data, planning_rows, viaggi, planner_client, berlink_client):
        alerts: List[CheckAlert] = []
        data_str = data.isoformat()

        for row in planning_rows:
            # ... logica di verifica ...

            if anomalia_rilevata:
                alerts.append(
                    CheckAlert(
                        check_name=self.name,
                        severity="warning",  # o "critical"
                        title="Titolo breve anomalia",
                        message="Descrizione leggibile del problema.",
                        context={
                            # Dati strutturati per il Monitor LLM Agent
                        },
                        entity_type="trailer",  # o "trip", ecc.
                        entity_id="identificativo",
                        dedup_key=f"nome_check:chiave_univoca",
                    )
                )

        return alerts


register_check(NomeCheck())
```

### Convenzioni

- **`name`**: snake_case, univoco tra tutti i check. Usato per filtraggio via CLI (`--checks nome_check`).
- **`severity`**: solo `"warning"` o `"critical"`. Non usare altri valori.
- **`title`**: max 200 caratteri. Diventa il titolo della notifica BERLink.
- **`message`**: testo leggibile in italiano. Includere: cosa succede, chi e coinvolto (autista, BG, targa), perche e un problema.
- **`context`**: includere tutti i dati che servono al Monitor LLM Agent per approfondire. Chiavi in snake_case, valori stringa o numerici.
- **`dedup_key`**: formato `"{check_name}:{chiavi_univoche}"`. Deve identificare univocamente l'anomalia specifica per evitare notifiche duplicate.
- **`entity_type`** / **`entity_id`**: usati nella notifica BERLink per collegare l'alert all'entita nel sistema.
- **Errori nelle chiamate HTTP**: catturare con try/except, loggare con `logger.warning()`, e continuare con la riga successiva. Un singolo errore non deve bloccare l'intero check.
