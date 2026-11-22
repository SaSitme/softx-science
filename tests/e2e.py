"""End-to-end acceptance against a fresh real server and SQLite, via Chromium."""
import contextlib
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from zipfile import ZipFile

from openpyxl import load_workbook
from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


class expect:
    """Poll browser state with public locator APIs."""
    def __init__(self, locator):
        self.locator = locator

    def check(self, predicate, wanted, timeout=6000):
        end = time.monotonic() + timeout / 1000
        actual = None
        while time.monotonic() < end:
            if self.locator.count():
                actual = self.locator.first.text_content(timeout=1200)
            if predicate():
                return
            time.sleep(.08)
        raise AssertionError(f"{self.locator}: expected {wanted!r}, got {actual!r}")

    def to_be_visible(self, timeout=6000):
        self.check(self.locator.first.is_visible, "visible", timeout)

    def to_have_count(self, value):
        self.check(lambda: self.locator.count() == value, value)

    def to_have_text(self, value):
        self.check(lambda: self.locator.count() and self.locator.first.text_content() == value, value)

    def to_contain_text(self, value):
        self.check(lambda: self.locator.count() and value in self.locator.first.text_content(), value)

    def to_have_class(self, value):
        self.check(lambda: self.locator.count() and self.locator.first.get_attribute("class") == value, value)


@contextlib.contextmanager
def server(data, *, private=False):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    with (data / "server.log").open("w+") as log:
        process = subprocess.Popen([sys.executable, "-m", "softx.app"], cwd=ROOT, stdout=log, stderr=log,
                                   env=dict(os.environ, SOFTX_DATA=str(data), SOFTX_DEMO="0", PORT=str(port),
                                            SOFTX_PRIVATE_WORKSPACES="1" if private else "0"))
        try:
            for _ in range(80):
                try:
                    urllib.request.urlopen(url, timeout=1).close()
                    break
                except OSError:
                    if process.poll() is not None:
                        raise RuntimeError("SoftX failed to launch")
                    time.sleep(.1)
            else:
                raise RuntimeError("SoftX timed out")
            yield url
        except Exception:
            log.seek(0)
            print(log.read()[-6500:], file=sys.stderr)
            raise
        finally:
            process.terminate()
            process.wait(timeout=10)


def choose(control, text):
    option = control.locator("option").filter(has_text=text).first
    control.select_option(option.get_attribute("value"))


