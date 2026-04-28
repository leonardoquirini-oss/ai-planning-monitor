#!/usr/bin/env bash
# Planning Monitor — script di avvio
# Uso: ./run.sh [comando] [opzioni]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
PYTHON="$VENV/bin/python"

# Verifica venv
if [ ! -f "$PYTHON" ]; then
    echo "Errore: venv non trovato in $VENV"
    echo "Crea il venv con: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

usage() {
    cat <<EOF
Planning Monitor — Monitor autonomo pianificazione trasporti

Uso: ./run.sh <comando> [opzioni]

Comandi:
  check [DATA]         Check one-shot (default: oggi)
  check-nollm [DATA]   Check solo deterministici, senza LLM
  check-notify [DATA]  Check + invio notifiche BERLink
  serve [PORTA]        Avvia server HTTP (default: 8610)
  daemon [INTERVALLO]  Loop continuo (default: 300s)
  health               Health check del server locale
  status               Verifica stato servizi esterni (Planning Agent + BERLink)

Esempi:
  ./run.sh check                  # Check oggi con LLM
  ./run.sh check 2026-04-28      # Check data specifica
  ./run.sh check-notify           # Check + notifiche BERLink
  ./run.sh check-nollm            # Solo check deterministici
  ./run.sh serve                  # Server su :8610
  ./run.sh serve 8611             # Server su porta custom
  ./run.sh daemon                 # Loop ogni 5 minuti
  ./run.sh daemon 60              # Loop ogni 60 secondi
  ./run.sh health                 # Health check locale
  ./run.sh status                 # Stato servizi esterni
EOF
}

cmd_check() {
    "$PYTHON" "$SCRIPT_DIR/run.py" check "$@"
}

cmd_check_nollm() {
    "$PYTHON" "$SCRIPT_DIR/run.py" check --no-llm "$@"
}

cmd_check_notify() {
    "$PYTHON" "$SCRIPT_DIR/run.py" check --notify "$@"
}

cmd_serve() {
    local port="${1:-8610}"
    "$PYTHON" "$SCRIPT_DIR/run.py" serve --port "$port"
}

cmd_daemon() {
    local interval="${1:-300}"
    "$PYTHON" "$SCRIPT_DIR/run.py" daemon --interval "$interval" --notify
}

cmd_health() {
    local port="${1:-8610}"
    curl -s "http://localhost:${port}/api/monitor/health" | python3 -m json.tool
}

cmd_status() {
    echo "=== Planning Agent (:8602) ==="
    if curl -sf --max-time 5 "http://192.168.0.14:8602/api/planning/health" > /dev/null 2>&1; then
        echo "  OK"
    else
        echo "  NON RAGGIUNGIBILE"
    fi

    echo "=== BERLink (:9095) ==="
    if curl -sf --max-time 5 "http://192.168.0.12:9095" > /dev/null 2>&1; then
        echo "  OK"
    else
        echo "  NON RAGGIUNGIBILE"
    fi

    echo "=== Planning Monitor (:8610) ==="
    if curl -sf --max-time 5 "http://localhost:8610/api/monitor/health" > /dev/null 2>&1; then
        echo "  OK"
    else
        echo "  NON IN ESECUZIONE"
    fi
}

# --- Main ---

case "${1:-}" in
    check)        shift; cmd_check "$@" ;;
    check-nollm)  shift; cmd_check_nollm "$@" ;;
    check-notify) shift; cmd_check_notify "$@" ;;
    serve)        shift; cmd_serve "$@" ;;
    daemon)       shift; cmd_daemon "$@" ;;
    health)       shift; cmd_health "$@" ;;
    status)       shift; cmd_status "$@" ;;
    -h|--help|"") usage ;;
    *)            echo "Comando sconosciuto: $1"; echo; usage; exit 1 ;;
esac
