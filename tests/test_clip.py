"""
Testes do clipping. Rodar: python -m pytest -q

Os XMLs sinteticos seguem o formato real do INLABS (<article> com metadados em
atributos e conteudo em CDATA dentro de <body>), conforme a implementacao
oficial da Imprensa Nacional e o carregador do Ro-DOU.
"""
import io
import os
import sys
import threading
import zipfile
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import clip  # noqa: E402

# ---------------------------------------------------------------------------
# fixtures de dados
# ---------------------------------------------------------------------------

ANTT = "Ministério dos Transportes/Agência Nacional de Transportes Terrestres"
SUFER = ANTT + "/Superintendência de Transporte Ferroviário"
DIRETORIA = ANTT + "/Diretoria Colegiada"
OUTRO = "Ministério da Saúde/Agência Nacional de Vigilância Sanitária"


def article_xml(art_id, art_type, category, identifica, texto_html, page="12",
                edition="177", pub_name="DO1", pub_date="16/09/2026"):
    ident_tag = f"<Identifica><![CDATA[{identifica}]]></Identifica>" if identifica is not None else "<Identifica/>"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<xml>
<article id="{art_id}" name="{art_id}-{art_type}" idOficio="9{art_id}" pubName="{pub_name}" artType="{art_type}" pubDate="{pub_date}" artClass="Portaria" artCategory="{category}" artSize="1" artNotes="" numberPage="{page}" pdfPage="https://pesquisa.in.gov.br/imprensa/jsp/visualiza/index.jsp?data={pub_date}&amp;jornal=515&amp;pagina={page}" editionNumber="{edition}" highlightType="" highlightPriority="" highlight="" highlightimage="" highlightimagename="" idMateria="{art_id}">
<body>
{ident_tag}
<Data><![CDATA[]]></Data>
<Ementa><![CDATA[]]></Ementa>
<Titulo><![CDATA[]]></Titulo>
<SubTitulo><![CDATA[]]></SubTitulo>
<Texto><![CDATA[{texto_html}]]></Texto>
</body>
<Midias></Midias>
</article>
</xml>""".encode("utf-8")


PORTARIA_SUFER = article_xml(
    "1001", "Portaria", SUFER, "PORTARIA Nº 45, DE 15 DE SETEMBRO DE 2026",
    '<p class="identifica">PORTARIA Nº 45, DE 15 DE SETEMBRO DE 2026</p>'
    '<p class="ementa">Autoriza a operação ferroviária no trecho X.</p>'
    '<p class="dou-paragraph">O SUPERINTENDENTE DE TRANSPORTE FERROVIÁRIO resolve:</p>'
    '<p class="dou-paragraph">Art. 1º Fica autorizada a operação.</p>'
    '<table><tr><td>Trecho</td><td>Km</td></tr><tr><td>A-B</td><td>120</td></tr></table>'
    '<p class="assina">JOÃO DA SILVA</p><p class="cargo">Superintendente</p>')
DELIBERACAO = article_xml(
    "1002", "Deliberação", DIRETORIA, "DELIBERAÇÃO Nº 300, DE 15 DE SETEMBRO DE 2026",
    '<p class="identifica">DELIBERAÇÃO Nº 300</p><p>A Diretoria delibera.</p>', page="13")
RESOLUCAO_ANTT = article_xml(
    "1003", "Resolução", ANTT, "RESOLUÇÃO Nº 6.100, DE 15 DE SETEMBRO DE 2026",
    '<p class="identifica">RESOLUÇÃO Nº 6.100</p><p>Texto da resolução.</p>', page="14")
DESPACHO_SUFER = article_xml(
    "1004", "Despacho", SUFER, "DESPACHO",
    '<p class="identifica">DESPACHO</p><p>Defiro o pedido.</p>', page="15")
ANEXO_SUFER = article_xml(
    "1005", "Anexo", SUFER, None,
    '<table><tr><td>quadro anexo</td></tr></table>', page="15")
OUTRO_ORGAO = article_xml(
    "2001", "Portaria", OUTRO, "PORTARIA Nº 9, DE 15 DE SETEMBRO DE 2026",
    '<p class="identifica">PORTARIA Nº 9</p><p>Nada a ver com transportes.</p>', page="80")

EDITION = date(2026, 9, 16)


def make_zip(*xmls):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, x in enumerate(xmls):
            zf.writestr(f"{i}.xml", x)
    return buf.getvalue()


def make_filtros():
    return [
        clip.Filtro("SUFER-Todas", "DO1", "Superintendência de Transporte Ferroviário", []),
        clip.Filtro("ANTT-Deliberacoes", "DO1", "Diretoria Colegiada", ["Deliberação"]),
        clip.Filtro("ANTT-Portarias", "DO1", "Agência Nacional de Transportes Terrestres", ["Portaria"]),
        clip.Filtro("ANTT-Instrucoes", "DO1", "Agência Nacional de Transportes Terrestres", ["Instrução Normativa"]),
        clip.Filtro("ANTT-Retificacoes", "DO1", "Agência Nacional de Transportes Terrestres", ["Retificação"]),
    ]


def make_config(tmp_path, **over):
    base = dict(
        inlabs_email="u@x", inlabs_password="p", filtros=make_filtros(), lookback_days=2,
        smtp_host="localhost", smtp_port=2525, smtp_user="u", smtp_pass="p",
        email_from="from@x", email_to=["to@x"], subject_prefix="[DOU][ANTT][SUFER]",
        db_path=str(tmp_path / "db" / "clipping.sqlite"),
    )
    base.update(over)
    return clip.Config(**base)


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def test_parse_articles_extracts_attributes_and_body():
    pubs = clip.parse_articles(PORTARIA_SUFER, "2026-09-16-DO1.zip", EDITION)
    assert len(pubs) == 1
    p = pubs[0]
    assert p.article_id == "1001"
    assert p.art_type == "Portaria"
    assert p.art_category == SUFER
    assert p.edition_number == "177" and p.number_page == "12" and p.pub_name == "DO1"
    assert p.pdf_page.startswith("https://pesquisa.in.gov.br/")
    assert p.identifica.startswith("PORTARIA Nº 45")
    assert "Art. 1º Fica autorizada" in p.texto
    assert "A-B | 120" in p.texto              # tabela vira linhas com " | "
    assert p.assina == "JOÃO DA SILVA" and p.cargo == "Superintendente"
    assert '<p class="identifica">' in p.texto_html
    assert not p.is_fragment


def test_parse_zip_handles_many_xmls_and_fragments():
    pubs = clip.parse_zip(make_zip(PORTARIA_SUFER, DELIBERACAO, ANEXO_SUFER, OUTRO_ORGAO),
                          "2026-09-16-DO1.zip", EDITION)
    assert [p.article_id for p in pubs] == ["1001", "1002", "1005", "2001"]
    assert pubs[2].is_fragment       # Identifica vazia + artType Anexo


def test_invalid_xml_raises():
    with pytest.raises(Exception):
        clip.parse_articles(b"<xml><article", "f.zip", EDITION)


# ---------------------------------------------------------------------------
# filtros
# ---------------------------------------------------------------------------

def test_filters_by_orgao_and_art_type():
    pubs = clip.parse_zip(
        make_zip(PORTARIA_SUFER, DELIBERACAO, RESOLUCAO_ANTT, DESPACHO_SUFER, ANEXO_SUFER, OUTRO_ORGAO),
        "2026-09-16-DO1.zip", EDITION)
    matches = clip.apply_filters(pubs, make_filtros())
    by_id = {m.pub.article_id: m.filtros for m in matches}
    assert by_id == {
        "1001": ["SUFER-Todas", "ANTT-Portarias"],   # sobreposicao aceita
        "1002": ["ANTT-Deliberacoes"],
        "1004": ["SUFER-Todas"],                     # qualquer tipo da SUFER
    }
    # Resolucao da ANTT nao esta em nenhum filtro; anexo e fragmento; outro orgao fora


def test_filter_is_accent_and_case_insensitive():
    f = clip.Filtro("x", "DO1", "superintendencia de transporte ferroviario", ["portaria"])
    assert f.aceita(SUFER, "Portaria")
    assert not f.aceita(SUFER, "Despacho")
    assert not f.aceita(DIRETORIA, "Portaria")


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def test_storage_dedups_by_article_id_and_tracks_emailed(tmp_path):
    db = clip.Storage(str(tmp_path / "a.sqlite"))
    pubs = clip.parse_articles(PORTARIA_SUFER, "f.zip", EDITION)
    m = clip.Match(pubs[0], ["SUFER-Todas"])
    assert db.add_match(m) is True
    assert db.add_match(m) is False
    assert len(db.pending_matches()) == 1
    db.mark_emailed(["1001"])
    assert db.pending_matches() == []
    db.set("k", "v")
    assert db.get("k") == "v"
    db.set("k", None)
    assert db.get("k") is None


def test_storage_migrates_legacy_schema(tmp_path):
    import sqlite3
    path = str(tmp_path / "legacy.sqlite")
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, run_ts TEXT, run_date TEXT,
            files_processed INTEGER, matches_found INTEGER, email_sent INTEGER, notes TEXT);
        CREATE TABLE processed_files (file_name TEXT PRIMARY KEY, processed_date TEXT, processed_ts TEXT);
        CREATE TABLE matches (id INTEGER PRIMARY KEY AUTOINCREMENT, run_date TEXT, filter_name TEXT,
            source_file TEXT, keyword_hit TEXT, publication_title TEXT, publication_date TEXT,
            text_snippet TEXT, full_text TEXT, created_ts TEXT);
        INSERT INTO processed_files VALUES ('2026-09-15-DO1.zip','2026-09-15','x');
        INSERT INTO runs (run_ts,run_date,files_processed,matches_found,email_sent) VALUES ('t','d',1,0,0);
    """)
    con.commit(); con.close()
    db = clip.Storage(path)
    tables = {r[0] for r in db.con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"legacy_runs", "legacy_processed_files", "legacy_matches", "runs", "matches", "state"} <= tables
    assert db.con.execute("SELECT count(*) FROM legacy_processed_files").fetchone()[0] == 1
    assert db.file_processed("2026-09-15-DO1.zip") is False   # tabela nova comeca vazia