def stage1(page, url):
    page.goto(url)
    page.get_by_role("button", name="Загрузить PDF", exact=True).click()
    page.locator("#files").set_input_files(sorted(FIXTURES.glob("*.pdf")))
    page.get_by_role("button", name="Обработать PDF").click()
    broken = page.locator("tr").filter(has_text="05-corrupted.pdf")
    working = page.locator("tr").filter(has_text="01-fabricated.pdf")
    expect(broken.get_by_text("Ошибка PDF", exact=True)).to_be_visible(timeout=15000)
    expect(working.get_by_text("Обработан", exact=True)).to_be_visible(timeout=15000)
    expect(page.locator("tr").filter(has_text="03-tandem.pdf").get_by_text("Тандем", exact=True)).to_be_visible()
    expect(page.locator("tr").filter(has_text="04-theoretical.pdf").get_by_text("Теоретическая", exact=True)).to_be_visible()
    for row, label in [(broken,"Ошибка PDF"),(working,"Обработан")]:
        with page.expect_response(lambda res: res.url.endswith("/retry") and res.status==202):
            row.get_by_role("button", name="Повторить обработку").click()
        expect(row.get_by_text(label, exact=True)).to_be_visible(timeout=15000)
    expect(page.locator("tbody tr")).to_have_count(5)
    print("PASS 1 · batch, corpus, individual failure and replay", flush=True)
    if os.getenv("SOFTX_SCREENSHOT"):
        page.screenshot(path=os.environ["SOFTX_SCREENSHOT"], full_page=True)

    working.get_by_role("link", name="Открыть PDF X-Ray").click()
    expect(page.get_by_role("heading",name="Perovskite thin films under AM1.5G")).to_be_visible()
    page.get_by_text("Приборы, материалы и измерения", exact=False).click()
    page.get_by_text("B · target · p-i-n", exact=False).click()
    expect(page.get_by_text("MAPbI3 · 500 nm", exact=False)).to_be_visible()
    page.locator(".record").filter(has_text="16 %").click()
    page.get_by_role("button",name="Проверить и исправить").click()
    editor=page.locator("#edit-dialog")
    choose(editor.get_by_label("Прибор",exact=True),"B · кандидат")
    choose(editor.get_by_label("Измерение",exact=True),"reverse")
    editor.get_by_role("button",name="Сохранить").click()
    expect(page.get_by_text("Подтверждено пользователем",exact=True)).to_be_visible()
    page.locator('nav a[href="#results"]').click()
    page.locator("#results-filters select[name=result_origin]").select_option("cited")
    expect(page.locator("tbody tr")).to_have_count(4)
    page.get_by_label("Поиск данных").fill("Voc")
    expect(page.locator("tbody tr")).to_have_count(1)
    headers=page.get_by_role("columnheader").all_text_contents()
    assert headers[:5]==["Код публикации","Показатель","Значение","Единица измерения","Обозначение прибора"]
    page.get_by_label("Поиск данных").fill("Напряжение холостого хода")
    expect(page.locator("tbody tr")).to_have_count(1)
    page.get_by_role("link",name="Напряжение холостого хода (Voc)",exact=True).click()
    focused=page.locator("rect.highlight-value.highlight-focused")
    expect(focused).to_have_count(1)
    assert focused.evaluate("node => getComputedStyle(node).fill")=="rgba(239, 49, 64, 0.48)"
    assert focused.evaluate("node => getComputedStyle(node).strokeDasharray")=="none"
    print("PASS 2 · devices, layers, origin, candidates, search and filter", flush=True)

    page.locator('nav a[href="#library"]').click()
    page.locator("tr").filter(has_text="02-rotated.pdf").get_by_role("link",name="Открыть PDF X-Ray").click()
    expect(page.locator("#pdf-angle")).to_have_text("90°")
    target=page.locator(".record").filter(has_text="Напряжение холостого хода")
    target.click()
    ref=page.locator("rect.highlight-value.highlight-current").first
    expect(ref).to_be_visible()
    ref_id=ref.get_attribute("data-ref")
    for new_angle in ("180°","270°"):
        page.get_by_role("button",name="Увеличить масштаб").click()
        page.get_by_role("button",name="Повернуть страницу").click()
        expect(page.locator("#pdf-angle")).to_have_text(new_angle)
        target.click()
        ref=page.locator(f'rect[data-ref="{ref_id}"]')
        ref.click()
        expect(page.locator(".record.active")).to_contain_text("Напряжение холостого хода (Voc)")
        expect(ref).to_have_class("highlight-value highlight-current highlight-focused")
        image=page.locator(".pdf-page img")
        expect(image).to_be_visible()
        assert image.evaluate("(image) => image.complete && image.naturalWidth > 0")
        a,b=ref.bounding_box(), image.bounding_box()
        assert b["x"] < a["x"] < b["x"]+b["width"] and b["y"] < a["y"] < b["y"]+b["height"]
    print("PASS 3 · original CropBox/Rotate, zoom, bidirectional PDF navigation", flush=True)

    page.locator('nav a[href="#library"]').click()
    working.get_by_role("link",name="Открыть PDF X-Ray").click()
    page.locator(".record").filter(has_text="16 %").click()
    page.get_by_role("button",name="Проверить и исправить").click()
    editor.get_by_label("Значение",exact=True).fill("15.5")
    editor.get_by_label("Единица",exact=True).fill("% ")
    choose(editor.get_by_label("Прибор",exact=True),"A · кандидат")
    choose(editor.get_by_label("Измерение",exact=True),"forward")
    editor.get_by_role("button",name="Сохранить").click()
    expect(page.locator(".record.active")).to_contain_text("15.5")
    expect(page.locator("#active-record")).to_contain_text("16.00 %")
    with page.expect_response(lambda res: res.url.endswith("/retry") and res.status==202):
        page.get_by_role("button",name="Повторить обработку",exact=True).click()
    expect(page.get_by_text("Обработан",exact=True)).to_be_visible(timeout=15000)
    page.reload()
    page.locator(".record").filter(has_text="15.5").click()
    expect(page.locator(".record.active")).to_contain_text("A · Прямой ход")
    expect(page.locator("#active-record")).to_contain_text("16.00 %")
    page.get_by_text("История изменений",exact=False).click()
    for name in ("Значение","Единица измерения","Идентификатор прибора","Идентификатор измерения"):
        expect(page.locator("details").last.get_by_text(name,exact=True).first).to_be_visible()
    expect(page.locator(".record")).to_have_count(23)
    print("PASS 4 · manual edits, immutable original, audit and reprocess", flush=True)


