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
        clip.Filtro("ANTT-Deliberacoes", "DO1", "Agência Nacional de Transportes Terrestres", ["Deliberação"]),
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
        self.login_404 = False       # out/2026: /logar.php sumiu durante a manutencao

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
                if srv.login_404:
                    self._send(404, b"<html><h1>Not Found</h1></html>"); return
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


def portal_item(url_title, art_type, hierarchy, title, page="10", edition="204",
                pub="DO1", pub_date="28/10/2026"):
    """Item do jsonArray do leiturajornal (14 chaves, como no portal real)."""
    return {"pubName": pub, "urlTitle": url_title, "numberPage": page, "subTitulo": "",
            "titulo": "", "title": title, "pubDate": pub_date,
            "content": f"{title} texto truncado pelo portal...", "editionNumber": edition,
            "hierarchyLevelSize": hierarchy.count("/") + 1, "artType": art_type,
            "pubOrder": "DO100001:00001", "hierarchyStr": hierarchy,
            "hierarchyList": hierarchy.split("/")}


class FakePortal:
    """www.in.gov.br: /leiturajornal?data=DD-MM-AAAA&secao=do1 e /web/dou/-/<urlTitle>.
    Fora do ar por padrao, para nenhum teste do INLABS acionar a rede real."""
    def __init__(self):
        self.down = True
        self.index = {}          # (date_iso, secao_lower) -> [items]
        self.acts = {}           # urlTitle -> HTML interno de .texto-dou
        self.broken_index = False
        self.requests = []

    def add(self, d, secao, item, texto_html):
        self.index.setdefault((d.isoformat(), secao.lower()), []).append(item)
        self.acts[item["urlTitle"]] = texto_html

    def start(self):
        import json as _json
        srv = self
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass
            def _send(self, code, body):
                body = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/html;charset=UTF-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_GET(self):
                u = urlparse(self.path); q = parse_qs(u.query)
                srv.requests.append(self.path)
                if srv.down:
                    self._send(503, "<html>Service Unavailable</html>"); return
                if u.path == "/leiturajornal":
                    dd = q.get("data", [""])[0]
                    d_iso = "-".join(reversed(dd.split("-")))
                    secao = q.get("secao", [""])[0]
                    base = secao[:3].upper()
                    tnd = {f"{base}{s}": bool(srv.index.get((d_iso, f"{base}{s}".lower())))
                           for s in ("E", "A", "ESP")}
                    params = {"typeNormDay": tnd, "idPortletInstance": "x", "dateUrl": dd,
                              "section": secao.upper(), "jsonArray": srv.index.get((d_iso, secao), [])}
                    script = ("" if srv.broken_index else
                              f'<script id="params" type="application/json">\n  {_json.dumps(params)}\n</script>')
                    self._send(200, "<html><body><div id='leitura'></div>"
                               '<script id="_br_com_seatecnologia_in_buscadou_BuscaDouPortlet_params" '
                               'type="application/json">{"jsonArray":[]}</script>'
                               f"{script}</body></html>")
                    return
                if u.path.startswith("/web/dou/-/"):
                    texto = srv.acts.get(u.path[len("/web/dou/-/"):])
                    if texto is None:
                        self._send(404, "<html>Pagina nao encontrada</html>"); return
                    self._send(200, "<html><body><article><div class='detalhes-dou'>Publicado em: "
                               "28/10/2026 | Edição: 204</div>"
                               f'<div class="texto-dou"> {texto} </div>'
                               "<p class='informacao-conteudo-dou'>Este conteúdo não substitui o "
                               "publicado na versão certificada.</p></article></body></html>")
                    return
                self._send(404, "<html>?</html>")
        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()


@pytest.fixture(autouse=True)
def portal(monkeypatch):
    srv = FakePortal().start()
    base = f"http://127.0.0.1:{srv.port}"
    monkeypatch.setattr(clip.PortalClient, "INDEX_URL", f"{base}/leiturajornal")
    monkeypatch.setattr(clip.PortalClient, "ACT_URL", f"{base}/web/dou/-/")
    monkeypatch.setattr(clip.PortalClient, "RETRY_WAITS", (0, 0))
    yield srv
    srv.stop()


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


