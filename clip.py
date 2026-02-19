#!/usr/bin/env python3
"""
Clipping DOU - ANTT via INLABS
Versão 3.0 — Correções:
  - Login robusto com debug completo (detecta mudanças no INLABS)
  - Parser XML estruturado (lê <orgao>, <titulo>, <texto> separados)
  - Matching por órgão + keywords mais preciso e sem falsos negativos
  - Re-login automático com backoff
  - Logs detalhados em cada etapa
"""
import argparse
import os
import re
import sqlite3
import smtplib
import sys
import time
import zipfile
from io import BytesIO
from datetime import date, datetime, timedelta
from typing import List, Tuple, Optional, Dict, Any
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from xml.etree import ElementTree as ET

import requests
import yaml
from bs4 import BeautifulSoup

try:
    import PyPDF2
    HAS_PYPDF2 = True
except ImportError:
    try:
        from pypdf import PdfReader as _PdfReader
        HAS_PYPDF2 = True
        PyPDF2 = type("_Fake", (), {"PdfReader": _PdfReader})()
    except ImportError:
        HAS_PYPDF2 = False


# ============================================================================
# CONFIGURAÇÃO
# ============================================================================

@dataclass
class FilterConfig:
    nome: str
    secao: str           # DO1, DO2, DO3
    orgao: str           # substring que deve aparecer na tag <orgao>
    keywords: List[str]  # qualquer uma dessas palavras no texto basta


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
            keywords=f["keywords"],
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
        "INSERT INTO runs (run_ts,run_date,files_processed,matches_found,email_sent,notes) VALUES (?,?,?,?,?,?)",
        (datetime.utcnow().isoformat(), run_date, files_processed,
         matches_found, 1 if email_sent else 0, notes),
    )
    con.commit()
    con.close()


# ============================================================================
# INLABS CLIENT
# ============================================================================