def stage2a(page, url, output):
    version=hashlib.sha256((FIXTURES/"01-fabricated.pdf").read_bytes()).hexdigest()
    for retired in ("graphs","detective"):
        page.goto(url+"/#"+retired)
        expect(page.get_by_role("heading",name="Корпус публикаций")).to_be_visible()
    expect(page.locator('nav a[href="#graphs"], nav a[href="#detective"]')).to_have_count(0)
    detail=page.request.get(url+"/api/pdf/"+version).json()
    results={r["metric_name"]:r for r in detail["results"] if r["extraction_method"]=="plot_calculation"}
    assert set(results)=={"Voc","Jsc","FF","PCE"}
    assert results["Voc"]["normalized_value"]==1 and results["Jsc"]["normalized_value"]==20
    assert abs(results["FF"]["normalized_value"]-53.196)<.001
    assert abs(results["PCE"]["normalized_value"]-10.6392)<.001
    assert results["PCE"]["calculation_inputs"]["pin_mW_cm2"]==100
    assert all(r["raw_value"] is None and r["plot_id"] for r in results.values())
    plot=detail["plots"][0]
    page.goto(url+"/#xray/"+version+"/"+results["Voc"]["id"])
    expect(page.locator("#active-record")).to_contain_text("Приближённое значение по графику")
    excepted_source=next(s for s in detail["sources"] if s["source_type"]=="figure" and s["fragment_role"]=="condition")
    page.goto(url+"/#xray/"+version+"/ref,"+excepted_source["id"])
    expect(page.locator(".highlight-focused").first).to_be_visible()
    plot["axes"]["x"]["v1"]=1.2
    changed=page.request.patch(url+"/api/plot/"+plot["id"],data=plot)
    assert changed.ok,changed.text()
    updated=page.request.get(url+"/api/pdf/"+version).json()
    voc=[r for r in updated["results"] if r["id"]==results["Voc"]["id"]]
    assert len(voc)==1 and voc[0]["normalized_value"]==1.2
    for extension in ("csv","xlsx"):
        downloaded=page.request.get(url+"/api/plot/"+plot["id"]+"/export/"+extension)
        assert downloaded.ok
        (output/f"series.{extension}").write_bytes(downloaded.body())
    with (output/"series.csv").open(encoding="utf-8-sig") as file:
        records=list(csv.DictReader(file))
    assert len(records)==11 and float(records[-1]["x"])==1.2 and float(records[-1]["pdf_x"])==495
    assert records[-1]["panel"]=="a" and records[-1]["x_unit"]=="V"
    workbook=load_workbook(output/"series.xlsx",read_only=True,data_only=True)
    try:
        excel=list(workbook.active.values)
        assert len(excel)==12 and excel[-1][2]==1.2 and excel[-1][13] is True
    finally:
        workbook.close()
    print("PASS 5 · vector JV, reactive calibration, PDF point, downloaded CSV and XLSX",flush=True)