# ---------------------------------------------------------------------------
# nomes de arquivo
# ---------------------------------------------------------------------------

def test_edition_keys():
    assert clip.zip_edition_key("2026-09-16-DO1.zip") == ("2026-09-16", "DO1")
    assert clip.zip_edition_key("2026-09-16-DO1E.zip") == ("2026-09-16", "DO1")
    assert clip.zip_edition_key("2026-09-16-DO1A.zip") == ("2026-09-16", "DO1")
    assert clip.zip_edition_key("2026-09-16-DO2.zip") == ("2026-09-16", "DO2")
    assert clip.zip_edition_key("2026_09_16_ASSINADO_do1.pdf") is None
    assert clip.pdf_edition_key("2026_08_01_ASSINADO_do1_extra_C.pdf") == ("2026-08-01", "DO1")
    assert clip.pdf_edition_key("2026_08_01_ASSINADO_do3.pdf") == ("2026-08-01", "DO3")
    assert clip.pdf_edition_key("2026-08-01-DO1.zip") is None


# ---------------------------------------------------------------------------
# e-mail (render)
# ---------------------------------------------------------------------------

def test_render_matches_html_keeps_dou_formatting(tmp_path):
    db = clip.Storage(str(tmp_path / "r.sqlite"))
    pubs = clip.parse_articles(PORTARIA_SUFER, "2026-09-16-DO1.zip", EDITION)
    db.add_match(clip.Match(pubs[0], ["SUFER-Todas", "ANTT-Portarias"]))
    html = clip.render_matches_html(db.pending_matches(), "[DOU]")
    assert "Filtro: SUFER-Todas, ANTT-Portarias" in html
    assert "Publicado em 16/09/2026 | Secao 1 | Edicao 177 | Pagina 12" in html
    assert "Ver no DOU" in html
    assert "font-weight:bold" in html            # p.identifica / p.assina
    assert "<table" in html and "A-B" in html
    assert "<script" not in html


