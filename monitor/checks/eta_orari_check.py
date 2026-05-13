import logging
import re
from datetime import datetime, timedelta, time as dtime
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


def _format_date_it(date_str: str) -> str:
    """Formatta data ISO (2026-05-08) -> italiano (08/05/2026)."""
    if not date_str:
        return date_str
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return date_str


def _get_viaggio_field(viaggio: dict, *keys: str, default: str = "") -> str:
    """Cerca il primo campo presente tra i nomi alternativi."""
    for k in keys:
        val = viaggio.get(k)
        if val:
            return val
    return default


def _parse_iso(val: str):
    """Parse ISO datetime, None on failure. Sempre naive (strip tzinfo)."""
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(val)
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
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


def _find_next_opening(arrival_dt: datetime, orari: dict):
    """Trova prossima finestra aperta dopo arrival_dt.

    Returns:
        datetime se arrivo dentro finestra o prima di una finestra successiva.
        None se arrivo dopo chiusura pomeridiana (giorno dopo necessario).
    """
    if not orari:
        return arrival_dt  # fallback: nessun vincolo orario

    windows = []
    for fascia in ("mattina", "pomeriggio"):
        w = orari.get(fascia)
        if w and w.get("dalle") and w.get("alle"):
            try:
                dalle = dtime.fromisoformat(w["dalle"])
                alle = dtime.fromisoformat(w["alle"])
                windows.append((dalle, alle))
            except (ValueError, TypeError):
                pass

    if not windows:
        return arrival_dt  # nessun orario configurato

    windows.sort(key=lambda x: x[0])
    arr_time = arrival_dt.time()

    for dalle, alle in windows:
        if dalle <= arr_time <= alle:
            return arrival_dt  # dentro finestra
        if arr_time < dalle:
            return arrival_dt.replace(hour=dalle.hour, minute=dalle.minute, second=0)

    return None  # dopo ultima finestra


