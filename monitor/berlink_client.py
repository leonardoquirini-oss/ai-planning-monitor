import httpx
import logging

logger = logging.getLogger("planning-monitor.berlink-client")


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

    def send_notification(self, notification: dict) -> dict:
        """POST notifica a BERLink."""
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                f"{self.base_url}/api/notifications/send",
                headers=self._headers(),
                json=notification,
            )
            resp.raise_for_status()
            return resp.json()
