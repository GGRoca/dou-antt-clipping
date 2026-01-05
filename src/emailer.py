from __future__ import annotations

import json
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import duckdb

from settings import SETTINGS


def load_runlog() -> dict:
    path = "data/runlog.json"
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_html(occ_rows) -> str:
    # HTML simples, legível
    parts = []
    parts.append("<html><body>")
    parts.append("<h2>Clipping DOU — ANTT/SUFER</h2>")
    parts.append(f"<p>Total de novas ocorrências: <b>{len(occ_rows)}</b></p>")
    parts.append("<hr/>")

    for (pub_date, edition_type, section, title, body_text, link_dou) in occ_rows:
        parts.append(f"<h3>{title}</h3>")
        meta = f"Data: {pub_date} | Edição: {edition_type}"
        if section:
            meta += f" | Seção: {section}"
        parts.append(f"<p><i>{meta}</i></p>")
        parts.append(f'<p><a href="{link_dou}">Abrir no DOU</a></p>')
        parts.append("<pre style='white-space:pre-wrap;font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, \"Liberation Mono\", \"Courier New\", monospace;'>")
        parts.append(body_text)
        parts.append("</pre>")
        parts.append("<hr/>")

    parts.append("</body></html>")
    return "\n".join(parts)


def send_email(subject: str, html: str) -> None:
    if not SETTINGS.smtp_host or not SETTINGS.smtp_user or not SETTINGS.smtp_pass:
        raise RuntimeError("SMTP não configurado (SMTP_HOST/USER/PASS).")

    if not SETTINGS.email_from:
        raise RuntimeError("EMAIL_FROM não configurado.")
    if not SETTINGS.email_to_list:
        raise RuntimeError("EMAIL_TO não configurado.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SETTINGS.email_from
    msg["To"] = ", ".join(SETTINGS.email_to_list)
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP(SETTINGS.smtp_host, SETTINGS.smtp_port, timeout=60) as server:
        server.starttls()
        server.login(SETTINGS.smtp_user, SETTINGS.smtp_pass)
        server.sendmail(SETTINGS.email_from, SETTINGS.email_to_list, msg.as_string())


def main():
    runlog = load_runlog()
    new_occ = int(runlog.get("new_occurrences", 0))

    # Regra: só envia se habilitado E se houver achados
    if not SETTINGS.email_enabled or new_occ <= 0:
        print(f"EMAIL_ENABLED={SETTINGS.email_enabled}; new_occurrences={new_occ}. Nada a enviar.")
        return

    con = duckdb.connect(SETTINGS.db_path)

    # pega as ocorrências inseridas na última execução (simplificado: últimas N por timestamp)
    rows = con.execute("""
        SELECT pub_date, edition_type, section, title, body_text, link_dou
        FROM ocorrencias
        ORDER BY inserted_ts DESC
        LIMIT 20
    """).fetchall()
    con.close()

    subject = f"DOU ANTT/SUFER — {len(rows)} ocorrência(s) nova(s)"
    html = build_html(rows)
    send_email(subject, html)
    print("E-mail enviado.")


if __name__ == "__main__":
    main()
