from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class Settings:
    # Banco
    db_path: str = "data/clipping.duckdb"

    # Janela de revarredura (0=hoje, 1=ontem, 2=anteontem)
    window_days: List[int] = (0, 1, 2)

    # Filtro (provisório, você vai calibrar depois)
    term_antt: List[str] = ("ANTT", "Agência Nacional de Transportes Terrestres")
    term_sufer: List[str] = ("SUFER", "Superintendência de Transporte Ferroviário")
    term_autoriza_regex: str = r"\bautoriza\w*\b"

    # E-mail
    email_enabled: bool = os.getenv("EMAIL_ENABLED", "0") == "1"
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587") or 587)
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_pass: str = os.getenv("SMTP_PASS", "")
    email_from: str = os.getenv("EMAIL_FROM", "")
    email_to_raw: str = os.getenv("EMAIL_TO", "")

    @property
    def email_to_list(self) -> List[str]:
        if not self.email_to_raw.strip():
            return []
        return [e.strip() for e in self.email_to_raw.split(",") if e.strip()]


SETTINGS = Settings()
