import logging
from datetime import datetime
from typing import List

from models import CheckAlert
from monitor.registry import BaseCheck, register_check

logger = logging.getLogger("planning-monitor.checks.eta_orari")


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


def _get_viaggio_field(viaggio: dict, *keys: str, default: str = "") -> str:
    """Cerca il primo campo presente tra i nomi alternativi."""
    for k in keys:
        val = viaggio.get(k)
        if val:
            return val
    return default


class ETAOrariCheck(BaseCheck):
    """Verifica ETA autista vs orari apertura sede carico/scarico."""

    name = "eta_orari"

    def run(self, data, planning_rows, viaggi, planner_client, berlink_client):
        alerts: List[CheckAlert] = []
        data_str = data.isoformat()

        # Indicizza viaggi per BG (per recuperare data_scarico)
        viaggi_list = viaggi if isinstance(viaggi, list) else viaggi.get("viaggi", [])
        viaggi_by_bg = {}
        for v in viaggi_list:
            bg = v.get("bg") or v.get("BG", "")
            if bg:
                viaggi_by_bg[bg.upper()] = v

        # Set per evitare chiamate duplicate sullo stesso autista
        autisti_processati = set()

        for row in planning_rows:
            if not row.get("id_employee"):
                continue

            plate = row.get("plate_number", "")
            driver = f"{row.get('driver_name', '')} {row.get('driver_surname', '')}".strip()
            if not driver or driver in autisti_processati:
                continue
            autisti_processati.add(driver)

            logger.info(f"[{plate}] {driver}")

            try:
                # Una sola chiamata: il tool trova il BG in corso da solo
                eta_args = {"nome_autista": driver, "data": data_str}
                logger.info(f"  -> execute get_eta_per_autista({eta_args})")
                eta_result = planner_client.execute_tool(
                    "get_eta_per_autista", eta_args,
                )
            except Exception as e:
                logger.warning(f"  ETA fallita per {driver}: {e}")
                continue

            bg = eta_result.get("bg_in_corso", "")
            eta_str = eta_result.get("eta")
            etoa_str = eta_result.get("etoa")
            etoa_detail = eta_result.get("etoa_dettagli", "")

            if not bg or not eta_str:
                logger.info(f"  {driver}: nessun BG in corso o ETA non disponibile, skip")
                continue

            # Verifica se la missione è ancora aperta
            try:
                bg_info = planner_client.execute_tool("get_info_bg", {"codice": bg})
                logger.debug(f"  get_info_bg({bg}): missione={bg_info.get('missione')}")
                missione = bg_info.get("missione", {})
                if not missione.get("aperta", True):
                    logger.info(f"  BG {bg}: missione chiusa (status={missione.get('status')}), skip")
                    continue
            except Exception as e:
                logger.warning(f"  get_info_bg fallito per {bg}: {e}")

            logger.info(
                f"  BG in corso: {bg}, ETA: {eta_result.get('eta_orario')}, "
                f"ETOA: {eta_result.get('etoa_orario')}, metodo: {eta_result.get('metodo')}"
            )

            # Recupera data_scarico dal viaggio TIR
            viaggio = viaggi_by_bg.get(bg.upper(), {})
            data_scarico = _normalize_date(_get_viaggio_field(viaggio, "data_scarico"))
            luogo_carico = _get_viaggio_field(viaggio, "partenza", "luogo_carico")
            luogo_scarico = eta_result.get("luogo_scarico") or _get_viaggio_field(
                viaggio, "arrivo", "luogo_scarico"
            )

            # Se il BG non è nei viaggi del giorno, recupera dettagli con cerca_stato_ordine
            if not data_scarico:
                try:
                    stato = planner_client.execute_tool(
                        "cerca_stato_ordine", {"codice": bg},
                    )
                    raw_consegna = _normalize_date(stato.get("data_consegna", ""))
                    # Scarta date placeholder (es. 1900-01-01)
                    if raw_consegna and raw_consegna > "2000-01-01":
                        data_scarico = raw_consegna
                    luogo_carico = luogo_carico or stato.get("luogo_carico", "")
                    logger.info(f"  cerca_stato_ordine({bg}): data_consegna={data_scarico}")
                except Exception as e:
                    logger.warning(f"  cerca_stato_ordine fallito per {bg}: {e}")

            # Data di riferimento: data_scarico del viaggio, fallback a data del check
            ref_date = data_scarico or data_str

            # Estrai data YYYY-MM-DD dall'ETOA ISO
            etoa_date_str = etoa_str[:10] if etoa_str and len(etoa_str) >= 10 else data_str

            # CRITICAL: ETOA slitta oltre la data di scarico prevista
            if etoa_date_str and etoa_date_str > ref_date:
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
                            "data_scarico": ref_date,
                            "luogo_scarico": luogo_scarico,
                            "luogo_carico": luogo_carico,
                        },
                        entity_type="trailer",
                        entity_id=plate,
                        dedup_key=f"eta_orari:{bg}:{plate}",
                    )
                )

            # WARNING: arrivo fuori finestra stesso giorno (sede chiusa)
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