def test_sanitize_html_strips_unknown_tags_and_attrs():
    out = clip.sanitize_html('<p class="ementa" onclick="x()">a <a href="h">b</a> <script>1</script></p>')
    assert 'onclick' not in out and '<a' not in out and '<script' not in out
    assert 'font-style:italic' in out and 'a b 1' in out


# ---------------------------------------------------------------------------
# PDF: apenas alerta
# ---------------------------------------------------------------------------

def test_find_orgao_mentions_handles_accents_and_line_breaks():
    text = ("Portaria... AGÊNCIA NACIONAL DE\nTRANSPORTES TERRESTRES resolve...\n"
            "Superintendência de Transporte\nFerroviário, no uso...")
    orgaos, snippets = clip.find_orgao_mentions(text, make_filtros())
    assert orgaos == ["Superintendência de Transporte Ferroviário",
                      "Agência Nacional de Transportes Terrestres"]
    assert any("resolve" in s for s in snippets)
    assert clip.find_orgao_mentions("nada aqui", make_filtros()) == ([], [])


# ---------------------------------------------------------------------------
# servidor INLABS falso: exercita o cliente HTTP real (login, cookie, listagem,
# download, sessao expirada, indisponibilidade)
# ---------------------------------------------------------------------------

class FakeInlabs:
    def __init__(self):
        self.files = {}          # (date_iso, name) -> bytes
        self.fail_next = 0       # respostas 503 antes de voltar ao normal
        self.drop_next = 0       # conexoes derrubadas antes de voltar ao normal
        self.expire_sessions = False
        self.requests = []
        self.valid = ("u@x", "p")

    def start(self):
        srv = self
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silencio
                pass
            def _send(self, code, body=b"", ctype="text/html; charset=utf-8", extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
            def _logged(self):
                return "inlabs_session_cookie=ok" in self.headers.get("Cookie", "") and not srv.expire_sessions
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                form = parse_qs(self.rfile.read(n).decode())
                srv.requests.append(("POST", self.path))
                if srv.drop_next:
                    srv.drop_next -= 1
                    self.connection.close(); return
                if srv.fail_next:
                    srv.fail_next -= 1
                    self._send(503, b"<html>Manutencao programada</html>"); return
                if (form.get("email", [""])[0], form.get("password", [""])[0]) == srv.valid:
                    srv.expire_sessions = False
                    self._send(200, b"<html>Bem-vindo <a href='sair.php'>Sair</a></html>",
                               extra={"Set-Cookie": "inlabs_session_cookie=ok; Path=/"})
                else:
                    self._send(200, b"<html><form action='logar.php'><input name='password'></form>Senha invalida</html>")
            def do_GET(self):
                u = urlparse(self.path); q = parse_qs(u.query)
                srv.requests.append(("GET", self.path))
                if srv.fail_next:
                    srv.fail_next -= 1
                    self._send(502, b"Bad gateway"); return
                if not self._logged():
                    self._send(200, b"<html><form action='logar.php'><input name='password'></form></html>"); return
                d = q.get("p", [""])[0]
                if "dl" in q:
                    name = q["dl"][0]
                    data = srv.files.get((d, name))
                    if data is None:
                        self._send(404, b"nao encontrado"); return
                    self._send(200, data, "application/octet-stream")
                    return
                links = "".join(f'<a title="Baixar Arquivo" href="?p={d}&amp;dl={n}">{n}</a>'
                                for (dd, n) in srv.files if dd == d)
                self._send(200, f"<html><body>{links}</body></html>".encode())
        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()


@pytest.fixture
def inlabs(monkeypatch):
    srv = FakeInlabs().start()
    base = f"http://127.0.0.1:{srv.port}"
    monkeypatch.setattr(clip.InlabsClient, "LOGIN_URL", f"{base}/logar.php")
    monkeypatch.setattr(clip.InlabsClient, "INDEX_URL", f"{base}/index.php")
    monkeypatch.setattr(clip.InlabsClient, "RETRY_WAITS", (0, 0, 0, 0))
    yield srv
    srv.stop()


class FakeMailer:
    def __init__(self, cfg, fail=False):
        self.sent = []
        self.fail = fail
    def send(self, subject, html):
        if self.fail:
            raise RuntimeError("SMTP recusou")
        self.sent.append((subject, html))


@pytest.fixture
def mailer(monkeypatch):
    box = {}
    def factory(cfg):
        box["m"] = FakeMailer(cfg, fail=box.get("fail", False))
        return box["m"]
    monkeypatch.setattr(clip, "Mailer", factory)
    return box


def freeze_today(monkeypatch, d: date, hour: int = 8):
    """Congela o relogio BRT do sistema em d às hour:00."""
    from zoneinfo import ZoneInfo
    fixed = datetime(d.year, d.month, d.day, hour, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))
    monkeypatch.setattr(clip, "now_brt", lambda: fixed)
    monkeypatch.setattr(clip, "today_brt", lambda: d)