class InlabsClient:
    """
    Acesso ao portal INLABS com:
    - Debug detalhado do login (mostra o que o servidor retorna)
    - Retry + backoff
    - Re-login automático quando sessão expira
    """

    BASE = "https://inlabs.in.gov.br"
    LOGIN_URL = f"{BASE}/logar.php"
    INDEX_URL = f"{BASE}/index.php"

    # Indicadores de que a resposta é a página logada
    LOGGED_IN_SIGNALS = ["sair", "logout", "minha conta", "meu perfil",
                         "index.php?p=", "download", "arquivo"]
    # Indicadores de que caiu na página de login (sessão expirou)
    LOGIN_PAGE_SIGNALS = ["acessar", "senha", "login", "e-mail", "entrar",
                          "logar.php", "esqueci"]

    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self.session = self._new_session()
        self._logged_in = False

    # ── helpers ──────────────────────────────────────────────────────────────

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
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        return s

    @staticmethod
    def _looks_logged_in(html: str) -> bool:
        low = html.lower()
        return any(sig in low for sig in InlabsClient.LOGGED_IN_SIGNALS)

    @staticmethod
    def _looks_like_login_page(html: str) -> bool:
        low = html.lower()
        return any(sig in low for sig in InlabsClient.LOGIN_PAGE_SIGNALS)

    def _debug_response(self, label: str, r: requests.Response):
        """Imprime diagnóstico completo para ajudar a entender falhas."""
        print(f"\n  ── DEBUG [{label}] ──────────────────────────────────────────", flush=True)
        print(f"  URL:    {r.url}", flush=True)
        print(f"  Status: {r.status_code}", flush=True)
        print(f"  Content-Type: {r.headers.get('content-type', 'N/A')}", flush=True)
        print(f"  Redirect history: {[rr.url for rr in r.history]}", flush=True)
        # Mostra primeiras e últimas 600 chars do body
        body = r.text
        preview = body[:600].replace("\n", " ").strip()
        tail    = body[-300:].replace("\n", " ").strip() if len(body) > 600 else ""
        print(f"  Body (início): {preview}", flush=True)
        if tail:
            print(f"  Body (fim):    {tail}", flush=True)
        print(f"  ─────────────────────────────────────────────────────────────", flush=True)

    # ── login ─────────────────────────────────────────────────────────────────

    def _attempt_login(self, timeout: int = 30) -> bool:
        """
        Tenta login e retorna True se bem-sucedido.
        Estratégia:
          1. GET na raiz para pegar cookies/CSRF se houver
          2. POST no logar.php com credenciais
          3. Verifica resposta com múltiplos sinais
        """
        # Passo 1: visita a raiz para obter cookies de sessão iniciais
        try:
            r0 = self.session.get(self.BASE, timeout=timeout)
            print(f"    GET {self.BASE} → {r0.status_code}", flush=True)
        except Exception as e:
            print(f"    Aviso: falha ao acessar raiz ({e}), continua...", flush=True)

        # Passo 2: tenta extrair CSRF token da página de login (se existir)
        csrf_token = None
        try:
            r_login_page = self.session.get(self.LOGIN_URL, timeout=timeout)
            soup = BeautifulSoup(r_login_page.text, "html.parser")
            for inp in soup.find_all("input", {"type": "hidden"}):
                name = inp.get("name", "").lower()
                if "csrf" in name or "token" in name or "_token" in name:
                    csrf_token = inp.get("value", "")
                    print(f"    CSRF token encontrado: {inp.get('name')} = {csrf_token[:20]}...", flush=True)
                    break
        except Exception as e:
            print(f"    Aviso: não foi possível buscar CSRF ({e})", flush=True)

        # Passo 3: monta payload
        payload: Dict[str, str] = {
            "email":    self.email,
            "password": self.password,
            # alguns sistemas usam "senha" ou "pass"
        }
        if csrf_token:
            # Tenta adivinhar o nome do campo CSRF
            payload["_token"]      = csrf_token
            payload["csrf_token"]  = csrf_token
            payload["token"]       = csrf_token

        # Passo 4: POST
        r = self.session.post(
            self.LOGIN_URL,
            data=payload,
            timeout=timeout,
            allow_redirects=True,
        )
        self._debug_response("POST login", r)

        # Passo 5: avalia resposta
        if self._looks_logged_in(r.text):
            print("    ✅ Login confirmado (sinal de sessão ativa encontrado)", flush=True)
            self._logged_in = True
            return True

        # Tenta seguir redirect manual se houver
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location", "")
            print(f"    Redirect para: {loc}", flush=True)
            r2 = self.session.get(loc if loc.startswith("http") else self.BASE + loc,
                                  timeout=timeout)
            self._debug_response("GET após redirect", r2)
            if self._looks_logged_in(r2.text):
                print("    ✅ Login confirmado após redirect", flush=True)
                self._logged_in = True
                return True

        # Se chegou até aqui, login falhou
        print("    ❌ Nenhum sinal de sessão ativa encontrado na resposta", flush=True)
        return False

    def login(self, max_attempts: int = 3):
        """Login público com retry e backoff."""
        print("  🔐 Iniciando login no INLABS...", flush=True)

        wait = 15
        for attempt in range(1, max_attempts + 1):
            print(f"  Tentativa {attempt}/{max_attempts}...", flush=True)
            try:
                if self._attempt_login(timeout=30 + (attempt - 1) * 30):
                    print("  ✅ Login bem-sucedido", flush=True)
                    return
                else:
                    print(f"  ⚠️ Login falhou na tentativa {attempt}. "
                          f"Verifique credenciais e o debug acima.", flush=True)
            except Exception as e:
                print(f"  ⚠️ Exceção na tentativa {attempt}: {type(e).__name__}: {e}", flush=True)

            if attempt < max_attempts:
                print(f"  Aguardando {wait}s antes da próxima tentativa...", flush=True)
                time.sleep(wait)
                wait *= 2
                # recria sessão
                self.session = self._new_session()

        raise RuntimeError(
            "Falha no login do INLABS após todas as tentativas. "
            "Verifique INLABS_EMAIL / INLABS_PASSWORD nos GitHub Secrets "
            "e analise o debug acima para entender o que o servidor retornou."
        )

    # ── verificação de sessão ────────────────────────────────────────────────

    def _ensure_session(self):
        """Verifica sessão; faz re-login se necessário."""
        try:
            r = self.session.get(self.INDEX_URL, params={"p": date.today().isoformat()},
                                 timeout=15)
            if self._looks_like_login_page(r.text) and not self._looks_logged_in(r.text):
                print("    ⚠️ Sessão expirada — re-login...", flush=True)
                self.session = self._new_session()
                self._logged_in = False
                self.login()
        except Exception as e:
            print(f"    ⚠️ Erro ao verificar sessão ({e}) — re-login preventivo...", flush=True)
            self.session = self._new_session()
            self._logged_in = False
            self.login()

    # ── listagem de arquivos ──────────────────────────────────────────────────

    def list_files(self, target_date: date, secao: str) -> Tuple[List[str], List[str]]:
        """
        Lista ZIPs e PDFs disponíveis para a data/seção.
        Retorna (zips, pdfs).
        """
        self._ensure_session()
        date_str = target_date.isoformat()
        sec_num = secao.replace("DO", "")

        for attempt in range(1, 4):
            try:
                r = self.session.get(
                    self.INDEX_URL,
                    params={"p": date_str},
                    timeout=30 + attempt * 15,
                )
                break
            except requests.exceptions.Timeout:
                if attempt == 3:
                    raise
                print(f"    Timeout ao listar {date_str}, tentando novamente...", flush=True)
                time.sleep(10 * attempt)
        else:
            return [], []

        text = r.text

        # ZIPs principais e extras
        zip_pats = [
            rf"({re.escape(date_str)}-DO{sec_num}\.zip)",
            rf"({re.escape(date_str)}-DO{sec_num}E\.zip)",
            rf"({re.escape(date_str)}-DO{sec_num}[A-Z]\.zip)",
        ]
        # PDFs (formato antigo do INLABS)
        date_us = date_str.replace("-", "_")
        pdf_pats = [
            rf"({date_us}_ASSINADO_do{sec_num}\.pdf)",
            rf"({date_us}_ASSINADO_do{sec_num}_extra_[A-Za-z]\.pdf)",
            # variante sem 'ASSINADO'
            rf"({date_us}_do{sec_num}\.pdf)",
        ]

        zips, pdfs = [], []
        for pat in zip_pats:
            zips.extend(re.findall(pat, text))
        for pat in pdf_pats:
            pdfs.extend(re.findall(pat, text))

        zips = list(set(zips))
        pdfs = list(set(pdfs))

        print(f"    {date_str} {secao}: {len(zips)} ZIP(s), {len(pdfs)} PDF(s)", flush=True)
        return zips, pdfs

    # ── download ──────────────────────────────────────────────────────────────

    def download_file(self, target_date: date, filename: str) -> bytes:
        """Download com retry/re-login."""
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
                content_type = r.headers.get("content-type", "")

                if r.status_code == 200 and "html" not in content_type.lower():
                    data = r.content
                    print(f"    ✓ Download {filename}: {len(data):,} bytes", flush=True)
                    return data

                # Pode ser sessão expirada
                if "html" in content_type.lower():
                    chunk = r.content[:200].lower()
                    if b"acessar" in chunk or b"login" in chunk or b"senha" in chunk:
                        print(f"    ⚠️ Sessão expirada durante download — re-login...", flush=True)
                        self.session = self._new_session()
                        self._logged_in = False
                        self.login()
                        continue

                raise RuntimeError(
                    f"Resposta inesperada para {filename}: "
                    f"HTTP {r.status_code} / content-type: {content_type}"
                )

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt == 3:
                    raise
                print(f"    ⚠️ Erro de rede no download ({e}), tentativa {attempt}/3...", flush=True)
                time.sleep(15 * attempt)

        raise RuntimeError(f"Não foi possível baixar {filename} após 3 tentativas")


