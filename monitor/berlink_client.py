import httpx
import logging

logger = logging.getLogger("planning-monitor.berlink-client")


class BERLinkClient:
    """Client HTTP per BERLink Connector (DB) e BERLink API (notifiche)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 60.0,
        notifications_base_url: str | None = None,
        notifications_api_key: str | None = None,
        notifications_timeout: float | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        # URL separato per le notifiche (BERLink API :8090)
        self.notifications_base_url = (
            notifications_base_url.rstrip("/") if notifications_base_url else self.base_url
        )
        self.notifications_api_key = notifications_api_key or self.api_key
        self.notifications_timeout = notifications_timeout or self.timeout

    def _headers(self):
        return {"X-API-Key": self.api_key, "Content-Type": "application/json"}

    def get_pianificazione_giornaliera(self, data: str) -> list:
        """Query planning giornaliero da BERLink."""
        query = (
            "SELECT p.id_trailer_planning, p.id_trailer, p.note, "
            "p.info_maintenance, p.id_employee, p.planning, p.planning_date, "
            "p.flag_ack, t.plate_number, t.id_trailer_type, "
            "e.name as driver_name, e.surname as driver_surname "
            "FROM pl_trailer_planning p "
            "JOIN flt_trailers t ON p.id_trailer = t.id_trailer "
            "LEFT JOIN emp_employees e ON p.id_employee = e.id_employee "
            f"WHERE p.planning_date = '{data}' "
            "ORDER BY t.plate_number"
        )
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                f"{self.base_url}/api/Query/execute",
                headers=self._headers(),
                json={"query": query},
            )
            resp.raise_for_status()
            return resp.json().get("data", [])

    def _notifications_headers(self):
        return {"X-API-Key": self.notifications_api_key, "Content-Type": "application/json"}

    def send_notification(self, notification: dict) -> dict:
        """POST notifica a BERLink API (:8090)."""
        url = f"{self.notifications_base_url}/api/notifications/send"
        logger.info(f"POST {url} body: {notification}")
        with httpx.Client(timeout=self.notifications_timeout) as client:
            resp = client.post(
                url,
                headers=self._notifications_headers(),
                json=notification,
            )
            resp.raise_for_status()
            return resp.json()

    def send_monitor_report(self, report: dict) -> dict:
        """POST report consolidato al tab Monitor di BERLink."""
        url = f"{self.notifications_base_url}/api/notifications/monitor/planning"
        logger.info(f"POST {url} (report data={report.get('data')})")
        with httpx.Client(timeout=self.notifications_timeout) as client:
            resp = client.post(
                url,
                headers=self._notifications_headers(),
                json=report,
            )
            resp.raise_for_status()
            return resp.json()