def stage2b(page, url):
    paths=sorted((FIXTURES/"graphs").glob("*.pdf"))
    page.goto(url)
    page.get_by_role("button",name="Загрузить PDF",exact=True).click()
    page.locator("#files").set_input_files(paths)
    page.get_by_role("button",name="Обработать PDF").click()
    for name, kind, count in (("eqe","EQE",5),("stability","stability",5),("thermal","thermal",4),
                               ("pce","PCE_vs_param",4),("scatter","scatter",4)):
        row=page.locator("tr").filter(has_text=name+".pdf")
        expect(row.get_by_text("Обработан",exact=True)).to_be_visible(timeout=15000)
        file=FIXTURES/"graphs"/(name+".pdf")
        version=hashlib.sha256(file.read_bytes()).hexdigest()
        detail=page.request.get(url+"/api/pdf/"+version).json()
        series=detail["plots"][0]
        assert series["plot_type"]==kind and len(series["points"])==count
        if kind in ("EQE","stability"):
            metric="Jsc_EQE" if kind=="EQE" else "T80"
            answer=page.request.post(url+"/api/plot/"+series["id"]+"/calculate",data={"metric":metric})
            assert answer.ok,answer.text()
            result=answer.json()
            assert result["formula"]
            if metric=="T80":
                assert abs(result["normalized_value"]-67.857)<.001
        page.goto(url)
    print("PASS 2B · EQE integration, T80 interpolation, thermal, process and scatter graphs", flush=True)


def graph_without_irradiance(page,url):
    source=(FIXTURES/"01-fabricated.pdf").read_bytes()
    content=source.replace(b"100 mW/cm2",b"unreported")
    assert len(source)==len(content) and source!=content
    version=hashlib.sha256(content).hexdigest()
    page.goto(url)
    page.get_by_role("button",name="Загрузить PDF",exact=True).click()
    page.locator("#files").set_input_files({"name":"irradiance-unreported.pdf","mimeType":"application/pdf","buffer":content})
    page.get_by_role("button",name="Обработать PDF").click()
    row=page.locator("tr").filter(has_text="irradiance-unreported.pdf")
    expect(row.get_by_text("Обработан",exact=True)).to_be_visible(timeout=15000)
    detail=page.request.get(url+"/api/pdf/"+version).json()
    assert {r["metric_name"] for r in detail["results"] if r["plot_id"]}=={"Voc","Jsc","FF"}
    print("PASS · graph extraction skips PCE without published irradiance",flush=True)