def test_client_login_list_download(inlabs):
    inlabs.files[("2026-09-16", "2026-09-16-DO1.zip")] = make_zip(PORTARIA_SUFER)
    inlabs.files[("2026-09-16", "2026_09_16_ASSINADO_do1.pdf")] = b"%PDF"
    c = clip.InlabsClient("u@x", "p")
    c.login()
    assert c.list_files(date(2026, 9, 16)) == ["2026-09-16-DO1.zip", "2026_09_16_ASSINADO_do1.pdf"]
    assert c.list_files(date(2026, 9, 15)) == []
    assert c.download(date(2026, 9, 16), "2026-09-16-DO1.zip") == inlabs.files[("2026-09-16", "2026-09-16-DO1.zip")]
    assert c.download(date(2026, 9, 16), "nao-existe.zip") is None
    assert any(("origem" in k.lower()) for k in c.session.headers)


def test_client_wrong_password_is_explicit_auth_error(inlabs):
    c = clip.InlabsClient("u@x", "errada")
    with pytest.raises(clip.InlabsAuthError):
        c.login()


def test_client_retries_dropped_connections_and_5xx(inlabs):
    inlabs.drop_next = 2
    c = clip.InlabsClient("u@x", "p")
    c.login()                      # 2 quedas, depois sucesso
    inlabs.fail_next = 1
    assert c.list_files(date(2026, 9, 16)) == []   # 1x 502, depois sucesso


def test_client_gives_up_as_unavailable(inlabs):
    inlabs.fail_next = 10
    c = clip.InlabsClient("u@x", "p")
    with pytest.raises(clip.InlabsUnavailable):
        c.login()


def test_client_relogins_when_session_expires(inlabs):
    inlabs.files[("2026-09-16", "2026-09-16-DO1.zip")] = make_zip(PORTARIA_SUFER)
    c = clip.InlabsClient("u@x", "p")
    c.login()
    inlabs.expire_sessions = True
    assert c.list_files(date(2026, 9, 16)) == ["2026-09-16-DO1.zip"]
    posts = [r for r in inlabs.requests if r[0] == "POST"]
    assert len(posts) == 2


# ---------------------------------------------------------------------------
# execucao completa
# ---------------------------------------------------------------------------

