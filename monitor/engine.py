import logging
import os
from datetime import date
from pathlib import Path
from typing import List, Optional

import yaml
from dotenv import load_dotenv

from models import CheckAlert
from monitor.berlink_client import BERLinkClient
from monitor.monitor_agent import MonitorAgent
from monitor.notifier import MonitorNotifier
from monitor.planner_client import PlannerClient
from monitor.registry import get_registered_checks

# Auto-import checks
import monitor.checks  # noqa: F401

logger = logging.getLogger("planning-monitor.engine")

# Carica .env
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def load_config() -> dict:
    config_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def _summarize(planning_rows: list) -> str:
    """Riassunto compatto delle righe planning per contesto LLM."""
    lines = []
    for row in planning_rows[:50]:
        driver = f"{row.get('driver_name', '')} {row.get('driver_surname', '')}".strip()
        plate = row.get("plate_number", "")
        planning = (row.get("planning") or "")[:80]
        if row.get("id_employee"):
            lines.append(f"- {plate} | {driver} | {planning}")
    return "\n".join(lines) if lines else "Nessuna riga planning con autista."


def run_check(
    data: str = None,
    notify: bool = False,
    use_llm: bool = True,
    checks: Optional[List[str]] = None,
    bg: Optional[List[str]] = None,
    notifier: MonitorNotifier = None,
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
    planner = PlannerClient(
        config["planner"]["base_url"],
        timeout=config["planner"].get("timeout", 120.0),
    )

    # Costruisci lista destinazioni notifiche (backward compat: dict → list)
    notif_raw = config.get("berlink_notifications", [])
    notif_list = notif_raw if isinstance(notif_raw, list) else [notif_raw]

    berlink = BERLinkClient(
        config["berlink"]["base_url"],
        config["berlink"]["api_key"],
        timeout=config["berlink"].get("timeout", 60.0),
        notifications_base_url=notif_list[0].get("base_url") if notif_list else None,
        notifications_api_key=notif_list[0].get("api_key") if notif_list else None,
        notifications_timeout=notif_list[0].get("timeout") if notif_list else None,
    )

    # Client BERLink per ogni destinazione notifiche
    notif_clients = []
    for notif_cfg in notif_list:
        notif_clients.append(
            BERLinkClient(
                config["berlink"]["base_url"],
                config["berlink"]["api_key"],
                timeout=config["berlink"].get("timeout", 60.0),
                notifications_base_url=notif_cfg.get("base_url"),
                notifications_api_key=notif_cfg.get("api_key"),
                notifications_timeout=notif_cfg.get("timeout"),
            )
        )

    data_obj = date.fromisoformat(data) if data else date.today()
    data_str = data_obj.isoformat()

    logger.info(f"Avvio check per data {data_str}")

    # 1. Fetch dati
    planning_rows = berlink.get_pianificazione_giornaliera(data_str)
    viaggi = planner.get_viaggi(data_str)

    logger.info(
        f"Dati: {len(planning_rows)} righe planning, "
        f"{len(viaggi) if isinstance(viaggi, list) else 'dict'} viaggi"
    )

    # 2. Check deterministici
    checks_config = config.get("monitor", {}).get("checks", {})
    all_alerts: List[CheckAlert] = []
    checks_eseguiti = 0
    for check in get_registered_checks():
        if checks and check.name not in checks:
            continue
        # Se non richiesti check specifici via CLI, rispetta enabled da config
        if not checks:
            check_cfg = checks_config.get(check.name, {})
            if not check_cfg.get("enabled", True):
                logger.info(f"Check '{check.name}' disabilitato da config, skip")
                continue
        try:
            alerts = check.run(data_obj, planning_rows, viaggi, planner, berlink, bg_filter=bg)
            all_alerts.extend(alerts)
            checks_eseguiti += 1
            logger.info(f"Check '{check.name}': {len(alerts)} alert")
        except Exception as e:
            logger.error(f"Check '{check.name}' fallito: {e}", exc_info=True)

    # 3. LLM analisi (opzionale)
    analisi_llm = None
    if use_llm and all_alerts:
        try:
            agent = MonitorAgent(planner, config["llm"])
            analisi_llm = agent.analyze(
                all_alerts, _summarize(planning_rows), data_oggi=data_str
            )
            logger.info("Analisi LLM completata")
        except Exception as e:
            logger.error(f"LLM analisi fallita: {e}", exc_info=True)

    # 4. Notifiche (opzionale)
    notifiche_inviate = 0
    if notify and all_alerts:
        if notifier is None:
            notifier = MonitorNotifier(notif_clients, config["monitor"])
        notifiche_inviate = notifier.send_batch(all_alerts)
        logger.info(f"Notifiche inviate: {notifiche_inviate}")

    result = {
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

    # 5. Invio report consolidato a tutte le destinazioni BERLink
    if notify:
        if notifier is None:
            notifier = MonitorNotifier(notif_clients, config["monitor"])
        notifier.send_report(result)

    return result