def stage3(page,url,output):
    issues=page.request.get(url+"/api/issues").json()
    discrepancy=next(i for i in issues if i["rule_code"]=="PCE_CONSISTENCY"
                     and i["resolution_status"]=="open" and any(
                         source["source_fragment"]=="17.00" for source in i["sources"]))
    assert discrepancy["threshold"]["relative"]==.03
    assert any(i["rule_code"]=="EQE_JSC" and i["severity"]=="warning" for i in issues)
    assert any(i["rule_code"]=="VOC_BANDGAP" and i["severity"] in ("warning","error") for i in issues)
    assert any(i["rule_code"]=="PCE_CONSISTENCY" and i["resolution_status"]=="not_applicable"
               and "Pin" in i["description"] for i in issues)
    source=next(s for s in discrepancy["sources"] if s["source_fragment"]=="17.00" and s["fragment_role"]=="value")
    page.goto(url+source["href"])
    expect(page.locator(".record.active")).to_contain_text("A · Обратный ход")
    expect(page.locator("#active-record")).to_contain_text("17.00 %")
    page.get_by_role("button",name="Проверить и исправить").click()
    editor=page.locator("#edit-dialog")
    editor.get_by_label("Значение",exact=True).fill("18")
    editor.get_by_role("button",name="Сохранить").click()
    expect(page.locator("#active-record h3")).to_contain_text("18 %")
    expect(page.locator("#active-record")).to_contain_text("17.00 %")
    updated=page.request.get(url+"/api/issues").json()
    assert any(i["id"]==discrepancy["id"] and i["resolution_status"]=="resolved" for i in updated)
    print("PASS 6 · validation API, PDF evidence and reactive resolution",flush=True)

    page.goto(url+"/#compare")
    left,right=page.get_by_label("Запись A",exact=True),page.get_by_label("Запись B",exact=True)
    choose(left,"Коэффициент полезного действия (PCE) · 18 % · A · Прямой ход")
    choose(right,"Коэффициент полезного действия (PCE) · 14 % · B · Обратный ход")
    expect(page.get_by_text("Несопоставимы",exact=True)).to_be_visible()
    expect(page.locator(".criteria-failed").filter(has_text="Направление развёртки")).to_have_count(1)
    page.get_by_role("link",name="Сертифицированные",exact=True).click()
    expect(page.get_by_text("В этой вкладке пока нет записей.",exact=True)).to_be_visible()
    page.get_by_role("link",name="Стабильность",exact=True).click()
    expect(page.locator("select[name=compare-a] option").filter(has_text="Время снижения эффективности до 80% (T80)")).to_have_count(1)

    page.goto(url+"/#export")
    with page.expect_download() as package:
        page.get_by_role("link",name="Скачать набор ZIP").click()
    package.value.save_as(output/"dataset.zip")
    with page.expect_download() as report:
        page.get_by_role("link",name="Скачать отчёт JSON").click()
    report.value.save_as(output/"issues.json")
    with ZipFile(output/"dataset.zip") as zip_file:
        assert zip_file.testzip() is None
        dataset=json.loads(zip_file.read("dataset.json"))
        validations=json.loads(zip_file.read("validation_report.json"))
        assert validations==json.loads((output/"issues.json").read_text())
        ndjson=[json.loads(record) for record in zip_file.read("dataset.ndjson").splitlines()]
        assert len(ndjson)==sum(map(len,dataset.values()))
        assert len(dataset["pdf_version"])==10
        for table in dataset:
            assert "csv/"+table+".csv" in zip_file.namelist()
        result_ids={r["id"] for r in dataset["measurement_result"]}
        source_ids={r["id"] for r in dataset["source_reference"]}
        assert all(row["result_id"] in result_ids and row["source_reference_id"] in source_ids for row in dataset["result_source"])
        plot_ids={r["id"] for r in dataset["plot_dataset"]}
        assert all(row["result_id"] in result_ids and row["plot_id"] in plot_ids for row in dataset["result_plot"])
        assert any(r["resolution_status"]=="resolved" and r["rule_code"]=="PCE_CONSISTENCY" for r in validations)
        assert all(source["id"] in source_ids for r in validations for source in r["sources"])
        for version in dataset["pdf_version"]:
            archive_pdf=zip_file.read("assets/"+version["id"]+"/original.pdf")
            assert hashlib.sha256(archive_pdf).hexdigest()==version["sha256"]
        (output/"dataset.xlsx").write_bytes(zip_file.read("dataset.xlsx"))
    workbook=load_workbook(output/"dataset.xlsx",read_only=True,data_only=True)
    try:
        assert set(workbook.sheetnames)==set(dataset)
        for table,rows in dataset.items():
            assert len(list(workbook[table].values))==1+len(rows)
    finally:
        workbook.close()
    print("PASS 7 · comparison criteria, downloaded JSON/NDJSON/CSV/XLSX, PDF hashes and foreign keys",flush=True)


def off_topic_pdf(page, url):
    path = ROOT / "output" / "pdf" / "softx-off-topic-power-supply.pdf"
    version = hashlib.sha256(path.read_bytes()).hexdigest()
    page.goto(url)
    page.get_by_role("button", name="Загрузить PDF", exact=True).click()
    page.locator("#files").set_input_files(path)
    page.get_by_role("button", name="Обработать PDF").click()
    row = page.locator("tr").filter(has_text=path.name)
    expect(row.get_by_text("Обработан", exact=True)).to_be_visible(timeout=15000)
    expect(row.get_by_text("На рассмотрении", exact=True)).to_be_visible()
    row.get_by_role("link", name="Открыть PDF X-Ray").click()
    expect(page.locator(".pdf-page img")).to_be_visible()
    expect(page.locator(".highlight-value, .highlight-unit")).to_have_count(0)
    detail = page.request.get(f"{url}/api/pdf/{version}").json()
    assert detail["pdf"]["pv_extraction_enabled"] == 0
    assert not detail["results"] and not detail["links"]
    print("PASS · power supply efficiency is not extracted as photovoltaic PCE", flush=True)