def test_run_end_to_end_sends_matches_and_persists(tmp_path, inlabs, mailer, monkeypatch):
    freeze_today(monkeypatch, date(2026, 9, 16))       # quarta-feira
    inlabs.files[("2026-09-16", "2026-09-16-DO1.zip")] = make_zip(PORTARIA_SUFER, OUTRO_ORGAO, ANEXO_SUFER)
    inlabs.files[("2026-09-15", "2026-09-15-DO1.zip")] = make_zip(DELIBERACAO)
    inlabs.files[("2026-09-15", "2026-09-15-DO1E.zip")] = make_zip(RESOLUCAO_ANTT)
    cfg = make_config(tmp_path)

    assert clip.cmd_run(cfg, date(2026, 9, 16), send_email=True, force=False) == 0
    m = mailer["m"]
    assert len(m.sent) == 1
    subject, html = m.sent[0]
    assert subject == "[DOU][ANTT][SUFER] 16/09/2026 - 2 publicacao(oes)"
    assert "DELIBERAÇÃO Nº 300" in html and "PORTARIA Nº 45" in html
    assert "RESOLUÇÃO" not in html and "Nada a ver" not in html

    db = clip.Storage(cfg.db_path)
    assert db.pending_matches() == []
    assert db.con.execute("SELECT count(*) FROM matches").fetchone()[0] == 2
    assert db.con.execute("SELECT count(*) FROM processed_files").fetchone()[0] == 3
    run = db.con.execute("SELECT * FROM runs ORDER BY id DESC").fetchone()
    assert (run["status"], run["files_processed"], run["new_matches"], run["emails_sent"]) == ("ok", 3, 2, 1)
    assert db.get("last_success_ts") and db.get("last_email_date") == "2026-09-16"

    # segunda execucao no mesmo dia: nada novo, nenhum e-mail; so a extra (DO1E) e
    # re-baixada para checar o tamanho, as edicoes base nao
    n_before = len(inlabs.requests)
    assert clip.cmd_run(cfg, date(2026, 9, 16), send_email=True, force=False) == 0
    assert len(mailer["m"].sent) == 0
    dl = [p for _, p in inlabs.requests[n_before:] if "dl=" in p]
    assert dl == ["/index.php?p=2026-09-15&dl=2026-09-15-DO1E.zip"]

    # edicao extra aparece mais tarde no mesmo dia -> e-mail imediato so com ela
    inlabs.files[("2026-09-16", "2026-09-16-DO1E.zip")] = make_zip(DESPACHO_SUFER)
    assert clip.cmd_run(cfg, date(2026, 9, 16), send_email=True, force=False) == 0
    assert len(mailer["m"].sent) == 1
    assert "1 publicacao(oes)" in mailer["m"].sent[0][0]
    assert "Defiro o pedido" in mailer["m"].sent[0][1]


def test_daily_confirmation_only_first_weekday_run(tmp_path, inlabs, mailer, monkeypatch):
    cfg = make_config(tmp_path)
    inlabs.files[("2026-09-16", "2026-09-16-DO1.zip")] = make_zip(OUTRO_ORGAO)
    inlabs.files[("2026-09-21", "2026-09-21-DO1.zip")] = make_zip(OUTRO_ORGAO)
    freeze_today(monkeypatch, date(2026, 9, 16))       # quarta, 08:00, edicao ja existe
    clip.cmd_run(cfg, date(2026, 9, 16), True, False)
    assert [s for s, _ in mailer["m"].sent] == ["[DOU][ANTT][SUFER] 16/09/2026 - sem novidades"]
    assert "Edicao de hoje verificada: DO1" in mailer["m"].sent[0][1]
    clip.cmd_run(cfg, date(2026, 9, 16), True, False)
    assert mailer["m"].sent == []                       # ja confirmou hoje
    freeze_today(monkeypatch, date(2026, 9, 19))       # sabado: silencio
    clip.cmd_run(cfg, date(2026, 9, 19), True, False)
    assert mailer["m"].sent == []
    freeze_today(monkeypatch, date(2026, 9, 21))       # segunda: confirma de novo
    clip.cmd_run(cfg, date(2026, 9, 21), True, False)
    assert len(mailer["m"].sent) == 1


def test_daily_confirmation_waits_for_todays_edition(tmp_path, inlabs, mailer, monkeypatch):
    cfg = make_config(tmp_path)
    freeze_today(monkeypatch, date(2026, 9, 16), hour=7)   # edicao ainda nao saiu
    clip.cmd_run(cfg, date(2026, 9, 16), True, False)
    assert mailer["m"].sent == []
    inlabs.files[("2026-09-16", "2026-09-16-DO1.zip")] = make_zip(PORTARIA_SUFER)
    freeze_today(monkeypatch, date(2026, 9, 16), hour=10)  # saiu, com achado
    clip.cmd_run(cfg, date(2026, 9, 16), True, False)
    assert [s for s, _ in mailer["m"].sent] == ["[DOU][ANTT][SUFER] 16/09/2026 - 1 publicacao(oes)"]
    # dia sem edicao (feriado): confirma depois das 12h com o aviso adequado
    freeze_today(monkeypatch, date(2026, 9, 17), hour=13)
    clip.cmd_run(cfg, date(2026, 9, 17), True, False)
    assert len(mailer["m"].sent) == 1
    assert "Nenhuma edicao publicada hoje ate 13:00" in mailer["m"].sent[0][1]


