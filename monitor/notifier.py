import logging
from datetime import datetime, timedelta
from typing import List

from models import CheckAlert
from monitor.berlink_client import BERLinkClient

logger = logging.getLogger("planning-monitor.notifier")


class MonitorNotifier:
    """Invio notifiche BERLink con dedup, multi-destinazione, receiver per check."""

    def __init__(
        self,
        berlink_clients: List[BERLinkClient],
        monitor_config: dict,
    ):
        self.berlink_clients = berlink_clients
        self.checks_config = monitor_config.get("checks", {})
        self.dedup_ttl = timedelta(hours=monitor_config.get("dedup_ttl_hours", 8))
        self._sent: dict[str, datetime] = {}

    def _is_duplicate(self, dedup_key: str) -> bool:
        if not dedup_key:
            return False
        last = self._sent.get(dedup_key)
        if last and (datetime.now() - last) < self.dedup_ttl:
            return True
        return False

    def _mark_sent(self, dedup_key: str):
        if dedup_key:
            self._sent[dedup_key] = datetime.now()

    def _get_receivers(self, check_name: str) -> list:
        """Ritorna i notification_receivers configurati per il check specifico."""
        check_cfg = self.checks_config.get(check_name, {})
        return check_cfg.get("notification_receivers", [])

    def cleanup_expired(self):
        now = datetime.now()
        expired = [k for k, v in self._sent.items() if (now - v) >= self.dedup_ttl]
        for k in expired:
            del self._sent[k]

    def send_batch(self, alerts: List[CheckAlert]) -> int:
        """Invia notifiche per ogni alert ai receiver del check, su tutte le destinazioni."""
        sent = 0
        self.cleanup_expired()

        for alert in alerts:
            if self._is_duplicate(alert.dedup_key):
                logger.debug(f"Dedup skip: {alert.dedup_key}")
                continue

            receivers = self._get_receivers(alert.check_name)
            if not receivers:
                logger.debug(f"Nessun receiver per check '{alert.check_name}', skip notifica")
                self._mark_sent(alert.dedup_key)
                continue

            for receiver in receivers:
                notification = {
                    "notification_type": "planning_check",
                    "title": alert.title[:200],
                    "message": alert.message,
                    "link": "/notifymessage",
                    **receiver,
                }
                for client in self.berlink_clients:
                    try:
                        # TODO: riattivare invio singole notifiche se necessario
                        # client.send_notification(notification)
                        # sent += 1
                        logger.info(
                            f"Notifica singola disabilitata: {alert.title} "
                            f"-> {receiver} @ {client.notifications_base_url}"
                        )
                    except Exception as e:
                        logger.error(
                            f"Errore invio notifica a {client.notifications_base_url}: {e}"
                        )

            self._mark_sent(alert.dedup_key)

        return sent

    def send_report(self, report: dict):
        """Invia report consolidato a tutte le destinazioni BERLink."""
        for client in self.berlink_clients:
            try:
                client.send_monitor_report(report)
                logger.info(
                    f"Report consolidato inviato a {client.notifications_base_url}"
                )
            except Exception as e:
                logger.error(
                    f"Invio report a {client.notifications_base_url} fallito: {e}",
                    exc_info=True,
                )
