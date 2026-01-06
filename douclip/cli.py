from __future__ import annotations
import argparse
import os
from datetime import date, datetime, timedelta
from dateutil.parser import isoparse

from .config import load_config, MailConfig
from .inlabs import InlabsClient
from .parser import parse_zip_for_text, parse_pdf_for_text, extract_publications_from_blob, find_relevant_hits
from .storage import Storage, MatchRow
from .mailer import send_email_smtp

def _daterange(d1: date, d2: date):
    cur = d1
    while cur <= d2:
        yield cur
        cur += timedelta(days=1)

def build_email_html(run_date: str, matches: list[MatchRow]) -> str:
    items = []
    for i, m in enumerate(matches, start=1):
        link = f'<a href="{m.dou_link}">link</a>' if m.dou_link else f'<a href="{m.source_file_url}">arquivo no INLABS</a>'
        items.append(f"""
          <hr/>
          <h3>Achado #{i} — keyword: <code>{m.keyword_hit}</code></h3>
          <p><b>Fonte:</b> {link}</p>
          <pre style="white-space: pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;">
{m.text_full}
          </pre>
        """)

    return f"""
    <html>
      <body>
        <h2>DOU — ANTT/SUFER — {run_date}</h2>
        <p>Total de achados: <b>{len(matches)}</b></p>
        {''.join(items)}
      </body>
    </html>
    """

def run_for_date(cfg_path: str, run_date: date, no_email: bool = False) -> int:
    cfg = load_config(cfg_path)
    
    # Se --no-email foi passado, força desabilitar email
    if no_email:
        cfg = cfg._replace(mail=MailConfig(
            enabled=False,
            smtp_host=cfg.mail.smtp_host,
            smtp_port=cfg.mail.smtp_port,
            from_email=cfg.mail.from_email,
            to_emails=cfg.mail.to_emails,
            subject_prefix=cfg.mail.subject_prefix,
        ))
    
    storage = Storage(cfg.storage.sqlite_path)
    client = InlabsClient(cfg.inlabs.base_url)

    files = client.list_files(run_date)
    files_seen = len(files)

    new_files = [f for f in files if not storage.was_file_processed(f.url)]
    files_new = len(new_files)

    all_match_rows: list[MatchRow] = []

    for f in new_files:
        name_lower = f.name.lower()
        try:
            data = client.download_bytes(f.url)
            if name_lower.endswith(".zip"):
                blob = parse_zip_for_text(data)
            elif name_lower.endswith(".pdf"):
                blob = parse_pdf_for_text(data)
            else:
                # xml direto
                blob = data.decode("utf-8", errors="ignore")

            pubs = extract_publications_from_blob(blob)
            hits = find_relevant_hits(pubs, cfg.filters.orgao_contains, cfg.filters.keywords_any)

            for pub, kw in hits:
                all_match_rows.append(
                    MatchRow(
                        run_date=run_date.isoformat(),
                        source_file_url=f.url,
                        publication_title=pub.title,
                        orgao=pub.orgao,
                        keyword_hit=kw,
                        text_full=pub.full_text,
                        dou_link=pub.dou_link,
                    )
                )

            storage.mark_file_processed(run_date.isoformat(), f.url, f.name)
        except Exception as e:
            # marca como processado? eu prefiro NÃO marcar para tentar de novo na próxima execução
            # e registrar no log.
            storage.log_run(
                run_date=run_date.isoformat(),
                files_seen=files_seen,
                files_new=files_new,
                matches_found=0,
                email_sent=False,
                notes=f"Erro processando {f.url}: {repr(e)}",
            )
            continue

    matches_found = storage.insert_matches(all_match_rows)

    email_sent = False
    if cfg.mail.enabled and matches_found > 0:
        smtp_user = os.environ.get("SMTP_USER", cfg.mail.from_email)
        smtp_pass = os.environ.get("SMTP_PASS", "")
        if not smtp_pass:
            raise RuntimeError("SMTP_PASS não definido (GitHub Secret).")

        subject = f"{cfg.mail.subject_prefix} {run_date.isoformat()} — {matches_found} achado(s)"
        html = build_email_html(run_date.isoformat(), all_match_rows)

        send_email_smtp(
            smtp_host=cfg.mail.smtp_host,
            smtp_port=cfg.mail.smtp_port,
            username=smtp_user,
            app_password=smtp_pass,
            from_email=cfg.mail.from_email,
            to_emails=cfg.mail.to_emails,
            subject=subject,
            html_body=html,
        )
        email_sent = True

    storage.log_run(
        run_date=run_date.isoformat(),
        files_seen=files_seen,
        files_new=files_new,
        matches_found=matches_found,
        email_sent=email_sent,
        notes="OK" if matches_found >= 0 else "Sem dados",
    )

    return matches_found

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.yml")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run")
    p_run.add_argument("--date", required=False, help="YYYY-MM-DD (default: hoje)")
    p_run.add_argument("--no-email", action="store_true", help="Desabilita envio de email")

    p_back = sub.add_parser("backfill")
    p_back.add_argument("--start", required=True, help="YYYY-MM-DD")
    p_back.add_argument("--end", required=True, help="YYYY-MM-DD")
    p_back.add_argument("--no-email", action="store_true", help="Desabilita envio de email")

    args = p.parse_args()

    if args.cmd == "run":
        d = date.today() if not args.date else isoparse(args.date).date()
        run_for_date(args.config, d, no_email=args.no_email)

    elif args.cmd == "backfill":
        d1 = isoparse(args.start).date()
        d2 = isoparse(args.end).date()
        for d in _daterange(d1, d2):
            run_for_date(args.config, d, no_email=args.no_email)