def test_bad_file_does_not_block_other_files(tmp_path, inlabs, mailer, monkeypatch):
    freeze_today(monkeypatch, date(2026, 9, 16))
    inlabs.files[("2026-09-15", "2026-09-15-DO1.zip")] = b"isto nao e um zip"
    inlabs.files[("2026-09-16", "2026-09-16-DO1.zip")] = make_zip(PORTARIA_SUFER)
    cfg = make_config(tmp_path)
    rc = clip.cmd_run(cfg, date(2026, 9, 16), True, False)
    assert rc == 1                                          # erro explicito...
    assert len(mailer["m"].sent) == 1                        # ...mas o achado de D0 foi enviado
    db = clip.Storage(cfg.db_path)
    run = db.con.execute("SELECT * FROM runs").fetchone()
    assert run["status"] == "error" and "BadZipFile" in run["notes"]
    assert not db.file_processed("2026-09-15-DO1.zip")      # arquivo ruim volta na proxima
    assert db.file_processed("2026-09-16-DO1.zip")
    assert db.get("last_success_ts")                        # INLABS estava acessivel


def test_illegal_control_bytes_in_xml_are_tolerated():
    xml = PORTARIA_SUFER.replace(b"Fica autorizada", b"Fica \x0c autorizada \x1f")
    pubs = clip.parse_articles(xml, "f.zip", EDITION)
    assert "Fica  autorizada" in pubs[0].texto or "Fica autorizada" in pubs[0].texto


def test_extra_zip_is_rechecked_when_size_changes(tmp_path, inlabs, mailer, monkeypatch):
    freeze_today(monkeypatch, date(2026, 9, 16))
    inlabs.files[("2026-09-16", "2026-09-16-DO1.zip")] = make_zip(OUTRO_ORGAO)
    inlabs.files[("2026-09-16", "2026-09-16-DO1E.zip")] = make_zip(OUTRO_ORGAO)
    cfg = make_config(tmp_path)
    clip.cmd_run(cfg, date(2026, 9, 16), True, False)
    assert "sem novidades" in mailer["m"].sent[0][0]
    # INLABS regenera o DO1E.zip com uma segunda extra do dia
    inlabs.files[("2026-09-16", "2026-09-16-DO1E.zip")] = make_zip(OUTRO_ORGAO, DESPACHO_SUFER)
    n = len(inlabs.requests)
    clip.cmd_run(cfg, date(2026, 9, 16), True, False)
    dl = [p for _, p in inlabs.requests[n:] if "dl=" in p]
    assert len(dl) == 1 and "DO1E" in dl[0]                 # so a extra e re-baixada
    assert len(mailer["m"].sent) == 1 and "Defiro o pedido" in mailer["m"].sent[0][1]
    # tamanho igual: baixa, mas nao reprocessa nem reenvia
    clip.cmd_run(cfg, date(2026, 9, 16), True, False)
    assert mailer["m"].sent == []


def test_listing_http_error_is_unavailable(inlabs, monkeypatch):
    c = clip.InlabsClient("u@x", "p")
    c.login()
    class R:  # resposta 403 com HTML de erro
        status_code, text, headers, content = 403, "<html>Forbidden</html>", {"content-type": "text/html"}, b""
    monkeypatch.setattr(c.session, "request", lambda *a, **k: R())
    with pytest.raises(clip.InlabsUnavailable):
        c.list_files(date(2026, 9, 16))


def test_base_pdf_only_used_for_past_days(tmp_path, inlabs, mailer, monkeypatch):
    freeze_today(monkeypatch, date(2026, 9, 16))
    inlabs.files[("2026-09-16", "2026_09_16_ASSINADO_do1.pdf")] = b"%PDF hoje"
    inlabs.files[("2026-09-15", "2026_09_15_ASSINADO_do1.pdf")] = b"%PDF ontem"
    monkeypatch.setattr(clip, "pdf_alert", lambda *a: None)
    cfg = make_config(tmp_path)
    clip.cmd_run(cfg, date(2026, 9, 16), True, False)
    names = [r[0] for r in clip.Storage(cfg.db_path).con.execute("SELECT file_name FROM processed_files")]
    assert names == ["2026_09_15_ASSINADO_do1.pdf"]


