from __future__ import annotations
import io
import re
import zipfile
from typing import List, Optional, Tuple
from dataclasses import dataclass
from PyPDF2 import PdfReader

@dataclass(frozen=True)
class ParsedPublication:
    title: Optional[str]
    orgao: Optional[str]
    full_text: str
    dou_link: Optional[str]  # se você conseguir extrair do XML; senão fica None

def _text_contains_any(text: str, needles: List[str]) -> Optional[str]:
    t = text.lower()
    for n in needles:
        if n.lower() in t:
            return n
    return None

def parse_zip_for_text(zip_bytes: bytes) -> str:
    """
    Extrai texto bruto de XMLs dentro do ZIP.
    Mantém simples: concatena tudo como texto, removendo tags.
    (Depois refinamos para pegar campos: título, órgão, link, etc.)
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        parts: List[str] = []
        for name in z.namelist():
            if name.lower().endswith(".xml"):
                raw = z.read(name).decode("utf-8", errors="ignore")
                # remove tags
                text = re.sub(r"<[^>]+>", " ", raw)
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    parts.append(text)
        return "\n\n".join(parts)

def parse_pdf_for_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = []
    for p in reader.pages:
        pages.append((p.extract_text() or "").strip())
    return "\n\n".join([p for p in pages if p])

def extract_publications_from_blob(blob_text: str) -> List[ParsedPublication]:
    """
    MVP: trata o blob inteiro como “um texto só”.
    O filtro por Órgão + keyword acontece fora.
    """
    return [ParsedPublication(title=None, orgao=None, full_text=blob_text, dou_link=None)]

def find_relevant_hits(
    publications: List[ParsedPublication],
    orgao_contains: str,
    keywords_any: List[str],
) -> List[Tuple[ParsedPublication, str]]:
    hits: List[Tuple[ParsedPublication, str]] = []
    orgao_l = orgao_contains.lower()

    for pub in publications:
        txt = pub.full_text
        if orgao_l not in txt.lower():
            continue
        kw = _text_contains_any(txt, keywords_any)
        if kw:
            hits.append((pub, kw))
    return hits
