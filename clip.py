#!/usr/bin/env python3
"""
Clipping DOU - ANTT/SUFER via INLABS

Sistema automatizado para monitorar publicações do Diário Oficial da União
relacionadas à ANTT (Agência Nacional de Transportes Terrestres) e SUFER
(Superintendência de Transporte Ferroviário).

Características:
- Login autenticado no INLABS
- Busca apenas seção 1 (DO1)
- Filtros: órgão + palavras-chave
- E-mail apenas quando há achados
- SQLite para deduplicação e histórico
- Suporte a backfill (sem envio de e-mail)
"""
import argparse
import os
import re
import sqlite3
import smtplib
import zipfile
from io import BytesIO
from datetime import date, datetime, timedelta
from typing import List, Tuple, Optional
from dataclasses import dataclass
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
import yaml
from bs4 import BeautifulSoup


# ============================================================================
# CONFIGURAÇÃO
# ============================================================================

@dataclass
class Config:
    """Configuração do sistema"""
    inlabs_email: str
    inlabs_password: str
    filter_orgao: str
    filter_keywords: List[str]
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str
    email_from: str
    email_to: List[str]
    email_subject_prefix: str
    db_path: str


def load_config(config_path: str) -> Config:
    """Carrega configuração do arquivo YAML"""
    with open(config_path, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f)
    
    # Credenciais podem vir de variáveis de ambiente (GitHub Secrets)
    inlabs_email = os.getenv('INLABS_EMAIL', cfg['inlabs']['email'])
    inlabs_password = os.getenv('INLABS_PASSWORD', cfg['inlabs']['password'])
    smtp_user = os.getenv('SMTP_USER', cfg['mail']['smtp_user'])
    smtp_pass = os.getenv('SMTP_PASS', cfg['mail']['smtp_pass'])
    
    return Config(
        inlabs_email=inlabs_email,
        inlabs_password=inlabs_password,
        filter_orgao=cfg['filters']['orgao_contains'],
        filter_keywords=cfg['filters']['keywords_any'],
        smtp_host=cfg['mail']['smtp_host'],
        smtp_port=cfg['mail']['smtp_port'],
        smtp_user=smtp_user,
        smtp_pass=smtp_pass,
        email_from=cfg['mail']['from_email'],
        email_to=cfg['mail']['to_emails'],
        email_subject_prefix=cfg['mail']['subject_prefix'],
        db_path=cfg['storage']['db_path'],
    )


# ============================================================================
# BANCO DE DADOS (SQLite)
# ============================================================================

