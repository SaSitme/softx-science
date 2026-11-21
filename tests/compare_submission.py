"""Compare exported results with the reference at full decimal precision."""
import argparse
from collections import Counter
import csv
from decimal import Decimal, InvalidOperation
import io
import json
from pathlib import Path
from zipfile import ZipFile

from evaluate_dev import TEMPLATE_METRICS


def read_results(path):
    if path.suffix == ".zip":
        with ZipFile(path) as archive:
            content = archive.read("results.csv").decode("utf-8-sig")
    else:
        content = path.read_text(encoding="utf-8-sig")
    return list(csv.DictReader(io.StringIO(content)))


def counted(records):
    for row in records:
        if row["metric_name"] not in TEMPLATE_METRICS or row["extraction_status"] != "extracted":
            continue
        try:
            value = Decimal(row["normalized_value"])
        except (InvalidOperation, TypeError):
            continue
        if value.is_finite():
            yield row["publication_id"], row["metric_name"], value, row["normalized_unit"]


def compare(gold, exported):
    expected = Counter(counted(gold))
    actual = Counter(counted(exported))
    matches = (expected & actual).total()

    def table(counts):
        return [dict(publication_id=pub, metric=metric, value=str(value), unit=unit, count=count)
                for (pub,metric,value,unit),count in sorted(counts.items())]

    return dict(matches=matches, exported=actual.total(), expected=expected.total(),
                precision=matches/actual.total() if actual else None,
                recall=matches/expected.total() if expected else None,
                exact_value_match=expected==actual, missing=table(expected-actual), extra=table(actual-expected))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("submission", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = compare(read_results(args.reference),read_results(args.submission))
    if args.output:
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({k:v for k,v in report.items() if k not in ("missing","extra")},ensure_ascii=False))
    return 0 if report["exact_value_match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
