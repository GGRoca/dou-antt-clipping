from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, List, Optional, Tuple

import duckdb
import requests
from dateutil.tz import gettz
from lxml import etree

from settings import SETTINGS


BRA_TZ = gettz(os.getenv("TZ", "America/Sao_Paulo"))


@dataclass
class Edition:
    pub_date: date
    edition_id: str               # identificador estável (url/nome/id)
    edition_type: str             # normal|extra|suplementar|especial
    section: Optional[str]        # 1|2|3|None
    source_url: str               # onde baixar o XML


@dataclass
class Occurrence:
    pub_date: date
    edition_id: str
    edition_type: str
    section: Optional[str]
    title: str
    body_text: str
    link_dou: str                 # link oficial (idealmente)
    hash_item: str


def now_bra() -> datetime:
    return datetime.now(tz=BRA_TZ)


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""
    CREATE TABLE IF NOT EXISTS execucoes (
      exec_ts TIMESTAMP,
      window_days VARCHAR,
      editions_discovered INTEGER,
      editions_processed INTEGER,
      new_editions INTEGER,
      new_occurrences INTEGER,
      status VARCHAR,
      note VARCHAR
    );
    """)

    con.execute("""
    CREATE TABLE IF NOT EXISTS edicoes (
      pub_date DATE,
      edition_id VARCHAR,
      edition_type VARCHAR,
      section VARCHAR,
      source_url VARCHAR,
      first_seen_ts TIMESTAMP,
      processed_ts TIMESTAMP,
      PRIMARY KEY (pub_date, edition_id)
    );
    """)

    con.execute("""
    CREATE TABLE IF NOT EXISTS ocorrencias (
      pub_date DATE,
      edition_id VARCHAR,
      edition_type VARCHAR,
      section VARCHAR,
      title VARCHAR,
      body_text VARCHAR,
      link_dou VARCHAR,
      hash_item VARCHAR,
      inserted_ts TIMESTAMP,
      PRIMARY KEY (hash_item)
    );
    """)


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()


def match_filter(text: str) -> bool:
    t = text.lower()
    if not any(k.lower() in t for k in SETTINGS.term_antt):
        return False
    if not any(k.lower() in t for k in SETTINGS.term_sufer):
        return False
    if not re.search(SETTINGS.term_autoriza_regex, t, flags=re.IGNORECASE):
        return False
    return True


# =========================
# INLABS fetch layer (AJUSTE AQUI)
# =========================

def fetch_inlabs_editions_for_date(pub_date: date) -> List[Edition]:
    """
    Devolve TODAS as edições do dia (normal + extras + suplementares/especiais),
    com URL direta para baixar XML.

    ⚠️ Você vai precisar ajustar esta função para a forma real de listar/baixar no INLABS.

    Referência: INLABS fornece XML/PDF para edições completas do DOU. :contentReference[oaicite:4]{index=4}
    """
    # ======= PLACEHOLDER =======
    # Estratégia recomendada:
    # 1) Descobrir um endpoint “índice” do INLABS (JSON/HTML) que liste as edições do dia
    # 2) Parsear e construir Edition(...) para cada xml_url encontrado
    # 3) edition_id deve ser algo estável (ex: nome do arquivo / parâmetro id / url)

    # Por enquanto, vazio — o pipeline segue funcionando (vai só registrar execuções “0 edições”)
    return []


def download_xml(url: str, timeout: int = 60) -> bytes:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content


def parse_xml_into_occurrences(xml_bytes: bytes, edition: Edition) -> List[Occurrence]:
    """
    Parse genérico: extrai nós de texto e tenta formar “itens”.
    ⚠️ O XML do INLABS tem um schema específico; quando você tiver um exemplo real,
    a gente ajusta isso para extrair título/ementa/link com precisão.
    """
    root = etree.fromstring(xml_bytes)

    # Heurística: juntar todo texto visível
    full_text = " ".join([t.strip() for t in root.itertext() if t and t.strip()])
    if not full_text:
        return []

    if not match_filter(full_text):
        return []

    # Placeholder: cria 1 ocorrência “por edição” (até termos extração por matéria)
    title = f"Possível ocorrência (edição {edition.edition_type})"
    link_dou = edition.source_url  # troque pelo link canônico do DOU quando tiver

    hash_item = sha1("|".join([
        str(edition.pub_date),
        edition.edition_id,
        edition.edition_type,
        edition.section or "",
        title,
        link_dou
    ]))

    return [Occurrence(
        pub_date=edition.pub_date,
        edition_id=edition.edition_id,
        edition_type=edition.edition_type,
        section=edition.section,
        title=title,
        body_text=full_text,
        link_dou=link_dou,
        hash_item=hash_item
    )]


# =========================
# Pipeline
# =========================

def run(window_days: List[int]) -> Tuple[int, int]:
    """
    Retorna: (new_occurrences, new_editions)
    """
    os.makedirs("data", exist_ok=True)
    con = duckdb.connect(SETTINGS.db_path)
    ensure_schema(con)

    exec_ts = now_bra()

    discovered = 0
    processed = 0
    new_editions = 0
    new_occ = 0

    try:
        for d in window_days:
            pub_date = (exec_ts.date() - timedelta(days=d))
            editions = fetch_inlabs_editions_for_date(pub_date)
            discovered += len(editions)

            for ed in editions:
                # upsert edição e marcar first_seen
                existing = con.execute(
                    "SELECT 1 FROM edicoes WHERE pub_date = ? AND edition_id = ?",
                    [ed.pub_date, ed.edition_id]
                ).fetchone()

                if existing is None:
                    new_editions += 1
                    con.execute("""
                        INSERT INTO edicoes (pub_date, edition_id, edition_type, section, source_url, first_seen_ts, processed_ts)
                        VALUES (?, ?, ?, ?, ?, ?, NULL)
                    """, [ed.pub_date, ed.edition_id, ed.edition_type, ed.section, ed.source_url, exec_ts])
                else:
                    # atualiza metadados caso tenham mudado
                    con.execute("""
                        UPDATE edicoes
                        SET edition_type = ?, section = ?, source_url = ?
                        WHERE pub_date = ? AND edition_id = ?
                    """, [ed.edition_type, ed.section, ed.source_url, ed.pub_date, ed.edition_id])

                # processar XML
                xml = download_xml(ed.source_url)
                occs = parse_xml_into_occurrences(xml, ed)

                # marcar edição como processada
                con.execute("""
                    UPDATE edicoes
                    SET processed_ts = ?
                    WHERE pub_date = ? AND edition_id = ?
                """, [exec_ts, ed.pub_date, ed.edition_id])
                processed += 1

                # inserir ocorrências (dedupe por PK hash_item)
                for o in occs:
                    try:
                        con.execute("""
                            INSERT INTO ocorrencias (pub_date, edition_id, edition_type, section, title, body_text, link_dou, hash_item, inserted_ts)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, [o.pub_date, o.edition_id, o.edition_type, o.section, o.title, o.body_text, o.link_dou, o.hash_item, exec_ts])
                        new_occ += 1
                    except duckdb.ConstraintException:
                        # já existe
                        pass

        con.execute("""
            INSERT INTO execucoes (exec_ts, window_days, editions_discovered, editions_processed, new_editions, new_occurrences, status, note)
            VALUES (?, ?, ?, ?, ?, ?, 'ok', ?)
        """, [exec_ts, ",".join(map(str, window_days)), discovered, processed, new_editions, new_occ, ""])

    except Exception as e:
        con.execute("""
            INSERT INTO execucoes (exec_ts, window_days, editions_discovered, editions_processed, new_editions, new_occurrences, status, note)
            VALUES (?, ?, ?, ?, ?, ?, 'erro', ?)
        """, [exec_ts, ",".join(map(str, window_days)), discovered, processed, new_editions, new_occ, str(e)])
        raise
    finally:
        con.close()

    # pequeno “runlog” para o workflow saber se manda email
    os.makedirs("data", exist_ok=True)
    with open("data/runlog.json", "w", encoding="utf-8") as f:
        json.dump({
            "exec_ts": exec_ts.isoformat(),
            "new_occurrences": new_occ,
            "new_editions": new_editions,
            "editions_discovered": discovered,
            "editions_processed": processed,
            "window_days": window_days,
        }, f, ensure_ascii=False, indent=2)

    return new_occ, new_editions


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--window-days", default="0,1,2", help="Ex: 0,1,2")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    window_days = [int(x.strip()) for x in args.window_days.split(",") if x.strip()]
    run(window_days)
