"""End-to-end acceptance against a fresh real server and SQLite, via Chromium."""
import contextlib
import csv
import hashlib
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
from playwright.sync_api import sync_playwright

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
def server(data):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    with (data / "server.log").open("w+") as log:
        process = subprocess.Popen([sys.executable, "-m", "softx.app"], cwd=ROOT, stdout=log, stderr=log,
                                   env=dict(os.environ, SOFTX_DATA=str(data), SOFTX_DEMO="0", PORT=str(port)))
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
    print("PASS 2 · devices, layers, origin, candidates, search and filter", flush=True)

    page.locator('nav a[href="#library"]').click()
    page.locator("tr").filter(has_text="02-rotated.pdf").get_by_role("link",name="Открыть PDF X-Ray").click()
    expect(page.locator("#pdf-angle")).to_have_text("90°")
    target=page.locator(".record").filter(has_text="Voc")
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
        expect(page.locator(".record.active")).to_contain_text("Voc")
        expect(ref).to_have_class("highlight-value highlight-current")
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
    expect(page.locator(".record.active")).to_contain_text("A · forward")
    expect(page.locator("#active-record")).to_contain_text("16.00 %")
    page.get_by_text("История изменений",exact=False).click()
    for name in ("metric_value","metric_unit","device_id","measurement_id"):
        expect(page.locator("details").last.get_by_text(name,exact=True).first).to_be_visible()
    expect(page.locator(".record")).to_have_count(19)
    print("PASS 4 · manual edits, immutable original, audit and reprocess", flush=True)


def stage2a(page, url, output):
    version=hashlib.sha256((FIXTURES/"01-fabricated.pdf").read_bytes()).hexdigest()
    page.goto(url+"/#graphs/"+version)
    expect(page.get_by_role("heading",name="Graph Hunter",exact=True)).to_be_visible()
    expect(page.locator("#point-count")).to_have_text("11 точек")
    expect(page.locator("#source-plot img")).to_be_visible()
    expect(page.locator("[data-reconstruction]")).to_have_count(11)
    page.get_by_label("Значение X2",exact=True).fill("1.2")
    last=page.locator('[data-reconstruction="10"]')
    expect(last.locator("title")).to_contain_text("1.2 V")
    last.click()
    page.get_by_role("link",name="К точке на PDF").click()
    expect(page.locator("#graph-marker")).to_be_visible()
    page.goto(url+"/#graphs/"+version)
    page.get_by_label("Значение X2",exact=True).fill("1.2")
    page.get_by_role("button",name="Сохранить серию").click()
    expect(page.get_by_text("Серия сохранена",exact=True)).to_be_visible()
    expect(page.locator('[data-reconstruction="10"] title')).to_contain_text("1.2 V")
    for extension in ("CSV","XLSX"):
        with page.expect_download() as info:
            page.get_by_role("link",name=extension,exact=True).click()
        info.value.save_as(output/f"series.{extension.lower()}")
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
        page.goto(url+"/#graphs/"+version)
        expect(page.locator("#point-count")).to_have_text(f"{count} точек")
        assert page.locator("select[name=plot_type]").input_value()==kind
        if kind in ("EQE","stability"):
            metric="Jsc_EQE" if kind=="EQE" else "T80"
            page.get_by_role("button",name="Рассчитать "+metric,exact=True).click()
            page.locator("#edit-dialog").get_by_role("button",name="Сохранить",exact=True).click()
            expect(page.locator("#active-record")).to_contain_text("Расчёт")
            expect(page.locator("#active-record h3")).to_contain_text(metric)
            if metric=="T80":
                expect(page.locator("#active-record h3")).to_contain_text("67.857")
        page.goto(url)
    print("PASS 2B · EQE integration, T80 interpolation, thermal, process and scatter graphs", flush=True)


def stage3(page,url,output):
    page.goto(url+"/#detective")
    pce=page.locator('[data-issue=PCE_CONSISTENCY]')
    discrepancy=pce.filter(has_text="Расхождение 5.55555556")
    expect(discrepancy).to_be_visible()
    expect(discrepancy.locator(".rule-threshold")).to_contain_text("3%")
    expect(page.locator('[data-issue=EQE_JSC]')).to_contain_text("15 %")
    expect(page.locator('[data-issue=VOC_BANDGAP]')).to_have_count(2)
    page.get_by_label("Состояние проверки",exact=True).select_option("passed")
    expect(pce.filter(has_text="PCE 14 %")).to_be_visible()
    expect(pce.filter(has_text="PCE 18 %")).to_have_count(1)
    page.get_by_label("Состояние проверки",exact=True).select_option("not_applicable")
    missing=pce.filter(has_text="Experimental CdTe")
    expect(missing).to_contain_text("нет подтверждённой Pin")
    expect(missing).to_contain_text("Проверка невозможна")
    page.get_by_label("Состояние проверки",exact=True).select_option("open")
    discrepancy.locator("summary").click()
    assert discrepancy.locator("a.issue-source").count()>=9
    discrepancy.get_by_role("link",name="с. 1 · Значение · 17.00",exact=True).click()
    expect(page.locator(".record.active")).to_contain_text("A · reverse")
    expect(page.locator("#active-record")).to_contain_text("17.00 %")
    page.get_by_role("button",name="Проверить и исправить").click()
    editor=page.locator("#edit-dialog")
    editor.get_by_label("Значение",exact=True).fill("18")
    editor.get_by_role("button",name="Сохранить").click()
    expect(page.locator("#active-record h3")).to_contain_text("18 %")
    expect(page.locator("#active-record")).to_contain_text("17.00 %")
    page.locator('nav a[href="#detective"]').click()
    expect(page.locator('[data-issue=EQE_JSC]')).to_be_visible()
    expect(discrepancy).to_have_count(0)
    page.get_by_label("Состояние проверки",exact=True).select_option("resolved")
    expect(pce.filter(has_text="PCE 18 %")).to_have_count(1)
    print("PASS 6 · PCE 3%, EQE 5%, Eg, passed checks, isolated scans, missing Pin and reactive resolution",flush=True)

    page.goto(url+"/#compare")
    left,right=page.get_by_label("Запись A",exact=True),page.get_by_label("Запись B",exact=True)
    choose(left,"PCE · 18 % · A · forward")
    choose(right,"PCE · 14 % · B · reverse")
    expect(page.get_by_text("Несопоставимы",exact=True)).to_be_visible()
    expect(page.locator(".criteria-failed").filter(has_text="Направление развёртки")).to_have_count(1)
    page.get_by_role("link",name="Сертифицированные",exact=True).click()
    expect(page.get_by_text("В этой вкладке пока нет записей.",exact=True)).to_be_visible()
    page.get_by_role("link",name="Стабильность",exact=True).click()
    expect(page.locator("select[name=compare-a] option").filter(has_text="T80")).to_have_count(1)

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
        off_topic_pdf(page,url)
        browser.close()


if __name__ == "__main__":
    main()