def init_db(db_path: str):
    """Inicializa banco de dados SQLite"""
    os.makedirs(os.path.dirname(db_path) or '.', exist_ok=True)
    
    con = sqlite3.connect(db_path)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_ts TEXT NOT NULL,
            run_date TEXT NOT NULL,
            files_processed INTEGER NOT NULL,
            matches_found INTEGER NOT NULL,
            email_sent INTEGER NOT NULL,
            notes TEXT
        );
        
        CREATE TABLE IF NOT EXISTS processed_files (
            file_name TEXT PRIMARY KEY,
            processed_date TEXT NOT NULL,
            processed_ts TEXT NOT NULL
        );
        
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            source_file TEXT NOT NULL,
            keyword_hit TEXT NOT NULL,
            text_snippet TEXT NOT NULL,
            created_ts TEXT NOT NULL
        );
    """)
    con.commit()
    con.close()


def was_file_processed(db_path: str, filename: str) -> bool:
    """Verifica se arquivo já foi processado"""
    con = sqlite3.connect(db_path)
    result = con.execute(
        "SELECT 1 FROM processed_files WHERE file_name = ?",
        (filename,)
    ).fetchone()
    con.close()
    return result is not None


def mark_file_processed(db_path: str, filename: str, run_date: str):
    """Marca arquivo como processado"""
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT OR IGNORE INTO processed_files (file_name, processed_date, processed_ts) VALUES (?, ?, ?)",
        (filename, run_date, datetime.utcnow().isoformat())
    )
    con.commit()
    con.close()


def insert_matches(db_path: str, matches: List[Tuple[str, str, str, str]]) -> int:
    """Insere matches no banco. Retorna quantidade inserida."""
    if not matches:
        return 0
    
    con = sqlite3.connect(db_path)
    con.executemany(
        "INSERT INTO matches (run_date, source_file, keyword_hit, text_snippet, created_ts) VALUES (?, ?, ?, ?, ?)",
        [(m[0], m[1], m[2], m[3], datetime.utcnow().isoformat()) for m in matches]
    )
    con.commit()
    count = len(matches)
    con.close()
    return count


def log_run(db_path: str, run_date: str, files_processed: int, matches_found: int, email_sent: bool, notes: str = ""):
    """Registra execução no banco"""
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO runs (run_ts, run_date, files_processed, matches_found, email_sent, notes) VALUES (?, ?, ?, ?, ?, ?)",
        (datetime.utcnow().isoformat(), run_date, files_processed, matches_found, 1 if email_sent else 0, notes)
    )
    con.commit()
    con.close()


# ============================================================================
# INLABS CLIENT (autenticado)
# ============================================================================

class InlabsClient:
    """Cliente para acessar INLABS com autenticação"""
    
    def __init__(self, email: str, password: str):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        self._login(email, password)
    
    def _login(self, email: str, password: str):
        """Faz login no INLABS"""
        login_url = "https://inlabs.in.gov.br/logar.php"
        data = {"email": email, "password": password}
        
        r = self.session.post(login_url, data=data, timeout=30)
        
        if r.status_code != 200 or "sair" not in r.text.lower():
            raise RuntimeError("Falha no login do INLABS. Verifique credenciais.")
    
    def list_files(self, target_date: date) -> List[str]:
        """Lista arquivos disponíveis para uma data (apenas DO1 e DO1E)"""
        date_str = target_date.isoformat()
        url = f"https://inlabs.in.gov.br/index.php?p={date_str}"
        
        r = self.session.get(url, timeout=30)
        
        # Extrai nomes de arquivo do HTML
        text = r.text
        files = []
        
        # Apenas DO1 (seção 1) e DO1E (extras da seção 1)
        for pattern in [r'(\d{4}-\d{2}-\d{2}-DO1\.zip)', r'(\d{4}-\d{2}-\d{2}-DO1E\.zip)']:
            files.extend(re.findall(pattern, text))
        
        return list(set(files))  # Remove duplicatas
    
    def download_file(self, target_date: date, filename: str) -> bytes:
        """Baixa arquivo do INLABS"""
        date_str = target_date.isoformat()
        url = f"https://inlabs.in.gov.br/index.php?p={date_str}&dl={filename}"
        
        r = self.session.get(url, timeout=60, stream=True)
        
        if r.status_code != 200:
            raise RuntimeError(f"Erro ao baixar {filename}: HTTP {r.status_code}")
        
        # Verifica se é realmente um arquivo (não HTML)
        content_type = r.headers.get('content-type', '')
        if 'text/html' in content_type:
            raise RuntimeError(f"INLABS retornou HTML em vez do arquivo {filename}")
        
        return r.content


# ============================================================================
# PARSER DE ARQUIVOS
# ============================================================================

def extract_text_from_zip(zip_bytes: bytes) -> str:
    """Extrai texto de todos os XMLs dentro do ZIP"""
    texts = []
    
    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if name.lower().endswith('.xml'):
                xml_content = zf.read(name).decode('utf-8', errors='ignore')
                
                # Remove tags XML
                text = re.sub(r'<[^>]+>', ' ', xml_content)
                text = re.sub(r'\s+', ' ', text).strip()
                
                if text:
                    texts.append(text)
    
    return '\n\n'.join(texts)


def find_matches(text: str, orgao_filter: str, keywords: List[str]) -> List[Tuple[str, str]]:
    """
    Busca matches no texto.
    Retorna lista de (keyword_matched, text_snippet).
    """
    matches = []
    text_lower = text.lower()
    
    # Filtro por órgão
    if orgao_filter.lower() not in text_lower:
        return matches
    
    # Busca por palavras-chave
    for keyword in keywords:
        if keyword.lower() in text_lower:
            # Extrai snippet ao redor da palavra-chave (500 chars)
            idx = text_lower.find(keyword.lower())
            start = max(0, idx - 250)
            end = min(len(text), idx + 250)
            snippet = text[start:end].strip()
            
            matches.append((keyword, snippet))
    
    return matches


# ============================================================================
# E-MAIL
# ============================================================================

def send_email(config: Config, run_date: str, matches: List[Tuple[str, str, str, str]]):
    """Envia e-mail com os achados"""
    if not matches:
        return
    
    # Monta HTML
    items_html = []
    for i, (_, source_file, keyword, snippet) in enumerate(matches, 1):
        items_html.append(f"""
            <hr/>
            <h3>Achado #{i} — Palavra-chave: <code>{keyword}</code></h3>
            <p><b>Arquivo fonte:</b> {source_file}</p>
            <pre style="white-space: pre-wrap; font-family: monospace; background: #f5f5f5; padding: 10px; border-radius: 5px;">
{snippet}
            </pre>
        """)
    
    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif;">
        <h2>{config.email_subject_prefix} {run_date}</h2>
        <p><b>Total de achados: {len(matches)}</b></p>
        {''.join(items_html)}
        <hr/>
        <p style="color: #666; font-size: 12px;">
            Clipping automático gerado via INLABS<br/>
            ANTT/SUFER - Seção 1 do DOU
        </p>
    </body>
    </html>
    """
    
    # Monta mensagem
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"{config.email_subject_prefix} {run_date} — {len(matches)} achado(s)"
    msg['From'] = config.email_from
    msg['To'] = ', '.join(config.email_to)
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))
    
    # Envia
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=60) as server:
        server.starttls()
        server.login(config.smtp_user, config.smtp_pass)
        server.sendmail(config.email_from, config.email_to, msg.as_string())


