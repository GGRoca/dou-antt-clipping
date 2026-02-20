#!/usr/bin/env python3
"""
Clipping DOU - ANTT via INLABS  v4.0
Mudancas nesta versao:
  - match_in="orgao": retorna TUDO do orgao sem checar keywords no corpo
  - match_in="titulo": keyword verificada APENAS no titulo/identificador
  - Email mostra texto COMPLETO da publicacao formatado como no DOU
  - Publication extrai metadados: data, edicao, secao, pagina, assinatura
  - Login com debug detalhado e deteccao de manutencao
"""
import argparse
import os
import re
import sqlite3
import smtplib
import sys
import time
import unicodedata
import zipfile
from io import BytesIO
from datetime import date, datetime, timedelta
from typing import List, Tuple, Optional
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from xml.etree import ElementTree as ET

import requests
import yaml
from bs4 import BeautifulSoup


class InlabsMaintenanceError(Exception):
    pass


try:
    import PyPDF2
    HAS_PYPDF2 = True
except ImportError:
    try:
        from pypdf import PdfReader as _R
        HAS_PYPDF2 = True
        PyPDF2 = type("_F", (), {"PdfReader": _R})()
    except ImportError:
        HAS_PYPDF2 = False


# ============================================================================
# CONFIGURACAO
# ============================================================================

@dataclass
class FilterConfig:
    nome: str
    secao: str
    orgao: str
    keywords: List[str]
    match_in: str = "titulo"
    # match_in:
    #   "orgao"  -> captura TUDO do orgao, sem verificar keywords no corpo
    #   "titulo" -> keyword deve aparecer no titulo/identificador do ato


@dataclass
class Config:
    inlabs_email: str
    inlabs_password: str
    filtros: List[FilterConfig]
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str
    email_from: str
    email_to: List[str]
    email_subject_prefix: str
    db_path: str
    lookback_days: int


def load_config(config_path: str) -> Config:
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    inlabs_email    = os.getenv("INLABS_EMAIL",    cfg["inlabs"].get("email", ""))
    inlabs_password = os.getenv("INLABS_PASSWORD", cfg["inlabs"].get("password", ""))
    smtp_user       = os.getenv("SMTP_USER",        cfg["mail"].get("smtp_user", ""))
    smtp_pass       = os.getenv("SMTP_PASS",        cfg["mail"].get("smtp_pass", ""))

    filtros = []
    for f in cfg.get("filtros", []):
        filtros.append(FilterConfig(
            nome=f["nome"],
            secao=f["secao"],
            orgao=f["orgao"],
            keywords=f.get("keywords", []),
            match_in=f.get("match_in", "titulo"),
        ))

    return Config(
        inlabs_email=inlabs_email,
        inlabs_password=inlabs_password,
        filtros=filtros,
        smtp_host=cfg["mail"]["smtp_host"],
        smtp_port=cfg["mail"]["smtp_port"],
        smtp_user=smtp_user,
        smtp_pass=smtp_pass,
        email_from=cfg["mail"]["from_email"],
        email_to=cfg["mail"]["to_emails"],
        email_subject_prefix=cfg["mail"]["subject_prefix"],
        db_path=cfg["storage"]["db_path"],
        lookback_days=cfg.get("lookback_days", 2),
    )


# ============================================================================
# BANCO DE DADOS
# ============================================================================

