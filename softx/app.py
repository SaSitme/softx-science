import hashlib
import io
import math
import os
import re
import subprocess
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file
from pypdf.errors import PdfReadError

from . import domain as d
from .derived import extract_plot_results
from .extract import DOI, Extraction, bibliographic_doi
from .export import exporter
from .graph_api import graph
from .graphs import automatic
from .local_ai import suggestions
from .pdf import draw, page_file, prepare, read_lines
from .science import recheck, science
from .store import DATA, db, edit, encode, initialize, insert, key, one, rows

app = Flask(__name__, static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 85 * 1024 * 1024
worker = ThreadPoolExecutor(max_workers=2, thread_name_prefix="softx-pdf")
lock = threading.Lock()
initialize()
app.register_blueprint(graph)
app.register_blueprint(science)
app.register_blueprint(exporter)


def process(version):
    with db() as connection:
        changed = connection.execute("UPDATE pdf_version SET processing_status='processing' WHERE id=? AND processing_status='queued'", (version,)).rowcount
        if not changed:
            return
        entry = one(connection, "SELECT * FROM pdf_version WHERE id=?", version)
    directory = DATA / version
    try:
        pages, metadata = prepare(directory / "original.pdf", directory / "pages")
        good, lines, failures = [], [], []
        for page in pages:
            try:
                extracted = read_lines(page_file(directory, page["page"]), page)
                if not extracted:
                    raise ValueError("Текстовый слой отсутствует или не распознан")
                lines.extend(extracted)
                good.append(page["page"])
            except (ValueError, subprocess.SubprocessError, ET.ParseError, OSError) as exc:
                failures.append({"page": page["page"], "reason": str(exc)[:240],
                                 "value_state": "unparseable", "fields": list(d.METRICS)})
        ai_candidates = suggestions(lines)
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            pub=entry["publication_id"]
            doi=bibliographic_doi(metadata,lines)
            if doi:
                candidate=one(connection,"SELECT id FROM publication WHERE lower(doi)=? AND id<>?",doi,pub)
                pristine=one(connection,"""SELECT 1 AS ok FROM publication WHERE id=? AND corpus_manual=0
                    AND NOT EXISTS (SELECT 1 FROM material WHERE publication_id=?)
                    AND NOT EXISTS (SELECT 1 FROM device WHERE publication_id=?)
                    AND NOT EXISTS (SELECT 1 FROM measurement_result WHERE publication_id=?)
                    AND NOT EXISTS (SELECT 1 FROM measurement_result WHERE cited_publication_id=?)
                    AND NOT EXISTS (SELECT 1 FROM plot_dataset WHERE publication_id=?)
                    AND NOT EXISTS (SELECT 1 FROM source_reference WHERE publication_id=?)
                    AND NOT EXISTS (SELECT 1 FROM validation_issue WHERE publication_id=?)""",*([pub]*8))
                if candidate and pristine:
                    connection.execute("UPDATE pdf_version SET publication_id=? WHERE id=?",(candidate["id"],version))
                    connection.execute("DELETE FROM publication WHERE id=?",(pub,))
                    pub=candidate["id"]
            extraction = Extraction(connection, pub, version, lines)
            if extraction.discover(metadata):
                extraction.parse()
                for group,metric,value,unit in ai_candidates:
                    extraction.add_prose_candidate(group,metric,value,unit,"local_ai_pdf_quote")
                for page in pages:
                    if page["page"] not in good:
                        continue
                    try:
                        automatic(extraction,[page])
                    except (subprocess.SubprocessError, ET.ParseError, ValueError, OSError) as exc:
                        app.logger.warning("Graph detection failed in %s, page %s: %s", version, page["page"], exc)
                        failures.append({"page":page["page"],"reason":str(exc)[:240],"fields":["plot_dataset"],"value_state":"unparseable"})
                for plot in rows(connection,"SELECT * FROM plot_dataset WHERE pdf_version_id=?",version):
                    extract_plot_results(connection,plot)
            recheck(connection,pub)
            status = "partial" if failures else "completed"
            connection.execute("UPDATE pdf_version SET processing_status=?,pages=?,failed_pages=?,error_message=NULL WHERE id=?",
                               (status, encode(pages), encode(failures), version))
    except Exception as exc:
        if isinstance(exc,PdfReadError):
            message="Не удалось открыть PDF. Файл повреждён или неполон. "+str(exc)
        else:
            app.logger.exception("PDF processing error for %s", version)
            message=str(exc)
        with db() as connection:
            connection.execute("UPDATE pdf_version SET processing_status='failed',error_message=? WHERE id=?",
                               (message[:550], version))


def ingest(content, filename):
    sha = hashlib.sha256(content).hexdigest()
    version, pub = sha, key("publication", sha)
    with lock, db() as connection:
        existing = one(connection, "SELECT * FROM pdf_version WHERE sha256=?", sha)
        if existing:
            return existing
        directory = DATA / version
        directory.mkdir(exist_ok=True)
        tmp = directory / "upload.tmp"
        tmp.write_bytes(content)
        tmp.replace(directory / "original.pdf")
        insert(connection, "publication", id=pub, title=Path(filename).stem,
               is_fixture=int(b"SYNTHETIC TEST" in content[:16000]))
        insert(connection, "pdf_version", id=version, publication_id=pub, sha256=sha,
               filename=Path(filename).name, processing_status="queued")
        created = one(connection, "SELECT * FROM pdf_version WHERE id=?", version)
    worker.submit(process, version)
    return created


def start():
    with db() as connection:
        connection.execute("UPDATE pdf_version SET processing_status='queued' WHERE processing_status='processing'")
        waiting = rows(connection, "SELECT id FROM pdf_version WHERE processing_status='queued'")
    for value in waiting:
        worker.submit(process, value["id"])
    if os.getenv("SOFTX_DEMO", "1") == "1":
        for path in sorted((Path(__file__).resolve().parents[1] / "fixtures").glob("*.pdf")):
            ingest(path.read_bytes(), path.name)


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(413)
def error(exc):
    return jsonify(error=exc.description), exc.code


@app.get("/")
def home():
    return app.send_static_file("index.html")


@app.post("/api/upload")
def upload():
    files = request.files.getlist("files")
    if not files:
        abort(400, "Выберите хотя бы один PDF")
    return jsonify([ingest(file.read(), file.filename or "document.pdf") for file in files]), 202


@app.post("/api/pdf/<version>/retry")
def retry(version):
    with db() as connection:
        entry = one(connection, "SELECT * FROM pdf_version WHERE id=?", version)
        if not entry:
            abort(404)
        if entry["processing_status"] not in ("queued", "processing"):
            connection.execute("UPDATE pdf_version SET processing_status='queued',error_message=NULL WHERE id=?", (version,))
    worker.submit(process, version)
    return jsonify(status="queued"), 202


@app.get("/api/library")
def library():
    with db() as connection:
        publications = rows(connection, """SELECT p.*, f.id AS version_id, f.processing_status, f.filename, f.pages, f.error_message,
            f.failed_pages, f.created_at,
            (SELECT group_concat(DISTINCT material_class) FROM material m WHERE m.publication_id=p.id) AS material_class,
            (SELECT COUNT(*) FROM active_pv_result r WHERE r.pdf_version_id=f.id) AS result_count,
            (SELECT COUNT(*) FROM plot_dataset g WHERE g.pdf_version_id=f.id) AS plot_count
            FROM publication p JOIN pdf_version f ON f.publication_id=p.id ORDER BY f.created_at DESC, f.filename""")
        result_list = results(connection)
        active_issues = connection.execute("SELECT COUNT(*) FROM validation_issue WHERE resolution_status='open' AND severity IN ('error','warning')").fetchone()[0]
    return jsonify(publications=publications, results=result_list, active_issues=active_issues)


def results(connection, version=None):
    condition = "WHERE r.pdf_version_id=?" if version else ""
    args = (version,) if version else ()
    return rows(connection, f"""SELECT r.*,p.title AS publication_title,
            (SELECT filename FROM pdf_version WHERE id=r.pdf_version_id) AS publication_code,
            (SELECT group_concat(DISTINCT s.source_page) FROM result_source rs JOIN source_reference s ON s.id=rs.source_reference_id WHERE rs.result_id=r.id) AS source_page,
            (SELECT group_concat(DISTINCT s.source_table) FROM result_source rs JOIN source_reference s ON s.id=rs.source_reference_id WHERE rs.result_id=r.id) AS source_table,
            (SELECT group_concat(DISTINCT s.source_figure) FROM result_source rs JOIN source_reference s ON s.id=rs.source_reference_id WHERE rs.result_id=r.id) AS source_figure,
            (SELECT rp.plot_id FROM result_plot rp WHERE rp.result_id=r.id ORDER BY rp.plot_id LIMIT 1) AS plot_id,
d.device_label,d.architecture,d.device_type,
            m.material_class,q.scan_direction,q.certified,(SELECT cp.doi FROM publication cp WHERE cp.id=r.cited_publication_id) AS cited_doi,
            (SELECT group_concat(s.source_fragment,' ') FROM result_source rs JOIN source_reference s ON s.id=rs.source_reference_id
             WHERE rs.result_id=r.id) AS source_text,
            EXISTS(SELECT 1 FROM issue_result ir JOIN validation_issue i ON i.id=ir.issue_id WHERE ir.result_id=r.id
              AND i.resolution_status='open' AND i.severity IN ('error','warning')) AS has_issues
            FROM active_pv_result r JOIN publication p ON p.id=r.publication_id
            LEFT JOIN device d ON d.id=r.device_id LEFT JOIN material m ON m.id=COALESCE(d.material_id,r.material_id)
            LEFT JOIN measurement q ON q.id=r.measurement_id {condition} ORDER BY r.record_order""", *args)


@app.get("/api/data/<name>")
def data_entities(name):
    tables={"publications":"publication","materials":"material","devices":"device","layers":"device_layer",
            "measurements":"measurement","issues":"validation_issue"}
    if name not in tables:
        abort(404)
    table=tables[name]
    with db() as connection:
        records=rows(connection,f"SELECT * FROM {table}")
        for record in records:
            pub=record.get("publication_id") or (record["id"] if name=="publications" else None)
            if not pub and record.get("device_id"):
                device=one(connection,"SELECT publication_id FROM device WHERE id=?",record["device_id"])
                pub=device["publication_id"] if device else None
            version=one(connection,"SELECT id,filename FROM pdf_version WHERE publication_id=? ORDER BY created_at LIMIT 1",pub)
            record["publication_code"]=version["filename"] if version else None
            record["pdf_version_id"]=version["id"] if version else None
        columns=["publication_code"]+[r["name"] for r in rows(connection,f"PRAGMA table_info({table})")]
    return jsonify(records=records,columns=columns)


@app.get("/api/pdf/<version>")
def detail(version):
    with db() as connection:
        f = one(connection, "SELECT * FROM pdf_version WHERE id=?", version)
        if not f:
            abort(404)
        pub = f["publication_id"]
        return jsonify(pdf=f, publication=one(connection,"SELECT * FROM publication WHERE id=?",pub),
                       materials=rows(connection,"SELECT * FROM material WHERE publication_id=?",pub),
                       devices=rows(connection,"SELECT * FROM device WHERE publication_id=?",pub),
                       measurements=rows(connection,"SELECT q.* FROM measurement q JOIN device d ON d.id=q.device_id WHERE d.publication_id=?",pub),
                       layers=rows(connection,"SELECT l.* FROM device_layer l JOIN device d ON d.id=l.device_id WHERE d.publication_id=?",pub),
                       results=results(connection, version),
                       sources=rows(connection,"SELECT * FROM source_reference WHERE pdf_version_id=?",version),
                       links=rows(connection,"SELECT l.* FROM result_source l JOIN active_pv_result r ON r.id=l.result_id WHERE r.pdf_version_id=?",version),
                       plots=rows(connection,"SELECT * FROM plot_dataset WHERE pdf_version_id=?",version),
                       audit=rows(connection,"SELECT a.* FROM audit_log a WHERE a.entity_id IN"
                           " (SELECT id FROM measurement_result WHERE pdf_version_id=?) OR a.entity_id=? ORDER BY a.id DESC",version,pub))


@app.get("/api/pdf/<version>/original")
def original(version):
    if not re.fullmatch(r"[0-9a-f]{64}", version):
        abort(404)
    with db() as connection:
        entry = one(connection,"SELECT filename FROM pdf_version WHERE id=?",version)
    if not entry:
        abort(404)
    return send_file(DATA / version / "original.pdf", mimetype="application/pdf", download_name=entry["filename"])


@app.get("/api/pdf/<version>/page/<int:page>")
def page_image(version, page):
    if not re.fullmatch(r"[0-9a-f]{64}", version):
        abort(404)
    path = page_file(DATA / version, page)
    if not path.is_file():
        abort(404)
    width = max(500, min(request.args.get("w", 1300, type=int), 2600))
    try:
        image = draw(path, "png", width=width)
    except (subprocess.SubprocessError, OSError):
        abort(404, "Страница не отрисована")
    return send_file(io.BytesIO(image), mimetype="image/png", max_age=86400)


@app.patch("/api/publication/<pub>")
def update_publication(pub):
    body = request.get_json()
    statuses = ("included", "excluded_tandem", "excluded_theoretical", "needs_review")
    status, reason = body.get("corpus_status"), body.get("corpus_reason")
    if status not in statuses or not isinstance(reason, str) or not reason.strip():
        abort(400, "Укажите статус и основание")
    with db() as connection:
        current = one(connection, "SELECT * FROM publication WHERE id=?", pub)
        if not current:
            abort(404)
        edit(connection, "publication", current, {"corpus_status":status, "corpus_reason":reason.strip()[:2000], "corpus_manual":1})
        recheck(connection,pub)
        if status=="included":
            versions=rows(connection,"SELECT id FROM pdf_version WHERE publication_id=? AND processing_status IN ('completed','partial')",pub)
            for version in versions:
                connection.execute("UPDATE pdf_version SET processing_status='queued' WHERE id=?",(version["id"],))
        else:
            versions=[]
    for version in versions:
        worker.submit(process,version["id"])
    return jsonify(ok=True)


@app.patch("/api/result/<ident>")
def update_result(ident):
    body = request.get_json()
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        result = one(connection,"SELECT * FROM measurement_result WHERE id=?",ident)
        if not result:
            abort(404)
        editable = ("metric_value","metric_unit","device_id","measurement_id","result_origin","aggregation",
                    "inequality_op","range_min","range_max")
        changes = {field:body[field] for field in editable if field in body}
        for field in ("metric_value","range_min","range_max"):
            value = changes.get(field)
            if value is not None and (type(value) not in (int,float) or not math.isfinite(value)):
                abort(400, "Недопустимое числовое значение")
        updated = dict(result, **changes)
        if updated["aggregation"] not in ("champion","mean","median","single","unknown") or updated["result_origin"] not in ("primary","cited"):
            abort(400, "Недопустимая категория")
        unit = updated["metric_unit"]
        if unit is not None and (not isinstance(unit,str) or len(unit)>40):
            abort(400, "Недопустимая единица")
        if updated["result_origin"]=="primary":
            changes["cited_publication_id"]=None
        elif "cited_doi" in body:
            cited_doi=body["cited_doi"]
            if cited_doi is None or cited_doi=="":
                changes["cited_publication_id"]=None
            elif not isinstance(cited_doi,str) or not DOI.fullmatch(cited_doi.strip()) or len(cited_doi)>220:
                abort(400,"Укажите DOI цитируемой публикации")
            else:
                cited_doi=cited_doi.strip().lower()
                record=one(connection,"SELECT id FROM publication WHERE lower(doi)=?",cited_doi)
                cited=record["id"] if record else key("doi",cited_doi)
                insert(connection,"publication",id=cited,title=cited_doi,doi=cited_doi,corpus_reason="Библиографическая ссылка")
                changes["cited_publication_id"]=cited
        if updated["device_id"]:
            device = one(connection,"SELECT * FROM device WHERE id=? AND publication_id=?",updated["device_id"],result["publication_id"])
            if not device:
                abort(400, "Прибор должен принадлежать публикации")
            changes["material_id"] = device["material_id"]
        if updated["measurement_id"]:
            measurement = one(connection,"SELECT * FROM measurement WHERE id=? AND device_id=?",updated["measurement_id"],updated["device_id"])
            if not measurement:
                abort(400, "Измерение должно принадлежать выбранному прибору")
        scalar = not updated["inequality_op"] and updated["range_min"] is None and updated["range_max"] is None
        norm, norm_unit = d.normalize(result["metric_name"], updated["metric_value"], unit) if scalar else (None,None)
        changes.update(normalized_value=norm, normalized_unit=norm_unit, manual=1,
                       value_state="missing" if updated["metric_value"] is None else "ambiguous" if norm is None
                       else "numeric_zero" if norm == 0 else "numeric", extraction_status=d.CONFIRMED)
        edit(connection,"measurement_result",result,changes)
        recheck(connection,result["publication_id"])
    return jsonify(ok=True)


if __name__ == "__main__":
    start()
    app.run(host="127.0.0.1", port=int(os.getenv("PORT","8000")), threaded=True)
