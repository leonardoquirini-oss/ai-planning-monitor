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
  "severity": "warning",
  "title": "Ritardo GPS: 26A02912_01",
  "message": "Autista **Franco Candia** (XA 821 YL) per BG [26A02912_01] — Fiorenzuola d'Arda\nETA teorico: 12:00, **ETA GPS: 12/05 14:36** (+156 min).\nGPS rileva ritardo: 369 km dalla destinazione, arrivo stimato GPS 14:36 vs ETA teorico 12:00 (+156 min)",
  "entity_type": "trailer",
  "entity_id": "XA 821 YL",
  "context": {
    "bg": "26A02912_01",
    "targa": "XA 821 YL",
    "eta": "2026-05-12T12:00:00",
    "eta_aggiornato": "2026-05-12T14:36:00",
    "etoa": "2026-05-12T14:00:00",
    "disponibile_da": "2026-05-12T14:00:00",
    "verifica_gps": "ritardo",
    "ritardo_minuti": 156,
    "autista": "Franco Candia",
    "data": "2026-05-12",
    "data_scarico": "2026-05-12",
    "luogo_scarico": "Fiorenzuola d'Arda",
    "luogo_carico": ""
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

**Step 5 — Calcolo ETA + ETOA + verifica GPS** (singola chiamata HTTP al Planning Agent)

```
POST /api/planning/execute
{
  "tool": "get_eta_per_autista",
  "args": {
    "nome_autista": "Franco Candia",
    "data": "2026-05-12"
  }
}
```

Il tool `get_eta_per_autista` restituisce in un'unica risposta:

- **ETA teorico** (`eta`, `eta_orario`): da missione TFP, evento GPS, routing, o fallback DataS
- **ETOA** (`etoa`, `etoa_orario`): ETA combinato con orari apertura sede
- **Disponibilità** (`disponibile_da`): quando l'autista sarà effettivamente disponibile
- **Verifica GPS** (`verifica_gps`, `eta_aggiornato`, `ritardo_minuti`, `verifica_dettagli`): validazione ETA teorico vs posizione GPS reale
  - `verifica_gps`: `"confermato"` | `"ritardo"` | `"non_disponibile"` | `"non_applicabile"`
  - Se `"ritardo"`: `eta_aggiornato` contiene l'ETA ricalcolato da GPS con delta in `ritardo_minuti`

Se il tool non restituisce `eta`, il BG viene saltato (dati insufficienti).

**Step 7 — Valutazione alert**

Tre scenari in ordine di priorità (mutuamente esclusivi per BG+targa):

| Priorità | Condizione                                          | Severity    | Titolo                           | Significato                                      |
|----------|-----------------------------------------------------|-------------|----------------------------------|--------------------------------------------------|
| A        | `verifica_gps == "ritardo"` e `eta_aggiornato` presente | **warning** | `Ritardo GPS: {bg}`             | GPS rileva ritardo rispetto a ETA teorico. Alert sempre, anche se rientra in orario. |
| B        | `disponibile_da > data_scarico` (fine giornata)     | **warning** | `ETA fuori orario: {bg}`        | Disponibilità slitta oltre la data di consegna prevista. |
| C        | `etoa > eta_effettivo` stesso giorno                | **warning** | `Arrivo fuori finestra: {bg}`   | Arrivo fuori finestra apertura sede, ma si recupera in giornata. |
| —        | Nessuna delle precedenti                            | —           | —                                | Nessun problema rilevato. |

**Note**:
- Lo scenario A scatta SEMPRE quando GPS rileva ritardo, indipendentemente dalla gravità
- L'ETA effettivo usa `eta_aggiornato` (GPS) quando disponibile, altrimenti `eta` (teorico)
- Tutti i confronti usano datetime completi (non solo date)

#### Campi `context` prodotti

Il dizionario `context` dell'alert è unificato per tutti gli scenari e contiene i dati strutturati che il Monitor LLM Agent usa per approfondire l'analisi:

| Campo              | Tipo        | Descrizione                                          |
|--------------------|-------------|------------------------------------------------------|
| `bg`               | `str`       | Codice BG del viaggio                                |
| `targa`            | `str`       | Targa semirimorchio                                  |
| `eta`              | `str`       | ETA teorico (datetime ISO)                           |
| `eta_aggiornato`   | `str/None`  | ETA ricalcolato da GPS (datetime ISO)                |
| `etoa`             | `str`       | ETOA con orari sede (datetime ISO)                   |
| `disponibile_da`   | `str`       | Quando l'autista sarà effettivamente disponibile     |
| `verifica_gps`     | `str`       | Stato verifica GPS: confermato/ritardo/non_disponibile/non_applicabile |
| `ritardo_minuti`   | `int`       | Delta in minuti tra ETA GPS e ETA teorico            |
| `autista`          | `str`       | Nome completo autista                                |
| `data`             | `str`       | Data di riferimento (YYYY-MM-DD)                     |
| `data_scarico`     | `str`       | Data consegna prevista (YYYY-MM-DD)                  |
| `luogo_scarico`    | `str`       | Località di scarico                                  |
| `luogo_carico`     | `str`       | Località di carico                                   |

#### Dedup key

Formato: `eta_orari:{bg}:{targa}`

Esempio: `eta_orari:26A02612:XA 821 YL`

Impedisce che la stessa anomalia generi notifiche ripetute entro il TTL configurato (default 8 ore). Il TTL e gestito dal `MonitorNotifier` ed e rilevante solo in modalita daemon o con invocazioni ripetute.

#### Chiamate HTTP per iterazione

Per ogni autista con BG valido, il check effettua **2 chiamate** al Planning Agent:

1. `get_eta_per_autista` — ETA + ETOA + verifica GPS (tutto in una chiamata)
2. `get_info_bg` — verifica se la missione è ancora aperta

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
