"""Relational, lossless JSON and spreadsheet exports from one SQLite read snapshot."""
import csv
import io
import json
from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile

from flask import Blueprint, send_file
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .science import issue_report
from .store import TABLES, data_dir, db, rows
from .submission import submission_files

exporter=Blueprint("exports",__name__)


def dump(value):
    return json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2).encode()


def csv_value(value):
    if isinstance(value,(list,dict)):
        return json.dumps(value, ensure_ascii=False,separators=(",",":"))
    if isinstance(value,str) and re.match(r"^\s*[=+@-]",value) and not re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?",value):
        return "'"+value
    return value


def table_csv(headers, iterable):
    data=io.StringIO(newline="")
    writer=csv.DictWriter(data,fieldnames=headers)
    writer.writeheader()
    for record in iterable:
        writer.writerow({k:csv_value(record.get(k)) for k in headers})
    return data.getvalue().encode("utf-8-sig")


def short_cell(sheet,value):
    if isinstance(value,(dict,list)):
        value=json.dumps(value,ensure_ascii=False,separators=(",",":"))
    # Excel cells have a hard 32,767-character limit. The JSON and CSV copies retain the complete payload.
    if isinstance(value,str):
        value=value if len(value)<=32767 else value[:32690]+" … [полное поле в JSON/CSV]"
    cell=WriteOnlyCell(sheet,value=value)
    if isinstance(value,str):
        cell.data_type="s"
    return cell


def workbook_bytes(dataset, columns):
    workbook=Workbook(write_only=True)
    for table,records in dataset.items():
        sheet=workbook.create_sheet(table)
        fields=columns[table]
        sheet.freeze_panes="B2"
        if fields and records:
            sheet.auto_filter.ref=f"A1:{get_column_letter(len(fields))}{len(records)+1}"
        for index,field in enumerate(fields,1):
            default=max(16,min(38,len(field)+5))
            if field=="sha256" or field=="pdf_version_id" or (field=="id" and table=="pdf_version"):
                width=68
            elif field.endswith("_id") or (field=="id" and table!="audit_log"):
                width=max(27,default)
            elif field in ("device_type","rule_code","acquisition_method","digitization_method","corpus_status","processing_status"):
                width=29
            elif field in ("title","source_fragment","description","hypothesis","extraction_status","caption_text","uncertainty_method"):
                width=44
            else:
                width=default
            sheet.column_dimensions[get_column_letter(index)].width=width
        header=[short_cell(sheet,field) for field in fields]
        for cell in header:
            cell.fill=PatternFill("solid",fgColor="40308B")
            cell.font=Font(name="Calibri",bold=True,color="FFFFFF")
        sheet.append(header)
        for record in records:
            cells=[short_cell(sheet,record[field]) for field in fields]
            for field,cell in zip(fields,cells):
                if field in ("title","source_fragment","description","hypothesis","uncertainty_method") and cell.value:
                    cell.alignment=Alignment(vertical="top",wrap_text=True)
            sheet.append(cells)
    output=io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def relational_snapshot():
    with db() as connection:
        connection.execute("BEGIN")
        data={}
        columns={}
        for table in TABLES:
            schema=rows(connection,f"PRAGMA table_info({table})")
            columns[table]=[col["name"] for col in schema]
            pks=[col["name"] for col in sorted(schema,key=lambda col:col["pk"]) if col["pk"]]
            data[table]=rows(connection,f"SELECT * FROM {table} ORDER BY {', '.join(pks)}")
        report=issue_report(connection)
    return data,columns,report


def artifacts(data,columns,report,asset_files=False):
    report_fields=("id","publication_id","publication_title","rule_code","rule_name","severity","resolution_status","description",
                   "hypothesis","compared_values","threshold","result_ids","sources","updated_at")
    files={"dataset.json":dump(data),
           "dataset.ndjson":b"".join(json.dumps({"table":table,"row":row},ensure_ascii=False,allow_nan=False).encode()+b"\n"
                                    for table,records in data.items() for row in records),
           "dataset.xlsx":workbook_bytes(data,columns),
           "validation_report.json":dump(report),"validation_report.csv":table_csv(report_fields,report)}
    files.update({"csv/"+table+".csv":table_csv(columns[table],records) for table,records in data.items()})
    if asset_files:
        for row in data["pdf_version"]:
            original=data_dir()/row["id"]/"original.pdf"
            if original.is_file():
                files["assets/"+row["id"]+"/original.pdf"]=original
        for row in data["plot_dataset"]:
            if row["crop_image"]:
                name=row["pdf_version_id"]+"/"+row["crop_image"]
                image=data_dir()/name
                if image.is_file():
                    files["assets/"+name]=image
    return files


def directory_export(destination):
    data,columns,report=relational_snapshot()
    target=Path(destination)
    for filename,content in artifacts(data,columns,report,True).items():
        file=target/filename
        file.parent.mkdir(parents=True,exist_ok=True)
        file.write_bytes(content.read_bytes() if isinstance(content,Path) else content)
    return {table:len(records) for table,records in data.items()}


@exporter.get("/api/export/dataset.zip")
def dataset():
    data,columns,report=relational_snapshot()
    result=io.BytesIO()
    with ZipFile(result,"w",ZIP_DEFLATED) as archive:
        for filename,content in artifacts(data,columns,report,True).items():
            if isinstance(content,Path):
                archive.write(content,filename)
            else:
                archive.writestr(filename,content)
    result.seek(0)
    return send_file(result,as_attachment=True,download_name="softx-dataset.zip",mimetype="application/zip")


@exporter.get("/api/export/validation_report.json")
def validation():
    with db() as connection:
        connection.execute("BEGIN")
        result=dump(issue_report(connection))
    return send_file(io.BytesIO(result),mimetype="application/json",as_attachment=True,download_name="validation_report.json")


@exporter.get("/api/export/submission.zip")
def submission():
    data, _, report = relational_snapshot()
    result = io.BytesIO()
    with ZipFile(result, "w", ZIP_DEFLATED) as archive:
        for name, content in submission_files(data, report):
            archive.writestr(name, content)
    result.seek(0)
    return send_file(result, as_attachment=True, download_name="softx-submission.zip", mimetype="application/zip")
