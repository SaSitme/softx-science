import csv
import io
import math
import subprocess
from uuid import uuid4

from flask import Blueprint, abort, jsonify, request, send_file
from openpyxl import Workbook

from .derived import publish
from .graphs import TYPES, projection, save_crop, uncertainty
from .science import recheck
from .export import csv_value
from .store import DATA, db, edit, insert, one, rows

graph = Blueprint("graph", __name__)


def number(value, optional=False):
    if value is None and optional:
        return None
    if type(value) not in (int,float) or not math.isfinite(value) or abs(value)>1e20:
        abort(400,"Недопустимые числовые параметры")
    return value


def label(value):
    if value is not None and (not isinstance(value,str) or len(value)>140):
        abort(400,"Некорректная подпись")
    return value


@graph.route("/api/plot/<ident>", methods=["PATCH"])
@graph.route("/api/pdf/<ident>/plot", methods=["POST"])
def save_plot(ident):
    body=request.get_json()
    creating=request.method=="POST"
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current=one(connection,"SELECT * FROM plot_dataset WHERE id=?",ident) if not creating else None
        version=one(connection,"SELECT * FROM pdf_version WHERE id=?",ident if creating else current["pdf_version_id"]) if creating or current else None
        if not version:
            abort(404)
        page=body.get("source_page",current["source_page"] if current else None)
        if type(page) is not int or not 1<=page<=len(version["pages"]):
            abort(400,"Выберите страницу")
        kind=body.get("plot_type",current["plot_type"] if current else "JV")
        if kind not in TYPES:
            abort(400,"Выберите поддерживаемый тип графика")
        meta=version["pages"][page-1]
        box=body.get("plot_bbox",current["plot_bbox"] if current else [])
        if not isinstance(box,list) or len(box)!=4:
            abort(400,"Задайте область построения")
        x0,y0,x1,y1=map(number,box)
        a,b,c,d=meta["crop_box"]
        if not (a<=x0<x1<=c and b<=y0<y1<=d):
            abort(400,"Область построения должна лежать внутри страницы")
        axes={}
        for axis in ("x","y"):
            source=body.get("axes",{}).get(axis,{})
            scale=source.get("scale","linear")
            if scale not in ("linear","log"):
                abort(400,"Некорректный тип шкалы")
            axes[axis]={k:number(source.get(k),optional=True) for k in ("p0","p1","v0","v1")}
            axes[axis].update(scale=scale,unit=label(source.get("unit")),label=label(source.get("label")))
        submitted=body.get("points",[])
        if not isinstance(submitted,list) or len(submitted)>12000:
            abort(400,"Допустимо до 12 000 точек в серии")
        raw=[{"px":number(p.get("px")),"py":number(p.get("py"))} for p in submitted]
        if any(not (x0-.1<=p["px"]<=x1+.1 and y0-.1<=p["py"]<=y1+.1) for p in raw):
            abort(400,"Точки серии должны лежать внутри области построения")
        try:
            points=projection(axes,raw)
            crop=save_crop(version["id"],page,meta,box)
        except (ValueError,OverflowError,subprocess.SubprocessError) as exc:
            abort(400,str(exc))
        device=body.get("device_id") or None
        measurement=body.get("measurement_id") or None
        if device and not one(connection,"SELECT id FROM device WHERE id=? AND publication_id=?",device,version["publication_id"]):
            abort(400,"Прибор должен принадлежать публикации")
        if measurement and not one(connection,"SELECT id FROM measurement WHERE id=? AND device_id=?",measurement,device):
            abort(400,"Измерение должно принадлежать прибору")
        prior_raw=[{"px":p["px"],"py":p["py"]} for p in current["points"]] if current else []
        width=current["stroke_width_pdf"] if current and prior_raw==raw and page==current["source_page"] else None
        estimate,method=uncertainty(axes,points,width)
        fields=dict(source_page=page,plot_type=kind,plot_bbox=box,axes=axes,points=points,crop_image=crop,
                    digitization_method="manual_calibration" if current and prior_raw==raw else "manual_points",
                    stroke_width_pdf=width,uncertainty=estimate,uncertainty_method=method,
                    device_id=device,measurement_id=measurement,series_label=label(body.get("series_label")) or "Серия",
                    panel_id=label(body.get("panel_id")),source_figure=label(body.get("source_figure")),
                    legend_text=label(body.get("legend_text")),manual=1)
        if creating:
            ident=uuid4().hex
            insert(connection,"plot_dataset",id=ident,publication_id=version["publication_id"],pdf_version_id=version["id"],**fields)
            source=uuid4().hex
            insert(connection,"source_reference",id=source,publication_id=version["publication_id"],pdf_version_id=version["id"],
                   source_page=page,source_figure=fields["source_figure"],bbox=box,source_type="figure",fragment_role="condition")
            insert(connection,"plot_source",plot_id=ident,source_reference_id=source)
        else:
            edit(connection,"plot_dataset",current,fields)
            if any(current[name]!=fields[name] for name in ("plot_bbox","source_page","source_figure")):
                source=uuid4().hex
                insert(connection,"source_reference",id=source,publication_id=version["publication_id"],pdf_version_id=version["id"],
                       source_page=page,source_figure=fields["source_figure"],bbox=box,source_type="figure",fragment_role="condition")
                insert(connection,"plot_source",plot_id=ident,source_reference_id=source)
        result=one(connection,"SELECT * FROM plot_dataset WHERE id=?",ident)
        for item in rows(connection,"SELECT r.metric_name FROM measurement_result r JOIN result_plot rp ON r.id=rp.result_id WHERE rp.plot_id=?",ident):
            publish(connection,result,item["metric_name"],refresh=True)
        recheck(connection,result["publication_id"])
    return jsonify(result)


