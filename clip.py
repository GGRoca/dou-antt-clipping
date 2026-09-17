#!/usr/bin/env python3
"""
Clipping DOU - ANTT/SUFER via INLABS  (v5, reescrito do zero)

Pipeline por execucao:
  1. login no INLABS (protocolo oficial: POST logar.php -> cookie inlabs_session_cookie)
  2. para cada data da janela (D-lookback ... D0) e cada secao: lista os arquivos
     publicados (index.php?p=DATA) e baixa os ZIPs ainda nao processados
  3. cada XML do ZIP e um <article> com metadados nos atributos (artType,
     artCategory, pubDate, editionNumber, numberPage, pdfPage) e o conteudo em
     CDATA dentro de <body> (Identifica, Ementa, Texto, ...)
  4. filtros por orgao (substring de artCategory) + tipo de ato (artType)
  5. matches novos vao para o SQLite (chave = id do article) e sao enviados
     por e-mail; o envio e marcado no banco (se o SMTP falhar, reenvia na proxima)
  6. edicoes extras publicadas apenas em PDF geram um alerta para conferencia
     manual (o texto do PDF nao e recortado)

Politica de e-mail:
  - achados: imediato, em qualquer execucao
  - "sem novidades": na primeira execucao bem-sucedida de cada dia util (BRT)
  - pane: se o INLABS ficar inacessivel por mais de N horas (config), 1 alerta/dia
  - SMTP com falha = erro explicito (exit 1); achados ficam pendentes no banco
"""
from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import os
import re
import smtplib
import sqlite3
import sys
import time
import traceback
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import BytesIO
from typing import Dict, Iterable, List, Optional, Tuple
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup

BRT = ZoneInfo("America/Sao_Paulo")
INLABS_BASE = "https://inlabs.in.gov.br"
# Header enviado pelo script oficial da Imprensa Nacional ("script" em hex)
INLABS_ORIGEM = "736372697074"

# artType de fragmentos (anexos/quadros/tabelas) que nao sao atos por si so.
FRAGMENT_TYPES = {"anexo", "quadro", "tabela"}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_brt() -> datetime:
    return datetime.now(BRT)


def today_brt() -> date:
    return now_brt().date()


def norm(text: str) -> str:
    """minusculas, sem acentos, espacos colapsados."""
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text).strip().lower()


# ============================================================================
# CONFIG
# ============================================================================

@dataclass
class Filtro:
    nome: str
    secao: str
    orgao: str
    art_types: List[str] = field(default_factory=list)

    def aceita(self, art_category: str, art_type: str) -> bool:
        if norm(self.orgao) not in norm(art_category):
            return False
        if not self.art_types:
            return True
        return norm(art_type) in {norm(t) for t in self.art_types}


@dataclass
class Config:
    inlabs_email: str
    inlabs_password: str
    filtros: List[Filtro]
    lookback_days: int
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str
    email_from: str
    email_to: List[str]
    subject_prefix: str
    db_path: str
    alerta_indisponibilidade_horas: int = 24
    confirmacao_diaria: bool = True

    @property
    def secoes(self) -> List[str]:
        return sorted({f.secao for f in self.filtros})


def load_config(path: str) -> Config:
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    inlabs = cfg.get("inlabs") or {}
    mail = cfg["mail"]
    filtros = [
        Filtro(
            nome=f["nome"],
            secao=str(f.get("secao", "DO1")).upper(),
            orgao=f["orgao"],
            art_types=list(f.get("art_types") or []),
        )
        for f in cfg.get("filtros", [])
    ]
    if not filtros:
        raise SystemExit("config.yml sem filtros.")
    return Config(
        inlabs_email=os.getenv("INLABS_EMAIL") or inlabs.get("email") or "",
        inlabs_password=os.getenv("INLABS_PASSWORD") or inlabs.get("password") or "",
        filtros=filtros,
        lookback_days=int(cfg.get("lookback_days", 2)),
        smtp_host=mail["smtp_host"],
        smtp_port=int(mail["smtp_port"]),
        smtp_user=os.getenv("SMTP_USER") or mail.get("smtp_user") or "",
        smtp_pass=os.getenv("SMTP_PASS") or mail.get("smtp_pass") or "",
        email_from=mail["from_email"],
        email_to=list(mail["to_emails"]),
        subject_prefix=mail.get("subject_prefix", "[DOU]"),
        db_path=cfg["storage"]["db_path"],
        alerta_indisponibilidade_horas=int(cfg.get("alerta_indisponibilidade_horas", 24)),
        confirmacao_diaria=bool(cfg.get("confirmacao_diaria", True)),
    )


