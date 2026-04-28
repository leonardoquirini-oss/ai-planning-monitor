import logging
from datetime import datetime, timedelta
from typing import List, Optional

from models import CheckAlert
from monitor.berlink_client import BERLinkClient

logger = logging.getLogger("planning-monitor.notifier")


class MonitorNotifier:
    """Invio notifiche BERLink con dedup."""

    def __init__(self, berlink: BERLinkClient, monitor_config: dict):
        self.berlink = berlink
        self.receivers = monitor_config.get("notification_receivers", [])
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

    def cleanup_expired(self):
        now = datetime.now()
        expired = [k for k, v in self._sent.items() if (now - v) >= self.dedup_ttl]
        for k in expired:
            del self._sent[k]

    def send_batch(
        self, alerts: List[CheckAlert], llm_message: Optional[str] = None
    ) -> int:
        """Invia notifiche per ogni alert a tutti i receiver. Ritorna conteggio invii."""
        sent = 0
        self.cleanup_expired()

        for alert in alerts:
            if self._is_duplicate(alert.dedup_key):
                logger.debug(f"Dedup skip: {alert.dedup_key}")
                continue

            message = llm_message if llm_message else alert.message

            for receiver in self.receivers:
                notification = {
                    "notification_type": "planning_check",
                    "title": alert.title[:200],
                    "message": message,
                    "entity_type": alert.entity_type,
                    "entity_id": alert.entity_id,
                    "link": f"/planning?date={alert.context.get('data', '')}",
                    **receiver,
                }
                try:
                    self.berlink.send_notification(notification)
                    sent += 1
                    logger.info(f"Notifica inviata: {alert.title} -> {receiver}")
                except Exception as e:
                    logger.error(f"Errore invio notifica: {e}")

            self._mark_sent(alert.dedup_key)

        return sent