# ============================================================================
# EXECUÇÃO PRINCIPAL
# ============================================================================

def run_for_date(config: Config, target_date: date, send_email_flag: bool = True) -> int:
    """
    Executa clipping para uma data específica.
    Retorna número de matches encontrados.
    """
    init_db(config.db_path)
    
    client = InlabsClient(config.inlabs_email, config.inlabs_password)
    
    # Lista arquivos
    files = client.list_files(target_date)
    new_files = [f for f in files if not was_file_processed(config.db_path, f)]
    
    all_matches = []
    files_processed = 0
    
    # Processa cada arquivo
    for filename in new_files:
        try:
            # Download
            content = client.download_file(target_date, filename)
            
            # Parse
            text = extract_text_from_zip(content)
            
            # Busca matches
            matches = find_matches(text, config.filter_orgao, config.filter_keywords)
            
            # Salva matches
            for keyword, snippet in matches:
                all_matches.append((
                    target_date.isoformat(),
                    filename,
                    keyword,
                    snippet
                ))
            
            # Marca como processado
            mark_file_processed(config.db_path, filename, target_date.isoformat())
            files_processed += 1
        
        except Exception as e:
            print(f"Erro processando {filename}: {e}")
            continue
    
    # Insere matches no banco
    matches_count = insert_matches(config.db_path, all_matches)
    
    # Envia e-mail (se habilitado e houver achados)
    email_sent = False
    if send_email_flag and matches_count > 0:
        try:
            send_email(config, target_date.isoformat(), all_matches)
            email_sent = True
        except Exception as e:
            print(f"Erro enviando e-mail: {e}")
    
    # Log da execução
    log_run(
        config.db_path,
        target_date.isoformat(),
        files_processed,
        matches_count,
        email_sent,
        notes=f"OK - {len(files)} arquivo(s) disponível(is), {files_processed} novo(s)"
    )
    
    return matches_count


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description='Clipping DOU - ANTT/SUFER')
    parser.add_argument('--config', default='config.yml', help='Caminho do arquivo de configuração')
    
    subparsers = parser.add_subparsers(dest='command', required=True)
    
    # Comando: run (execução única)
    run_parser = subparsers.add_parser('run', help='Executa clipping para uma data')
    run_parser.add_argument('--date', help='Data no formato YYYY-MM-DD (padrão: hoje)')
    run_parser.add_argument('--no-email', action='store_true', help='Não enviar e-mail')
    
    # Comando: backfill (intervalo de datas)
    backfill_parser = subparsers.add_parser('backfill', help='Backfill para intervalo de datas')
    backfill_parser.add_argument('--start', required=True, help='Data inicial (YYYY-MM-DD)')
    backfill_parser.add_argument('--end', required=True, help='Data final (YYYY-MM-DD)')
    
    args = parser.parse_args()
    
    # Carrega config
    config = load_config(args.config)
    
    if args.command == 'run':
        target_date = date.fromisoformat(args.date) if args.date else date.today()
        send_email = not args.no_email
        
        print(f"Executando clipping para {target_date.isoformat()}...")
        matches = run_for_date(config, target_date, send_email)
        print(f"✓ Concluído: {matches} achado(s)")
    
    elif args.command == 'backfill':
        start_date = date.fromisoformat(args.start)
        end_date = date.fromisoformat(args.end)
        
        print(f"Backfill de {start_date} até {end_date}")
        print("(E-mails desabilitados durante backfill)\n")
        
        current = start_date
        total_matches = 0
        
        while current <= end_date:
            print(f"Processando {current.isoformat()}...", end=' ')
            matches = run_for_date(config, current, send_email_flag=False)
            total_matches += matches
            print(f"{matches} achado(s)")
            
            current += timedelta(days=1)
        
        print(f"\n✓ Backfill concluído: {total_matches} achado(s) no total")


if __name__ == '__main__':
    main()