def _get_distance_json(planner_client, origine, destinazione, cache):
    """Chiama calcola_distanza con formato JSON, con cache."""
    key = (origine.strip().lower(), destinazione.strip().lower())
    if key in cache:
        return cache[key]
    result = planner_client.execute_tool(
        "calcola_distanza",
        {"origine": origine, "destinazione": destinazione, "formato": "json"},
    )
    parsed = {
        "distanza_km": result.get("distanza_km") or result.get("km"),
        "duration_min": result.get("duration_min") or result.get("tempo_min"),
    }
    cache[key] = parsed
    return parsed


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
          try:
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

            # Skip viaggi con scarico in data futura rispetto al giorno di check
            if data_scarico and data_scarico > data_str:
                logger.info(f"  {bg}: data_scarico={data_scarico} > check={data_str}, skip (futuro)")
                continue

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

            # Display: eta_data = finale, eta_missione_orario = teorico, eta_gps_orario = GPS
            eta_finale_display = eta_result.get("eta_data", "") or eta_result.get("eta_orario", "") or _format_dt_it(eta_str)
            eta_missione_display = eta_result.get("eta_missione_orario", "") or _format_dt_it(eta_result.get("eta_missione", ""))
            eta_gps_display = eta_result.get("eta_gps_orario", "") or _format_dt_it(eta_result.get("eta_gps", ""))
            has_gps = verifica_gps in ("ritardo", "confermato", "anticipo")
            alert_fired = False

            # Scenario A: GPS rileva ritardo — alert solo se ritardo effettivo > 0
            if is_gps_delayed and ritardo_minuti > 0:
                alerts.append(
                    CheckAlert(
                        check_name=self.name,
                        severity="warning",
                        title=f"Ritardo GPS: {bg}",
                        message=(
                            f"Autista **{driver}** ({plate}) per BG [{bg}] — {luogo_scarico}\n"
                            f"ETA missione: **{eta_missione_display}**, "
                            f"ETA GPS: **{eta_gps_display}** (+{ritardo_minuti} min)."
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
                        severity="warning" if has_gps else "info",
                        title=f"ETA fuori orario: {bg}",
                        message=(
                            f"Autista **{driver}** ({plate}) per BG [{bg}] — {luogo_scarico}\n"
                            f"ETA: {eta_finale_display}, "
                            f"disponibilità spostata a **{_format_dt_it(disponibile_da_str)}**.\n"
                            f"Consegna prevista entro: {_format_date_it(data_scarico)}.\n"
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
            # Se disponibile_da == etoa, il gap ETA→ETOA è solo tempo operazione, non finestra
            if (
                not alert_fired
                and etoa_dt
                and eff_eta_dt
                and disponibile_da_dt
                and disponibile_da_dt > etoa_dt
                and disponibile_da_dt.date() == eff_eta_dt.date()
            ):
                alerts.append(
                    CheckAlert(
                        check_name=self.name,
                        severity="warning" if has_gps else "info",
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
          except Exception as e:
            logger.error(f"  Errore check BG {entry.get('bg', '?')}: {e}", exc_info=True)

        # Scenario D: impatto a catena — ritardo su BG corrente compromette BG successivi
        for entry in entries:
            try:
                chain_alerts = self._check_chain_impact(
                    entry, viaggi_by_bg, planner_client, data_str
                )
                alerts.extend(chain_alerts)
            except Exception as e:
                logger.error(f"  Chain impact check fallito per {entry.get('bg', '?')}: {e}", exc_info=True)

        return alerts

    def _check_chain_impact(self, entry, viaggi_by_bg, planner_client, data_str):
        """Verifica se ritardo su BG corrente compromette BG successivi dello stesso autista.

        Simula viaggio completo per ogni BG successivo:
        1. Viaggio a vuoto (scarico corrente → carico successivo)
        2. Verifica orari sede CARICO
        3. Viaggio carico (carico → scarico successivo)
        4. Verifica orari sede SCARICO
        Severity CRITICAL se simulazione mostra impossibilita' consegna in orario.
        """
        TEMPO_CARICO_ORE = 1.5
        TEMPO_SCARICO_ORE = 2.0
        MARGIN_WARNING_MIN = 60

        alerts = []
        bg_in_corso = entry["bg"].upper()
        bg_list = entry.get("bg_list_ordered", [])
        driver = entry["driver"]
        plate = entry["plate"]
        eta_result = entry["eta_result"]

        disponibile_da_str = eta_result.get("disponibile_da")
        disponibile_da_dt = _parse_iso(disponibile_da_str)
        if not disponibile_da_dt or not bg_list:
            return alerts

        try:
            idx = bg_list.index(bg_in_corso)
        except ValueError:
            return alerts

        subsequent_bgs = bg_list[idx + 1:]
        if not subsequent_bgs:
            return alerts

        verifica_gps = eta_result.get("verifica_gps", "")
        ritardo_minuti = eta_result.get("ritardo_minuti", 0) or 0
        luogo_scarico_corrente = eta_result.get("luogo_scarico", "")

        # Alert solo se GPS conferma ritardo
        if verifica_gps != "ritardo":
            return alerts

        current_time = disponibile_da_dt
        current_location = luogo_scarico_corrente
        dist_cache = {}

        for next_bg in subsequent_bgs:
            # --- Recupera info BG successivo ---
            viaggio = viaggi_by_bg.get(next_bg, {})
            data_scarico = _normalize_date(_get_viaggio_field(viaggio, "data_scarico"))
            luogo_carico_next = _get_viaggio_field(viaggio, "partenza", "luogo_carico")
            luogo_scarico_next = _get_viaggio_field(viaggio, "arrivo", "luogo_scarico")

            if not data_scarico:
                try:
                    stato = planner_client.execute_tool(
                        "cerca_stato_ordine", {"codice": next_bg}
                    )
                    raw = _normalize_date(stato.get("data_consegna", ""))
                    if raw and raw > "2000-01-01":
                        data_scarico = raw
                    luogo_carico_next = luogo_carico_next or stato.get("luogo_carico", "")
                    luogo_scarico_next = luogo_scarico_next or stato.get("luogo_scarico", "")
                except Exception as e:
                    logger.warning(f"  cerca_stato_ordine fallito per {next_bg}: {e}")
                    continue

            if not data_scarico:
                continue

            # Skip BG con scarico futuro rispetto al giorno di check
            if data_scarico > data_str:
                continue

            data_scarico_date = datetime.strptime(data_scarico, "%Y-%m-%d").date()

            # --- Simulazione viaggio ---
            can_simulate = bool(current_location and luogo_carico_next and luogo_scarico_next)
            origin_location = current_location  # salva prima che venga aggiornato
            sim_ok = False
            severity = "warning"
            sim_details = {}

            if can_simulate:
                try:
                    sim_details = self._simulate_chain_trip(
                        planner_client, next_bg, current_time, current_location,
                        luogo_carico_next, luogo_scarico_next, data_scarico,
                        data_scarico_date, TEMPO_CARICO_ORE, TEMPO_SCARICO_ORE,
                        MARGIN_WARNING_MIN, dist_cache,
                    )
                    severity = sim_details["severity"]
                    sim_ok = True

                    # Aggiorna stato catena per BG seguente
                    if sim_details.get("next_disponibile_da"):
                        current_time = sim_details["next_disponibile_da"]
                        current_location = luogo_scarico_next

                    # Se simulazione OK con margine ampio, catena recuperata
                    if severity == "ok":
                        logger.info(f"  Chain {bg_in_corso} → {next_bg}: simulazione OK, catena recuperata")
                        break

                except Exception as e:
                    logger.warning(f"  Simulazione fallita per {next_bg}: {e}")

            # Fallback senza simulazione
            if not sim_ok:
                deadline_dt = _parse_iso(data_scarico + "T23:59:59")
                if deadline_dt and current_time > deadline_dt:
                    severity = "critical"
                else:
                    severity = "warning"

            if severity == "ok":
                continue

            # --- Costruisci messaggio ---
            compromette = severity == "critical"
            msg_lines = [
                f"Autista **{driver}** ({plate}) — ritardo di **{ritardo_minuti} min** su BG [{bg_in_corso}] "
                f"{'compromette' if compromette else 'potrebbe impattare'} BG successivo [{next_bg}].",
                f"Libero da BG corrente: **{_format_dt_it(disponibile_da_str)}**.",
            ]

            if sim_ok:
                # Dettagli simulazione viaggio a vuoto
                dist_empty = sim_details.get("distanza_empty_km")
                dur_empty = sim_details.get("duration_empty_min")
                arrivo_carico = sim_details.get("arrivo_carico_dt")
                carico_status = sim_details.get("carico_status", "")

                if dist_empty and dur_empty:
                    ore_e = dur_empty / 60
                    msg_lines.append(
                        f"Viaggio a vuoto: {origin_location or '?'} → {luogo_carico_next} "
                        f"({dist_empty:.0f} km, ~{ore_e:.1f}h)."
                    )
                if arrivo_carico:
                    msg_lines.append(
                        f"Arrivo carico stimato: **{arrivo_carico.strftime('%d/%m/%Y %H:%M')}** — {carico_status}."
                    )

                # Dettagli simulazione viaggio carico
                dist_loaded = sim_details.get("distanza_loaded_km")
                dur_loaded = sim_details.get("duration_loaded_min")
                arrivo_scarico = sim_details.get("arrivo_scarico_dt")
                scarico_status = sim_details.get("scarico_status", "")

                if dist_loaded and dur_loaded:
                    ore_l = dur_loaded / 60
                    msg_lines.append(
                        f"Viaggio carico: {luogo_carico_next} → {luogo_scarico_next} "
                        f"({dist_loaded:.0f} km, ~{ore_l:.1f}h)."
                    )
                if arrivo_scarico:
                    msg_lines.append(
                        f"Arrivo scarico stimato: **{arrivo_scarico.strftime('%d/%m/%Y %H:%M')}** — {scarico_status}."
                    )
            else:
                # Fallback: solo info base
                if luogo_carico_next:
                    msg_lines.append(f"Prossimo carico: **{luogo_carico_next}** (simulazione non disponibile).")

            consegna_str = f"Consegna [{next_bg}]:"
            if luogo_scarico_next:
                msg_lines.append(f"{consegna_str} **{luogo_scarico_next}**, entro **{_format_date_it(data_scarico)}**.")
            else:
                msg_lines.append(f"{consegna_str} entro **{_format_date_it(data_scarico)}**.")

            alerts.append(CheckAlert(
                check_name=self.name,
                severity=severity,
                title=f"Impatto a catena: {next_bg}",
                message="\n".join(msg_lines),
                context={
                    "bg": bg_in_corso,
                    "bg_impattato": next_bg,
                    "targa": plate,
                    "autista": driver,
                    "disponibile_da": disponibile_da_str,
                    "data_scarico_next": data_scarico,
                    "luogo_carico_next": luogo_carico_next,
                    "luogo_scarico_next": luogo_scarico_next,
                    "ritardo_minuti": ritardo_minuti,
                    "data": data_str,
                    **(sim_details.get("context_extra", {})),
                },
                entity_type="trailer",
                entity_id=plate,
                dedup_key=f"chain_impact:{bg_in_corso}:{next_bg}:{plate}",
            ))

        return alerts

    def _simulate_chain_trip(self, planner_client, next_bg,
                             start_time, start_location,
                             carico_location, scarico_location,
                             data_scarico_str, data_scarico_date,
                             tempo_carico_ore, tempo_scarico_ore,
                             margin_warning_min, dist_cache):
        """Simula viaggio completo per un BG successivo nella catena.

        Returns dict con: severity, arrivo_carico_dt, arrivo_scarico_dt,
        distanza_empty_km, duration_empty_min, distanza_loaded_km, duration_loaded_min,
        carico_status, scarico_status, next_disponibile_da, context_extra.
        """
        result = {"severity": "warning", "context_extra": {}}

        # --- Leg 1: viaggio a vuoto ---
        dist_empty = _get_distance_json(planner_client, start_location, carico_location, dist_cache)
        km_empty = dist_empty.get("distanza_km") or 0
        dur_empty = dist_empty.get("duration_min") or 0
        result["distanza_empty_km"] = km_empty
        result["duration_empty_min"] = dur_empty

        arrivo_carico_dt = start_time + timedelta(minutes=dur_empty)
        result["arrivo_carico_dt"] = arrivo_carico_dt

        # --- Orari sede CARICO ---
        effective_carico_dt = arrivo_carico_dt
        carico_status = "dentro finestra"
        try:
            orari_carico = planner_client.execute_tool(
                "check_orari_sede",
                {"bg": next_bg, "data": arrivo_carico_dt.strftime("%Y-%m-%d"), "tipo": "CARICO"},
            )
            orari_c = orari_carico.get("orari", {})
            opening = _find_next_opening(arrivo_carico_dt, orari_c)
            if opening is None:
                # Dopo chiusura pomeridiana → giorno dopo, prima finestra mattina
                next_day = arrivo_carico_dt.date() + timedelta(days=1)
                mattina = orari_c.get("mattina", {})
                dalle_str = mattina.get("dalle", "08:00")
                dalle_t = dtime.fromisoformat(dalle_str)
                effective_carico_dt = datetime.combine(next_day, dalle_t)
                carico_status = f"fuori finestra (attesa fino a {effective_carico_dt.strftime('%d/%m %H:%M')})"
            elif opening > arrivo_carico_dt:
                effective_carico_dt = opening
                carico_status = f"attesa apertura ({effective_carico_dt.strftime('%H:%M')})"
        except Exception as e:
            logger.warning(f"  check_orari_sede CARICO fallito per {next_bg}: {e}")

        result["carico_status"] = carico_status
        result["context_extra"]["arrivo_carico_stimato"] = arrivo_carico_dt.isoformat()
        result["context_extra"]["carico_status"] = carico_status

        # Partenza da carico (dopo operazioni carico)
        partenza_carico_dt = effective_carico_dt + timedelta(hours=tempo_carico_ore)

        # --- Leg 2: viaggio carico ---
        dist_loaded = _get_distance_json(planner_client, carico_location, scarico_location, dist_cache)
        km_loaded = dist_loaded.get("distanza_km") or 0
        dur_loaded = dist_loaded.get("duration_min") or 0
        result["distanza_loaded_km"] = km_loaded
        result["duration_loaded_min"] = dur_loaded

        arrivo_scarico_dt = partenza_carico_dt + timedelta(minutes=dur_loaded)
        result["arrivo_scarico_dt"] = arrivo_scarico_dt

        # --- Orari sede SCARICO ---
        scarico_status = "dentro finestra"
        effective_scarico_dt = arrivo_scarico_dt
        try:
            orari_scarico = planner_client.execute_tool(
                "check_orari_sede",
                {"bg": next_bg, "data": arrivo_scarico_dt.strftime("%Y-%m-%d"), "tipo": "SCARICO"},
            )
            orari_s = orari_scarico.get("orari", {})
            opening_s = _find_next_opening(arrivo_scarico_dt, orari_s)
            if opening_s is None:
                scarico_status = "FUORI ORARIO — NON FATTIBILE"
            elif opening_s > arrivo_scarico_dt:
                effective_scarico_dt = opening_s
                scarico_status = f"attesa apertura ({effective_scarico_dt.strftime('%H:%M')})"
        except Exception as e:
            logger.warning(f"  check_orari_sede SCARICO fallito per {next_bg}: {e}")

        result["scarico_status"] = scarico_status
        result["context_extra"]["arrivo_scarico_stimato"] = arrivo_scarico_dt.isoformat()
        result["context_extra"]["scarico_status"] = scarico_status
        result["context_extra"]["distanza_empty_km"] = km_empty
        result["context_extra"]["distanza_loaded_km"] = km_loaded

        # --- Severity ---
        arrivo_date = arrivo_scarico_dt.date()
        if arrivo_date > data_scarico_date:
            # Arrivo giorno dopo la data di consegna
            result["severity"] = "critical"
        elif scarico_status.startswith("FUORI ORARIO") and arrivo_date >= data_scarico_date:
            # Arrivo dopo chiusura sede nel giorno di consegna
            result["severity"] = "critical"
        elif effective_scarico_dt and effective_scarico_dt != arrivo_scarico_dt:
            # Deve aspettare apertura ma riesce — warning se margine stretto
            result["severity"] = "warning"
        else:
            # Simulazione OK — consegna fattibile
            result["severity"] = "ok"

        # next_disponibile_da per catena successiva
        if effective_scarico_dt and not scarico_status.startswith("FUORI ORARIO"):
            result["next_disponibile_da"] = effective_scarico_dt + timedelta(hours=tempo_scarico_ore)

        return result

    def _resolve_bg_entries(self, bg_filter, planning_rows, planner_client, data_str):
        """Fast path: filtra planning_rows per BG richiesti, poi calcola ETA solo per quelli."""
        entries = []
        bg_filter_upper = {b.upper() for b in bg_filter}
        autisti_processati = set()

        for row in planning_rows:
            if not row.get("id_employee"):
                continue

            planning_text = row.get("planning") or ""
            bg_list_ordered = [bg.upper() for bg in BG_REGEX.findall(planning_text)]

            # Controlla se almeno un BG della riga è nel filtro
            matching_bgs = [bg for bg in bg_list_ordered if bg.upper() in bg_filter_upper]
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
            entries.append({"bg": bg, "driver": driver, "plate": plate, "eta_result": eta_result, "bg_list_ordered": bg_list_ordered})
        return entries

    def _resolve_all_entries(self, planning_rows, planner_client, data_str):
        """Path standard: itera tutti gli autisti dalle righe planning."""
        entries = []
        autisti_processati = set()

        for row in planning_rows:
            if not row.get("id_employee"):
                continue

            planning_text = row.get("planning") or ""
            bg_list_ordered = [bg.upper() for bg in BG_REGEX.findall(planning_text)]
            row_bgs = set(bg_list_ordered)
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
            entries.append({"bg": bg, "driver": driver, "plate": plate, "eta_result": eta_result, "bg_list_ordered": bg_list_ordered})
        return entries


register_check(ETAOrariCheck())
