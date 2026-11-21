"""Measure dev extraction against the supplied gold CSV using a one-to-one value match."""
import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from softx.domain import AI_EXTRACTION_METHODS, REVIEW
from softx.store import db, rows

METRICS = {"PCE", "Voc", "Jsc", "FF", "Rs", "Rsh", "HI", "Jsc_EQE", "T80"}
TEMPLATE_METRICS = METRICS | {"stabilized_PCE", "retained_PCE"}
DEEPSEEK_METHODS = {"deepseek_pdf_quote", "deepseek_vision"}


def token(metric, value, unit):
    try:
        value = round(float(value), 5)
    except (TypeError, ValueError):
        return None
    unit = (unit or "").replace("²", "2")
    if metric in ("Rs", "Rsh") and unit in ("Ohm*cm2", "Ω·cm2"):
        unit = "Ohm*cm2"
    return metric, value, unit


def evaluate(gold, predicted, metrics=METRICS):
    report = []
    for publication in sorted({item["publication_id"] for item in gold}):
        expected = Counter(value for item in gold if item["publication_id"] == publication
                           and item["metric_name"] in metrics and item["extraction_status"] == "extracted"
                           if (value := token(item["metric_name"], item["normalized_value"], item["normalized_unit"])))
        actual = Counter()
        ai = Counter()
        deepseek = Counter()
        for record in predicted:
            if record["filename"] != publication+".pdf" or record["metric_name"] not in metrics:
                continue
            value = token(record["metric_name"], record["normalized_value"], record["normalized_unit"])
            if value is not None:
                actual[value] += 1
                if record["extraction_method"] in AI_EXTRACTION_METHODS.values():
                    ai[value] += 1
                if record["extraction_method"] in DEEPSEEK_METHODS:
                    deepseek[value] += 1
        matches = sum((actual & expected).values())
        report.append(dict(
            publication=publication, matches=matches, predicted=sum(actual.values()), expected=sum(expected.values()),
            ai_predictions=sum(ai.values()), ai_additional_matches=matches-sum(((actual-ai) & expected).values()),
            deepseek_predictions=sum(deepseek.values()),
            deepseek_additional_matches=matches-sum(((actual-deepseek) & expected).values()),
            missing=[dict(metric=key[0], value=key[1], unit=key[2], count=count)
                     for key,count in (expected-actual).items()],
        ))
    return report


def summarize(publications):
    totals = {key:sum(row[key] for row in publications)
              for key in ("matches", "predicted", "expected", "ai_predictions", "ai_additional_matches",
                          "deepseek_predictions", "deepseek_additional_matches")}
    return dict(totals,precision=totals["matches"]/totals["predicted"] if totals["predicted"] else None,
                recall=totals["matches"]/totals["expected"] if totals["expected"] else None)


def unmatched(gold,predicted,metrics=METRICS):
    budget=Counter((row["publication_id"]+".pdf",key) for row in gold
                   if row["metric_name"] in metrics and row["extraction_status"]=="extracted"
                   if (key:=token(row["metric_name"],row["normalized_value"],row["normalized_unit"])))
    publications={row["publication_id"]+".pdf" for row in gold}
    for row in sorted(predicted,key=lambda item:(item["extraction_status"]==REVIEW,
                                                  item["extraction_method"] in AI_EXTRACTION_METHODS.values())):
        if row["filename"] not in publications or row["metric_name"] not in metrics:
            continue
        key=token(row["metric_name"],row["normalized_value"],row["normalized_unit"])
        if key is None:
            continue
        if budget[row["filename"],key]:
            budget[row["filename"],key]-=1
        else:
            yield row