def init_db(db_path: str):
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_ts          TEXT NOT NULL,
            run_date        TEXT NOT NULL,
            files_processed INTEGER NOT NULL,
            matches_found   INTEGER NOT NULL,
            email_sent      INTEGER NOT NULL,
            notes           TEXT
        );
        CREATE TABLE IF NOT EXISTS processed_files (
            file_name       TEXT PRIMARY KEY,
            processed_date  TEXT NOT NULL,
            processed_ts    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS matches (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date         TEXT NOT NULL,
            filter_name      TEXT NOT NULL,
            source_file      TEXT NOT NULL,
            keyword_hit      TEXT NOT NULL,
            publication_title TEXT,
            publication_date  TEXT,
            text_snippet     TEXT NOT NULL,
            full_text        TEXT,
            created_ts       TEXT NOT NULL
        );
    """)
    con.commit()
    con.close()


def was_file_processed(db_path: str, filename: str) -> bool:
    con = sqlite3.connect(db_path)
    row = con.execute(
        "SELECT 1 FROM processed_files WHERE file_name = ?", (filename,)
    ).fetchone()
    con.close()
    return row is not None


def mark_file_processed(db_path: str, filename: str, run_date: str):
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT OR IGNORE INTO processed_files (file_name, processed_date, processed_ts) VALUES (?,?,?)",
        (filename, run_date, datetime.utcnow().isoformat()),
    )
    con.commit()
    con.close()


def insert_matches(db_path: str, matches: list) -> int:
    if not matches:
        return 0
    con = sqlite3.connect(db_path)
    con.executemany(
        """INSERT INTO matches
           (run_date, filter_name, source_file, keyword_hit,
            publication_title, publication_date, text_snippet, full_text, created_ts)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        [(m[0], m[1], m[2], m[3], m[4], m[5], m[6], m[7],
          datetime.utcnow().isoformat()) for m in matches],
    )
    con.commit()
    con.close()
    return len(matches)


def log_run(db_path, run_date, files_processed, matches_found, email_sent, notes=""):
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO runs (run_ts,run_date,files_processed,matches_found,email_sent,notes) "
        "VALUES (?,?,?,?,?,?)",
        (datetime.utcnow().isoformat(), run_date, files_processed,
         matches_found, 1 if email_sent else 0, notes),
    )
    con.commit()
    con.close()


# ============================================================================
# INLABS CLIENT
# ============================================================================