def test_pending_emails_are_chunked(tmp_path, inlabs, mailer, monkeypatch):
    freeze_today(monkeypatch, date(2026, 9, 16))
    monkeypatch.setattr(clip, "EMAIL_MAX_ROWS", 2)
    xmls = [article_xml(str(3000 + i), "Despacho", SUFER, f"DESPACHO {i}", f"<p>texto {i}</p>") for i in range(5)]
    inlabs.files[("2026-09-16", "2026-09-16-DO1.zip")] = make_zip(*xmls)
    cfg = make_config(tmp_path)
    clip.cmd_run(cfg, date(2026, 9, 16), True, False)
    subjects = [s for s, _ in mailer["m"].sent]
    assert len(subjects) == 3 and subjects[0].endswith("5 publicacao(oes) (parte 1/3)")
    assert clip.Storage(cfg.db_path).pending_matches() == []


def test_no_email_flag_keeps_matches_pending(tmp_path, inlabs, mailer, monkeypatch):
    freeze_today(monkeypatch, date(2026, 9, 16))
    inlabs.files[("2026-09-16", "2026-09-16-DO1.zip")] = make_zip(PORTARIA_SUFER)
    cfg = make_config(tmp_path)
    clip.cmd_run(cfg, date(2026, 9, 16), send_email=False, force=False)
    assert "m" not in mailer
    assert len(clip.Storage(cfg.db_path).pending_matches()) == 1
    # proxima execucao com e-mail envia o pendente
    clip.cmd_run(cfg, date(2026, 9, 16), send_email=True, force=False)
    assert len(mailer["m"].sent) == 1


def test_smtp_failure_is_explicit_and_keeps_pending(tmp_path, inlabs, mailer, monkeypatch):
    freeze_today(monkeypatch, date(2026, 9, 16))
    inlabs.files[("2026-09-16", "2026-09-16-DO1.zip")] = make_zip(PORTARIA_SUFER)
    cfg = make_config(tmp_path)
    mailer["fail"] = True
    assert clip.cmd_run(cfg, date(2026, 9, 16), True, False) == 1
    db = clip.Storage(cfg.db_path)
    assert len(db.pending_matches()) == 1
    assert db.con.execute("SELECT status FROM runs").fetchone()[0] == "error"