def test_client_login_404_is_unavailable_not_auth_error(inlabs):
    # 21/10/2026: INLABS em manutencao, POST /logar.php passou a responder 404
    # sem texto de manutencao. Nao e senha errada: deve ser transitorio.
    inlabs.login_404 = True
    c = clip.InlabsClient("u@x", "p")
    with pytest.raises(clip.InlabsUnavailable):
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
    assert "Fonte:" not in html and "via INLABS." in html

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
    assert "ALERTA - INLABS e portal inacessiveis ha 30h" in mailer["m"].sent[0][0]
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
    assert clip.cmd_backfill(cfg, date(2026, 9, 14), date(2026, 9, 15), fonte="inlabs") == 0
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


def test_invalid_date_gives_clear_message(tmp_path, capsys):
    y = tmp_path / "c.yml"
    y.write_text("""
inlabs: {email: e, password: p}
filtros: [{nome: A, secao: DO1, orgao: X, art_types: []}]
mail: {smtp_host: h, smtp_port: 1, from_email: a@b, to_emails: [c@d]}
storage: {db_path: x.sqlite}
""", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        clip.main(["--config", str(y), "inspect", "--date", "2026-09-196"])
    assert "Data invalida: '2026-09-196'" in str(e.value)
    assert clip.parse_date("") == clip.today_brt() and clip.parse_date(" 2026-09-16 ") == date(2026, 9, 16)


def test_empty_extra_zip_does_not_hide_weekend_pdf(tmp_path, inlabs, mailer, monkeypatch):
    """Domingo 30/08/2026 real: DO1E.zip vazio (0 articles) ao lado de do1_extra_C.pdf."""
    freeze_today(monkeypatch, date(2026, 8, 31))
    inlabs.files[("2026-08-30", "2026-08-30-DO1E.zip")] = make_zip()          # ZIP sem XML
    inlabs.files[("2026-08-30", "2026_08_30_ASSINADO_do1_extra_C.pdf")] = b"%PDF ANTT"
    monkeypatch.setattr(clip, "pdf_alert",
                        lambda data, name, d, filtros: clip.Publication(
                            article_id=f"pdf:{name}", source_file=name, edition_date=d,
                            identifica="EDICAO EM PDF -- CONFERIR", texto="x", kind="pdf_alert",
                            art_type="Edicao em PDF") if b"ANTT" in data else None)
    cfg = make_config(tmp_path)
    assert clip.cmd_run(cfg, date(2026, 8, 31), True, False) == 0
    assert len(mailer["m"].sent) == 1 and "extra_C" in mailer["m"].sent[0][1]
    db = clip.Storage(cfg.db_path)
    assert db.file_processed("2026-08-30-DO1E.zip") and db.file_articles("2026-08-30-DO1E.zip") == 0
    # ZIP regenerado com conteudo: reprocessa; o PDF ja processado nao volta
    inlabs.files[("2026-08-30", "2026-08-30-DO1E.zip")] = make_zip(DESPACHO_SUFER)
    clip.cmd_run(cfg, date(2026, 8, 31), True, False)
    assert len(mailer["m"].sent) == 1 and "Defiro o pedido" in mailer["m"].sent[0][1]


# ---------------------------------------------------------------------------
# fallback: portal in.gov.br (leiturajornal + pagina do ato)
# ---------------------------------------------------------------------------

D_PORTAL = date(2026, 10, 28)                      # quarta-feira
SUROD = ANTT + "/Superintendência de Infraestrutura Rodoviária"

SUFER_URL = "decisao-sufer-n-130-de-26-de-outubro-de-2026-740000001"
SUFER_ITEM = portal_item(SUFER_URL, "Decisão", SUFER,
                         "DECISÃO SUFER Nº 130, DE 26 DE OUTUBRO DE 2026", page="150")
SUFER_TEXTO = ('<p class="identifica">DECISÃO SUFER Nº 130, DE 26 DE OUTUBRO DE 2026</p>'
               '<p class="dou-paragraph">O SUPERINTENDENTE DE TRANSPORTE FERROVIÁRIO decide:</p>'
               '<p class="dou-paragraph">Art. 1º Aprovar o projeto.</p>'
               '<p class="assina">MARIA SOUZA</p><p class="cargo">Superintendente</p>')
# Extra real de 20/10/2026: o portal omitiu "Diretoria Colegiada" da hierarquia
DELIB_URL = "deliberacao-antt-n-310-de-27-de-outubro-de-2026-740000002"
DELIB_ITEM = portal_item(DELIB_URL, "Deliberação", ANTT,
                         "DELIBERAÇÃO ANTT Nº 310, DE 27 DE OUTUBRO DE 2026",
                         page="3", edition="204-A", pub="DO1_EXTRA_A")
DELIB_TEXTO = ('<p class="identifica">DELIBERAÇÃO ANTT Nº 310, DE 27 DE OUTUBRO DE 2026</p>'
               '<p class="ementa">Autoriza nova seção.</p><p class="dou-paragraph">A Diretoria delibera.</p>')
OUTRO_ITEM = portal_item("portaria-n-9-740000003", "Portaria", OUTRO, "PORTARIA Nº 9", page="80")
SUROD_ITEM = portal_item("decisao-surod-n-1-740000004", "Decisão", SUROD, "DECISÃO SUROD Nº 1", page="151")


def fill_portal(portal):
    portal.down = False
    portal.add(D_PORTAL, "do1", SUFER_ITEM, SUFER_TEXTO)
    portal.add(D_PORTAL, "do1", OUTRO_ITEM, "<p>nada</p>")
    portal.add(D_PORTAL, "do1", SUROD_ITEM, "<p>rodovia</p>")
    portal.add(D_PORTAL, "do1e", DELIB_ITEM, DELIB_TEXTO)


def act_pages(portal, since=0):
    return [p for p in portal.requests[since:] if p.startswith("/web/dou/-/")]


def test_portal_client_parses_index_and_act(portal):
    fill_portal(portal)
    c = clip.PortalClient()
    items, edicoes = c.index(D_PORTAL, "DO1")
    assert [i["urlTitle"] for i in items] == [SUFER_URL, OUTRO_ITEM["urlTitle"], SUROD_ITEM["urlTitle"]]
    assert edicoes["DO1E"] is True and edicoes["DO1A"] is False
    assert "data=28-10-2026&secao=do1" in portal.requests[-1]
    assert c.index(date(2026, 10, 31), "DO1") == ([], {"DO1E": False, "DO1A": False, "DO1ESP": False})
    p = clip.portal_publication(DELIB_ITEM, D_PORTAL, "DO1")
    assert (p.article_id, p.source_file, p.pub_name, p.edition_number, p.number_page) == (
        f"portal:{DELIB_URL}", "portal:2026-10-28-DO1E", "DO1E", "204-A", "3")
    assert p.art_category == ANTT and p.pdf_page.endswith(f"/web/dou/-/{DELIB_URL}")
    clip.portal_fill_text(p, c.act_html(DELIB_URL))
    assert p.texto.startswith("DELIBERAÇÃO ANTT Nº 310") and "A Diretoria delibera." in p.texto
    assert p.ementa == "Autoriza nova seção." and '<p class="ementa">' in p.texto_html
    s = clip.portal_publication(SUFER_ITEM, D_PORTAL, "DO1")
    clip.portal_fill_text(s, c.act_html(SUFER_URL))
    assert (s.assina, s.cargo, s.pub_name) == ("MARIA SOUZA", "Superintendente", "DO1")


def test_portal_errors_unavailable_vs_format(portal):
    c = clip.PortalClient()
    with pytest.raises(clip.PortalUnavailable):          # 503 em todas as tentativas
        c.index(D_PORTAL, "DO1")
    portal.down = False
    with pytest.raises(clip.PortalFormatError):          # listado no indice, pagina 404
        c.act_html("nao-existe-1")
    portal.broken_index = True
    with pytest.raises(clip.PortalFormatError):          # pagina sem <script id="params">
        c.index(D_PORTAL, "DO1")


def test_run_falls_back_to_portal_when_inlabs_is_down(tmp_path, inlabs, mailer, monkeypatch, portal):
    freeze_today(monkeypatch, D_PORTAL)
    inlabs.login_404 = True
    fill_portal(portal)
    cfg = make_config(tmp_path)
    assert clip.cmd_run(cfg, D_PORTAL, True, False) == 0
    m = mailer["m"]
    assert len(m.sent) == 1
    subject, html = m.sent[0]
    assert subject == "[DOU][ANTT][SUFER] 28/10/2026 - 2 publicacao(oes)"
    assert "Fonte: portal in.gov.br" in html and "vieram do indice publico do DOU" in html
    assert "Art. 1º Aprovar o projeto." in html and "MARIA SOUZA" in html
    assert "Secao 1 - Extra | Edicao 204-A | Pagina 3" in html     # DO1_EXTRA_A -> DO1E
    assert "PORTARIA Nº 9" not in html and "SUROD" not in html
    # so as paginas dos atos que casaram com os filtros sao baixadas
    assert sorted(act_pages(portal)) == sorted([f"/web/dou/-/{SUFER_URL}", f"/web/dou/-/{DELIB_URL}"])
    db = clip.Storage(cfg.db_path)
    run = db.con.execute("SELECT * FROM runs ORDER BY id DESC").fetchone()
    assert run["status"] == "portal" and "fallback portal" in run["notes"] and "HTTP 404" in run["notes"]
    row = db.con.execute("SELECT * FROM matches WHERE article_id=?", (f"portal:{SUFER_URL}",)).fetchone()
    assert row["file_name"] == "portal:2026-10-28-DO1" and row["assina"] == "MARIA SOUZA"
    assert db.file_articles("portal:2026-10-28-DO1") == 3 and db.get("last_success_ts")
    db.close()
    # proxima execucao: nenhuma pagina de ato de novo, nenhum e-mail
    n = len(portal.requests)
    assert clip.cmd_run(cfg, D_PORTAL, True, False) == 0
    assert act_pages(portal, n) == [] and mailer["m"].sent == []


def inlabs_versions():
    """Os mesmos atos de fill_portal como o INLABS os entrega (ids e artCategory proprios)."""
    sufer = article_xml("9001", "Decisão", SUFER, "DECISÃO SUFER Nº 130, DE 26 DE OUTUBRO DE 2026",
                        SUFER_TEXTO, page="150", edition="204", pub_date="28/10/2026")
    delib = article_xml("9002", "Deliberação", DIRETORIA, "Deliberação ANTT Nº 310, DE 27 DE OUTUBRO DE 2026",
                        DELIB_TEXTO, page="3", edition="204-A", pub_name="DO1E", pub_date="28/10/2026")
    return sufer, delib


def test_inlabs_back_after_portal_does_not_duplicate(tmp_path, inlabs, mailer, monkeypatch, portal):
    freeze_today(monkeypatch, D_PORTAL)
    inlabs.login_404 = True
    fill_portal(portal)
    cfg = make_config(tmp_path)
    clip.cmd_run(cfg, D_PORTAL, True, False)
    assert len(mailer["m"].sent) == 1
    # INLABS volta e a janela reprocessa a data: mesmos atos com outro id -> sem reenvio
    inlabs.login_404 = False
    sufer, delib = inlabs_versions()
    inlabs.files[("2026-10-28", "2026-10-28-DO1.zip")] = make_zip(sufer, DESPACHO_SUFER)
    inlabs.files[("2026-10-28", "2026-10-28-DO1E.zip")] = make_zip(delib)
    assert clip.cmd_run(cfg, D_PORTAL, True, False) == 0
    assert len(mailer["m"].sent) == 1                       # so o ato realmente novo
    assert "Defiro o pedido" in mailer["m"].sent[0][1] and "Aprovar o projeto" not in mailer["m"].sent[0][1]
    db = clip.Storage(cfg.db_path)
    assert db.con.execute("SELECT count(*) FROM matches").fetchone()[0] == 3
    assert db.con.execute("SELECT status FROM runs ORDER BY id DESC").fetchone()[0] == "ok"


def test_portal_skips_acts_inlabs_already_captured(tmp_path, inlabs, mailer, monkeypatch, portal):
    freeze_today(monkeypatch, D_PORTAL)
    sufer, delib = inlabs_versions()
    inlabs.files[("2026-10-28", "2026-10-28-DO1.zip")] = make_zip(sufer)
    inlabs.files[("2026-10-28", "2026-10-28-DO1E.zip")] = make_zip(delib)
    cfg = make_config(tmp_path)
    clip.cmd_run(cfg, D_PORTAL, True, False)
    assert "2 publicacao(oes)" in mailer["m"].sent[0][0]
    # INLABS cai; o portal ve os mesmos atos e nem abre as paginas deles
    inlabs.login_404 = True
    fill_portal(portal)
    assert clip.cmd_run(cfg, D_PORTAL, True, False) == 0
    assert act_pages(portal) == [] and mailer["m"].sent == []
    assert clip.Storage(cfg.db_path).con.execute("SELECT count(*) FROM matches").fetchone()[0] == 2


def test_same_source_acts_with_same_identifica_are_kept(tmp_path):
    """15/09/2026 real: tres 'RETIFICAÇÃO' da ANTT na mesma pagina, todas do INLABS."""
    db = clip.Storage(str(tmp_path / "s.sqlite"))
    xmls = [article_xml(str(4000 + i), "Retificação", SUFER, "RETIFICAÇÃO", f"<p>ret {i}</p>", page="102")
            for i in range(3)]
    pubs = clip.parse_zip(make_zip(*xmls), "2026-09-15-DO1.zip", date(2026, 9, 15))
    assert [db.add_match(clip.Match(p, ["x"])) for p in pubs] == [True, True, True]
    ret = clip.portal_publication(portal_item("retificacao-1", "Retificação", SUFER, "RETIFICAÇÃO",
                                              page="102", edition="177"), date(2026, 9, 15), "DO1")
    assert db.same_act_other_source(ret) is True


def test_portal_down_too_keeps_unavailable_status(tmp_path, inlabs, mailer, monkeypatch, portal):
    freeze_today(monkeypatch, D_PORTAL)
    inlabs.login_404 = True                                  # portal.down = True (padrao)
    cfg = make_config(tmp_path)
    assert clip.cmd_run(cfg, D_PORTAL, True, False) == 0
    run = clip.Storage(cfg.db_path).con.execute("SELECT * FROM runs").fetchone()
    assert run["status"] == "inlabs_unavailable"
    assert "INLABS: Login respondeu HTTP 404" in run["notes"] and "portal: portal in.gov.br inacessivel" in run["notes"]


def test_portal_format_change_fails_explicitly(tmp_path, inlabs, mailer, monkeypatch, portal):
    freeze_today(monkeypatch, D_PORTAL)
    inlabs.login_404 = True
    fill_portal(portal)
    portal.broken_index = True
    cfg = make_config(tmp_path)
    assert clip.cmd_run(cfg, D_PORTAL, True, False) == 1
    run = clip.Storage(cfg.db_path).con.execute("SELECT * FROM runs").fetchone()
    assert run["status"] == "error" and "PortalFormatError" in run["notes"]


def test_portal_act_page_error_is_recorded_and_retried(tmp_path, inlabs, mailer, monkeypatch, portal):
    freeze_today(monkeypatch, D_PORTAL)
    inlabs.login_404 = True
    fill_portal(portal)
    texto = portal.acts.pop(DELIB_URL)                       # pagina do ato responde 404
    cfg = make_config(tmp_path)
    assert clip.cmd_run(cfg, D_PORTAL, True, False) == 1
    assert len(mailer["m"].sent) == 1 and "1 publicacao(oes)" in mailer["m"].sent[0][0]
    run = clip.Storage(cfg.db_path).con.execute("SELECT * FROM runs").fetchone()
    assert run["status"] == "error" and DELIB_URL in run["notes"]
    portal.acts[DELIB_URL] = texto
    assert clip.cmd_run(cfg, D_PORTAL, True, False) == 0
    assert "DELIBERAÇÃO ANTT Nº 310" in mailer["m"].sent[0][1]


def test_daily_confirmation_via_portal(tmp_path, inlabs, mailer, monkeypatch, portal):
    freeze_today(monkeypatch, D_PORTAL)
    inlabs.login_404 = True
    portal.down = False
    portal.add(D_PORTAL, "do1", OUTRO_ITEM, "<p>nada</p>")
    cfg = make_config(tmp_path)
    assert clip.cmd_run(cfg, D_PORTAL, True, False) == 0
    subject, html = mailer["m"].sent[0]
    assert subject.endswith("28/10/2026 - sem novidades")
    assert "Edicao de hoje verificada: DO1" in html and "Fonte desta consulta: portal in.gov.br" in html


# ---------------------------------------------------------------------------
# backfill em lotes retomaveis
# ---------------------------------------------------------------------------

def read_kv(path):
    return dict(l.split("=", 1) for l in path.read_text(encoding="utf-8").splitlines() if "=" in l)


def test_backfill_portal_in_batches_resumes_and_reports(tmp_path, mailer, monkeypatch, portal):
    d1, d3 = date(2026, 10, 26), D_PORTAL
    portal.down = False
    portal.add(d1, "do1", portal_item("decisao-sufer-n-1-1", "Decisão", SUFER, "DECISÃO SUFER Nº 1",
                                      pub_date="26/10/2026"), "<p>um</p>")
    portal.add(d3, "do1", SUFER_ITEM, SUFER_TEXTO)
    monkeypatch.setattr(clip.time, "sleep", lambda s: None)
    clock = iter(range(0, 100_000, 61))                     # cada leitura do relogio: +61 s
    monkeypatch.setattr(clip.time, "monotonic", lambda: next(clock))
    out, summary = tmp_path / "out", tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    cfg = make_config(tmp_path)

    assert clip.cmd_backfill(cfg, d1, d3, max_minutes=1) == 0        # lote de 1 min: so d1
    assert read_kv(out) == {"next_start": "2026-10-27", "end": "2026-10-28", "status": "parcial"}
    db = clip.Storage(cfg.db_path)
    assert (db.get("backfill_next"), db.get("backfill_end")) == ("2026-10-27", "2026-10-28")
    assert db.con.execute("SELECT count(*) FROM matches WHERE emailed_ts='backfill'").fetchone()[0] == 1
    db.close()

    out.unlink()
    assert clip.cmd_backfill(cfg, None, None) == 0                     # retoma ate o fim gravado
    assert read_kv(out) == {"next_start": "", "end": "2026-10-28", "status": "concluido"}
    db = clip.Storage(cfg.db_path)
    assert db.get("backfill_next") is None and db.get("backfill_end") is None
    assert db.con.execute("SELECT count(*) FROM matches WHERE emailed_ts='backfill'").fetchone()[0] == 2
    assert db.pending_matches() == [] and "m" not in mailer
    notes = [r[0] for r in db.con.execute("SELECT notes FROM runs ORDER BY id")]
    assert notes[0].startswith("backfill[portal] 2026-10-26..2026-10-28: processado 2026-10-26..2026-10-26; proximo: 2026-10-27")
    assert notes[1].startswith("backfill[portal] 2026-10-27..2026-10-28: processado 2026-10-27..2026-10-28; concluido")
    assert "Backfill concluido" in summary.read_text(encoding="utf-8")


def test_backfill_enviar_leaves_matches_for_daily_email(tmp_path, monkeypatch, portal):
    portal.down = False
    portal.add(D_PORTAL, "do1", SUFER_ITEM, SUFER_TEXTO)
    monkeypatch.setattr(clip.time, "sleep", lambda s: None)
    cfg = make_config(tmp_path)
    assert clip.cmd_backfill(cfg, D_PORTAL, D_PORTAL, enviar=True) == 0
    assert len(clip.Storage(cfg.db_path).pending_matches()) == 1


def test_backfill_stops_where_source_failed(tmp_path, monkeypatch, portal):
    monkeypatch.setattr(clip.time, "sleep", lambda s: None)
    cfg = make_config(tmp_path)
    with pytest.raises(SystemExit):                                    # nada para retomar
        clip.cmd_backfill(cfg, None, None)
    # portal fora do ar: para na primeira data, exit 0, retomada comeca nela
    assert clip.cmd_backfill(cfg, date(2026, 10, 26), D_PORTAL) == 0
    db = clip.Storage(cfg.db_path)
    assert db.get("backfill_next") == "2026-10-26"
    assert db.con.execute("SELECT status FROM runs").fetchone()[0] == "portal_unavailable"
    db.close()
    # pagina de ato com erro: exit 1, a data volta na retomada
    fill_portal(portal)
    portal.acts.pop(SUFER_URL)
    assert clip.cmd_backfill(cfg, None, None) == 1
    db = clip.Storage(cfg.db_path)
    assert db.get("backfill_next") == "2026-10-28"
    assert db.con.execute("SELECT status FROM runs ORDER BY id DESC").fetchone()[0] == "error"


def test_backfill_cli_arguments(tmp_path, monkeypatch, portal):
    calls = []
    monkeypatch.setattr(clip, "cmd_backfill", lambda cfg, s, e, **kw: calls.append((s, e, kw)) or 0)
    y = tmp_path / "c.yml"
    y.write_text("""
filtros: [{nome: A, secao: DO1, orgao: X, art_types: []}]
mail: {smtp_host: h, smtp_port: 1, from_email: a@b, to_emails: [c@d]}
storage: {db_path: x.sqlite}
""", encoding="utf-8")
    clip.main(["--config", str(y), "backfill"])
    clip.main(["--config", str(y), "backfill", "--start", "2022-01-01", "--end", "2022-01-31",
               "--fonte", "inlabs", "--max-minutes", "45", "--enviar"])
    assert calls[0] == (None, None, {"fonte": "portal", "max_minutes": 0, "enviar": False})
    assert calls[1] == (date(2022, 1, 1), date(2022, 1, 31), {"fonte": "inlabs", "max_minutes": 45, "enviar": True})


def test_backfill_enviar_requeues_existing_backfill_matches_and_keeps_resume_point(tmp_path, monkeypatch, portal):
    """01/11/2026: backfill de 2022 ate hoje sem e-mail em andamento; a lacuna recente
    (ja gravada como 'backfill') deve sair por e-mail sem perder o ponto de retomada."""
    portal.down = False
    portal.add(D_PORTAL, "do1", SUFER_ITEM, SUFER_TEXTO)
    monkeypatch.setattr(clip.time, "sleep", lambda s: None)
    cfg = make_config(tmp_path)
    assert clip.cmd_backfill(cfg, D_PORTAL, D_PORTAL) == 0               # sem e-mail
    db = clip.Storage(cfg.db_path)
    assert db.pending_matches() == []
    db.set("backfill_next", "2024-01-01"); db.set("backfill_end", "2026-11-01")
    db.close()
    n = len(portal.requests)
    assert clip.cmd_backfill(cfg, date(2026, 10, 22), date(2026, 10, 31), enviar=True) == 0
    db = clip.Storage(cfg.db_path)
    assert [r["article_id"] for r in db.pending_matches()] == [f"portal:{SUFER_URL}"]
    assert act_pages(portal, n) == []                                     # nada baixado de novo
    assert (db.get("backfill_next"), db.get("backfill_end")) == ("2024-01-01", "2026-11-01")


# ---------------------------------------------------------------------------
# busca web (export-busca -> data/busca.sqlite.gz -> docs/index.html)
# ---------------------------------------------------------------------------

def open_busca(gz_path, tmp_path):
    import gzip
    import sqlite3
    f = tmp_path / "busca_aberto.sqlite"
    f.write_bytes(gzip.decompress(gz_path.read_bytes()))
    con = sqlite3.connect(str(f))
    con.row_factory = sqlite3.Row
    return con


def test_unidade_antt_unifies_ministries_and_missing_unit():
    assert clip.unidade_antt(SUFER) == "Superintendência de Transporte Ferroviário"
    assert clip.unidade_antt("Ministério da Infraestrutura/Agência Nacional de "
                             "Transportes Terrestres/Diretoria Colegiada") == "Diretoria Colegiada"
    assert clip.unidade_antt(ANTT) == clip.SEM_UNIDADE
    assert clip.unidade_antt(OUTRO) == OUTRO


def test_export_busca_only_acts_with_accent_insensitive_fts(tmp_path):
    db = clip.Storage(str(tmp_path / "c.sqlite"))
    for xml, filtro in ((PORTARIA_SUFER, "SUFER-Todas"), (DELIBERACAO, "ANTT-Deliberacoes")):
        db.add_match(clip.Match(clip.parse_articles(xml, "f.zip", EDITION)[0], [filtro]))
    alerta = clip.Publication("pdf:x", "x.pdf", EDITION, art_type="Edicao em PDF",
                              identifica="Extra so em PDF", texto="ANTT", kind="pdf_alert")
    db.add_match(clip.Match(alerta, ["SUFER-Todas"]))
    db.close()
    out = tmp_path / "pagina" / "busca.sqlite.gz"
    assert clip.export_busca(str(tmp_path / "c.sqlite"), str(out)) == 2      # sem o alerta
    assert sorted(os.listdir(out.parent)) == ["busca.sqlite.gz"]             # sem temporarios
    con = open_busca(out, tmp_path)
    rows = con.execute("SELECT * FROM atos ORDER BY id").fetchall()
    assert [r["art_type"] for r in rows] == ["Portaria", "Deliberação"]
    assert (rows[0]["unidade"], rows[0]["fonte"]) == ("Superintendência de Transporte Ferroviário", "INLABS")
    assert "<table>" in rows[0]["texto_html"] and rows[0]["link"].startswith("https://pesquisa.in.gov.br")

    def busca(q):
        return [r[0] for r in con.execute(
            "SELECT a.article_id FROM atos_fts JOIN atos a ON a.id = atos_fts.rowid "
            "WHERE atos_fts MATCH ? ORDER BY a.id", (q,))]
    assert busca("operacao ferroviaria") == ["1001"]       # sem acento acha o texto acentuado
    assert busca('"FERROVIÁRIA no trecho"') == ["1001"]    # frase, maiusculas
    assert busca("assina:silva") == ["1001"]
    assert busca("delibera") == ["1002"]
    info = dict(con.execute("SELECT key, value FROM info").fetchall())
    assert (info["atos"], info["data_inicial"], info["data_final"]) == ("2", "2026-09-16", "2026-09-16")
    assert info["ultimo_ato_gravado"].startswith(date.today().isoformat()[:4])


def test_export_busca_cli_is_deterministic(tmp_path):
    db_path = tmp_path / "dados" / "clipping.sqlite"
    db = clip.Storage(str(db_path))
    db.add_match(clip.Match(clip.parse_articles(PORTARIA_SUFER, "f.zip", EDITION)[0], ["SUFER-Todas"]))
    db.close()
    y = tmp_path / "c.yml"
    y.write_text(f"""
filtros: [{{nome: A, secao: DO1, orgao: X, art_types: []}}]
mail: {{smtp_host: h, smtp_port: 1, from_email: a@b, to_emails: [c@d]}}
storage: {{db_path: '{db_path.as_posix()}'}}
""", encoding="utf-8")
    gz = db_path.parent / "busca.sqlite.gz"
    assert clip.main(["--config", str(y), "export-busca"]) == 0
    first = gz.read_bytes()
    assert clip.main(["--config", str(y), "export-busca"]) == 0
    assert gz.read_bytes() == first                          # mesmos atos, mesmos bytes
    db = clip.Storage(str(db_path))
    db.add_match(clip.Match(clip.parse_articles(DELIBERACAO, "f.zip", EDITION)[0], ["ANTT-Deliberacoes"]))
    db.close()
    assert clip.main(["--config", str(y), "export-busca"]) == 0
    assert gz.read_bytes() != first