def raster_figure(page,url):
    writer=PdfWriter(clone_from=PdfReader(FIXTURES/"02-rotated.pdf"))
    added=writer.add_blank_page(width=620,height=790)
    image=Image.new("RGB",(300,200),"white")
    draw=ImageDraw.Draw(image)
    draw.rectangle((25,15,285,170),outline="black",width=2)
    draw.line(((25,49),(145,54),(208,72),(248,112),(268,170)),fill="green",width=3)
    bitmap=DecodedStreamObject()
    bitmap.update({NameObject(k):NameObject(v) if isinstance(v,str) else NumberObject(v) for k,v in
                   {"/Type":"/XObject","/Subtype":"/Image","/ColorSpace":"/DeviceRGB",
                    "/Width":image.width,"/Height":image.height,"/BitsPerComponent":8}.items()})
    bitmap.set_data(image.tobytes())
    added[NameObject("/Resources")]=DictionaryObject({
        NameObject("/XObject"):DictionaryObject({NameObject("/Curve"):writer._add_object(bitmap)}),
        NameObject("/Font"):writer.pages[0]["/Resources"]["/Font"],
    })
    stream=DecodedStreamObject()
    stream.set_data(b"""q 300 0 0 200 95 462 cm /Curve Do Q
        BT /F1 11 Tf 95 440 Td (Figure 8. Photocurrent-voltage curves of CdTe devices.) Tj ET
        q 300 0 0 200 95 160 cm /Curve Do Q
        BT /F1 11 Tf 95 138 Td (Figure 9. Photo of the thermal annealing equipment.) Tj ET""")
    added[NameObject("/Contents")]=writer._add_object(stream)
    output=io.BytesIO()
    writer.write(output)
    content=output.getvalue()
    version=hashlib.sha256(content).hexdigest()
    page.goto(url)
    page.get_by_role("button",name="Загрузить PDF",exact=True).click()
    page.locator("#files").set_input_files({"name":"embedded-figure.pdf","mimeType":"application/pdf","buffer":content})
    page.get_by_role("button",name="Обработать PDF").click()
    expect(page.locator("tr").filter(has_text="embedded-figure.pdf").get_by_text("Обработан",exact=True)).to_be_visible(timeout=15000)
    detail=page.request.get(f"{url}/api/pdf/{version}").json()
    assert len(detail["plots"])==1
    plot=detail["plots"][0]
    assert plot["source_figure"]=="8" and plot["source_page"]==2 and plot["plot_bbox"]==[95,462,395,662]
    assert not plot["points"] and not any(result["plot_id"] for result in detail["results"])
    assert page.request.get(url+"/api/plot/"+plot["id"]+"/image").ok
    trace=page.request.post(url+"/api/pdf/"+version+"/trace",data={
        "source_page":2,"plot_bbox":plot["plot_bbox"],"seed":[240,608],
    })
    assert trace.ok,trace.text()
    points=trace.json()["points"]
    assert len(points)>50
    plot.update(digitization_method="raster_trace",points=points,axes={
        "x":{"p0":95,"p1":395,"v0":0,"v1":1.2,"unit":"V"},
        "y":{"p0":462,"p1":662,"v0":0,"v1":25,"unit":"mA/cm2"},
    })
    response=page.request.patch(url+"/api/plot/"+plot["id"],data=plot)
    assert response.ok,response.text()
    saved=response.json()
    assert saved["digitization_method"]=="raster_trace" and saved["is_approximate"] and saved["points"][0]["x"] is not None
    print("PASS · raster curve tracing, calibration and saving approximate pixels",flush=True)


