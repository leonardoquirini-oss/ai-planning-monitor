import json
import os
import logging
from typing import List

from openai import OpenAI

from models import CheckAlert
from monitor.planner_client import PlannerClient

logger = logging.getLogger("planning-monitor.agent")

MONITOR_TOOL_NAMES = {
    "localizza_entita",
    "calcola_eta_autista",
    "get_eta_per_autista",
    "check_orari_sede",
    "trova_autista_piu_vicino",
    "get_autisti_disponibili",
    "get_pianificazione_corrente",
    "calcola_distanza",
    "calcola_etoa",
    "cerca_bg_da_targa",
}

MONITOR_SYSTEM_PROMPT = """Sei il Monitor Agent per la pianificazione trasporti della ditta Bernardini.

Il tuo ruolo è analizzare anomalie rilevate nei viaggi pianificati di oggi e:
1. Valutare la gravità reale del problema
2. Cercare possibili soluzioni usando i tool disponibili
3. Formulare una notifica chiara e operativa con proposta d'azione

TOOL DISPONIBILI:
- localizza_entita: localizza autista/semirimorchio (GPS, eventi, planning)
- calcola_eta_autista: calcola ETA dato BG e targa
- get_eta_per_autista: calcola ETA cercando per nome autista
- check_orari_sede: verifica orari apertura sede carico/scarico
- trova_autista_piu_vicino: trova autista alternativo più vicino a una località
- get_autisti_disponibili: lista autisti disponibili oggi
- calcola_distanza: distanza in km tra due località

DATA DI LAVORO: {data_oggi}

REGOLE:
- Sii conciso e operativo
- Includi sempre: cosa succede, perché è un problema, cosa si può fare
- Se c'è un autista disponibile più vicino, proponilo
- NON INVENTARE DATI — usa solo informazioni dai tool
- Rispondi in italiano
- Formato output: JSON con campi "titolo", "messaggio", "severita", "azione_proposta"
"""


class MonitorAgent:
    def __init__(self, planner_client: PlannerClient, llm_config: dict):
        self.planner = planner_client
        self.client = OpenAI(
            api_key=os.environ["OPENROUTER_API_KEY"],
            base_url=llm_config.get("base_url", "https://openrouter.ai/api/v1"),
        )
        self.model = llm_config.get("model", "anthropic/claude-3-5-haiku")
        self.temperature = llm_config.get("temperature", 0.1)
        self.max_tokens = llm_config.get("max_tokens", 2000)

        # Fetch tool schema dal Planning Agent e filtra per whitelist
        try:
            all_tools = planner_client.get_tools_schema()
            self.tools_schema = [
                t for t in all_tools if t["function"]["name"] in MONITOR_TOOL_NAMES
            ]
            logger.info(
                f"Tool schema caricati: {len(self.tools_schema)}/{len(all_tools)}"
            )
        except Exception as e:
            logger.warning(f"Impossibile caricare tool schema: {e}")
            self.tools_schema = []

    def analyze(
        self, alerts: List[CheckAlert], planning_context: str, data_oggi: str = ""
    ) -> str:
        """Analizza alert con LLM. I tool call vengono eseguiti via HTTP al Planning Agent."""
        alert_text = "\n\n".join(
            f"### Alert {i+1} [{a.severity.upper()}]\n"
            f"**{a.title}**\n{a.message}\n"
            f"Contesto: {json.dumps(a.context, ensure_ascii=False)}"
            for i, a in enumerate(alerts)
        )

        user_content = (
            f"## Anomalie rilevate ({len(alerts)} totali)\n\n"
            f"{alert_text}\n\n"
            f"## Contesto planning\n{planning_context}\n\n"
            f"Analizza le anomalie, usa i tool per approfondire se necessario, "
            f"e proponi azioni concrete."
        )

        messages = [
            {
                "role": "system",
                "content": MONITOR_SYSTEM_PROMPT.format(data_oggi=data_oggi),
            },
            {"role": "user", "content": user_content},
        ]

        for _ in range(10):
            kwargs = {
                "model": self.model,
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
            if self.tools_schema:
                kwargs["tools"] = self.tools_schema

            response = self.client.chat.completions.create(**kwargs)
            msg = response.choices[0].message
            messages.append(msg)

            if msg.tool_calls:
                for tc in msg.tool_calls:
                    args = json.loads(tc.function.arguments)
                    logger.info(f"LLM tool call: {tc.function.name}({args})")
                    try:
                        result = self.planner.execute_tool(tc.function.name, args)
                    except Exception as e:
                        result = {"error": str(e)}
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                continue

            return msg.content

        return "Analisi LLM: raggiunto limite iterazioni tool call."
