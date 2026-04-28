from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CheckAlert:
    check_name: str
    severity: str
    title: str
    message: str
    context: dict = field(default_factory=dict)
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    dedup_key: str = ""


@dataclass
class ETAResult:
    bg: str
    targa: str
    eta: Optional[str] = None
    eta_orario: Optional[str] = None
    disponibile_da: Optional[str] = None
    metodo: Optional[str] = None
    affidabilita: Optional[float] = None
    distanza_residua_km: Optional[float] = None
    tempo_viaggio_ore: Optional[float] = None
    posizione_gps: Optional[str] = None
    dettagli: Optional[str] = None
