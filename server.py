import asyncio
from typing import List, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from monitor.engine import run_check
from monitor.registry import get_registered_checks

app = FastAPI(title="Planning Monitor", version="1.0.0")

# --- Tool schema (stesso formato OpenAI function calling di ai-planner) ---

MONITOR_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "esegui_check_monitoraggio",
            "description": (
                "Esegue i check di monitoraggio sulla pianificazione del giorno. "
                "Verifica ETA vs orari apertura sede, anomalie nella pianificazione, "
                "e analizza i problemi trovati. Usa questo tool quando l'utente chiede "
                "di controllare, verificare o monitorare la pianificazione, "
                "o di cercare anomalie nei viaggi del giorno."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "data": {
                        "type": "string",
                        "description": "Data in formato YYYY-MM-DD (default: oggi)",
                    },
                    "notify": {
                        "type": "boolean",
                        "description": "Se true, invia notifiche BERLink",
                        "default": False,
                    },
                    "use_llm": {
                        "type": "boolean",
                        "description": "Se true, usa LLM per analisi approfondita",
                        "default": True,
                    },
                    "checks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Lista check specifici da eseguire (default: tutti)",
                    },
                    "bg": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filtra per codici BG specifici (default: tutti)",
                    },
                },
                "required": [],
            },
        },
    }
]

MONITOR_TOOLS_FUNCTIONS = {
    "esegui_check_monitoraggio": run_check,
    "agent_check": run_check,  # backward compatibility
}


# --- Endpoint tool discovery ---


@app.get("/api/monitor/tools")
async def get_tools():
    """Schema tool in formato OpenAI function calling."""
    return MONITOR_TOOLS_SCHEMA


@app.post("/api/monitor/execute")
async def execute_tool(request: dict):
    """Invoca un tool per nome (identico a /api/planning/execute di ai-planner)."""
    tool_name = request.get("tool")
    args = request.get("args", {})
    func = MONITOR_TOOLS_FUNCTIONS.get(tool_name)
    if not func:
        return {
            "error": f"Tool '{tool_name}' non trovato",
            "available": list(MONITOR_TOOLS_FUNCTIONS.keys()),
        }
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: func(**args))


# --- Endpoint dedicati ---


class CheckRequest(BaseModel):
    data: Optional[str] = None
    notify: bool = False
    use_llm: bool = True
    checks: Optional[List[str]] = None
    bg: Optional[List[str]] = None


@app.post("/api/monitor/check")
async def api_check(req: CheckRequest):
    """Esegue un ciclo completo di check (endpoint diretto)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: run_check(
            data=req.data, notify=req.notify, use_llm=req.use_llm,
            checks=req.checks, bg=req.bg,
        ),
    )


@app.get("/api/monitor/health")
async def health():
    """Health check del monitor."""
    checks = get_registered_checks()
    return {
        "status": "ok",
        "tools": len(MONITOR_TOOLS_SCHEMA),
        "tool_names": list(MONITOR_TOOLS_FUNCTIONS.keys()),
        "checks_registrati": [c.name for c in checks],
    }


@app.get("/api/monitor/checks")
async def list_checks():
    """Lista check disponibili."""
    return [
        {"name": c.name, "description": c.__doc__ or ""}
        for c in get_registered_checks()
    ]