# ============================================================================
# BANCO (SQLite)
# ============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_ts          TEXT NOT NULL,
    run_date        TEXT NOT NULL,
    status          TEXT NOT NULL,          -- ok | inlabs_unavailable | error
    files_processed INTEGER NOT NULL DEFAULT 0,
    new_matches     INTEGER NOT NULL DEFAULT 0,
    emails_sent     INTEGER NOT NULL DEFAULT 0,
    notes           TEXT
);
CREATE TABLE IF NOT EXISTS processed_files (
    file_name    TEXT PRIMARY KEY,
    edition_date TEXT NOT NULL,
    processed_ts TEXT NOT NULL,
    articles     INTEGER NOT NULL DEFAULT 0,
    matched      INTEGER NOT NULL DEFAULT 0,
    size         INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS matches (
    article_id     TEXT PRIMARY KEY,
    file_name      TEXT NOT NULL,
    edition_date   TEXT NOT NULL,           -- ISO (data da edicao)
    filter_names   TEXT NOT NULL,           -- separados por ", "
    pub_name       TEXT, art_type TEXT, art_category TEXT,
    edition_number TEXT, number_page TEXT, pdf_page TEXT,
    identifica     TEXT, ementa TEXT, texto TEXT, texto_html TEXT,
    assina         TEXT, cargo TEXT,
    kind           TEXT NOT NULL DEFAULT 'article',   -- article | pdf_alert
    created_ts     TEXT NOT NULL,
    emailed_ts     TEXT                                -- NULL = pendente de envio
);
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Storage:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self._migrate_legacy()
        self.con.executescript(SCHEMA)
        self.con.commit()

    def _migrate_legacy(self) -> None:
        """Banco da versao anterior (matches sem article_id): renomeia as tabelas
        antigas para legacy_* e deixa o schema novo ser criado do zero."""
        cols = [r[1] for r in self.con.execute("PRAGMA table_info(matches)")]
        if cols and "article_id" not in cols:
            log("  Banco no formato antigo -- tabelas renomeadas para legacy_*")
            existing = {r[0] for r in self.con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for t in ("runs", "processed_files", "matches"):
                if t in existing:
                    self.con.execute(f"DROP TABLE IF EXISTS legacy_{t}")
                    self.con.execute(f"ALTER TABLE {t} RENAME TO legacy_{t}")
            self.con.commit()

    def close(self) -> None:
        self.con.close()

    # -- state -----------------------------------------------------------
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: Optional[str]) -> None:
        self.con.execute(
            "INSERT INTO state(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.con.commit()

    # -- files -----------------------------------------------------------
    def file_processed(self, name: str) -> bool:
        return self.con.execute(
            "SELECT 1 FROM processed_files WHERE file_name=?", (name,)).fetchone() is not None

    def file_size(self, name: str) -> Optional[int]:
        row = self.con.execute(
            "SELECT size FROM processed_files WHERE file_name=?", (name,)).fetchone()
        return row["size"] if row else None

    def file_articles(self, name: str) -> int:
        row = self.con.execute(
            "SELECT articles FROM processed_files WHERE file_name=?", (name,)).fetchone()
        return int(row["articles"]) if row else 0

    def mark_file(self, name: str, edition_date: date, articles: int, matched: int,
                  size: int = 0) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO processed_files VALUES (?,?,?,?,?,?)",
            (name, edition_date.isoformat(), now_utc().isoformat(), articles, matched, size))
        self.con.commit()

    # -- matches ---------------------------------------------------------
    def add_match(self, m: "Match", emailed_ts: Optional[str] = None) -> bool:
        """Insere se ainda nao existe. Retorna True se e novo."""
        p = m.pub
        cur = self.con.execute(
            """INSERT OR IGNORE INTO matches (article_id, file_name, edition_date,
               filter_names, pub_name, art_type, art_category, edition_number,
               number_page, pdf_page, identifica, ementa, texto, texto_html,
               assina, cargo, kind, created_ts, emailed_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (p.article_id, p.source_file, p.edition_date.isoformat(),
             ", ".join(m.filtros), p.pub_name, p.art_type, p.art_category,
             p.edition_number, p.number_page, p.pdf_page, p.identifica, p.ementa,
             p.texto, p.texto_html, p.assina, p.cargo, p.kind,
             now_utc().isoformat(), emailed_ts))
        self.con.commit()
        return cur.rowcount == 1

    def pending_matches(self) -> List[sqlite3.Row]:
        return self.con.execute(
            "SELECT * FROM matches WHERE emailed_ts IS NULL "
            "ORDER BY edition_date, pub_name, CAST(number_page AS INTEGER)").fetchall()

    def mark_emailed(self, article_ids: Iterable[str]) -> None:
        ts = now_utc().isoformat()
        self.con.executemany(
            "UPDATE matches SET emailed_ts=? WHERE article_id=?",
            [(ts, a) for a in article_ids])
        self.con.commit()

    def log_run(self, run_date: date, status: str, files: int, new_matches: int,
                emails: int, notes: str = "") -> None:
        self.con.execute(
            "INSERT INTO runs (run_ts, run_date, status, files_processed, new_matches, "
            "emails_sent, notes) VALUES (?,?,?,?,?,?,?)",
            (now_utc().isoformat(), run_date.isoformat(), status, files, new_matches,
             emails, notes))
        self.con.commit()


# ============================================================================
# CLIENTE INLABS
# ============================================================================

class InlabsUnavailable(Exception):
    """Falha transitoria de rede/servidor. Nao e erro do sistema."""


class InlabsAuthError(Exception):
    """O servidor respondeu, mas nao devolveu o cookie de sessao."""


class InlabsClient:
    LOGIN_URL = f"{INLABS_BASE}/logar.php"
    INDEX_URL = f"{INLABS_BASE}/index.php"
    COOKIE = "inlabs_session_cookie"
    RETRY_WAITS = (0, 20, 45, 90)  # segundos antes de cada tentativa

    def __init__(self, email: str, password: str, timeout: int = 60):
        if not email or not password:
            raise InlabsAuthError("INLABS_EMAIL / INLABS_PASSWORD nao definidos.")
        self.email, self.password, self.timeout = email, password, timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; dou-antt-clipping/5.0)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "origem": INLABS_ORIGEM,
        })

    # -- transporte com retentativas --------------------------------------
    def _request(self, method: str, url: str, **kw) -> requests.Response:
        last: Optional[Exception] = None
        for i, wait in enumerate(self.RETRY_WAITS, 1):
            if wait:
                log(f"    aguardando {wait}s antes da tentativa {i}/{len(self.RETRY_WAITS)}...")
                time.sleep(wait)
            try:
                r = self.session.request(method, url, timeout=self.timeout, **kw)
            except requests.RequestException as e:   # RemoteDisconnected, timeout, TLS...
                last = e
                log(f"    rede: {type(e).__name__}: {str(e)[:160]}")
                continue
            if r.status_code >= 500:
                last = InlabsUnavailable(f"HTTP {r.status_code} em {url}")
                log(f"    servidor: HTTP {r.status_code}")
                continue
            return r
        raise InlabsUnavailable(f"INLABS inacessivel apos {len(self.RETRY_WAITS)} tentativas: {last}")

    @staticmethod
    def _is_maintenance(r: requests.Response) -> bool:
        if "html" not in r.headers.get("content-type", ""):
            return False
        head = norm(r.text[:3000])
        return "manutencao" in head or "maintenance" in head

    # -- sessao -----------------------------------------------------------
    def login(self) -> None:
        log("  Login no INLABS...")
        self.session.cookies.clear()
        r = self._request(
            "POST", self.LOGIN_URL,
            data={"email": self.email, "password": self.password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if not self.session.cookies.get(self.COOKIE):
            if self._is_maintenance(r):
                raise InlabsUnavailable("INLABS em manutencao (pagina de aviso no login).")
            raise InlabsAuthError(
                f"Login respondeu HTTP {r.status_code} sem cookie {self.COOKIE}. "
                "Verifique INLABS_EMAIL / INLABS_PASSWORD nos GitHub Secrets.")
        log("  Login OK")

    def _get_authenticated(self, params: dict, **kw) -> requests.Response:
        r = self._request("GET", self.INDEX_URL, params=params, **kw)
        ct = r.headers.get("content-type", "")
        if "html" in ct and self._is_login_page(r.text[:4000]):
            log("    sessao expirada -- refazendo login")
            self.login()
            r = self._request("GET", self.INDEX_URL, params=params, **kw)
            if "html" in r.headers.get("content-type", "") and self._is_login_page(r.text[:4000]):
                raise InlabsAuthError("INLABS continua exigindo login apos novo login.")
        return r

    @staticmethod
    def _is_login_page(html: str) -> bool:
        return re.search(r"""name\s*=\s*["']password["']""", html, re.I) is not None

    # -- operacoes --------------------------------------------------------
    def list_files(self, d: date) -> List[str]:
        """Nomes dos arquivos (zip/pdf) publicados para a data."""
        r = self._get_authenticated({"p": d.isoformat()})
        if r.status_code != 200:
            raise InlabsUnavailable(f"Listagem de {d}: HTTP {r.status_code}")
        soup = BeautifulSoup(r.text, "html.parser")
        names: List[str] = []
        for a in soup.find_all("a", href=True):
            m = re.search(r"[?&]dl=([^&\"'\s]+)", a["href"])
            if m and m.group(1) not in names:
                names.append(m.group(1))
        return names

    def download(self, d: date, name: str) -> Optional[bytes]:
        r = self._get_authenticated({"p": d.isoformat(), "dl": name})
        if r.status_code == 404:
            return None
        ct = r.headers.get("content-type", "")
        if r.status_code != 200 or "html" in ct.lower():
            raise InlabsUnavailable(
                f"Download de {name}: HTTP {r.status_code} content-type={ct!r}")
        return r.content


# ============================================================================
# PARSER DO XML DO INLABS
# ============================================================================

@dataclass
class Publication:
    article_id: str
    source_file: str
    edition_date: date
    pub_name: str = ""          # DO1, DO1E ...
    art_type: str = ""
    art_category: str = ""      # hierarquia "Ministerio/.../Unidade"
    edition_number: str = ""
    number_page: str = ""
    pdf_page: str = ""          # URL da pagina no DOU
    identifica: str = ""        # "PORTARIA N 123, DE ..."
    ementa: str = ""
    texto: str = ""             # texto plano
    texto_html: str = ""        # HTML original (CDATA de <Texto>)
    assina: str = ""
    cargo: str = ""
    kind: str = "article"       # article | pdf_alert

    @property
    def is_fragment(self) -> bool:
        return not self.identifica.strip() or norm(self.art_type) in FRAGMENT_TYPES


@dataclass
class Match:
    pub: Publication
    filtros: List[str]


def _body_fields(article: ET.Element) -> Dict[str, str]:
    body = article.find("body")
    if body is None:
        body = next((c for c in article if c.tag.lower() == "body"), None)
    out: Dict[str, str] = {}
    if body is not None:
        for child in body:
            out[child.tag.lower()] = (child.text or "").strip()
    return out


def html_to_text(fragment: str) -> str:
    soup = BeautifulSoup(fragment or "", "html.parser")
    lines: List[str] = []
    blocks = soup.find_all(["p", "tr", "li", "h1", "h2", "h3", "h4"])
    if not blocks:
        return re.sub(r"[ \t]+", " ", soup.get_text("\n")).strip()
    for b in blocks:
        if b.name == "tr":
            cells = [c.get_text(" ", strip=True) for c in b.find_all(["td", "th"])]
            t = " | ".join(x for x in cells if x)
        elif b.find_parent("tr"):
            continue
        else:
            t = b.get_text(" ", strip=True)
        if t:
            lines.append(re.sub(r"[ \t]+", " ", t))
    return "\n".join(lines)


def _class_text(fragment: str, cls: str) -> str:
    soup = BeautifulSoup(fragment or "", "html.parser")
    return "; ".join(p.get_text(" ", strip=True) for p in soup.find_all("p", class_=cls))


_XML_ILLEGAL = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def parse_articles(xml_bytes: bytes, source_file: str, edition_date: date) -> List[Publication]:
    # bytes de controle ilegais em XML 1.0 aparecem ocasionalmente dentro do CDATA
    root = ET.fromstring(_XML_ILLEGAL.sub(b"", xml_bytes))   # ParseError sobe: erro real
    nodes = [root] if root.tag.lower() == "article" else [
        n for n in root.iter() if n.tag.lower() == "article"]
    pubs: List[Publication] = []
    for a in nodes:
        g = a.attrib.get
        body = _body_fields(a)
        texto_html = body.get("texto", "")
        art_id = g("id") or hashlib.sha1(
            f"{source_file}|{a.attrib.get('name','')}|{body.get('identifica','')}".encode()
        ).hexdigest()[:16]
        pubs.append(Publication(
            article_id=str(art_id),
            source_file=source_file,
            edition_date=edition_date,
            pub_name=g("pubName", ""),
            art_type=g("artType", ""),
            art_category=g("artCategory", ""),
            edition_number=g("editionNumber", ""),
            number_page=g("numberPage", ""),
            pdf_page=g("pdfPage", ""),
            identifica=body.get("identifica", ""),
            ementa=body.get("ementa", ""),
            texto=html_to_text(texto_html),
            texto_html=texto_html,
            assina=_class_text(texto_html, "assina"),
            cargo=_class_text(texto_html, "cargo"),
        ))
    return pubs


def parse_zip(zip_bytes: bytes, source_file: str, edition_date: date) -> List[Publication]:
    pubs: List[Publication] = []
    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        for n in names:
            pubs.extend(parse_articles(zf.read(n), source_file, edition_date))
    log(f"    {source_file}: {len(pubs)} article(s) em {len(names)} XML(s)")
    return pubs


# ============================================================================
# FILTROS
# ============================================================================

def apply_filters(pubs: List[Publication], filtros: List[Filtro]) -> List[Match]:
    out: List[Match] = []
    for p in pubs:
        if p.is_fragment:
            continue
        hits = [f.nome for f in filtros if f.aceita(p.art_category, p.art_type)]
        if hits:
            out.append(Match(p, hits))
    return out


# ============================================================================
# PDF (edicoes publicadas so em PDF): apenas alerta
# ============================================================================

def norm_same_length(text: str) -> str:
    """Como norm(), mas 1 char -> 1 char, para que os offsets sirvam no texto original."""
    out = []
    for ch in text:
        if ch.isspace():
            out.append(" ")
            continue
        dec = unicodedata.normalize("NFKD", ch)
        base = next((c for c in dec if not unicodedata.combining(c)), ch)
        out.append(base.lower() if base.isascii() else "?")
    return "".join(out)


def find_orgao_mentions(text: str, filtros: List[Filtro]) -> Tuple[List[str], List[str]]:
    """(orgaos encontrados, trechos do texto original ao redor das mencoes)."""
    ntext = norm_same_length(text)
    orgaos: List[str] = []
    snippets: List[str] = []
    for f in filtros:
        if f.orgao in orgaos:
            continue
        pat = r"\s+".join(re.escape(w) for w in norm(f.orgao).split())
        hits = list(re.finditer(pat, ntext))
        if not hits:
            continue
        orgaos.append(f.orgao)
        for m in hits[:3]:
            lo, hi = max(0, m.start() - 250), min(len(text), m.end() + 400)
            snippets.append("..." + re.sub(r"\s+", " ", text[lo:hi]) + "...")
    return orgaos, snippets


def pdf_alert(pdf_bytes: bytes, name: str, d: date, filtros: List[Filtro]) -> Optional[Publication]:
    from pypdf import PdfReader  # import tardio: so quando houver PDF
    text = "\n".join((pg.extract_text() or "") for pg in PdfReader(BytesIO(pdf_bytes)).pages)
    orgaos_hit, snippets = find_orgao_mentions(text, filtros)
    if not orgaos_hit:
        return None
    body = ("Esta edicao foi publicada apenas em PDF (sem XML), e o texto abaixo e um "
            "trecho bruto extraido automaticamente. Confira a edicao no DOU.\n\n"
            + "\n\n".join(snippets[:6]))
    return Publication(
        article_id=f"pdf:{name}", source_file=name, edition_date=d,
        pub_name="PDF", art_type="Edicao em PDF",
        art_category=" / ".join(orgaos_hit),
        pdf_page=f"https://www.in.gov.br/leiturajornal?data={d.strftime('%d-%m-%Y')}&secao=do1",
        identifica=f"EDICAO EM PDF ({name}) MENCIONA: {', '.join(orgaos_hit)} -- CONFERIR MANUALMENTE",
        texto=body, texto_html="", kind="pdf_alert",
    )


# ============================================================================
# E-MAIL
# ============================================================================

_KEEP_TAGS = {"p", "br", "b", "strong", "i", "em", "u", "sup", "sub", "span", "div",
              "table", "thead", "tbody", "tr", "td", "th", "ul", "ol", "li"}
_CLASS_STYLE = {
    "identifica": "font-weight:bold;",
    "ementa": "font-style:italic;",
    "assina": "font-weight:bold;margin-top:12px;",
    "cargo": "font-style:italic;",
}


def sanitize_html(fragment: str) -> str:
    soup = BeautifulSoup(fragment or "", "html.parser")
    for t in soup.find_all(True):
        if t.name not in _KEEP_TAGS:
            t.unwrap()
            continue
        cls = " ".join(t.get("class", []))
        style = "".join(v for k, v in _CLASS_STYLE.items() if k in cls)
        t.attrs = {}
        if t.name in ("td", "th"):
            style += "border:1px solid #ccc;padding:3px 6px;vertical-align:top;"
        if t.name == "table":
            style += "border-collapse:collapse;margin:8px 0;font-size:12px;"
        if t.name == "p":
            style += "margin:0 0 8px 0;"
        if style:
            t["style"] = style
    return str(soup)


def _secao_label(pub_name: str) -> str:
    m = re.match(r"DO(\d)([A-Z]?)", pub_name or "")
    if not m:
        return pub_name or ""
    return f"Secao {m.group(1)}" + (" - Extra" if m.group(2) else "")


def render_matches_html(rows: List, prefix: str) -> str:
    """rows: sqlite3.Row ou dict com as colunas de `matches`."""
    cards = []
    for r in rows:
        try:
            d = date.fromisoformat(r["edition_date"]).strftime("%d/%m/%Y")
        except ValueError:
            d = r["edition_date"]
        meta = [f"Publicado em {d}", _secao_label(r["pub_name"])]
        if r["edition_number"]:
            meta.append(f"Edicao {r['edition_number']}")
        if r["number_page"]:
            meta.append(f"Pagina {r['number_page']}")
        link = (f' &nbsp;|&nbsp; <a href="{html_lib.escape(r["pdf_page"])}">Ver no DOU</a>'
                if r["pdf_page"] else "")
        if r["kind"] == "pdf_alert":
            body = (f"<p style='font-weight:bold;'>{html_lib.escape(r['identifica'] or '')}</p>"
                    f"<p style='font-size:12px;color:#6c757d;'>Arquivo: {html_lib.escape(r['file_name'])}</p>"
                    "<p>" + html_lib.escape(r["texto"]).replace("\n", "<br>") + "</p>")
            border = "#e0a800"
        else:
            body = sanitize_html(r["texto_html"]) or (
                "<p>" + html_lib.escape(r["texto"]).replace("\n", "<br>") + "</p>")
            border = "#28a745"
        cards.append(f"""
<div style="margin:0 0 28px 0;padding:16px 20px;border:1px solid #dee2e6;border-left:5px solid {border};border-radius:6px;background:#fff;">
  <div style="font-size:11px;color:#6c757d;text-transform:uppercase;letter-spacing:.5px;">
    Filtro: {html_lib.escape(r['filter_names'])} &nbsp;|&nbsp; {html_lib.escape(r['art_type'] or '')}
  </div>
  <div style="font-size:12px;color:#495057;margin:6px 0 2px 0;">{" | ".join(html_lib.escape(x) for x in meta)}{link}</div>
  <div style="font-size:12px;color:#495057;margin-bottom:12px;">{html_lib.escape(r['art_category'] or '')}</div>
  <div style="font-family:Georgia,serif;font-size:14px;line-height:1.6;color:#212529;">{body}</div>
</div>""")
    return f"""<html><body style="font-family:Arial,sans-serif;background:#f8f9fa;margin:0;padding:24px;">
<div style="max-width:880px;margin:0 auto;">
<h2 style="color:#28a745;font-size:20px;margin:0 0 4px 0;">{html_lib.escape(prefix)} {len(rows)} publicacao(oes)</h2>
<p style="color:#495057;margin:0 0 20px 0;">Clipping automatico via INLABS.</p>
{''.join(cards)}
</div></body></html>"""


def render_simple_html(title: str, lines: List[str], color: str = "#6c757d") -> str:
    items = "".join(f"<p style='margin:0 0 8px 0;'>{html_lib.escape(l)}</p>" for l in lines)
    return (f"<html><body style='font-family:Arial,sans-serif;padding:24px;color:#495057;'>"
            f"<h2 style='color:{color};font-size:18px;'>{html_lib.escape(title)}</h2>{items}"
            f"<p style='font-size:11px;color:#adb5bd;margin-top:20px;'>Clipping DOU - ANTT/SUFER via INLABS</p>"
            f"</body></html>")


class Mailer:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def send(self, subject: str, html: str) -> None:
        c = self.cfg
        if not c.smtp_user or not c.smtp_pass:
            raise RuntimeError("SMTP_USER / SMTP_PASS nao definidos.")
        msg = MIMEMultipart("alternative")
        msg["Subject"], msg["From"], msg["To"] = subject, c.email_from, ", ".join(c.email_to)
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP(c.smtp_host, c.smtp_port, timeout=60) as srv:
            srv.starttls()
            srv.login(c.smtp_user, c.smtp_pass)
            srv.sendmail(c.email_from, c.email_to, msg.as_string())
        log(f"  E-mail enviado: {subject}")


# ============================================================================
# EXECUCAO
# ============================================================================

def zip_edition_key(name: str) -> Optional[Tuple[str, str]]:
    """'2026-09-16-DO1E.zip' -> ('2026-09-16', 'DO1'); None se nao for zip de edicao."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})-(DO\d)[A-Z]?\.zip$", name, re.I)
    return (m.group(1), m.group(2).upper()) if m else None


def pdf_edition_key(name: str) -> Optional[Tuple[str, str]]:
    """'2026_08_01_ASSINADO_do1_extra_c.pdf' -> ('2026-08-01', 'DO1')."""
    m = re.match(r"(\d{4})_(\d{2})_(\d{2})_.*?do(\d).*\.pdf$", name, re.I)
    return (f"{m.group(1)}-{m.group(2)}-{m.group(3)}", f"DO{m.group(4)}") if m else None


@dataclass
class RunStats:
    files: int = 0
    new_matches: int = 0
    emails: int = 0
    notes: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def is_extra_zip(name: str) -> bool:
    return re.fullmatch(r"\d{4}-\d{2}-\d{2}-DO\d[A-Z]\.zip", name, re.I) is not None


def select_pdfs(names: List[str], d: date, secao: str, zip_has_content) -> List[str]:
    """PDFs a processar: apenas quando a edicao correspondente nao tem ZIP com
    conteudo. Extras de fim de semana costumam sair so em PDF -- as vezes ao lado
    de um DO1E.zip vazio (0 articles). A edicao base sem ZIP so conta a partir do
    dia seguinte (o XML pode aparecer horas depois do PDF)."""
    zips = [n for n in names if zip_edition_key(n) == (d.isoformat(), secao)]
    pdfs = [n for n in names if pdf_edition_key(n) == (d.isoformat(), secao)]
    base_ok = any(not is_extra_zip(n) and zip_has_content(n) for n in zips)
    extra_ok = any(is_extra_zip(n) and zip_has_content(n) for n in zips)
    out = []
    for n in pdfs:
        if "extra" in n.lower():
            if not extra_ok:
                out.append(n)
        elif not base_ok and d < today_brt():
            out.append(n)
    return out


def process_file(client: InlabsClient, db: Storage, filtros: List[Filtro], d: date,
                 name: str, stats: RunStats, emailed_ts: Optional[str],
                 data: Optional[bytes] = None) -> None:
    if data is None:
        data = client.download(d, name)
    if data is None:
        log(f"    {name}: 404 (listado mas indisponivel) -- fica para a proxima")
        return
    matches: List[Match] = []
    n_articles = 0
    if name.lower().endswith(".zip"):
        pubs = parse_zip(data, name, d)
        n_articles = len(pubs)
        matches = apply_filters(pubs, filtros)
    else:
        alert = pdf_alert(data, name, d, filtros)
        log(f"    {name}: PDF ({len(data):,} bytes) -- "
            f"{'menciona orgao(s) do filtro' if alert else 'sem mencao aos orgaos'}")
        if alert:
            matches = [Match(alert, [f.nome for f in filtros])]
    new = sum(1 for m in matches if db.add_match(m, emailed_ts))
    db.mark_file(name, d, n_articles, len(matches), len(data))
    stats.files += 1
    stats.new_matches += new
    log(f"    {name}: {len(matches)} match(es), {new} novo(s)")


def process_date(client: InlabsClient, db: Storage, cfg: Config, d: date,
                 stats: RunStats, force: bool = False,
                 emailed_ts: Optional[str] = None) -> None:
    names = client.list_files(d)
    log(f"  {d.isoformat()}: {len(names)} arquivo(s) no INLABS")
    for secao in cfg.secoes:
        filtros = [f for f in cfg.filtros if f.secao == secao]
        zips = [n for n in names if zip_edition_key(n) == (d.isoformat(), secao)]
        # 1) ZIPs primeiro; so depois decide sobre PDFs, com base no que os ZIPs continham
        for name in zips:
            data: Optional[bytes] = None
            if db.file_processed(name) and not force:
                # O DO1E.zip e acumulativo: o INLABS o regenera a cada nova extra do dia.
                # Baixa de novo e reprocessa so se o tamanho mudou (dedup por article_id).
                if not is_extra_zip(name):
                    continue
                data = client.download(d, name)
                if data is None or len(data) == db.file_size(name):
                    continue
                log(f"    {name}: tamanho mudou ({db.file_size(name)} -> {len(data)}) -- reprocessando")
            else:
                log(f"    baixando {name}...")
            _process_safely(client, db, filtros, d, name, stats, emailed_ts, data)
        # 2) PDFs apenas para edicoes sem ZIP com conteudo (fins de semana)
        for name in select_pdfs(names, d, secao, lambda n: db.file_articles(n) > 0):
            if db.file_processed(name) and not force:
                continue
            log(f"    baixando {name}...")
            _process_safely(client, db, filtros, d, name, stats, emailed_ts, None)


def _process_safely(client, db, filtros, d, name, stats, emailed_ts, data) -> None:
    try:
        process_file(client, db, filtros, d, name, stats, emailed_ts, data)
    except InlabsUnavailable:
        raise
    except Exception as e:   # ZIP/XML/PDF invalido: registra e segue para o proximo
        msg = f"{name}: {type(e).__name__}: {str(e)[:200]}"
        stats.errors.append(msg)
        log(f"    ERRO {msg}")
        traceback.print_exc()


EMAIL_MAX_ROWS = 25          # publicacoes por e-mail
EMAIL_MAX_HTML = 150_000     # chars de texto_html por publicacao


def send_pending(db: Storage, mailer: Mailer, cfg: Config, stats: RunStats) -> int:
    rows = db.pending_matches()
    if not rows:
        return 0
    total = len(rows)
    for i in range(0, total, EMAIL_MAX_ROWS):
        chunk = [dict(r) for r in rows[i:i + EMAIL_MAX_ROWS]]
        for r in chunk:
            if r["texto_html"] and len(r["texto_html"]) > EMAIL_MAX_HTML:
                r["texto_html"] = (r["texto_html"][:EMAIL_MAX_HTML]
                                   + "<p><i>[texto truncado -- ver integra no DOU]</i></p>")
        parte = f" (parte {i // EMAIL_MAX_ROWS + 1}/{-(-total // EMAIL_MAX_ROWS)})" if total > EMAIL_MAX_ROWS else ""
        subject = (f"{cfg.subject_prefix} {today_brt().strftime('%d/%m/%Y')} - "
                   f"{total} publicacao(oes){parte}")
        mailer.send(subject, render_matches_html(chunk, cfg.subject_prefix))
        db.mark_emailed(r["article_id"] for r in chunk)
        db.set("last_email_date", today_brt().isoformat())
        stats.emails += 1
    return total


def maybe_daily_confirmation(db: Storage, mailer: Mailer, cfg: Config, stats: RunStats) -> None:
    """'Sem novidades' 1x por dia util, so depois de a edicao base do dia ter sido
    processada -- ou apos 12h BRT (dia sem edicao, ex. feriado)."""
    hoje = today_brt()
    if not cfg.confirmacao_diaria or hoje.weekday() >= 5:
        return
    if db.get("last_email_date") == hoje.isoformat():
        return
    edicao_hoje = [s for s in cfg.secoes if db.file_processed(f"{hoje.isoformat()}-{s}.zip")]
    agora = now_brt()
    if not edicao_hoje and agora.hour < 12:
        log("  Edicao de hoje ainda nao publicada -- confirmacao diaria fica para a proxima execucao")
        return
    files = db.con.execute(
        "SELECT file_name FROM processed_files ORDER BY processed_ts DESC LIMIT 6").fetchall()
    lines = [
        "Sistema operacional. Nenhuma publicacao nova para os filtros configurados.",
        (f"Edicao de hoje verificada: {', '.join(edicao_hoje)}." if edicao_hoje else
         f"Nenhuma edicao publicada hoje ate {agora:%H:%M} (BRT)."),
        "Ultimas edicoes processadas: " + (", ".join(r["file_name"] for r in files) or "nenhuma"),
        "Filtros: " + ", ".join(f.nome for f in cfg.filtros),
    ]
    mailer.send(f"{cfg.subject_prefix} {hoje.strftime('%d/%m/%Y')} - sem novidades",
                render_simple_html("Sem novidades no DOU de hoje", lines))
    db.set("last_email_date", hoje.isoformat())
    stats.emails += 1


def handle_unavailable(db: Storage, mailer: Optional[Mailer], cfg: Config, err: Exception,
                       stats: RunStats) -> None:
    last_ok = db.get("last_success_ts")
    hours = ((now_utc() - datetime.fromisoformat(last_ok)).total_seconds() / 3600
             if last_ok else None)
    log(f"\nINLABS indisponivel: {err}")
    log(f"Ultimo sucesso: {last_ok or 'nunca'}"
        + (f" ({hours:.1f}h atras)" if hours is not None else ""))
    stats.notes.append(str(err)[:200])
    if mailer is None or hours is None or hours < cfg.alerta_indisponibilidade_horas:
        return
    last_alert = db.get("unavailable_alert_ts")
    if last_alert and (now_utc() - datetime.fromisoformat(last_alert)) < timedelta(hours=24):
        return
    mailer.send(
        f"{cfg.subject_prefix} ALERTA - INLABS inacessivel ha {hours:.0f}h",
        render_simple_html("O clipping nao consegue consultar o DOU", [
            f"Ultima consulta bem-sucedida: {last_ok} (UTC), ha {hours:.0f} horas.",
            f"Erro mais recente: {err}",
            "As execucoes continuam a cada ~3h; este alerta se repete no maximo 1x por dia.",
            "Se persistir, verifique https://inlabs.in.gov.br manualmente.",
        ], color="#dc3545"))
    db.set("unavailable_alert_ts", now_utc().isoformat())
    stats.emails += 1


def cmd_run(cfg: Config, target: date, send_email: bool, force: bool) -> int:
    db = Storage(cfg.db_path)
    mailer = Mailer(cfg) if send_email else None
    stats = RunStats()
    dates = [target - timedelta(days=i) for i in range(cfg.lookback_days, -1, -1)]
    log("=" * 64)
    log(f"Clipping DOU -- alvo {target.isoformat()}  janela {dates[0]} .. {dates[-1]}")
    log(f"Filtros: {[f.nome for f in cfg.filtros]}   e-mail: {'sim' if send_email else 'nao'}")
    log("=" * 64)
    status = "ok"

    def fail(e: Exception) -> None:
        nonlocal status
        status = "error"
        stats.errors.append(f"{type(e).__name__}: {str(e)[:300]}")
        traceback.print_exc()

    # 1) consulta ao INLABS
    try:
        client = InlabsClient(cfg.inlabs_email, cfg.inlabs_password)
        client.login()
        for d in dates:
            process_date(client, db, cfg, d, stats, force=force)
        db.set("last_success_ts", now_utc().isoformat())
        if db.get("unavailable_alert_ts"):
            db.set("unavailable_alert_ts", None)
            stats.notes.append("INLABS voltou apos alerta")
    except InlabsUnavailable as e:
        status = "inlabs_unavailable"
        try:
            handle_unavailable(db, mailer, cfg, e, stats)
        except Exception as e2:   # SMTP do alerta falhou
            fail(e2)
    except Exception as e:        # credenciais, bug... -> erro explicito
        fail(e)

    # 2) e-mail: o que ja esta no banco vai, mesmo que o INLABS tenha caido no meio
    if mailer:
        try:
            sent = send_pending(db, mailer, cfg, stats)
            if sent:
                log(f"  {sent} publicacao(oes) enviada(s) por e-mail")
            if status == "ok" and not stats.errors:
                maybe_daily_confirmation(db, mailer, cfg, stats)
        except Exception as e:    # SMTP: achados continuam pendentes para a proxima
            fail(e)
    else:
        log(f"  {len(db.pending_matches())} match(es) pendente(s) de envio (--no-email)")

    if stats.errors and status == "ok":
        status = "error"
    db.log_run(target, status, stats.files, stats.new_matches, stats.emails,
               "; ".join(stats.notes + stats.errors))
    db.close()
    log(f"\nStatus: {status} | arquivos: {stats.files} | novos matches: {stats.new_matches} "
        f"| e-mails: {stats.emails}" + (f" | erros: {len(stats.errors)}" if stats.errors else ""))
    return 0 if status in ("ok", "inlabs_unavailable") else 1


def cmd_backfill(cfg: Config, start: date, end: date) -> int:
    """Popula o banco sem enviar e-mail; matches ficam marcados como 'backfill'."""
    db = Storage(cfg.db_path)
    stats = RunStats()
    status = "ok"
    try:
        client = InlabsClient(cfg.inlabs_email, cfg.inlabs_password)
        client.login()
        d = start
        while d <= end:
            process_date(client, db, cfg, d, stats, emailed_ts="backfill")
            d += timedelta(days=1)
            time.sleep(1)
    except InlabsUnavailable as e:
        status = "inlabs_unavailable"
        log(f"\nINLABS indisponivel: {e} -- backfill interrompido em {d}")
    except Exception as e:
        status = "error"
        stats.errors.append(f"{type(e).__name__}: {str(e)[:300]}")
        traceback.print_exc()
    finally:
        if stats.errors and status == "ok":
            status = "error"
        db.log_run(start, status, stats.files, stats.new_matches, 0,
                   f"backfill {start}..{end}; " + "; ".join(stats.notes + stats.errors))
        db.close()
    log(f"\nBackfill: {status} | arquivos: {stats.files} | matches: {stats.new_matches}")
    return 0 if status == "ok" else 1


def cmd_inspect(cfg: Config, d: date, grep: str, days: int = 1) -> int:
    """Diagnostico: artCategory/artType reais e o que cada filtro pegaria, em
    `days` dias terminando em `d`. Mostra tambem as edicoes (editionNumber)
    contidas em cada ZIP, para conferir se um DO1E.zip cobre todas as extras."""
    client = InlabsClient(cfg.inlabs_email, cfg.inlabs_password)
    client.login()
    pubs: List[Publication] = []
    for i in range(days - 1, -1, -1):
        day = d - timedelta(days=i)
        names = client.list_files(day)
        zips = [n for n in names if zip_edition_key(n) and zip_edition_key(n)[1] in cfg.secoes]
        others = [n for n in names if n not in zips]
        log(f"\n{day}: {len(names)} arquivo(s) | zips das secoes monitoradas: {zips}")
        if others:
            log(f"    outros: {others}")
        for n in zips:
            data = client.download(day, n)
            if not data:
                continue
            day_pubs = parse_zip(data, n, day)
            editions: Dict[Tuple[str, str], int] = {}
            for p in day_pubs:
                k = (p.pub_name, p.edition_number)
                editions[k] = editions.get(k, 0) + 1
            log(f"    {n}: edicoes (pubName, editionNumber): "
                + ", ".join(f"{pn}/{en}={c}" for (pn, en), c in sorted(editions.items())))
            pubs.extend(day_pubs)
    key = norm(grep)
    sel = [p for p in pubs if key in norm(p.art_category)]
    log(f"\n{len(pubs)} article(s) no total; {len(sel)} com '{grep}' em artCategory\n")
    combos: Dict[Tuple[str, str], int] = {}
    for p in sel:
        combos[(p.art_category, p.art_type)] = combos.get((p.art_category, p.art_type), 0) + 1
    for (cat, typ), n in sorted(combos.items()):
        log(f"  {n:3d}  [{typ}]  {cat}")
    log("\nO que cada filtro capturaria (fragmentos sem Identifica excluidos):")
    for f in cfg.filtros:
        hits = [p for p in pubs if not p.is_fragment and f.aceita(p.art_category, p.art_type)]
        log(f"\n  {f.nome}: {len(hits)}")
        for p in hits:
            log(f"     - {p.edition_date} {p.pub_name} [{p.art_type}] {p.identifica[:90]}  (p.{p.number_page})")
    return 0


def cmd_test_email(cfg: Config) -> int:
    Mailer(cfg).send(f"{cfg.subject_prefix} teste de envio",
                     render_simple_html("Teste de e-mail", [
                         f"Enviado em {now_brt():%d/%m/%Y %H:%M} (BRT).",
                         "Se voce recebeu esta mensagem, SMTP_USER/SMTP_PASS estao corretos."]))
    return 0


def parse_date(s: Optional[str]) -> date:
    """YYYY-MM-DD; vazio = hoje (BRT). Entrada invalida = mensagem clara, exit 2."""
    if not s or not s.strip():
        return today_brt()
    try:
        return date.fromisoformat(s.strip())
    except ValueError:
        raise SystemExit(f"Data invalida: {s!r} -- use o formato YYYY-MM-DD (ex.: 2026-09-16)")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Clipping DOU -- ANTT/SUFER via INLABS")
    ap.add_argument("--config", default="config.yml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="execucao normal (janela de lookback + e-mail)")
    p.add_argument("--date", help="data alvo YYYY-MM-DD (padrao: hoje BRT)")
    p.add_argument("--no-email", action="store_true")
    p.add_argument("--force", action="store_true", help="reprocessa arquivos ja vistos")

    p = sub.add_parser("backfill", help="popula o banco sem e-mail")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)

    p = sub.add_parser("inspect", help="lista artCategory/artType reais de uma data")
    p.add_argument("--date", help="YYYY-MM-DD (padrao: hoje BRT)")
    p.add_argument("--days", type=int, default=1, help="quantos dias ate --date (padrao 1)")
    p.add_argument("--grep", default="Transportes", help="substring de artCategory")

    sub.add_parser("test-email", help="envia um e-mail de teste")

    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    if a.cmd == "run":
        return cmd_run(cfg, parse_date(a.date), not a.no_email, a.force)
    if a.cmd == "backfill":
        return cmd_backfill(cfg, parse_date(a.start), parse_date(a.end))
    if a.cmd == "inspect":
        return cmd_inspect(cfg, parse_date(a.date), a.grep, max(1, a.days))
    if a.cmd == "test-email":
        return cmd_test_email(cfg)
    return 2


if __name__ == "__main__":
    sys.exit(main())
