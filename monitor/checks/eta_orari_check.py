import re
import logging
from datetime import date, datetime
from typing import List

from models import CheckAlert
from monitor.registry import BaseCheck, register_check

logger = logging.getLogger("planning-monitor.checks.eta_orari")

BG_REGEX = re.compile(r"#(\d{2}[A-Z]\d+[_\d]*)")


def _get_viaggio_field(viaggio: dict, *keys: str, default: str = "") -> str:
    """Cerca il primo campo presente tra i nomi alternativi."""
    for k in keys:
        val = viaggio.get(k)
        if val:
            return val
    return default


def _normalize_date(date_str: str) -> str:
    """Converte DD/MM/YYYY -> YYYY-MM-DD. Se già ISO, ritorna invariato."""
    if not date_str:
        return date_str
    if "/" in date_str:
        try:
            return datetime.strptime(date_str, "%d/%m/%Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return date_str


class ETAOrariCheck(BaseCheck):
    """Verifica ETA autista vs orari apertura sede carico/scarico."""

    name = "eta_orari"

    def run(self, data, planning_rows, viaggi, planner_client, berlink_client):
        alerts: List[CheckAlert] = []
        data_str = data.isoformat()

        # Indicizza viaggi per BG
        viaggi_list = viaggi if isinstance(viaggi, list) else viaggi.get("viaggi", [])
        viaggi_by_bg = {}
        for v in viaggi_list:
            bg = v.get("bg") or v.get("BG", "")
            if bg:
                viaggi_by_bg[bg.upper()] = v

        for row in planning_rows:
            if not row.get("id_employee"):
                continue

            plate = row.get("plate_number", "")
            driver = f"{row.get('driver_name', '')} {row.get('driver_surname', '')}".strip()
            planning_text = row.get("planning") or ""

            # Estrai BG da testo planning — skip righe senza BG
            bg_codes = BG_REGEX.findall(planning_text)
            if not bg_codes:
                continue

            logger.info(f"[{plate}] {driver} — BG trovati: {bg_codes}")

            for bg in bg_codes:
                viaggio = viaggi_by_bg.get(bg.upper())
                if not viaggio:
                    logger.debug(f"  BG {bg} non trovato nei viaggi TIR, skip")
                    continue

                luogo_carico = _get_viaggio_field(viaggio, "partenza", "luogo_carico")
                luogo_scarico = _get_viaggio_field(viaggio, "arrivo", "luogo_scarico")
                data_scarico = _normalize_date(_get_viaggio_field(viaggio, "data_scarico"))

                logger.info(
                    f"  BG {bg}: {luogo_carico or '?'} -> "
                    f"{luogo_scarico or '?'} "
                    f"(scarico {data_scarico or '?'})"
                )

                try:
                    # Calcola ETA via Planning Agent HTTP
                    eta_args = {
                        "bg": bg,
                        "targa": plate,
                        "luogo_scarico": luogo_scarico,
                        "data_scarico": data_scarico,
                    }
                    logger.info(f"  -> execute calcola_eta_autista({eta_args})")
                    eta_result = planner_client.execute_tool(
                        "calcola_eta_autista", eta_args,
                    )
                    logger.info(f"  <- ETA: {eta_result.get('eta')} (metodo: {eta_result.get('metodo', '?')})")
                except Exception as e:
                    logger.warning(f"  ETA fallita per BG {bg}: {e}")
                    continue

                eta_str = eta_result.get("eta")
                if not eta_str:
                    logger.info(f"  BG {bg}: nessuna ETA disponibile, skip")
                    continue

                try:
                    # Calcola ETOA via Planning Agent HTTP
                    etoa_args = {"eta": eta_str, "bg": bg, "data": data_str}
                    logger.info(f"  -> execute calcola_etoa({etoa_args})")
                    etoa_result = planner_client.execute_tool(
                        "calcola_etoa", etoa_args,
                    )
                    logger.info(f"  <- ETOA: {etoa_result.get('etoa')} ({etoa_result.get('dettagli', '')})")
                except Exception as e:
                    logger.warning(f"  ETOA fallita per BG {bg}: {e}")
                    continue

                etoa_str = etoa_result.get("etoa")
                # Estrai data YYYY-MM-DD dall'ETOA ISO (etoa_data dal server non è ISO)
                etoa_date_str = etoa_str[:10] if etoa_str and len(etoa_str) >= 10 else data_str
                etoa_detail = etoa_result.get("dettagli", "")

                # CRITICAL: ETOA giorno dopo
                if etoa_date_str and etoa_date_str > data_str:
                    alerts.append(
                        CheckAlert(
                            check_name=self.name,
                            severity="critical",
                            title=f"ETA fuori orario: {bg}",
                            message=(
                                f"Autista {driver} ({plate}) per BG {bg}: "
                                f"ETA {eta_result.get('eta_orario', eta_str)}, "
                                f"disponibilità spostata a {etoa_str}. {etoa_detail}"
                            ),
                            context={
                                "bg": bg,
                                "targa": plate,
                                "eta": eta_str,
                                "etoa": etoa_str,
                                "autista": driver,
                                "data": data_str,
                                "luogo_scarico": luogo_scarico,
                                "luogo_carico": luogo_carico,
                            },
                            entity_type="trailer",
                            entity_id=plate,
                            dedup_key=f"eta_orari:{bg}:{plate}",
                        )
                    )

                # WARNING: arrivo fuori finestra stesso giorno
                elif etoa_str and eta_str and etoa_str > eta_str and "sede chiusa" in etoa_detail:
                    alerts.append(
                        CheckAlert(
                            check_name=self.name,
                            severity="warning",
                            title=f"Arrivo fuori finestra: {bg}",
                            message=(
                                f"Autista {driver} ({plate}) BG {bg}: "
                                f"ETA {eta_result.get('eta_orario', eta_str)} "
                                f"fuori finestra apertura. {etoa_detail}"
                            ),
                            context={
                                "bg": bg,
                                "targa": plate,
                                "eta": eta_str,
                                "etoa": etoa_str,
                                "autista": driver,
                                "data": data_str,
                            },
                            entity_type="trailer",
                            entity_id=plate,
                            dedup_key=f"eta_orari:{bg}:{plate}",
                        )
                    )

        return alerts


register_check(ETAOrariCheck())
