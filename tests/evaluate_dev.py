"""Measure dev extraction against the supplied gold CSV using a one-to-one value match."""
import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from softx.store import db, rows

METRICS = {"PCE", "Voc", "Jsc", "FF", "Rs", "Rsh", "HI", "Jsc_EQE", "T80"}


def token(metric, value, unit):
    try:
        value = round(float(value), 5)
    except (TypeError, ValueError):
        return None
    unit = (unit or "").replace("²", "2")
    if metric in ("Rs", "Rsh") and unit in ("Ohm*cm2", "Ω·cm2"):
        unit = "Ohm*cm2"
    return metric, value, unit


def evaluate(gold, predicted):
    report = []
    for publication in sorted({item["publication_id"] for item in gold}):
        expected = Counter(value for item in gold if item["publication_id"] == publication
                           and item["metric_name"] in METRICS and item["extraction_status"] == "extracted"
                           if (value := token(item["metric_name"], item["normalized_value"], item["normalized_unit"])))
        actual = Counter()
        ai = Counter()
        for record in predicted:
            if record["filename"] != publication+".pdf" or record["metric_name"] not in METRICS:
                continue
            value = token(record["metric_name"], record["normalized_value"], record["normalized_unit"])
            if value is not None:
                actual[value] += 1
                if record["extraction_method"] == "local_ai_pdf_quote":
                    ai[value] += 1
        matches = sum((actual & expected).values())
        report.append(dict(
            publication=publication, matches=matches, predicted=sum(actual.values()), expected=sum(expected.values()),
            ai_predictions=sum(ai.values()), ai_additional_matches=matches-sum(((actual-ai) & expected).values()),
            missing=[dict(metric=key[0], value=key[1], unit=key[2], count=count)
                     for key,count in (expected-actual).items()],
        ))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=Path("/home/danya/Изображения/dev_gold/results.csv"))
    parser.add_argument("--pdfs", type=Path, help="Optional directory of dev PDFs to ingest into SOFTX_DATA before evaluation.")
    parser.add_argument("--output", type=Path, default=ROOT/"export"/"dev_comparison.json")
    args = parser.parse_args()
    if args.pdfs:
        from softx.app import ingest, worker
        files = sorted(args.pdfs.glob("D*.pdf"))
        if not files:
            parser.error("no D*.pdf files in --pdfs")
        for file in files:
            ingest(file.read_bytes(), file.name)
        worker.shutdown()
    with args.gold.open(encoding="utf-8-sig") as file:
        gold = list(csv.DictReader(file))
    with db() as connection:
        predicted = rows(connection, """SELECT r.*,v.filename FROM measurement_result r
                         JOIN pdf_version v ON v.id=r.pdf_version_id WHERE v.filename LIKE 'D%.pdf'""")
        statuses = rows(connection, """SELECT filename,processing_status,error_message FROM pdf_version
                           WHERE filename LIKE 'D%.pdf' ORDER BY filename""")
    publications = evaluate(gold, predicted)
    total = {key:sum(row[key] for row in publications)
             for key in ("matches", "predicted", "expected", "ai_predictions", "ai_additional_matches")}
    args.output.write_text(json.dumps({
        "criterion":"One-to-one metric, normalized value (5 decimal places), unit and publication. "
                    "Device, aggregation and source accuracy are not covered by this diagnostic.",
        "totals":total, "pdf_statuses":statuses, "publications":publications,
    },ensure_ascii=False,indent=2)+"\n")
    for row in publications:
        print(row["publication"],row["matches"],row["predicted"],row["expected"],flush=True)
    print("TOTAL",total,flush=True)


if __name__ == "__main__":
    main()