def manual_result(page,url):
    version=hashlib.sha256((FIXTURES/"02-rotated.pdf").read_bytes()).hexdigest()
    detail_url=f"{url}/api/pdf/{version}"
    initial=page.request.get(detail_url).json()
    existing=next(r for r in initial["results"] if r["metric_name"]=="PCE" and r["normalized_value"]==12)
    page.goto(f"{url}/#xray/{version}/{existing['id']}")
    highlight=page.locator(".highlight-value.highlight-focused")
    expect(highlight).to_be_visible()
    highlight.scroll_into_view_if_needed()
    coords=highlight.bounding_box()
    page.get_by_role("button",name="Добавить запись из PDF").click()
    page.mouse.move(coords["x"]+1,coords["y"]+1)
    page.mouse.down()
    page.mouse.move(coords["x"]+coords["width"]+8,coords["y"]+coords["height"]+5,steps=6)
    page.mouse.up()
    form=page.locator("dialog[open]")
    form.get_by_label("Число как в PDF").fill("12.00")
    form.get_by_label("Единица как в PDF").fill("%")
    form.get_by_label("Цитата из выделения").fill("PCE = 12.00 %")
    with page.expect_response(lambda res:res.url.endswith("/result") and res.status==201) as created:
        form.get_by_role("button",name="Добавить запись").click()
    result=created.value.json()
    expect(page.locator(".highlight-value.highlight-focused")).to_be_visible()
    page.get_by_label("Только требуют проверки").check()
    expect(page.locator(f'[data-result="{result["id"]}"]')).to_be_visible()
    page.get_by_role("button",name="Проверить и исправить").click()
    page.locator("dialog[open]").get_by_role("button",name="Сохранить").click()
    expect(page.get_by_text("Подтверждено пользователем",exact=True)).to_be_visible()
    after=page.request.get(detail_url).json()
    record=next(r for r in after["results"] if r["id"]==result["id"])
    assert record["raw_value"]=="12.00" and record["raw_unit"]=="%" and record["extraction_method"]=="manual_pdf"
    assert any(row["entity_id"]==result["id"] and row["field_name"]=="created" for row in after["audit"])
    print("PASS · manual result from a rotated PDF, review queue, confirmation and audit",flush=True)


def mobile_layout(browser,url):
    phone=browser.new_page(viewport={"width":390,"height":844},is_mobile=True,has_touch=True)
    try:
        phone.goto(url)
        expect(phone.locator(".publication-card").first).to_be_visible()
        assert phone.locator("#nav").evaluate("nav => nav.scrollWidth === nav.clientWidth")
        phone.locator('nav a[href="#results"]').tap()
        result=phone.locator(".result-card").first
        expect(result).to_be_visible()
        assert phone.locator(".result-filter-panel").get_attribute("open") is None
        result.get_by_text("Все поля",exact=True).tap()
        expect(result.get_by_text("Нормализованное значение",exact=True)).to_be_visible()
        result.get_by_role("link",name="К источнику в PDF").tap()
        expect(phone.locator(".highlight-focused").first).to_be_visible()
        phone.get_by_role("button",name="Записи",exact=True).tap()
        expect(phone.locator("#active-record h3")).to_be_visible()
        phone.locator('nav a[href="#results"]').tap()
        phone.get_by_role("button",name="Таблица результатов").tap()
        table=phone.get_by_role("region",name="Таблица данных")
        expect(table).to_be_visible()
        assert table.evaluate("region => {region.scrollLeft=200; return region.scrollLeft>0;}")
        assert phone.evaluate("document.documentElement.scrollWidth === innerWidth")
    finally:
        phone.close()
    print("PASS · phone layout, expandable result fields, PDF navigation and table scrolling",flush=True)


