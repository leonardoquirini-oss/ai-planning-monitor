import json
import logging
import sys
from pathlib import Path
from typing import Optional

import typer
from rich import print_json
from rich.console import Console

# Aggiungi root al path per import models/monitor
sys.path.insert(0, str(Path(__file__).resolve().parent))

app = typer.Typer(
    name="planning-monitor", help="Monitor autonomo pianificazione trasporti"
)
console = Console()

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).resolve().parent / "logs" / "monitor.log"),
    ],
)


@app.command()
def check(
    data: Optional[str] = typer.Argument(None, help="Data YYYY-MM-DD (default: oggi)"),
    notify: bool = typer.Option(
        False, "--notify", "-n", help="Invia notifiche BERLink"
    ),
    no_llm: bool = typer.Option(
        False, "--no-llm", help="Solo check deterministici, senza LLM"
    ),
    checks: Optional[str] = typer.Option(
        None, "--checks", "-c", help="Check specifici (comma-separated)"
    ),
):
    """Esegue un ciclo di check sulla pianificazione (one-shot, modalità standard)."""
    from monitor.engine import run_check

    checks_list = checks.split(",") if checks else None
    result = run_check(
        data=data, notify=notify, use_llm=not no_llm, checks=checks_list
    )

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
    interval: int = typer.Option(None, "--interval", "-i", help="Secondi tra cicli (default da config)"),
    notify: bool = typer.Option(True, "--notify", "-n"),
    no_llm: bool = typer.Option(False, "--no-llm"),
):
    """Loop continuo (alternativa a cron + endpoint HTTP)."""
    import signal
    import time

    from monitor.engine import load_config, run_check

    config = load_config()
    if interval is None:
        interval = config.get("monitor", {}).get("check_interval_seconds", 300)

    running = True

    def _stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    console.print(
        f"[bold blue]Planning Monitor Daemon[/bold blue] — ogni {interval}s"
    )
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
