import hashlib
import io
import math
import os
import re
import secrets
import subprocess
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path

from flask import Flask, abort, g, jsonify, request, send_file
from pypdf.errors import PdfReadError

from . import domain as d
from .derived import extract_plot_results
from .dedup import consolidate, consolidate_table_citations, flag_reading_conflicts
from .extract import DOI, VALUE, Extraction, bibliographic_doi, locate_prose_span, number
from .export import exporter
from .graph_api import graph
from .graphs import automatic
from .local_ai import automatic_deepseek, provider, providers, suggestions
from .ocr import read_ocr
from .pdf import draw, page_file, prepare, read_lines
from .rounding_review import flag_rounded_review
from .publications import delete_publication
from .qwen_review import approve_qwen_evidence
from .science import recheck, science
from .store import DATA, data_dir, db, edit, encode, initialize, insert, key, one, rows, workspace
from .vision import vision_suggestions

app = Flask(__name__, static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 85 * 1024 * 1024
worker = ThreadPoolExecutor(max_workers=2, thread_name_prefix="softx-pdf")
lock = threading.Lock()
workspace_lock = threading.Lock()
private_workspaces = os.getenv("SOFTX_PRIVATE_WORKSPACES", "0") == "1"
workspace_root = DATA / "workspaces"
workspace_id = re.compile(r"[0-9a-f]{64}")
initialize()
app.register_blueprint(graph)
app.register_blueprint(science)
app.register_blueprint(exporter)


@app.before_request
def select_workspace():
    if not private_workspaces or (request.path != "/" and not request.path.startswith("/api/")):
        return
    cookie_name = "__Host-softx_workspace" if request.is_secure else "softx_workspace"
    ident = request.cookies.get(cookie_name, "")
    if not workspace_id.fullmatch(ident):
        ident = secrets.token_hex(32)
        g.new_workspace = (cookie_name, ident)
    g.workspace_token = workspace.set(workspace_root / ident)
    if request.path.startswith("/api/"):
        with workspace_lock:
            if not (data_dir() / "softx.sqlite").is_file():
                initialize()


@app.after_request
def private_response(response):
    if private_workspaces and (request.path == "/" or request.path.startswith("/api/")):
        response.cache_control.private = True
        response.cache_control.no_store = True
        response.vary.add("Cookie")
        if new_cookie := g.get("new_workspace"):
            response.set_cookie(*new_cookie, max_age=365*24*60*60, secure=request.is_secure,
                                httponly=True, samesite="Lax")
    return response


@app.teardown_request
def release_workspace(_):
    if token := g.pop("workspace_token", None):
        workspace.reset(token)


def queue_pdf(version):
    worker.submit(copy_context().run, process, version)


def finish_processing(connection, pub, version, pages, failures, lines):
    consolidate(connection,version)
    consolidate_table_citations(connection,version,lines)
    flag_reading_conflicts(connection,version)
    flag_rounded_review(connection,version)
    recheck(connection,pub)
    status = "partial" if failures else "completed"
    connection.execute("UPDATE pdf_version SET processing_status=?,pages=?,failed_pages=?,error_message=NULL WHERE id=?",
                       (status, encode(pages), encode(failures), version))


def process(version):
    with db() as connection:
        changed = connection.execute("UPDATE pdf_version SET processing_status='processing' WHERE id=? AND processing_status='queued'", (version,)).rowcount
        if not changed:
            return
        entry = one(connection, "SELECT * FROM pdf_version WHERE id=?", version)
    directory = data_dir() / version
    try:
        pages, metadata = prepare(directory / "original.pdf", directory / "pages")
        good, lines, failures = [], [], []
        ai_providers = providers(cloud=bool(entry["allow_cloud"]))
        vision_enabled = "deepseek" in ai_providers and os.getenv("SOFTX_DEEPSEEK_VISION","0") == "1"
        image_evidence = []
        use_ocr = vision_enabled or os.getenv("SOFTX_OCR", "0") == "1"
        for page in pages:
            try:
                path = page_file(directory, page["page"])
                extracted = read_lines(path, page)
                if use_ocr:
                    try:
                        extracted = read_ocr(path,page,extracted,vision_regions=image_evidence if vision_enabled else None)
                    except (ValueError, subprocess.SubprocessError, ET.ParseError, OSError) as exc:
                        app.logger.warning("OCR unavailable on page %s (%s)", page["page"], type(exc).__name__)
                        if not extracted:
                            raise ValueError("Нет текстового слоя, локальный OCR недоступен") from exc
                if not extracted:
                    raise ValueError("Текстовый слой отсутствует или не распознан")
                lines.extend(extracted)
                good.append(page["page"])
            except (ValueError, subprocess.SubprocessError, ET.ParseError, OSError) as exc:
                failures.append({"page": page["page"], "reason": str(exc)[:240],
                                 "value_state": "unparseable", "fields": list(d.METRICS)})
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
            eligible=extraction.discover(metadata)
            if eligible:
                extraction.parse()
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
            if not eligible or not ai_providers:
                finish_processing(connection,pub,version,pages,failures,extraction.body_lines if eligible else [])
                return
            covered_by_page={}
            for source in rows(connection,"""SELECT DISTINCT s.source_page,s.bbox FROM source_reference s
                JOIN result_source l ON l.source_reference_id=s.id
                WHERE s.pdf_version_id=? AND s.fragment_role='value'""",version):
                covered_by_page.setdefault(source["source_page"],[]).append(source["bbox"])

        # Network requests run after the SQLite write transaction has released its lock.
        def covered(line,span):
            x0,y0,x1,y1=span.bbox
            x,y=(x0+x1)/2,(y0+y1)/2
            return any(a<=x<=c and b<=y<=d for a,b,c,d in covered_by_page.get(line.page,()))

        ai_candidates = []
        for engine in ai_providers:
            for group,metric,value,unit in suggestions(extraction.body_lines,engine,covered=covered):
                anchor = locate_prose_span(group,*value.span("n"))
                if not anchor or covered(*anchor):
                    continue
                line,span = anchor
                covered_by_page.setdefault(line.page,[]).append(span.bbox)
                ai_candidates.append((group,metric,value,unit,d.AI_EXTRACTION_METHODS[engine]))
        visual = vision_suggestions(image_evidence,covered=covered) if vision_enabled else []
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            extraction.db=connection
            publication=one(connection,"SELECT corpus_status FROM publication WHERE id=?",pub)
            if not publication["corpus_status"].startswith("excluded"):
                for group,metric,value,unit,method in ai_candidates:
                    extraction.add_prose_candidate(group,metric,value,unit,method)
                for metric,line,span,value,unit_line,unit in visual:
                    if covered(line,span):
                        continue
                    refs = [(extraction.ref(line,span,"value"),"text"),(extraction.ref(unit_line,unit,"unit"),"text")]
                    extraction.result(metric,value,line,span,unit.text,refs,None,None,"primary",
                                      extraction_method="deepseek_vision")
                    covered_by_page.setdefault(line.page,[]).append(span.bbox)
            approve_qwen_evidence(extraction)
            finish_processing(connection,pub,version,pages,failures,extraction.body_lines)
    except Exception as exc:
        if isinstance(exc,PdfReadError):
            message="Не удалось открыть PDF. Файл повреждён или неполон. "+str(exc)
        else:
            app.logger.exception("PDF processing error for %s", version)
            message=str(exc)
        with db() as connection:
            connection.execute("UPDATE pdf_version SET processing_status='failed',error_message=? WHERE id=?",
                               (message[:550], version))


def ingest(content, filename, *, allow_cloud=False):
    sha = hashlib.sha256(content).hexdigest()
    version, pub = sha, key("publication", sha)
    with lock, db() as connection:
        existing = one(connection, "SELECT * FROM pdf_version WHERE sha256=?", sha)
        if existing:
            return existing
        directory = data_dir() / version
        directory.mkdir(exist_ok=True)
        tmp = directory / "upload.tmp"
        tmp.write_bytes(content)
        tmp.replace(directory / "original.pdf")
        insert(connection, "publication", id=pub, title=Path(filename).stem,
               is_fixture=int(b"SYNTHETIC TEST" in content[:16000]))
        insert(connection, "pdf_version", id=version, publication_id=pub, sha256=sha,
               filename=Path(filename).name, processing_status="queued",allow_cloud=int(allow_cloud))
        created = one(connection, "SELECT * FROM pdf_version WHERE id=?", version)
    queue_pdf(version)
    return created


def resume_queue():
    with db() as connection:
        connection.execute("UPDATE pdf_version SET processing_status='queued' WHERE processing_status='processing'")
        waiting = rows(connection, "SELECT id FROM pdf_version WHERE processing_status='queued'")
    for value in waiting:
        queue_pdf(value["id"])


def start():
    if private_workspaces:
        workspace_root.mkdir(mode=0o700, exist_ok=True)
        for directory in workspace_root.iterdir():
            if not workspace_id.fullmatch(directory.name) or not (directory / "softx.sqlite").is_file():
                continue
            token = workspace.set(directory)
            try:
                initialize()
                resume_queue()
            finally:
                workspace.reset(token)
        return
    resume_queue()
    if os.getenv("SOFTX_DEMO", "1") == "1":
        for path in sorted((Path(__file__).resolve().parents[1] / "fixtures").glob("*.pdf")):
            ingest(path.read_bytes(), path.name)


def production_app():
    start()
    return app


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(409)
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
    allow_cloud = automatic_deepseek() or request.form.get("allow_cloud") == "1"
    return jsonify([ingest(file.read(), file.filename or "document.pdf",allow_cloud=allow_cloud) for file in files]), 202


@app.post("/api/pdf/<version>/retry")
def retry(version):
    body = request.get_json(silent=True)
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        entry = one(connection, "SELECT * FROM pdf_version WHERE id=?", version)
        if not entry:
            abort(404)
        if entry["processing_status"] not in ("queued", "processing"):
            requested = body.get("allow_cloud") is True if isinstance(body,dict) else entry["allow_cloud"]
            allow_cloud = int(automatic_deepseek() or requested)
            connection.execute("UPDATE pdf_version SET processing_status='queued',error_message=NULL,allow_cloud=? WHERE id=?",
                               (allow_cloud,version))
    queue_pdf(version)
    return jsonify(status="queued"), 202


@app.delete("/api/publication/<publication_id>")
def remove_publication(publication_id):
    with lock:
        delete_publication(publication_id)
    return jsonify(ok=True)


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
    return jsonify(publications=publications, results=result_list, active_issues=active_issues,
                   ai_provider=provider(), cloud_automatic=automatic_deepseek(), private_workspace=private_workspaces)


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
    return send_file(data_dir() / version / "original.pdf", mimetype="application/pdf", download_name=entry["filename"])


@app.post("/api/pdf/<version>/result")
def add_result(version):
    body = request.get_json()
    if not isinstance(body,dict):
        abort(400,"Укажите параметры записи")
    page,box = body.get("page"),body.get("bbox")
    metric,origin,agg = body.get("metric_name"),body.get("result_origin"),body.get("aggregation","unknown")
    literal,raw_unit = body.get("raw_value"),body.get("raw_unit") or None
    unit = body.get("metric_unit") or raw_unit
    if (not isinstance(metric,str) or metric not in d.METRICS or origin not in ("primary","cited")
            or agg not in ("champion","mean","median","single","unknown")):
        abort(400,"Укажите показатель, происхождение и агрегацию")
    if not isinstance(literal,str) or len(literal)>120 or not (match:=VALUE.fullmatch(literal.strip())):
        abort(400,"Перепишите число, диапазон или погрешность из выделенного фрагмента")
    if any(u is not None and (not isinstance(u,str) or len(u)>40) for u in (raw_unit,unit)):
        abort(400,"Недопустимая единица")
    value,spread,end = [number(match[k]) if match[k] is not None else None for k in ("n","spread","end")]
    if any(v is not None and not math.isfinite(v) for v in (value,spread,end)):
        abort(400,"Недопустимое числовое значение")
    normalized, normal_unit = d.normalize(metric,value,unit)
    if unit and (normalized is None or not math.isfinite(normalized)):
        abort(400,"Укажите единицу, совместимую с показателем")
    if match["op"] or end is not None:
        normalized,normal_unit = None,None
    low,high = sorted((value,end)) if end is not None else (None,None)
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        pdf=one(connection,"SELECT * FROM pdf_version WHERE id=?",version)
        if not pdf:
            abort(404)
        if pdf["pv_extraction_enabled"] == 0:
            abort(400,"Сначала проверьте решение о включении публикации в корпус")
        if (type(page) is not int or not 1<=page<=len(pdf["pages"]) or not isinstance(box,list)
                or len(box)!=4 or any(type(n) not in (int,float) or not math.isfinite(n) for n in box)):
            abort(400,"Выделите фрагмент на странице PDF")
        a,b,c,d_box = pdf["pages"][page-1]["crop_box"]
        x0,y0,x1,y1 = box
        if not (a<=x0<x1<=c and b<=y0<y1<=d_box):
            abort(400,"Выделение должно лежать внутри страницы")
        pub = pdf["publication_id"]
        device,measurement = body.get("device_id") or None,body.get("measurement_id") or None
        if any(val is not None and not isinstance(val,str) for val in (device,measurement)):
            abort(400,"Некорректная привязка")
        bound = one(connection,"SELECT material_id FROM device WHERE id=? AND publication_id=?",device,pub) if device else None
        if device and not bound or measurement and not one(connection,"SELECT id FROM measurement WHERE id=? AND device_id=?",measurement,device):
            abort(400,"Прибор и измерение должны принадлежать публикации")
        quote = body.get("quote") or literal
        if not isinstance(quote,str) or len(quote)>1500:
            abort(400,"Цитата должна быть короче 1500 символов")
        bbox = [round(n,4) for n in box]
        ident = key(version,page,metric,bbox,literal.strip(),raw_unit,device,measurement,origin,agg)
        existing=one(connection,"SELECT * FROM measurement_result WHERE id=?",ident)
        if existing:
            if existing["deleted"]:
                abort(409,"Такая запись была удалена")
            return jsonify(existing)
        wanted = body.get("confirmed") is True
        state = "numeric_zero" if normalized == 0 else "numeric" if normalized is not None else "ambiguous"
        insert(connection,"measurement_result",id=ident,publication_id=pub,pdf_version_id=version,metric_name=metric,
               result_origin=origin,aggregation=agg,device_id=device,measurement_id=measurement,
               material_id=bound["material_id"] if bound else None,metric_value=value,metric_unit=unit,value_state=state,
               raw_value=literal.strip(),raw_unit=raw_unit,raw_record={"value":literal.strip(),"unit":raw_unit,"quote":quote},
               normalized_value=normalized,normalized_unit=normal_unit,range_min=low,range_max=high,
               inequality_op=match["op"],spread_value=spread,extraction_method="manual_pdf",manual=1,
               extraction_status=d.CONFIRMED if wanted else d.REVIEW)
        source=key(version,page,bbox,quote,"manual_pdf")
        insert(connection,"source_reference",id=source,publication_id=pub,pdf_version_id=version,source_page=page,
               source_fragment=quote,bbox=bbox,source_type="text",fragment_role="value")
        insert(connection,"result_source",result_id=ident,source_reference_id=source)
        insert(connection,"audit_log",entity_type="measurement_result",entity_id=ident,field_name="created",
               new_value=encode({"metric":metric,"source_reference_id":source}),author="local-reviewer")
        recheck(connection,pub)
        return jsonify(one(connection,"SELECT * FROM measurement_result WHERE id=?",ident)),201


@app.get("/api/pdf/<version>/page/<int:page>")
def page_image(version, page):
    if not re.fullmatch(r"[0-9a-f]{64}", version):
        abort(404)
    path = page_file(data_dir() / version, page)
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
            allow_cloud = int(automatic_deepseek())
            for version in versions:
                connection.execute("UPDATE pdf_version SET processing_status='queued',allow_cloud=MAX(allow_cloud,?) WHERE id=?",
                                   (allow_cloud,version["id"]))
        else:
            versions=[]
    for version in versions:
        queue_pdf(version["id"])
    return jsonify(ok=True)


@app.patch("/api/result/<ident>")
def update_result(ident):
    body = request.get_json()
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        result = one(connection,"SELECT * FROM measurement_result WHERE id=? AND deleted=0",ident)
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


@app.delete("/api/result/<ident>")
def delete_result(ident):
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        result = one(connection, "SELECT * FROM measurement_result WHERE id=?", ident)
        if not result:
            abort(404)
        if not result["deleted"]:
            edit(connection, "measurement_result", result, {"deleted":1, "manual":1})
            recheck(connection, result["publication_id"])
    return jsonify(ok=True)


if __name__ == "__main__":
    start()
    app.run(host="127.0.0.1", port=int(os.getenv("PORT","8000")), threaded=True)
