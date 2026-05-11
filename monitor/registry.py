from abc import ABC, abstractmethod
from datetime import date
from typing import List, Optional

from models import CheckAlert


class BaseCheck(ABC):
    name: str = "unnamed"

    @abstractmethod
    def run(
        self,
        data: date,
        planning_rows: list,
        viaggi: dict,
        planner_client,
        berlink_client,
        bg_filter: Optional[List[str]] = None,
    ) -> List[CheckAlert]:
        """Esegue il check e ritorna lista alert."""
        ...


_checks: List[BaseCheck] = []


def register_check(check: BaseCheck):
    _checks.append(check)


def get_registered_checks() -> List[BaseCheck]:
    return list(_checks)