def test_unavailable_exits_zero_and_alerts_after_threshold(tmp_path, inlabs, mailer, monkeypatch):
    freeze_today(monkeypatch, date(2026, 9, 16))
    cfg = make_config(tmp_path, alerta_indisponibilidade_horas=24)
    inlabs.fail_next = 100
    # 1) nunca teve sucesso: sem alerta
    assert clip.cmd_run(cfg, date(2026, 9, 16), True, False) == 0
    assert mailer["m"].sent == []
    db = clip.Storage(cfg.db_path)
    assert db.con.execute("SELECT status FROM runs").fetchone()[0] == "inlabs_unavailable"
    # 2) ultimo sucesso ha 30h: alerta
    db.set("last_success_ts", (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat())
    db.close()
    assert clip.cmd_run(cfg, date(2026, 9, 16), True, False) == 0
    assert "ALERTA - INLABS inacessivel ha 30h" in mailer["m"].sent[0][0]
    # 3) de novo no mesmo dia: nao repete
    assert clip.cmd_run(cfg, date(2026, 9, 16), True, False) == 0
    assert mailer["m"].sent == []
    # 4) voltou: run ok limpa o alerta
    inlabs.fail_next = 0
    assert clip.cmd_run(cfg, date(2026, 9, 16), True, False) == 0
    assert clip.Storage(cfg.db_path).get("unavailable_alert_ts") is None


def test_wrong_credentials_fail_the_run(tmp_path, inlabs, mailer, monkeypatch):
    freeze_today(monkeypatch, date(2026, 9, 16))
    cfg = make_config(tmp_path, inlabs_password="errada")
    assert clip.cmd_run(cfg, date(2026, 9, 16), True, False) == 1


def test_pdf_only_extra_generates_alert(tmp_path, inlabs, mailer, monkeypatch):
    freeze_today(monkeypatch, date(2026, 8, 1))        # sabado
    pdf_text = "PORTARIA N 1\nAgência Nacional de Transportes Terrestres resolve..."
    monkeypatch.setattr(clip, "pdf_alert",
                        lambda data, name, d, filtros: (
                            clip.Publication(article_id=f"pdf:{name}", source_file=name, edition_date=d,
                                             identifica="EDICAO EM PDF -- CONFERIR", texto=pdf_text,
                                             kind="pdf_alert", art_type="Edicao em PDF")
                            if b"ANTT" in data else None))
    inlabs.files[("2026-08-01", "2026_08_01_ASSINADO_do1_extra_C.pdf")] = b"%PDF ANTT"
    inlabs.files[("2026-08-01", "2026_08_01_ASSINADO_do1_extra_D.pdf")] = b"%PDF nada"
    cfg = make_config(tmp_path)
    assert clip.cmd_run(cfg, date(2026, 8, 1), True, False) == 0
    assert len(mailer["m"].sent) == 1
    assert "extra_C" in mailer["m"].sent[0][1] and "extra_D" not in mailer["m"].sent[0][1]


def test_pdf_ignored_when_zip_exists(tmp_path, inlabs, mailer, monkeypatch):
    freeze_today(monkeypatch, date(2026, 9, 16))
    inlabs.files[("2026-09-16", "2026-09-16-DO1.zip")] = make_zip(OUTRO_ORGAO)
    inlabs.files[("2026-09-16", "2026-09-16-DO1E.zip")] = make_zip(OUTRO_ORGAO)
    inlabs.files[("2026-09-16", "2026_09_16_ASSINADO_do1.pdf")] = b"%PDF"
    inlabs.files[("2026-09-16", "2026_09_16_ASSINADO_do1_extra_A.pdf")] = b"%PDF"
    cfg = make_config(tmp_path)
    clip.cmd_run(cfg, date(2026, 9, 16), True, False)
    names = [r[0] for r in clip.Storage(cfg.db_path).con.execute("SELECT file_name FROM processed_files")]
    assert sorted(names) == ["2026-09-16-DO1.zip", "2026-09-16-DO1E.zip"]


def test_backfill_marks_matches_as_not_to_email(tmp_path, inlabs, mailer, monkeypatch):
    inlabs.files[("2026-09-14", "2026-09-14-DO1.zip")] = make_zip(PORTARIA_SUFER)
    inlabs.files[("2026-09-15", "2026-09-15-DO1.zip")] = make_zip(DELIBERACAO)
    monkeypatch.setattr(clip.time, "sleep", lambda s: None)
    cfg = make_config(tmp_path)
    assert clip.cmd_backfill(cfg, date(2026, 9, 14), date(2026, 9, 15)) == 0
    db = clip.Storage(cfg.db_path)
    assert db.con.execute("SELECT count(*) FROM matches WHERE emailed_ts='backfill'").fetchone()[0] == 2
    assert db.pending_matches() == []


def test_load_config_from_yaml(tmp_path, monkeypatch):
    y = tmp_path / "c.yml"
    y.write_text("""
inlabs: {email: "", password: ""}
lookback_days: 1
filtros:
  - {nome: A, secao: DO1, orgao: "Superintendência de Transporte Ferroviário", art_types: []}
  - {nome: B, secao: DO1, orgao: "Diretoria Colegiada", art_types: ["Deliberação"]}
mail: {smtp_host: smtp.gmail.com, smtp_port: 587, smtp_user: "", smtp_pass: "",
       from_email: a@b, to_emails: [c@d], subject_prefix: "[X]"}
storage: {db_path: data/x.sqlite}
""", encoding="utf-8")
    monkeypatch.setenv("INLABS_EMAIL", "e@e"); monkeypatch.setenv("INLABS_PASSWORD", "pw")
    monkeypatch.setenv("SMTP_USER", "su"); monkeypatch.setenv("SMTP_PASS", "sp")
    cfg = clip.load_config(str(y))
    assert cfg.inlabs_email == "e@e" and cfg.smtp_pass == "sp" and cfg.lookback_days == 1
    assert [f.nome for f in cfg.filtros] == ["A", "B"] and cfg.filtros[1].art_types == ["Deliberação"]
    assert cfg.secoes == ["DO1"] and cfg.alerta_indisponibilidade_horas == 24


def test_inspect_reports_editions_and_filter_hits(tmp_path, inlabs, monkeypatch, capsys):
    inlabs.files[("2026-09-15", "2026-09-15-DO1.zip")] = make_zip(DELIBERACAO, OUTRO_ORGAO)
    inlabs.files[("2026-09-16", "2026-09-16-DO1E.zip")] = make_zip(
        article_xml("7001", "Portaria", SUFER, "PORTARIA Nº 1", "<p>a</p>", pub_name="DO1E", edition="176-A"),
        article_xml("7002", "Portaria", ANTT, "PORTARIA Nº 2", "<p>b</p>", pub_name="DO1E", edition="176-B"))
    inlabs.files[("2026-09-16", "2026_09_16_ASSINADO_do1_extra_A.pdf")] = b"%PDF"
    cfg = make_config(tmp_path)
    assert clip.cmd_inspect(cfg, date(2026, 9, 16), "Transportes", days=2) == 0
    out = capsys.readouterr().out
    assert "2026-09-16-DO1E.zip: edicoes (pubName, editionNumber): DO1E/176-A=1, DO1E/176-B=1" in out
    assert "outros: ['2026_09_16_ASSINADO_do1_extra_A.pdf']" in out
    assert "SUFER-Todas: 1" in out and "ANTT-Portarias: 2" in out and "ANTT-Deliberacoes: 1" in out
    assert "2026-09-15 DO1 [Deliberação] DELIBERAÇÃO Nº 300" in out