def private_libraries(browser, url, data=None):
    first = browser.new_context()
    second = browser.new_context()
    a, b = first.new_page(), second.new_page()
    pdf = FIXTURES / "01-fabricated.pdf"
    version = hashlib.sha256(pdf.read_bytes()).hexdigest()
    detail = f"{url}/api/pdf/{version}"

    def upload(page):
        page.get_by_role("button", name="Загрузить публикации").first.click()
        page.locator("#files").set_input_files(str(pdf))
        page.get_by_role("button",name="Обработать PDF").click()
        row = page.locator("tr").filter(has_text=pdf.name)
        expect(row.get_by_text("Обработан", exact=True)).to_be_visible(timeout=20000)

    try:
        a.goto(url)
        b.goto(url)
        expect(a.get_by_text("Моя библиотека", exact=True)).to_be_visible()
        expect(b.get_by_text("Моя библиотека", exact=True)).to_be_visible()
        upload(a)
        assert len(a.request.get(url + "/api/library").json()["publications"]) == 1
        assert b.request.get(url + "/api/library").json()["publications"] == []
        assert b.request.get(detail).status == 404
        assert b.request.get(detail + "/original").status == 404
        assert b.request.get(detail + "/page/1").status == 404
        first_record = a.request.get(detail).json()["results"][0]
        result_url = url + "/api/result/" + first_record["id"]
        assert b.request.delete(result_url).status == 404

        upload(b)
        a.goto(f"{url}/#xray/{version}/{first_record['id']}")
        a.get_by_role("button", name="Удалить запись").click()
        a.get_by_role("button", name="Отмена").click()
        assert any(r["id"] == first_record["id"] for r in a.request.get(detail).json()["results"])
        a.get_by_role("button", name="Удалить запись").click()
        a.locator("dialog[open]").get_by_role("button", name="Удалить запись").click()
        expect(a.get_by_text("Запись удалена", exact=True)).to_be_visible()
        a.reload()
        a.locator("[data-retry]").click()
        expect(a.locator("main").get_by_text("Обработан", exact=True)).to_be_visible(timeout=20000)
        own = a.request.get(detail).json()
        other = b.request.get(detail).json()
        assert all(r["id"] != first_record["id"] for r in own["results"])
        assert any(r["id"] == first_record["id"] for r in other["results"])
        assert any(row["entity_id"] == first_record["id"] and row["field_name"] == "deleted"
                   for row in own["audit"])
        assert a.request.get(detail + "/original").ok
        a.get_by_role("button",name="Удалить статью").click()
        a.locator("dialog[open]").get_by_role("button",name="Удалить статью").click()
        expect(a.get_by_text("Статья удалена",exact=True)).to_be_visible()
        assert a.request.get(detail + "/original").status == 404
        assert b.request.get(detail + "/original").ok
        if data is not None:
            assert len(list((data / "workspaces").glob("*/softx.sqlite"))) == 2
        else:
            key_a, = [cookie for cookie in first.cookies() if cookie["name"] == "__Host-softx_workspace"]
            key_b, = [cookie for cookie in second.cookies() if cookie["name"] == "__Host-softx_workspace"]
            assert key_a["value"] != key_b["value"] and key_a["secure"] and key_b["secure"]
    finally:
        first.close()
        second.close()
    print("PASS · separate browser libraries, isolated PDF files and deletion preserved after reprocessing",flush=True)


def main():
    with tempfile.TemporaryDirectory(prefix="softx-e2e-") as directory, server(Path(directory)) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get("CHROMIUM","/usr/bin/chromium"),
                                              headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width":1440,"height":1024}, accept_downloads=True)
        page.set_default_timeout(6000)
        page.on("pageerror", lambda ex: print(f"BROWSER ERROR: {ex}",file=sys.stderr))
        stage1(page,url)
        stage2a(page,url,Path(directory))
        stage2b(page,url)
        stage3(page,url,Path(directory))
        graph_without_irradiance(page,url)
        off_topic_pdf(page,url)
        raster_figure(page,url)
        mobile_layout(browser,url)
        manual_result(page,url)
        with tempfile.TemporaryDirectory(prefix="softx-private-e2e-") as workspaces, server(Path(workspaces), private=True) as private_url:
            private_libraries(browser,private_url,Path(workspaces))
        browser.close()


if __name__ == "__main__":
    main()
