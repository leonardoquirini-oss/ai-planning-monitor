import logging
import re
from datetime import datetime
from typing import List

from models import CheckAlert
from monitor.registry import BaseCheck, register_check

BG_REGEX = re.compile(r"#(\d{2}[A-Z]\d+[_\d]*)")

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


def _format_dt_it(iso_str: str) -> str:
    """Formatta ISO datetime (2026-05-08T10:30:00) -> italiano (08/05/2026 10:30)."""
    if not iso_str:
        return iso_str
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%d/%m/%Y %H:%M")
    except (ValueError, TypeError):
        return iso_str


def _get_viaggio_field(viaggio: dict, *keys: str, default: str = "") -> str:
    """Cerca il primo campo presente tra i nomi alternativi."""
    for k in keys:
        val = viaggio.get(k)
        if val:
            return val
    return default


def _parse_iso(val: str):
    """Parse ISO datetime, None on failure."""
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except (ValueError, TypeError):
        return None


def _effective_eta(eta_result: dict):
    """Restituisce (eta_iso, eta_display, is_gps_delayed) usando GPS se disponibile."""
    verifica = eta_result.get("verifica_gps", "")
    eta_agg = eta_result.get("eta_aggiornato", "")
    eta_agg_display = eta_result.get("eta_aggiornato_data", "") or _format_dt_it(eta_agg)
    if verifica == "ritardo" and eta_agg:
        return eta_agg, eta_agg_display, True
    eta = eta_result.get("eta", "")
    eta_display = eta_result.get("eta_orario", "") or _format_dt_it(eta)
    return eta, eta_display, False