class InlabsClient:
    BASE      = "https://inlabs.in.gov.br"
    LOGIN_URL = f"{BASE}/logar.php"
    INDEX_URL = f"{BASE}/index.php"

    LOGGED_IN_SIGNALS   = ["sair", "logout", "minha conta", "index.php?p=", "download"]
    LOGIN_PAGE_SIGNALS  = ["acessar", "senha", "login", "e-mail", "entrar", "logar.php"]
    MAINTENANCE_SIGNALS = ["manutencao programada", "maintenance",
                           "tente novamente mais tarde", "manutenção"]

    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self.session = self._new_session()

    @staticmethod
    def _new_session() -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "pt-BR,pt;q=0.9",
        })
        return s

    @staticmethod
    def _looks_logged_in(html: str) -> bool:
        low = html.lower()
        return any(s in low for s in InlabsClient.LOGGED_IN_SIGNALS)

    @staticmethod
    def _looks_like_login_page(html: str) -> bool:
        low = html.lower()
        return any(s in low for s in InlabsClient.LOGIN_PAGE_SIGNALS)

    @staticmethod
    def _is_maintenance(r: requests.Response) -> bool:
        if r.status_code in (502, 503, 504):
            body = unicodedata.normalize("NFD", r.text.lower()).encode("ascii", "ignore").decode()
            return any(s in body for s in InlabsClient.MAINTENANCE_SIGNALS)
        return False

    def _debug_response(self, label: str, r: requests.Response):
        print(f"\n  -- DEBUG [{label}] --", flush=True)
        print(f"  URL:    {r.url}", flush=True)
        print(f"  Status: {r.status_code}", flush=True)
        print(f"  Content-Type: {r.headers.get('content-type', 'N/A')}", flush=True)
        print(f"  Redirects: {[rr.url for rr in r.history]}", flush=True)
        body = r.text
        print(f"  Body (inicio): {body[:500].replace(chr(10), ' ').strip()}", flush=True)
        if len(body) > 500:
            print(f"  Body (fim):    {body[-300:].replace(chr(10), ' ').strip()}", flush=True)
        print("  --", flush=True)

    def _attempt_login(self, timeout: int = 30) -> bool:
        try:
            self.session.get(self.BASE, timeout=timeout)
        except Exception:
            pass

        csrf_token = None
        try:
            r_lp = self.session.get(self.LOGIN_URL, timeout=timeout)
            soup = BeautifulSoup(r_lp.text, "html.parser")
            for inp in soup.find_all("input", {"type": "hidden"}):
                name = inp.get("name", "").lower()
                if any(x in name for x in ("csrf", "token", "_token")):
                    csrf_token = inp.get("value", "")
                    break
        except Exception:
            pass

        payload = {"email": self.email, "password": self.password}
        if csrf_token:
            payload["_token"] = csrf_token

        r = self.session.post(self.LOGIN_URL, data=payload, timeout=timeout, allow_redirects=True)

        if self._is_maintenance(r):
            raise InlabsMaintenanceError(
                f"INLABS em manutencao programada (HTTP {r.status_code}). "
                "O sistema voltara automaticamente na proxima execucao."
            )

        self._debug_response("POST login", r)

        if self._looks_logged_in(r.text):
            print("    OK Login confirmado", flush=True)
            return True

        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location", "")
            r2 = self.session.get(
                loc if loc.startswith("http") else self.BASE + loc, timeout=timeout
            )
            self._debug_response("GET apos redirect", r2)
            if self._looks_logged_in(r2.text):
                print("    OK Login confirmado apos redirect", flush=True)
                return True

        print("    ERRO Nenhum sinal de sessao ativa encontrado", flush=True)
        return False

    def login(self, max_attempts: int = 3):
        print("  Iniciando login no INLABS...", flush=True)
        wait = 15
        for attempt in range(1, max_attempts + 1):
            print(f"  Tentativa {attempt}/{max_attempts}...", flush=True)
            try:
                r_probe = self.session.get(self.LOGIN_URL, timeout=30)
                if self._is_maintenance(r_probe):
                    raise InlabsMaintenanceError(
                        f"INLABS em manutencao (HTTP {r_probe.status_code})."
                    )
                if self._attempt_login(timeout=30 + (attempt - 1) * 30):
                    print("  Login bem-sucedido", flush=True)
                    return
                print(f"  Login falhou na tentativa {attempt}.", flush=True)
            except InlabsMaintenanceError:
                raise
            except Exception as e:
                print(f"  Excecao: {type(e).__name__}: {e}", flush=True)

            if attempt < max_attempts:
                print(f"  Aguardando {wait}s...", flush=True)
                time.sleep(wait)
                wait *= 2
                self.session = self._new_session()

        raise RuntimeError(
            "Falha no login do INLABS apos todas as tentativas. "
            "Verifique INLABS_EMAIL / INLABS_PASSWORD nos GitHub Secrets."
        )

    def _ensure_session(self):
        try:
            r = self.session.get(
                self.INDEX_URL, params={"p": date.today().isoformat()}, timeout=15
            )
            if self._looks_like_login_page(r.text) and not self._looks_logged_in(r.text):
                print("    Sessao expirada -- re-login...", flush=True)
                self.session = self._new_session()
                self.login()
        except InlabsMaintenanceError:
            raise
        except Exception as e:
            print(f"    Erro ao verificar sessao ({e}) -- re-login preventivo...", flush=True)
            self.session = self._new_session()
            self.login()

    def list_files(self, target_date: date, secao: str) -> Tuple[List[str], List[str]]:
        self._ensure_session()
        date_str = target_date.isoformat()
        sec_num  = secao.replace("DO", "")

        for attempt in range(1, 4):
            try:
                r = self.session.get(
                    self.INDEX_URL, params={"p": date_str},
                    timeout=30 + attempt * 15,
                )
                break
            except requests.exceptions.Timeout:
                if attempt == 3:
                    raise
                time.sleep(10 * attempt)
        else:
            return [], []

        text = r.text
        zip_pats = [
            rf"({re.escape(date_str)}-DO{sec_num}\.zip)",
            rf"({re.escape(date_str)}-DO{sec_num}E\.zip)",
            rf"({re.escape(date_str)}-DO{sec_num}[A-Z]\.zip)",
        ]
        date_us = date_str.replace("-", "_")
        pdf_pats = [
            rf"({date_us}_ASSINADO_do{sec_num}\.pdf)",
            rf"({date_us}_ASSINADO_do{sec_num}_extra_[A-Za-z]\.pdf)",
            rf"({date_us}_do{sec_num}\.pdf)",
        ]

        zips = list(set(m for p in zip_pats for m in re.findall(p, text)))
        pdfs = list(set(m for p in pdf_pats for m in re.findall(p, text)))
        print(f"    {date_str} {secao}: {len(zips)} ZIP(s), {len(pdfs)} PDF(s)", flush=True)
        return zips, pdfs

    def download_file(self, target_date: date, filename: str) -> bytes:
        self._ensure_session()
        date_str = target_date.isoformat()

        for attempt in range(1, 4):
            try:
                r = self.session.get(
                    self.INDEX_URL,
                    params={"p": date_str, "dl": filename},
                    timeout=60 + attempt * 30,
                    stream=True,
                )
                ct = r.headers.get("content-type", "")
                if r.status_code == 200 and "html" not in ct.lower():
                    data = r.content
                    print(f"    Download OK {filename}: {len(data):,} bytes", flush=True)
                    return data
                if "html" in ct.lower():
                    chunk = r.content[:200].lower()
                    if any(s in chunk for s in (b"acessar", b"login", b"senha")):
                        print("    Sessao expirada durante download -- re-login...", flush=True)
                        self.session = self._new_session()
                        self.login()
                        continue
                raise RuntimeError(f"HTTP {r.status_code} / {ct} ao baixar {filename}")
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt == 3:
                    raise
                time.sleep(15 * attempt)

        raise RuntimeError(f"Nao foi possivel baixar {filename} apos 3 tentativas")


