#!/usr/bin/env python3
"""
Clipping DOU - ANTT/SUFER via INLABS (Versão Final)

Características:
- Janela de busca: D-2, D-1, D+0 (3 dias)
- E-mail inteligente: sempre seg-sex 10:08, outros horários só com achados
- ZIP preferencial + PDF fallback
- Arquitetura extensível (multi-filtro)
- Deduplicação automática
"""
import argparse
import os
import re
import sqlite3
import smtplib
import zipfile
from io import BytesIO
from datetime import date, datetime, timedelta
from typing import List, Tuple, Optional, Dict, Any
from dataclasses import dataclass
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
import yaml
from bs4 import BeautifulSoup

try:
    from PyPDF2 import PdfReader
except ImportError:
    PdfReader = None


# ============================================================================
# CONFIGURAÇÃO
# ============================================================================

@dataclass
class FilterConfig:
    """Configuração de um filtro de busca"""
    nome: str
    secao: str  # DO1, DO2, DO3
    orgao: str
    keywords: List[str]


@dataclass
class Config:
    """Configuração do sistema"""
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
    lookback_days: int  # Janela de revarredura (padrão: 2 = D-2, D-1, D+0)


def load_config(config_path: str) -> Config:
    """Carrega configuração do arquivo YAML"""
    with open(config_path, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f)
    
    # Credenciais podem vir de variáveis de ambiente (GitHub Secrets)
    inlabs_email = os.getenv('INLABS_EMAIL', cfg['inlabs']['email'])
    inlabs_password = os.getenv('INLABS_PASSWORD', cfg['inlabs']['password'])
    smtp_user = os.getenv('SMTP_USER', cfg['mail']['smtp_user'])
    smtp_pass = os.getenv('SMTP_PASS', cfg['mail']['smtp_pass'])
    
    # Carrega filtros (suporta múltiplos)
    filtros = []
    for f in cfg['filtros']:
        filtros.append(FilterConfig(
            nome=f['nome'],
            secao=f['secao'],
            orgao=f['orgao'],
            keywords=f['keywords']
        ))
    
    return Config(
        inlabs_email=inlabs_email,
        inlabs_password=inlabs_password,
        filtros=filtros,
        smtp_host=cfg['mail']['smtp_host'],
        smtp_port=cfg['mail']['smtp_port'],
        smtp_user=smtp_user,
        smtp_pass=smtp_pass,
        email_from=cfg['mail']['from_email'],
        email_to=cfg['mail']['to_emails'],
        email_subject_prefix=cfg['mail']['subject_prefix'],
        db_path=cfg['storage']['db_path'],
        lookback_days=cfg.get('lookback_days', 2),  # Padrão: 2 dias (D-2, D-1, D+0)
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
            filter_name TEXT NOT NULL,
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


def insert_matches(db_path: str, matches: List[Tuple[str, str, str, str, str]]) -> int:
    """Insere matches no banco. Retorna quantidade inserida."""
    if not matches:
        return 0
    
    con = sqlite3.connect(db_path)
    con.executemany(
        "INSERT INTO matches (run_date, filter_name, source_file, keyword_hit, text_snippet, created_ts) VALUES (?, ?, ?, ?, ?, ?)",
        [(m[0], m[1], m[2], m[3], m[4], datetime.utcnow().isoformat()) for m in matches]
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
    
    def list_files(self, target_date: date, secao: str) -> List[str]:
        """
        Lista arquivos disponíveis para uma data e seção.
        
        Args:
            target_date: Data alvo
            secao: "DO1", "DO2" ou "DO3"
        
        Returns:
            Lista de nomes de arquivo (ZIPs e PDFs)
        """
        date_str = target_date.isoformat()
        date_str_underscore = date_str.replace("-", "_")
        url = f"https://inlabs.in.gov.br/index.php?p={date_str}"
        
        r = self.session.get(url, timeout=30)
        text = r.text
        
        files = []
        secao_num = secao.replace("DO", "")  # "DO1" -> "1"
        
        # ZIPs (normal + extras)
        zip_patterns = [
            rf'(\d{{4}}-\d{{2}}-\d{{2}}-DO{secao_num}\.zip)',
            rf'(\d{{4}}-\d{{2}}-\d{{2}}-DO{secao_num}E\.zip)',
        ]
        
        for pattern in zip_patterns:
            files.extend(re.findall(pattern, text))
        
        # PDFs (normal + extras A, B, C)
        pdf_patterns = [
            rf'(\d{{4}}_\d{{2}}_\d{{2}}_ASSINADO_do{secao_num}\.pdf)',
            rf'(\d{{4}}_\d{{2}}_\d{{2}}_ASSINADO_do{secao_num}_extra_[ABC]\.pdf)',
        ]
        
        for pattern in pdf_patterns:
            files.extend(re.findall(pattern, text))
        
        # Remove duplicatas
        return list(set(files))
    
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


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extrai texto de PDF"""
    if not PdfReader:
        raise RuntimeError("PyPDF2 não instalado. Instale com: pip install PyPDF2")
    
    reader = PdfReader(BytesIO(pdf_bytes))
    texts = []
    
    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            texts.append(text.strip())
    
    return '\n\n'.join(texts)


def find_matches(text: str, filter_config: FilterConfig) -> List[Tuple[str, str]]:
    """
    Busca matches no texto baseado em um filtro.
    Retorna lista de (keyword_matched, text_snippet).
    """
    matches = []
    text_lower = text.lower()
    
    # Filtro por órgão
    if filter_config.orgao.lower() not in text_lower:
        return matches
    
    # Busca por palavras-chave
    for keyword in filter_config.keywords:
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

def should_always_send_email() -> bool:
    """
    Verifica se deve sempre enviar e-mail (mesmo sem achados).
    Retorna True se for segunda a sexta entre 09:00 e 11:00 UTC (06:00-08:00 BRT -> 10:08 BRT).
    """
    now = datetime.utcnow()
    
    # Segunda (0) a Sexta (4)
    is_weekday = now.weekday() < 5
    
    # Entre 09:00 e 11:00 UTC (aproximação do horário 10:08 BRT)
    is_morning_run = 9 <= now.hour < 11
    
    return is_weekday and is_morning_run


def send_email(config: Config, run_date: str, matches: List[Tuple[str, str, str, str, str]], force_send: bool = False):
    """
    Envia e-mail com os achados.
    
    Args:
        force_send: Se True, envia mesmo sem achados (para confirmação diária)
    """
    # Se não deve forçar envio e não há matches, não envia
    if not force_send and not matches:
        return False
    
    # Monta HTML
    if matches:
        items_html = []
        for i, (_, filter_name, source_file, keyword, snippet) in enumerate(matches, 1):
            items_html.append(f"""
                <hr/>
                <h3>Achado #{i} — Filtro: {filter_name}</h3>
                <p><b>Palavra-chave:</b> <code>{keyword}</code></p>
                <p><b>Arquivo fonte:</b> {source_file}</p>
                <pre style="white-space: pre-wrap; font-family: monospace; background: #f5f5f5; padding: 10px; border-radius: 5px; border-left: 3px solid #007bff;">
{snippet}
                </pre>
            """)
        
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif;">
            <h2 style="color: #28a745;">{config.email_subject_prefix} {run_date}</h2>
            <p><b>✅ Total de achados: {len(matches)}</b></p>
            {''.join(items_html)}
            <hr/>
            <p style="color: #666; font-size: 12px;">
                Clipping automático gerado via INLABS<br/>
                Janela de busca: D-2, D-1, D+0 | Seções: {', '.join(set(f.secao for f in config.filtros))}
            </p>
        </body>
        </html>
        """
        subject = f"{config.email_subject_prefix} {run_date} — {len(matches)} achado(s)"
    else:
        # E-mail de confirmação (sem achados)
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif;">
            <h2 style="color: #6c757d;">{config.email_subject_prefix} {run_date}</h2>
            <p><b>✓ Sistema operacional</b></p>
            <p>Nenhuma publicação encontrada com os critérios de busca.</p>
            <hr/>
            <p style="color: #666; font-size: 12px;">
                Este é um e-mail de confirmação diária (segunda a sexta, 10:08 BRT).<br/>
                O sistema continua monitorando o DOU automaticamente.
            </p>
        </body>
        </html>
        """
        subject = f"{config.email_subject_prefix} {run_date} — Sistema operacional (0 achados)"
    
    # Monta mensagem
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = config.email_from
    msg['To'] = ', '.join(config.email_to)
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))
    
    # Envia
    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=60) as server:
            server.starttls()
            server.login(config.smtp_user, config.smtp_pass)
            server.sendmail(config.email_from, config.email_to, msg.as_string())
        return True
    except Exception as e:
        print(f"Erro enviando e-mail: {e}")
        return False


# ============================================================================
# EXECUÇÃO PRINCIPAL
# ============================================================================

def run_for_date(config: Config, target_date: date, send_email_flag: bool = True, use_lookback: bool = True) -> int:
    """
    Executa clipping para uma data específica com janela de lookback opcional.
    
    Args:
        config: Configuração do sistema
        target_date: Data alvo
        send_email_flag: Se deve enviar e-mail
        use_lookback: Se deve usar janela D-2, D-1, D+0 (True) ou apenas D+0 (False)
    
    Retorna número de matches encontrados.
    """
    init_db(config.db_path)
    
    client = InlabsClient(config.inlabs_email, config.inlabs_password)
    
    # Janela de busca: com ou sem lookback
    if use_lookback:
        # Diário: D-lookback até D+0 (captura extras tardias)
        dates_to_check = [target_date - timedelta(days=i) for i in range(config.lookback_days, -1, -1)]
    else:
        # Backfill: apenas D+0 (dados históricos já estão completos)
        dates_to_check = [target_date]
    
    all_matches = []
    files_processed = 0
    
    # Para cada filtro configurado
    for filter_cfg in config.filtros:
        print(f"  Filtro: {filter_cfg.nome} (Seção {filter_cfg.secao})")
        
        # Para cada data na janela
        for check_date in dates_to_check:
            # Lista arquivos
            files = client.list_files(check_date, filter_cfg.secao)
            new_files = [f for f in files if not was_file_processed(config.db_path, f)]
            
            # Processa cada arquivo
            for filename in new_files:
                try:
                    # Download
                    content = client.download_file(check_date, filename)
                    
                    # Parse (ZIP ou PDF)
                    if filename.endswith('.zip'):
                        text = extract_text_from_zip(content)
                    elif filename.endswith('.pdf'):
                        text = extract_text_from_pdf(content)
                    else:
                        continue
                    
                    # Busca matches
                    matches = find_matches(text, filter_cfg)
                    
                    # Salva matches
                    for keyword, snippet in matches:
                        all_matches.append((
                            check_date.isoformat(),
                            filter_cfg.nome,
                            filename,
                            keyword,
                            snippet
                        ))
                    
                    # Marca como processado
                    mark_file_processed(config.db_path, filename, check_date.isoformat())
                    files_processed += 1
                
                except Exception as e:
                    print(f"    Erro processando {filename}: {e}")
                    continue
    
    # Insere matches no banco
    matches_count = insert_matches(config.db_path, all_matches)
    
    # Decide se envia e-mail
    force_send = should_always_send_email()
    email_sent = False
    
    if send_email_flag:
        if matches_count > 0 or force_send:
            email_sent = send_email(config, target_date.isoformat(), all_matches, force_send)
    
    # Log da execução
    lookback_note = f"Lookback: {config.lookback_days} dias" if use_lookback else "Sem lookback (backfill)"
    log_run(
        config.db_path,
        target_date.isoformat(),
        files_processed,
        matches_count,
        email_sent,
        notes=f"{lookback_note}, {files_processed} arquivo(s) processado(s)"
    )
    
    return matches_count


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description='Clipping DOU - Multi-filtro')
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
        print(f"Janela: {config.lookback_days} dias (D-{config.lookback_days} até D+0)")
        matches = run_for_date(config, target_date, send_email, use_lookback=True)
        print(f"✓ Concluído: {matches} achado(s)")
    
    elif args.command == 'backfill':
        start_date = date.fromisoformat(args.start)
        end_date = date.fromisoformat(args.end)
        
        print(f"Backfill de {start_date} até {end_date}")
        print("(E-mails desabilitados, sem lookback - apenas D+0 por dia)\n")
        
        current = start_date
        total_matches = 0
        
        while current <= end_date:
            print(f"Processando {current.isoformat()}...", end=' ')
            matches = run_for_date(config, current, send_email_flag=False, use_lookback=False)
            total_matches += matches
            print(f"{matches} achado(s)")
            
            current += timedelta(days=1)
        
        print(f"\n✓ Backfill concluído: {total_matches} achado(s) no total")


if __name__ == '__main__':
    main()