def missing_relationships(gold, predicted, metrics, sources_for):
    """Relate unmatched gold rows to plausible diagnostic candidates without awarding matches."""
    evidence = {}
    for result in predicted:
        if key := token(result["metric_name"], result["normalized_value"], result["normalized_unit"]):
            evidence.setdefault(result["filename"], []).append((key, result))
    remaining = Counter((file, key) for file, observations in evidence.items() for key, _ in observations)
    for expected in sorted(gold, key=lambda row: bool(row.get("duplicate_of"))):
        if expected["metric_name"] not in metrics or expected["extraction_status"] != "extracted":
            continue
        if not (key := token(expected["metric_name"], expected["normalized_value"], expected["normalized_unit"])):
            continue
        file = expected["publication_id"] + ".pdf"
        if remaining[file, key]:
            remaining[file, key] -= 1
            continue
        name, value, unit = key
        proposals = []
        for observed, actual in evidence.get(file, ()):
            observed_name, observed_value, observed_unit = observed
            if observed == key:
                rank, relation = 0, "exact_value_already_paired"
            elif (observed_value, observed_unit) == (value, unit):
                rank, relation = 1, "same_number_and_unit_different_metric"
            elif observed_name == name and abs(observed_value-value) <= max(abs(value)*.005, .0001) and observed_unit == unit:
                rank, relation = 2, "near_number_same_metric_and_unit"
            elif observed_name == name and observed_value == value:
                rank, relation = 3, "same_metric_and_number_different_unit"
            else:
                continue
            sources = sources_for(actual["id"])
            same_page = any(str(source["source_page"]) == expected.get("source_page") for source in sources)
            proposals.append((rank, not same_page, abs(observed_value-value), dict(
                relation=relation, result_id=actual["id"], metric=observed_name, value=observed_value,
                unit=observed_unit, same_page=same_page, method=actual["extraction_method"], sources=sources,
            )))
        proposals.sort(key=lambda candidate: candidate[:3])
        yield dict(expected=expected, candidates=[candidate[-1] for candidate in proposals[:3]])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=Path("/home/danya/Изображения/dev_gold/results.csv"))
    parser.add_argument("--pdfs", type=Path, help="Optional directory of dev PDFs to ingest into SOFTX_DATA before evaluation.")
    parser.add_argument("--allow-deepseek", action="store_true",
                        help="Send selected fragments from gold-listed PDFs to the configured DeepSeek API.")
    parser.add_argument("--output", type=Path, default=ROOT/"export"/"dev_comparison.json")
    parser.add_argument("--diagnostics", action="store_true", help="Include unmatched predictions and their PDF sources.")
    parser.add_argument("--template-metrics", action="store_true", help="Include the full submission template metric list.")
    args = parser.parse_args()
    metrics = TEMPLATE_METRICS if args.template_metrics else METRICS
    with args.gold.open(encoding="utf-8-sig") as file:
        gold = list(csv.DictReader(file))
    if args.allow_deepseek and not args.pdfs:
        parser.error("--allow-deepseek requires --pdfs")
    if args.pdfs:
        from softx.app import ingest, worker
        files = sorted(args.pdfs.glob("D*.pdf"))
        if args.allow_deepseek:
            from softx.local_ai import providers
            if "deepseek" not in providers():
                parser.error("DeepSeek is not configured or is disabled")
            allowed = {item["publication_id"]+".pdf" for item in gold}
            files = [file for file in files if file.name in allowed]
            if {file.name for file in files} != allowed:
                parser.error("Some PDFs in the gold corpus are missing")
        if not files:
            parser.error("no D*.pdf files in --pdfs")
        for file in files:
            ingest(file.read_bytes(),file.name,allow_cloud=args.allow_deepseek)
        worker.shutdown()
    with db() as connection:
        predicted = rows(connection, """SELECT r.*,v.filename FROM measurement_result r
                         JOIN pdf_version v ON v.id=r.pdf_version_id WHERE v.filename LIKE 'D%.pdf'
                         AND r.duplicate_of IS NULL AND r.deleted=0""")
        statuses = rows(connection, """SELECT filename,processing_status,error_message FROM pdf_version
                           WHERE filename LIKE 'D%.pdf' ORDER BY filename""")
        diagnostics=[]
        missing=[]
        if args.diagnostics:
            def sources_for(result_id):
                return rows(connection,"""SELECT s.source_page,s.source_type,s.fragment_role,s.source_fragment,s.bbox
                    FROM result_source l JOIN source_reference s ON s.id=l.source_reference_id WHERE l.result_id=?""",result_id)

            for result in unmatched(gold,predicted,metrics):
                diagnostics.append(dict({key:result[key] for key in
                    ("filename","metric_name","normalized_value","raw_value","raw_unit","extraction_method","extraction_status","result_origin","device_id","note")},
                    sources=sources_for(result["id"])))
            missing=list(missing_relationships(gold,predicted,metrics,sources_for))
    publications = evaluate(gold, predicted, metrics)
    total = summarize(publications)
    trusted=[r for r in predicted if r["extraction_status"]!=REVIEW]
    trusted_publications=evaluate(gold,trusted,metrics)
    def by_method(records):
        return {method:summarize(evaluate(gold,[r for r in records if (r["extraction_method"] or "unknown")==method],metrics))
                for method in sorted({r["extraction_method"] or "unknown" for r in records})}
    args.output.write_text(json.dumps({
        "criterion":"One-to-one metric, normalized value (5 decimal places), unit and publication. "
                    "Device, aggregation and source accuracy are not covered by this diagnostic.",
        "metrics":sorted(metrics),
        "totals":total, "pdf_statuses":statuses, "publications":publications,
        "without_review":{"totals":summarize(trusted_publications),"publications":trusted_publications,
                          "methods":by_method(trusted)},
        "methods":by_method(predicted),
        **({"unmatched_predictions":diagnostics, "missing_relationships":missing} if args.diagnostics else {}),
    },ensure_ascii=False,indent=2)+"\n")
    for row in publications:
        print(row["publication"],row["matches"],row["predicted"],row["expected"],flush=True)
    print("TOTAL",total,flush=True)


if __name__ == "__main__":
    main()