class ETAOrariCheck(BaseCheck):
    """Verifica ETA autista vs orari apertura sede carico/scarico."""

    name = "eta_orari"

    def run(self, data, planning_rows, viaggi, planner_client, berlink_client, bg_filter=None):
        alerts: List[CheckAlert] = []
        data_str = data.isoformat()

        # Indicizza viaggi per BG (per recuperare data_scarico)
        viaggi_list = viaggi if isinstance(viaggi, list) else viaggi.get("viaggi", [])
        viaggi_by_bg = {}
        for v in viaggi_list:
            bg = v.get("bg") or v.get("BG", "")
            if bg:
                viaggi_by_bg[bg.upper()] = v

        # Fast path: BG specifici → get_info_bg diretto, senza iterare tutti gli autisti
        if bg_filter:
            entries = self._resolve_bg_entries(bg_filter, planning_rows, planner_client, data_str)
        else:
            entries = self._resolve_all_entries(planning_rows, planner_client, data_str)

        for entry in entries:
            bg = entry["bg"]
            driver = entry["driver"]
            plate = entry["plate"]
            eta_result = entry["eta_result"]

            # Campi base
            eta_str = eta_result.get("eta")
            etoa_str = eta_result.get("etoa")
            etoa_detail = eta_result.get("etoa_dettagli", "")

            # GPS validation
            verifica_gps = eta_result.get("verifica_gps", "")
            ritardo_minuti = eta_result.get("ritardo_minuti", 0)
            verifica_dettagli = eta_result.get("verifica_dettagli", "")

            # Fallback: calcola ritardo_minuti da eta_aggiornato - eta se mancante
            if verifica_gps == "ritardo" and not ritardo_minuti:
                eta_dt = _parse_iso(eta_str)
                eta_agg_dt = _parse_iso(eta_result.get("eta_aggiornato", ""))
                if eta_dt and eta_agg_dt:
                    # Rimuovi timezone per confronto sicuro
                    if eta_agg_dt.tzinfo and not eta_dt.tzinfo:
                        eta_agg_dt = eta_agg_dt.replace(tzinfo=None)
                    ritardo_minuti = int((eta_agg_dt - eta_dt).total_seconds() / 60)

            # ETA effettivo (GPS-corrected se disponibile)
            eff_eta_iso, eff_eta_display, is_gps_delayed = _effective_eta(eta_result)

            # Disponibilità reale
            disponibile_da_str = eta_result.get("disponibile_da", etoa_str)

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
                    if raw_consegna and raw_consegna > "2000-01-01":
                        data_scarico = raw_consegna
                    luogo_carico = luogo_carico or stato.get("luogo_carico", "")
                    logger.info(f"  cerca_stato_ordine({bg}): data_consegna={data_scarico}")
                except Exception as e:
                    logger.warning(f"  cerca_stato_ordine fallito per {bg}: {e}")

            # Parse datetime per confronti
            etoa_dt = _parse_iso(etoa_str)
            eff_eta_dt = _parse_iso(eff_eta_iso)
            disponibile_da_dt = _parse_iso(disponibile_da_str)
            scarico_deadline = _parse_iso(data_scarico + "T23:59:59") if data_scarico else None

            # Context comune per tutti gli scenari
            alert_context = {
                "bg": bg,
                "targa": plate,
                "eta": eta_str,
                "eta_aggiornato": eta_result.get("eta_aggiornato"),
                "etoa": etoa_str,
                "disponibile_da": disponibile_da_str,
                "verifica_gps": verifica_gps,
                "ritardo_minuti": ritardo_minuti,
                "autista": driver,
                "data": data_str,
                "data_scarico": data_scarico or "",
                "luogo_scarico": luogo_scarico,
                "luogo_carico": luogo_carico,
            }

            eta_teorico_display = eta_result.get("eta_orario", "") or _format_dt_it(eta_str)
            alert_fired = False

            # Scenario A: GPS rileva ritardo — alert SEMPRE
            if is_gps_delayed:
                alerts.append(
                    CheckAlert(
                        check_name=self.name,
                        severity="warning",
                        title=f"Ritardo GPS: {bg}",
                        message=(
                            f"Autista **{driver}** ({plate}) per BG [{bg}] — {luogo_scarico}\n"
                            f"ETA teorico: {eta_teorico_display}, "
                            f"**ETA GPS: {eff_eta_display}** (+{ritardo_minuti} min).\n"
                            f"{verifica_dettagli}"
                        ),
                        context=alert_context,
                        entity_type="trailer",
                        entity_id=plate,
                        dedup_key=f"eta_orari:{bg}:{plate}",
                    )
                )
                alert_fired = True

            # Scenario B: disponibilità slitta oltre data_scarico (next-day)
            if (
                not alert_fired
                and scarico_deadline
                and disponibile_da_dt
                and disponibile_da_dt > scarico_deadline
            ):
                alerts.append(
                    CheckAlert(
                        check_name=self.name,
                        severity="warning",
                        title=f"ETA fuori orario: {bg}",
                        message=(
                            f"Autista **{driver}** ({plate}) per BG [{bg}] — {luogo_scarico}\n"
                            f"ETA: {eta_teorico_display}, "
                            f"disponibilità spostata a **{_format_dt_it(disponibile_da_str)}**.\n"
                            f"Consegna prevista entro: {data_scarico}.\n"
                            f"{etoa_detail}"
                        ),
                        context=alert_context,
                        entity_type="trailer",
                        entity_id=plate,
                        dedup_key=f"eta_orari:{bg}:{plate}",
                    )
                )
                alert_fired = True

            # Scenario C: stesso giorno, fuori finestra apertura
            if (
                not alert_fired
                and etoa_dt
                and eff_eta_dt
                and etoa_dt > eff_eta_dt
                and etoa_dt.date() == eff_eta_dt.date()
            ):
                alerts.append(
                    CheckAlert(
                        check_name=self.name,
                        severity="warning",
                        title=f"Arrivo fuori finestra: {bg}",
                        message=(
                            f"Autista **{driver}** ({plate}) per BG [{bg}] — {luogo_scarico}\n"
                            f"ETA arrivo: {_format_dt_it(eff_eta_iso)}, "
                            f"fuori finestra apertura.\n"
                            f"Disponibile da: **{_format_dt_it(etoa_str)}**.\n"
                            f"{etoa_detail}"
                        ),
                        context=alert_context,
                        entity_type="trailer",
                        entity_id=plate,
                        dedup_key=f"eta_orari:{bg}:{plate}",
                    )
                )
                alert_fired = True

            if not alert_fired:
                logger.debug(
                    f"  {bg}: nessun alert — eff_eta={eff_eta_iso}, "
                    f"etoa={etoa_str}, scarico={data_scarico}, gps={verifica_gps}"
                )

        return alerts

    def _resolve_bg_entries(self, bg_filter, planning_rows, planner_client, data_str):
        """Fast path: filtra planning_rows per BG richiesti, poi calcola ETA solo per quelli."""
        entries = []
        bg_filter_upper = {b.upper() for b in bg_filter}
        autisti_processati = set()

        for row in planning_rows:
            if not row.get("id_employee"):
                continue

            planning_text = row.get("planning") or ""
            bg_codes = BG_REGEX.findall(planning_text)

            # Controlla se almeno un BG della riga è nel filtro
            matching_bgs = [bg for bg in bg_codes if bg.upper() in bg_filter_upper]
            if not matching_bgs:
                continue

            plate = row.get("plate_number", "")
            driver = f"{row.get('driver_name', '')} {row.get('driver_surname', '')}".strip()
            if not driver or driver in autisti_processati:
                continue
            autisti_processati.add(driver)

            logger.info(f"[BG filter] {driver} ({plate}) — BG: {matching_bgs}")

            try:
                eta_result = planner_client.execute_tool(
                    "get_eta_per_autista", {"nome_autista": driver, "data": data_str},
                )
            except Exception as e:
                logger.warning(f"  ETA fallita per {driver}: {e}")
                continue

            bg = eta_result.get("bg_in_corso", matching_bgs[0])
            if not eta_result.get("eta"):
                logger.info(f"  {driver}: ETA non disponibile, skip")
                continue

            # Verifica se la missione è ancora aperta
            try:
                bg_info = planner_client.execute_tool("get_info_bg", {"codice": bg})
                missione = bg_info.get("missione", {})
                if not missione.get("aperta", True):
                    logger.info(f"  BG {bg}: missione chiusa (status={missione.get('status')}), skip")
                    continue
            except Exception as e:
                logger.warning(f"  get_info_bg fallito per {bg}: {e}")

            logger.info(
                f"  ETA: {eta_result.get('eta_orario')}, "
                f"ETOA: {eta_result.get('etoa_orario')}, metodo: {eta_result.get('metodo')}"
            )
            logger.debug(f"  get_eta_per_autista response: {eta_result}")
            entries.append({"bg": bg, "driver": driver, "plate": plate, "eta_result": eta_result})
        return entries

    def _resolve_all_entries(self, planning_rows, planner_client, data_str):
        """Path standard: itera tutti gli autisti dalle righe planning."""
        entries = []
        autisti_processati = set()

        for row in planning_rows:
            if not row.get("id_employee"):
                continue

            planning_text = row.get("planning") or ""
            row_bgs = {bg.upper() for bg in BG_REGEX.findall(planning_text)}
            if not row_bgs:
                continue

            plate = row.get("plate_number", "")
            driver = f"{row.get('driver_name', '')} {row.get('driver_surname', '')}".strip()
            if not driver or driver in autisti_processati:
                continue
            autisti_processati.add(driver)

            logger.info(f"[{plate}] {driver} — BG planning: {row_bgs}")

            try:
                eta_args = {"nome_autista": driver, "data": data_str}
                logger.info(f"  -> execute get_eta_per_autista({eta_args})")
                eta_result = planner_client.execute_tool("get_eta_per_autista", eta_args)
            except Exception as e:
                logger.warning(f"  ETA fallita per {driver}: {e}")
                continue

            bg = eta_result.get("bg_in_corso", "")
            if not bg or not eta_result.get("eta"):
                logger.info(f"  {driver}: nessun BG in corso o ETA non disponibile, skip")
                continue

            # Verifica che il BG in corso sia tra quelli assegnati nel planning di oggi
            if bg.upper() not in row_bgs:
                logger.info(f"  {driver}: BG in corso {bg} non presente nel planning odierno ({row_bgs}), skip")
                continue

            # Verifica se la missione è ancora aperta
            try:
                bg_info = planner_client.execute_tool("get_info_bg", {"codice": bg})
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
            logger.debug(f"  get_eta_per_autista response: {eta_result}")
            entries.append({"bg": bg, "driver": driver, "plate": plate, "eta_result": eta_result})
        return entries


register_check(ETAOrariCheck())
