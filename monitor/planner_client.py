import httpx
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
                json={"tool": tool_name, "args": args},
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