# ============================================================================
# PUBLICATION -- estrutura de dados
# ============================================================================

@dataclass
class Publication:
    orgao: str
    titulo: str
    identifica: str
    texto: str
    pub_data: str   = ""
    edicao: str     = ""
    secao: str      = ""
    pagina: str     = ""
    assinatura: str = ""
    cargo: str      = ""


def _el_text(element: ET.Element, *tags: str) -> str:
    """Busca texto em sub-elementos, tentando multiplas variantes de nome de tag."""
    for tag in tags:
        for variant in (tag, tag.upper(), tag.lower(), tag.capitalize()):
            el = element.find(variant)
            if el is not None:
                return "".join(el.itertext()).strip()
    return ""


def _all_text(element: ET.Element) -> str:
    return " ".join(element.itertext()).strip()


def parse_xml_publications(xml_bytes: bytes) -> List[Publication]:
    """
    Extrai publicacoes estruturadas de um XML do INLABS.
    Tenta multiplas variantes de schema.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f"    XML malformado: {e}", flush=True)
        return []

    # Metadados globais do arquivo
    global_data  = _el_text(root, "DATA",  "data",  "dataPublicacao")
    global_edicao= _el_text(root, "EDICAO","edicao","numeroEdicao")
    global_secao = _el_text(root, "SECAO", "secao", "numSecao")

    def _find_pub_elements(node: ET.Element, depth: int = 0) -> List[ET.Element]:
        results = []
        for child in node:
            has_orgao  = (child.find("orgao")  is not None or
                          child.find("ORGAO")  is not None)
            has_titulo = (child.find("titulo") is not None or
                          child.find("TITULO") is not None)
            has_texto  = (child.find("texto")  is not None or
                          child.find("TEXTO")  is not None)

            if (has_orgao or has_titulo) and has_texto:
                results.append(child)
            elif depth < 6:
                results.extend(_find_pub_elements(child, depth + 1))
        return results

    pub_elements = _find_pub_elements(root)

    # Varredura plana como fallback
    if not pub_elements:
        for el in root.iter():
            has_orgao  = el.find("orgao")  is not None or el.find("ORGAO")  is not None
            has_titulo = el.find("titulo") is not None or el.find("TITULO") is not None
            has_texto  = el.find("texto")  is not None or el.find("TEXTO")  is not None
            if (has_orgao or has_titulo) and has_texto:
                pub_elements.append(el)

    print(f"    XML: {len(pub_elements)} publicacao(oes) encontrada(s)", flush=True)

    pubs: List[Publication] = []
    seen: set = set()

    for el in pub_elements:
        orgao      = _el_text(el, "orgao",      "ORGAO")
        titulo     = _el_text(el, "titulo",     "TITULO")
        identifica = _el_text(el, "identifica", "IDENTIFICA", "subtitulo", "SUBTITULO")
        texto      = _el_text(el, "texto",      "TEXTO")
        pub_data   = _el_text(el, "data",       "DATA",  "dataPublicacao")  or global_data
        edicao     = _el_text(el, "edicao",     "EDICAO","numeroEdicao")    or global_edicao
        secao_pub  = _el_text(el, "secao",      "SECAO", "numSecao")        or global_secao
        pagina     = _el_text(el, "pagina",     "PAGINA","numeroPagina")
        assinatura = _el_text(el, "assinatura", "ASSINATURA", "signatario", "SIGNATARIO")
        cargo      = _el_text(el, "cargo",      "CARGO")

        if not orgao and not titulo:
            continue

        key = (orgao[:80], titulo[:80])
        if key in seen:
            continue
        seen.add(key)

        pubs.append(Publication(
            orgao=orgao, titulo=titulo, identifica=identifica, texto=texto,
            pub_data=pub_data, edicao=edicao, secao=secao_pub,
            pagina=pagina, assinatura=assinatura, cargo=cargo,
        ))

    return pubs


def extract_publications_from_zip(zip_bytes: bytes) -> Tuple[List[Publication], str]:
    pubs: List[Publication] = []
    raw_texts: List[str] = []

    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        xml_names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        print(f"    ZIP contem {len(xml_names)} XML(s)", flush=True)
        for name in xml_names:
            data = zf.read(name)
            pubs.extend(parse_xml_publications(data))
            raw = data.decode("utf-8", errors="ignore")
            raw_texts.append(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip())

    return pubs, "\n\n".join(raw_texts)


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    if not HAS_PYPDF2:
        raise RuntimeError("PyPDF2 nao instalado")
    reader = PyPDF2.PdfReader(BytesIO(pdf_bytes))
    return "\n\n".join(
        p.extract_text().strip() for p in reader.pages if p.extract_text()
    )


# ============================================================================
# MATCHING
# ============================================================================

def _norm(text: str) -> str:
    return unicodedata.normalize("NFD", text.lower()).encode("ascii", "ignore").decode()


def orgao_matches(pub_orgao: str, filter_orgao: str) -> bool:
    if not pub_orgao:
        return False
    norm_pub = _norm(pub_orgao)
    if _norm(filter_orgao) in norm_pub:
        return True
    for part in filter_orgao.split("/"):
        part = part.strip()
        if part and _norm(part) in norm_pub:
            return True
    return False


def titulo_has_keyword(pub: Publication, keyword: str) -> bool:
    """Keyword deve estar APENAS no titulo ou identificador -- nao no corpo."""
    norm_kw = _norm(keyword)
    return (norm_kw in _norm(pub.titulo) or norm_kw in _norm(pub.identifica))


def format_publication(pub: Publication, pub_date_fallback: str = "") -> str:
    """
    Formata uma publicacao como aparece no DOU.
    Sem metadados do sistema (sem filtro, sem keyword, sem arquivo fonte).
    """
    lines = []
    lines.append("Diario Oficial da Uniao")

    meta_parts = []
    if pub.pub_data or pub_date_fallback:
        meta_parts.append(f"Publicado em: {pub.pub_data or pub_date_fallback}")
    if pub.edicao:
        meta_parts.append(f"Edicao: {pub.edicao}")
    if pub.secao:
        meta_parts.append(f"Secao: {pub.secao}")
    if pub.pagina:
        meta_parts.append(f"Pagina: {pub.pagina}")
    if meta_parts:
        lines.append(" | ".join(meta_parts))

    if pub.orgao:
        lines.append(f"Orgao: {pub.orgao}")

    lines.append("")

    if pub.titulo:
        lines.append(pub.titulo)
    if pub.identifica and pub.identifica != pub.titulo:
        lines.append(pub.identifica)

    lines.append("")

    if pub.texto:
        lines.append(pub.texto)

    if pub.assinatura or pub.cargo:
        lines.append("")
        if pub.assinatura:
            lines.append(pub.assinatura)
        if pub.cargo:
            lines.append(pub.cargo)

    return "\n".join(lines).strip()


def find_matches_in_publications(
    pubs: List[Publication],
    raw_fallback: str,
    filter_cfg: FilterConfig,
    pub_date_fallback: str = "",
) -> List[Tuple[str, str, str, str, str]]:
    """
    Retorna lista de (keyword_hit, titulo, pub_date, formatted_text, full_text).

    match_in="orgao":  tudo do orgao, sem verificar keyword no corpo
    match_in="titulo": keyword deve estar no titulo/identificador
    """
    results = []
    seen_titles: set = set()

    for pub in pubs:
        if not orgao_matches(pub.orgao, filter_cfg.orgao):
            continue

        if filter_cfg.match_in == "orgao":
            key = _norm(pub.titulo[:80]) or _norm(pub.texto[:40])
            if key in seen_titles:
                continue
            seen_titles.add(key)
            formatted = format_publication(pub, pub_date_fallback)
            results.append(("(orgao)", pub.titulo, pub.pub_data, formatted, formatted))

        elif filter_cfg.match_in == "titulo":
            for kw in filter_cfg.keywords:
                if titulo_has_keyword(pub, kw):
                    key = _norm(pub.titulo[:80])
                    if key in seen_titles:
                        break
                    seen_titles.add(key)
                    formatted = format_publication(pub, pub_date_fallback)
                    results.append((kw, pub.titulo, pub.pub_data, formatted, formatted))
                    break

    # Fallback texto bruto (apenas quando XML nao parseou)
    if not results and raw_fallback:
        orgao_parts = [p.strip() for p in filter_cfg.orgao.split("/") if p.strip()]
        orgao_found = any(_norm(p) in _norm(raw_fallback) for p in orgao_parts)

        if orgao_found and filter_cfg.match_in == "orgao":
            print(
                f"    AVISO: fallback texto bruto para '{filter_cfg.nome}' "
                f"(XML sem estrutura reconhecida)", flush=True
            )
            results.append((
                "(fallback)",
                "XML sem estrutura -- verificar manualmente no INLABS",
                pub_date_fallback,
                (f"O orgao '{filter_cfg.orgao}' foi encontrado no arquivo mas o XML "
                 f"nao pudo ser parseado em publicacoes individuais.\n"
                 f"Verifique o arquivo diretamente no portal INLABS."),
                raw_fallback[:1000],
            ))
        elif orgao_found and filter_cfg.match_in == "titulo":
            for kw in filter_cfg.keywords:
                if _norm(kw) in _norm(raw_fallback):
                    print(
                        f"    AVISO: fallback texto bruto para '{filter_cfg.nome}' "
                        f"(XML sem estrutura)", flush=True
                    )
                    results.append((
                        kw,
                        "XML sem estrutura -- verificar manualmente no INLABS",
                        pub_date_fallback,
                        (f"Keyword '{kw}' encontrada no arquivo mas o XML "
                         f"nao pude ser parseado. Verifique no portal INLABS."),
                        raw_fallback[:1000],
                    ))
                    break

    return results


# ============================================================================
# E-MAIL
# ============================================================================

def should_always_send_email() -> bool:
    now = datetime.utcnow()
    return now.weekday() < 5 and 9 <= now.hour < 11


def send_email(
    config: Config,
    run_date: str,
    matches: list,
    force_send: bool = False,
) -> bool:
    if not force_send and not matches:
        return False

    if matches:
        items_html = []
        for i, (_, filter_name, source_file, keyword_hit, pub_title, pub_date,
                formatted_text, full_text) in enumerate(matches, 1):

            text_to_show = formatted_text or full_text or ""
            # Escapa HTML e preserva quebras de linha
            text_html = (
                text_to_show
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\n", "<br>\n")
            )

            items_html.append(f"""
                <div style="margin-bottom:32px; padding:20px 24px;
                            border:1px solid #dee2e6; border-radius:6px;
                            background:#ffffff; border-left: 4px solid #28a745;">
                  <div style="font-size:11px; color:#6c757d; margin-bottom:12px;
                              text-transform:uppercase; letter-spacing:0.5px;">
                    Filtro: {filter_name}
                  </div>
                  <div style="font-family:Georgia, serif; font-size:14px;
                              line-height:1.7; color:#212529;">
                    {text_html}
                  </div>
                </div>
            """)

        html_body = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif; background:#f8f9fa;
             padding:24px; margin:0;">
  <div style="max-width:860px; margin:0 auto;">
    <h2 style="color:#28a745; margin-bottom:4px; font-size:20px;">
      {config.email_subject_prefix} {run_date}
    </h2>
    <p style="color:#495057; margin-top:4px; margin-bottom:24px;">
      <strong>{len(matches)}</strong> publicacao(oes) encontrada(s)
    </p>
    {''.join(items_html)}
    <p style="font-size:11px; color:#adb5bd; margin-top:24px; border-top:1px solid #dee2e6; padding-top:12px;">
      Clipping automatico via INLABS | DOU Secao 1
    </p>
  </div>
</body>
</html>"""
        subject = f"{config.email_subject_prefix} {run_date} — {len(matches)} publicacao(oes)"

    else:
        html_body = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif; padding:24px; color:#495057;">
  <h2 style="color:#6c757d;">{config.email_subject_prefix} {run_date}</h2>
  <p>Sistema operacional. Nenhuma publicacao encontrada hoje.</p>
  <p style="font-size:11px; color:#adb5bd;">
    E-mail de confirmacao diaria (seg-sex, aprox. 10h BRT).
  </p>