@graph.post("/api/plot/<ident>/calculate")
def calculate(ident):
    body=request.get_json()
    metric=body.get("metric")
    if metric not in ("Jsc_EQE","T80","Voc","Jsc"):
        abort(400,"Неизвестный расчёт")
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        plot=one(connection,"SELECT * FROM plot_dataset WHERE id=?",ident)
        if not plot:
            abort(404)
        try:
            aggregation=body.get("aggregation","unknown")
            if aggregation not in ("champion","mean","median","single","unknown"):
                abort(400,"Укажите допустимую агрегацию")
            result=publish(connection,plot,metric,coverage_confirmed=body.get("coverage_confirmed") is True,aggregation=aggregation)
            recheck(connection,result["publication_id"])
        except ValueError as exc:
            abort(400,str(exc))
    return jsonify(result)


@graph.get("/api/plot/<ident>/image")
def crop_image(ident):
    with db() as connection:
        plot=one(connection,"SELECT pdf_version_id,crop_image FROM plot_dataset WHERE id=?",ident)
    if not plot or not plot["crop_image"]:
        abort(404)
    return send_file(DATA/plot["pdf_version_id"]/plot["crop_image"],mimetype="image/png")


@graph.get("/api/plot/<ident>/export/<kind>")
def export(ident,kind):
    if kind not in ("csv","xlsx"):
        abort(404)
    with db() as connection:
        plot = one(connection,"SELECT * FROM plot_dataset WHERE id=?",ident)
    if not plot:
        abort(404)
    axes,uncertainty = plot["axes"],plot["uncertainty"] or {}
    header=("plot_id","series","x","x_unit","y","y_unit","pdf_x","pdf_y","publication_id","pdf_version_id",
            "page","figure","panel","is_approximate","x_uncertainty","y_uncertainty","uncertainty_method","source")
    data=[(plot["id"],plot["series_label"],p["x"],axes.get("x",{}).get("unit"),p["y"],axes.get("y",{}).get("unit"),
           p["px"],p["py"],plot["publication_id"],plot["pdf_version_id"],plot["source_page"],plot["source_figure"],
           plot["panel_id"],True,uncertainty.get("x"),uncertainty.get("y"),plot["uncertainty_method"],
           f"/#xray/{plot['pdf_version_id']}/point,{plot['source_page']},{p['px']},{p['py']}") for p in plot["points"]]
    if kind=="csv":
        stream=io.StringIO(newline="")
        csv.writer(stream).writerows([[csv_value(value) for value in record] for record in (header,*data)])
        content=stream.getvalue().encode("utf-8-sig")
    else:
        workbook=Workbook()
        sheet=workbook.active
        sheet.title="Series"
        for record in (header,*data):
            sheet.append(record)
            for cell in sheet[sheet.max_row]:
                if isinstance(cell.value,str):
                    cell.data_type="s"
        stream=io.BytesIO()
        workbook.save(stream)
        content=stream.getvalue()
    return send_file(io.BytesIO(content),as_attachment=True,download_name=f"softx-series-{ident}.{kind}")
