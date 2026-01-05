from __future__ import annotations
import re
import requests
from bs4 import BeautifulSoup
from dataclasses import dataclass
from typing import List
from datetime import date

@dataclass(frozen=True)
class InlabsFile:
    name: str
    url: str

class InlabsClient:
    """
    Estratégia:
    - Abrir a página do INLABS do dia
    - Extrair os links de arquivos (ZIP/PDF/XML etc)
    - Você vai processar preferencialmente ZIP (que tende a conter XML),
      mas como fallback pode ler PDF.
    """
    def __init__(self, base_url: str, timeout_s: int = 60):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "dou-antt-clipping/1.0"})

    def day_page_url(self, d: date) -> str:
        # Pelo seu print, o breadcrumb mostra algo como 2025-12-18.
        # O INLABS usa navegação web; este endpoint costuma funcionar:
        # /index.php?p=YYYY-MM-DD  (se não funcionar, ajustamos em 2 min com um teste seu)
        return f"{self.base_url}/index.php?p={d.isoformat()}"

    def list_files(self, d: date) -> List[InlabsFile]:
        url = self.day_page_url(d)
        r = self.session.get(url, timeout=self.timeout_s)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "lxml")
        links = soup.find_all("a", href=True)

        out: List[InlabsFile] = []
        for a in links:
            href = a["href"].strip()
            text = (a.get_text() or "").strip()

            # Normaliza URL relativa
            if href.startswith("/"):
                full = self.base_url + href
            elif href.startswith("http"):
                full = href
            else:
                full = self.base_url + "/" + href.lstrip("./")

            # Heurística: só pegar arquivos "baixáveis"
            if re.search(r"\.(zip|pdf|xml)$", full, flags=re.IGNORECASE):
                name = text if text else full.split("/")[-1]
                out.append(InlabsFile(name=name, url=full))

        # Remove duplicados por URL
        uniq = {f.url: f for f in out}
        return list(uniq.values())

    def download_bytes(self, file_url: str) -> bytes:
        r = self.session.get(file_url, timeout=self.timeout_s)
        r.raise_for_status()
        return r.content