# ============================================================================
# PARSER XML (estruturado)
# ============================================================================

def _tag_text(element, *tags) -> str:
    """Lê texto de um sub-elemento (retorna '' se não encontrar)."""
    for tag in tags:
        el = element.find(tag)
        if el is not None and el.text:
            return el.text.strip()
    return ""


def _all_text(element) -> str:
    """Texto completo de um elemento (incluindo filhos)."""
    return " ".join((element.itertext() or [])).strip()


@dataclass
class Publication:
    """Representa uma publicação individual do DOU."""
    orgao: str
    titulo: str
    identifica: str  # subtítulo / identificador do ato
    texto: str       # corpo da publicação
    raw: str         # texto bruto para fallback


def parse_xml_publications(xml_bytes: bytes) -> List[Publication]:
    """
    Lê um XML do INLABS e extrai lista de publicações estruturadas.
    O INLABS usa diferentes schemas; tenta os mais comuns.
    """
    pubs = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f"    ⚠️ XML malformado: {e}. Usando extração de texto bruto.", flush=True)
        return []

    # Schema 1: <TEXTO_ORGANIZADO> ou <materia>
    # Procura qualquer tag que contenha <ORGAO> ou <orgao>
    for el in root.iter():
        tag = el.tag.lower()

        # Pula tags folha e de estrutura
        if tag in ("dou", "diario", "secao", "data", "numero", "pagina",
                   "assinatura", "cargo", "emissor"):
            continue

        # Heurística: elemento que tem tanto filho de orgão quanto de texto
        orgao_el  = el.find("orgao")   or el.find("ORGAO")
        titulo_el = el.find("titulo")  or el.find("TITULO")
        texto_el  = el.find("texto")   or el.find("TEXTO")
        ident_el  = (el.find("identifica") or el.find("IDENTIFICA")
                     or el.find("subtitulo") or el.find("SUBTITULO"))

        if orgao_el is None and titulo_el is None:
            continue

        orgao     = _all_text(orgao_el)  if orgao_el  is not None else ""
        titulo    = _all_text(titulo_el) if titulo_el is not None else ""
        identifica= _all_text(ident_el)  if ident_el  is not None else ""
        texto     = _all_text(texto_el)  if texto_el  is not None else ""
        raw       = _all_text(el)

        if not (orgao or titulo):
            continue

        pubs.append(Publication(
            orgao=orgao,
            titulo=titulo,
            identifica=identifica,
            texto=texto,
            raw=raw,
        ))

    # Deduplica (mesmo orgao + titulo)
    seen = set()
    unique = []
    for p in pubs:
        key = (p.orgao[:80], p.titulo[:80])
        if key not in seen:
            seen.add(key)
            unique.append(p)

    return unique