</body>
</html>"""
        subject = f"{config.email_subject_prefix} {run_date} — 0 publicacoes"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = config.email_from
    msg["To"]      = ", ".join(config.email_to)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=60) as srv:
            srv.starttls()
            srv.login(config.smtp_user, config.smtp_pass)
            srv.sendmail(config.email_from, config.email_to, msg.as_string())
        print(f"  E-mail enviado: {subject}", flush=True)
        return True
    except Exception as e:
        print(f"  ERRO ao enviar e-mail: {e}", flush=True)
        return False


# ============================================================================
# EXECUCAO PRINCIPAL
# ============================================================================

def run_for_date(
    config: Config,
    target_date: date,
    send_email_flag: bool = True,
    use_lookback: bool = True,
    client: Optional[InlabsClient] = None,
) -> int:
    init_db(config.db_path)

    if client is None:
        client = InlabsClient(config.inlabs_email, config.inlabs_password)
        client.login()

    dates_to_check = (
        [target_date - timedelta(days=i) for i in range(config.lookback_days, -1, -1)]
        if use_lookback
        else [target_date]
    )

    all_matches: list = []
    files_processed = 0

    for filter_cfg in config.filtros:
        print(
            f"\n  Filtro: {filter_cfg.nome} "
            f"(secao={filter_cfg.secao}, match_in={filter_cfg.match_in})",
            flush=True,
        )

        for check_date in dates_to_check:
            print(f"    Data: {check_date.isoformat()}", flush=True)

            try:
                zips, pdfs = client.list_files(check_date, filter_cfg.secao)
            except Exception as e:
                print(f"    ERRO ao listar: {e}", flush=True)
                continue

            candidates = zips if zips else pdfs
            new_files  = [f for f in candidates if not was_file_processed(config.db_path, f)]

            if not new_files:
                print("    Nenhum arquivo novo.", flush=True)
                continue

            for filename in new_files:
                print(f"    Baixando {filename}...", flush=True)
                try:
                    content = client.download_file(check_date, filename)
                except Exception as e:
                    print(f"    ERRO no download: {e}", flush=True)
                    continue

                pubs: List[Publication] = []
                raw_fallback = ""
                try:
                    if filename.endswith(".zip"):
                        pubs, raw_fallback = extract_publications_from_zip(content)
                        print(f"    Total de publicacoes no ZIP: {len(pubs)}", flush=True)
                    elif filename.endswith(".pdf"):
                        raw_fallback = extract_text_from_pdf(content)
                    else:
                        print(f"    Formato desconhecido: {filename}", flush=True)
                        continue
                except Exception as e:
                    print(f"    ERRO na extracao: {e}", flush=True)
                    continue

                hits = find_matches_in_publications(
                    pubs, raw_fallback, filter_cfg,
                    pub_date_fallback=check_date.strftime("%d/%m/%Y"),
                )
                print(
                    f"    '{filter_cfg.nome}': {len(hits)} publicacao(oes) encontrada(s)",
                    flush=True,
                )

                for kw, pub_title, pub_date, formatted, full_text in hits:
                    all_matches.append((
                        check_date.isoformat(),
                        filter_cfg.nome,
                        filename,
                        kw,
                        pub_title,
                        pub_date,
                        formatted,
                        full_text,
                    ))

                mark_file_processed(config.db_path, filename, check_date.isoformat())
                files_processed += 1

    matches_count = insert_matches(config.db_path, all_matches)

    force_send = should_always_send_email()
    email_sent = False
    if send_email_flag and (matches_count > 0 or force_send):
        email_sent = send_email(config, target_date.isoformat(), all_matches, force_send)

    log_run(
        config.db_path, target_date.isoformat(), files_processed, matches_count,
        email_sent,
        notes=f"{'Lookback' if use_lookback else 'Sem lookback'}, {files_processed} arquivo(s)",
    )

    return matches_count


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Clipping DOU -- ANTT via INLABS")
    parser.add_argument("--config", default="config.yml")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run")
    run_p.add_argument("--date")
    run_p.add_argument("--no-email", action="store_true")

    bf_p = sub.add_parser("backfill")
    bf_p.add_argument("--start", required=True)
    bf_p.add_argument("--end",   required=True)

    args   = parser.parse_args()
    config = load_config(args.config)

    if args.command == "run":
        target_date = date.fromisoformat(args.date) if args.date else date.today()
        print(f"\n{'='*60}", flush=True)
        print(f"Clipping DOU -- {target_date.isoformat()}", flush=True)
        print(f"Filtros: {[f.nome for f in config.filtros]}", flush=True)
        print(f"Janela:  D-{config.lookback_days} ... D+0", flush=True)
        print(f"{'='*60}\n", flush=True)
        try:
            matches = run_for_date(config, target_date, not args.no_email, use_lookback=True)
            print(f"\nConcluido: {matches} publicacao(oes)", flush=True)
        except InlabsMaintenanceError as e:
            print(f"\nINLABS em manutencao: {e}", flush=True)
            print("Encerrado sem erro -- retentado na proxima execucao.", flush=True)
            sys.exit(0)

    elif args.command == "backfill":
        start_date = date.fromisoformat(args.start)
        end_date   = date.fromisoformat(args.end)
        print(f"\nBackfill: {start_date} -> {end_date}", flush=True)
        print("(sem e-mail, sem lookback)\n", flush=True)
        try:
            client = InlabsClient(config.inlabs_email, config.inlabs_password)
            client.login()
        except InlabsMaintenanceError as e:
            print(f"\nINLABS em manutencao: {e}", flush=True)
            sys.exit(0)

        current = start_date
        total   = 0
        while current <= end_date:
            print(f"\n{'-'*50}", flush=True)
            print(f"Processando {current.isoformat()}...", flush=True)
            try:
                n = run_for_date(config, current, send_email_flag=False,
                                 use_lookback=False, client=client)
            except InlabsMaintenanceError as e:
                print(f"\nINLABS em manutencao: {e}", flush=True)
                sys.exit(0)
            total += n
            current += timedelta(days=1)
            time.sleep(2)

        print(f"\nBackfill concluido: {total} publicacao(oes)", flush=True)


if __name__ == "__main__":
    main()