def extract_publications_from_zip(zip_bytes: bytes) -> Tuple[List[Publication], str]:
    """
    Extrai publicações de todos os XMLs dentro do ZIP.
    Retorna (lista de publicações estruturadas, texto bruto completo para fallback).
    """
    pubs: List[Publication] = []
    raw_texts: List[str] = []

    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        xml_names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        print(f"    ZIP contém {len(xml_names)} XML(s)", flush=True)

        for name in xml_names:
            xml_bytes_file = zf.read(name)
            file_pubs = parse_xml_publications(xml_bytes_file)
            pubs.extend(file_pubs)

            # Texto bruto como fallback
            raw = xml_bytes_file.decode("utf-8", errors="ignore")
            raw_clean = re.sub(r"<[^>]+>", " ", raw)
            raw_clean = re.sub(r"\s+", " ", raw_clean).strip()
            raw_texts.append(raw_clean)

    return pubs, "\n\n".join(raw_texts)


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extrai texto de PDF via PyPDF2/pypdf."""
    if not HAS_PYPDF2:
        raise RuntimeError("PyPDF2 não instalado")
    reader = PyPDF2.PdfReader(BytesIO(pdf_bytes))
    parts = []
    for page in reader.pages:
        t = page.extract_text() or ""
        if t.strip():
            parts.append(t.strip())
    return "\n\n".join(parts)


# ============================================================================
# MATCHING
# ============================================================================

def normalize(text: str) -> str:
    """Normaliza texto para comparação insensível a acentos/case."""
    import unicodedata
    return unicodedata.normalize("NFD", text.lower()).encode("ascii", "ignore").decode()


def orgao_matches(pub_orgao: str, filter_orgao: str) -> bool:
    """
    Verifica se o órgão da publicação corresponde ao filtro.
    Usa matching flexível:
    - substring normalizada
    - qualquer palavra-chave do filtro (splitada por '/')
    """
    if not pub_orgao:
        return False
    norm_pub = normalize(pub_orgao)
    # Testa o termo inteiro
    if normalize(filter_orgao) in norm_pub:
        return True
    # Testa cada parte separada por '/' (hierarquia do DOU)
    for part in filter_orgao.split("/"):
        part = part.strip()
        if part and normalize(part) in norm_pub:
            return True
    return False


def keyword_matches(pub: Publication, keyword: str) -> Tuple[bool, str]:
    """
    Retorna (hit, snippet) se a keyword aparece em qualquer campo da publicação.
    Snippet: 400 chars ao redor da primeira ocorrência.
    """
    norm_kw = normalize(keyword)
    # Campos onde buscar (em ordem de prioridade)
    fields = [
        ("titulo",     pub.titulo),
        ("identifica", pub.identifica),
        ("texto",      pub.texto),
        ("raw",        pub.raw),
    ]
    for field_name, field_val in fields:
        norm_val = normalize(field_val)
        idx = norm_val.find(norm_kw)
        if idx != -1:
            # Snippet do texto original (não normalizado)
            start = max(0, idx - 200)
            end   = min(len(field_val), idx + 200)
            snippet = field_val[start:end].strip()
            return True, snippet
    return False, ""


def find_matches_in_publications(
    pubs: List[Publication],
    raw_fallback: str,
    filter_cfg: FilterConfig,
) -> List[Tuple[str, str, str, str, str]]:
    """
    Aplica um filtro à lista de publicações.
    Retorna lista de (keyword, titulo, data_pub, snippet, full_text).
    """
    results = []
    seen_titles = set()

    # ── Matching estruturado (via XML parseado) ──────────────────────────────
    for pub in pubs:
        if not orgao_matches(pub.orgao, filter_cfg.orgao):
            continue

        for kw in filter_cfg.keywords:
            hit, snippet = keyword_matches(pub, kw)
            if hit:
                title_key = normalize(pub.titulo[:80])
                if title_key in seen_titles:
                    continue
                seen_titles.add(title_key)

                # Monta full_text
                full_text = "\n".join(filter(None, [
                    f"Órgão:    {pub.orgao}",
                    f"Título:   {pub.titulo}",
                    f"Ident.:   {pub.identifica}" if pub.identifica else "",
                    "",
                    pub.texto or pub.raw,
                ]))

                results.append((kw, pub.titulo, "", snippet[:500], full_text[:4000]))

    # ── Fallback: texto bruto (quando XML não parseou bem) ──────────────────
    if not results and raw_fallback:
        print(f"    Usando fallback de texto bruto para filtro '{filter_cfg.nome}'", flush=True)
        raw_lower = normalize(raw_fallback)
        orgao_norm = normalize(filter_cfg.orgao)

        # Divide por linha e busca blocos que contenham o órgão
        # Para texto bruto, fazemos matching simples de substring
        if orgao_norm not in raw_lower:
            # Tenta partes do orgao
            orgao_parts = [normalize(p.strip()) for p in filter_cfg.orgao.split("/") if p.strip()]
            has_orgao = any(p in raw_lower for p in orgao_parts)
        else:
            has_orgao = True

        if has_orgao:
            for kw in filter_cfg.keywords:
                kw_norm = normalize(kw)
                idx = raw_lower.find(kw_norm)
                if idx != -1:
                    start = max(0, idx - 250)
                    end   = min(len(raw_fallback), idx + 250)
                    snippet = raw_fallback[start:end].strip()
                    key = kw_norm[:40]
                    if key not in seen_titles:
                        seen_titles.add(key)
                        results.append((kw, "(texto bruto — XML sem estrutura)", "",
                                        snippet[:500], raw_fallback[:3000]))

    return results


# ============================================================================
# E-MAIL
# ============================================================================

def should_always_send_email() -> bool:
    """Seg–Sex, entre 09h–11h UTC (≈ 06h–08h BRT ou 10h–12h BRT dependendo do DST)."""
    now = datetime.utcnow()
    return now.weekday() < 5 and 9 <= now.hour < 11


def send_email(config: Config, run_date: str, matches: list, force_send: bool = False) -> bool:
    if not force_send and not matches:
        return False

    if matches:
        items_html = []
        for i, (_, filter_name, source_file, keyword, pub_title, pub_date, snippet, _) in enumerate(matches, 1):
            title_display = pub_title or "Sem título identificado"
            date_display  = f" — {pub_date}" if pub_date else ""
            items_html.append(f"""
                <hr/>
                <h3>Achado #{i} — {title_display}{date_display}</h3>
                <p><b>Filtro:</b> {filter_name}</p>
                <p><b>Palavra-chave:</b> <code>{keyword}</code></p>
                <p><b>Arquivo fonte:</b> {source_file}</p>
                <pre style="white-space:pre-wrap;font-family:monospace;
                            background:#f5f5f5;padding:10px;border-radius:5px;
                            border-left:3px solid #007bff;">{snippet}</pre>
            """)
        html_body = f"""
        <html><body style="font-family:Arial,sans-serif;">
            <h2 style="color:#28a745;">{config.email_subject_prefix} {run_date}</h2>
            <p><b>✅ Total de achados: {len(matches)}</b></p>
            {''.join(items_html)}
            <hr/>
            <p style="color:#666;font-size:12px;">
                Clipping automático via INLABS<br/>
                Seções monitoradas: {', '.join(sorted(set(f.secao for f in config.filtros)))}
            </p>
        </body></html>"""
        subject = f"{config.email_subject_prefix} {run_date} — {len(matches)} achado(s)"
    else:
        html_body = f"""
        <html><body style="font-family:Arial,sans-serif;">
            <h2 style="color:#6c757d;">{config.email_subject_prefix} {run_date}</h2>
            <p><b>✓ Sistema operacional</b></p>
            <p>Nenhuma publicação encontrada com os critérios de busca.</p>
            <hr/>
            <p style="color:#666;font-size:12px;">
                E-mail de confirmação diária (seg–sex, ≈10h BRT).
            </p>
        </body></html>"""
        subject = f"{config.email_subject_prefix} {run_date} — Sistema operacional (0 achados)"

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
        print(f"  📧 E-mail enviado: {subject}", flush=True)
        return True
    except Exception as e:
        print(f"  ❌ Erro ao enviar e-mail: {e}", flush=True)
        return False


# ============================================================================
# EXECUÇÃO PRINCIPAL
# ============================================================================

def run_for_date(
    config: Config,
    target_date: date,
    send_email_flag: bool = True,
    use_lookback: bool = True,
    client: Optional[InlabsClient] = None,
) -> int:
    init_db(config.db_path)

    owned_client = client is None
    if owned_client:
        client = InlabsClient(config.inlabs_email, config.inlabs_password)
        client.login()

    if use_lookback:
        dates_to_check = [
            target_date - timedelta(days=i)
            for i in range(config.lookback_days, -1, -1)
        ]
    else:
        dates_to_check = [target_date]

    all_matches = []
    files_processed = 0

    for filter_cfg in config.filtros:
        print(f"\n  ▸ Filtro: {filter_cfg.nome} (seção {filter_cfg.secao})", flush=True)

        for check_date in dates_to_check:
            print(f"    Data: {check_date.isoformat()}", flush=True)

            try:
                zips, pdfs = client.list_files(check_date, filter_cfg.secao)
            except Exception as e:
                print(f"    ❌ Erro ao listar arquivos: {e}", flush=True)
                continue

            # Prioridade: ZIPs (têm XML estruturado); PDFs como fallback
            candidates = zips if zips else pdfs
            new_files  = [f for f in candidates if not was_file_processed(config.db_path, f)]

            if not new_files:
                print(f"    Nenhum arquivo novo para processar.", flush=True)
                continue

            for filename in new_files:
                print(f"    ⬇️  Baixando {filename}...", flush=True)
                try:
                    content = client.download_file(check_date, filename)
                except Exception as e:
                    print(f"    ❌ Erro no download de {filename}: {e}", flush=True)
                    continue

                # Extrai publicações
                pubs: List[Publication] = []
                raw_fallback = ""
                try:
                    if filename.endswith(".zip"):
                        pubs, raw_fallback = extract_publications_from_zip(content)
                        print(f"    Extraídas {len(pubs)} publicações do ZIP.", flush=True)
                    elif filename.endswith(".pdf"):
                        raw_fallback = extract_text_from_pdf(content)
                        print(f"    PDF extraído ({len(raw_fallback):,} chars).", flush=True)
                    else:
                        print(f"    Formato desconhecido: {filename}", flush=True)
                        continue
                except Exception as e:
                    print(f"    ❌ Erro ao extrair {filename}: {e}", flush=True)
                    continue

                # Aplica filtro
                hits = find_matches_in_publications(pubs, raw_fallback, filter_cfg)
                print(f"    Filtro '{filter_cfg.nome}': {len(hits)} achado(s)", flush=True)

                for kw, pub_title, pub_date, snippet, full_text in hits:
                    all_matches.append((
                        check_date.isoformat(),
                        filter_cfg.nome,
                        filename,
                        kw,
                        pub_title,
                        pub_date,
                        snippet,
                        full_text,
                    ))

                mark_file_processed(config.db_path, filename, check_date.isoformat())
                files_processed += 1

    matches_count = insert_matches(config.db_path, all_matches)

    force_send = should_always_send_email()
    email_sent = False
    if send_email_flag and (matches_count > 0 or force_send):
        email_sent = send_email(config, target_date.isoformat(), all_matches, force_send)

    lookback_note = f"Lookback: {config.lookback_days}d" if use_lookback else "Sem lookback"
    log_run(config.db_path, target_date.isoformat(), files_processed, matches_count,
            email_sent, notes=f"{lookback_note}, {files_processed} arquivo(s)")

    return matches_count


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Clipping DOU — ANTT via INLABS")
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
        print(f"Clipping DOU — {target_date.isoformat()}", flush=True)
        print(f"Filtros: {[f.nome for f in config.filtros]}", flush=True)
        print(f"Janela:  D-{config.lookback_days} … D+0", flush=True)
        print(f"{'='*60}\n", flush=True)

        matches = run_for_date(config, target_date, not args.no_email, use_lookback=True)
        print(f"\n✓ Concluído: {matches} achado(s)", flush=True)

    elif args.command == "backfill":
        start_date = date.fromisoformat(args.start)
        end_date   = date.fromisoformat(args.end)

        print(f"\nBackfill: {start_date} → {end_date}", flush=True)
        print("(sem e-mail, sem lookback — apenas D+0 por dia)\n", flush=True)

        client = InlabsClient(config.inlabs_email, config.inlabs_password)
        client.login()

        current = start_date
        total   = 0
        while current <= end_date:
            print(f"\n{'─'*50}", flush=True)
            print(f"Processando {current.isoformat()}...", flush=True)
            n = run_for_date(config, current, send_email_flag=False,
                             use_lookback=False, client=client)
            total += n
            current += timedelta(days=1)
            # Pequena pausa para não sobrecarregar o servidor
            time.sleep(2)

        print(f"\n✓ Backfill concluído: {total} achado(s) no total", flush=True)


if __name__ == "__main__":
    main()
