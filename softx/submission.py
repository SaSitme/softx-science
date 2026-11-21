"""Project the local evidence into the seven CSV tables used for case submission."""
from collections import defaultdict
import csv
import io
from pathlib import Path
import re

from .dedup import same_fragment

COLUMNS = {
    "publications": "publication_id title year DOI authors in_scope exclusion_reason",
    "materials": "publication_id material_id material_name_raw formula_normalized material_class dimensionality additives bandgap_value bandgap_unit bandgap_method note",
    "devices": "publication_id device_id absorber_material_id device_label device_role device_type architecture substrate_type active_area area_unit area_definition encapsulation note",
    "layers": "publication_id layer_id device_id layer_order layer_function layer_material layer_thickness thickness_unit deposition_method annealing_temperature annealing_time atmosphere note",
    "measurements": "publication_id measurement_id device_id measurement_type spectrum light_intensity intensity_unit scan_direction scan_rate mask_used certified certification_lab temperature stability_protocol stress_conditions note",
    "results": "publication_id result_id device_id measurement_id material_id metric_name metric_value metric_unit aggregation spread_value n_devices time_point time_unit raw_value raw_unit normalized_value normalized_unit result_origin cited_publication_id source_page source_section source_table source_figure source_fragment extraction_status duplicate_of note",
    "issues": "publication_id issue_id record_ids check_name description source_page source_fragment",
}

MATERIAL_CLASSES = {
    "Перовскиты": "perovskite", "CIGS": "CIGS", "CdTe": "CdTe", "Кестериты": "kesterite",
    "Sb₂Se₃": "Sb2Se3", "Тонкоплёночный Si": "thin-film Si", "perovskite": "perovskite",
}
CHECK_NAMES = {
    "PCE_CONSISTENCY": "jv_consistency", "VOC_BANDGAP": "voc_limit",
    "PHYSICAL_RANGE": "range", "EQE_JSC": "eqe_consistency", "BATCH_CHAMPION": "champion_vs_mean",
    "CROSS_SOURCE": "contradiction",
}
REFERENCE = re.compile(r"\[\s*\d+(?:\s*[-–,;]\s*\d+)*\s*\]")


def units(value):
    return {"Ω·cm²": "Ohm*cm2", "Ω": "Ohm", "mA/cm²": "mA/cm2", "mW/cm²": "mW/cm2",
            "cm²": "cm2"}.get(value, value)


def choice(value, *allowed):
    return value if value in allowed else None


def overlapping(first, second):
    return (first["pdf_version_id"] == second["pdf_version_id"]
            and first["source_page"] == second["source_page"] and same_fragment(first["bbox"], second["bbox"]))


def source_fields(source):
    return {key: source.get(key) for key in
            ("source_page", "source_section", "source_table", "source_figure", "source_fragment")}


def first_source(sources):
    return min(sources, key=lambda s: (s["source_type"] != "table_cell", s["source_page"], s["id"]),
               default=None)


def same_result(first, second):
    fields = ("publication_id", "result_origin", "metric_name", "normalized_unit",
              "normalized_value", "time_point", "time_unit")
    aggregations = {first["aggregation"], second["aggregation"]} - {None, "unknown"}
    return len(aggregations) <= 1 and all(first[field] == second[field] for field in fields) and all(
        first[field] is None or second[field] is None or first[field] == second[field]
        for field in ("device_id", "measurement_id", "cited_publication_id")
    )


def export_rows(data, issues):
    tables = {name: [] for name in COLUMNS}
    publications = {p["id"]: p for p in data["publication"]}
    versions = {}
    for version in data["pdf_version"]:
        code = Path(version["filename"]).stem
        previous = versions.get(code)
        if previous is None or (version["created_at"] or "", version["id"]) > (previous["created_at"] or "", previous["id"]):
            versions[code] = version
    included = {}
    records_by_pub = defaultdict(list)
    for code, version in sorted(versions.items()):
        pub = publications[version["publication_id"]]
        status = pub["corpus_status"]
        scope = status == "included" or (status == "needs_review" and version["pv_extraction_enabled"] == 1)
        tables["publications"].append(dict(pub, publication_id=code, DOI=pub["doi"], in_scope=scope,
                                           exclusion_reason=None if scope else pub["corpus_reason"] or status))
        if scope:
            included[version["id"]] = code
            records_by_pub[pub["id"]].append(code)
    device_by_id = {d["id"]: d for d in data["device"]}
    for row in data["material"]:
        for code in records_by_pub[row["publication_id"]]:
            tables["materials"].append(dict(row, publication_id=code, material_id=row["id"],
                material_class=MATERIAL_CLASSES.get(row["material_class"], "other" if row["material_class"] else None),
                dimensionality=choice(row["dimensionality"], "3D", "2D", "2D-3D"),
                bandgap_method=choice(row["bandgap_method"], "Tauc", "EQE", "PL", "cited")))
    for row in data["device"]:
        for code in records_by_pub[row["publication_id"]]:
            kind = "cell" if row["device_type"] == "single-junction thin-film" else row["device_type"]
            tables["devices"].append(dict(row, publication_id=code, device_id=row["id"],
                absorber_material_id=row["material_id"], device_type=choice(kind, "cell", "module", "minimodule"),
                device_role=choice(row["device_role"], "control", "target", "record"),
                architecture=choice(row["architecture"], "n-i-p", "p-i-n", "substrate", "superstrate"),
                area_unit=units(row["area_unit"]), substrate_type=choice(row["substrate_type"], "rigid", "flexible"),
                area_definition=choice(row["area_definition"], "designated illumination", "aperture", "total")))
    for row in data["device_layer"]:
        for code in records_by_pub[device_by_id[row["device_id"]]["publication_id"]]:
            functions = {"contact": None, "buffer": "ETL", "electron transport layer": "ETL",
                         "hole transport layer": "HTL"}
            function = functions.get((row["layer_function"] or "").lower(), row["layer_function"])
            function = choice(function, "front_contact", "ETL", "absorber", "passivation", "HTL",
                              "interlayer", "recombination", "back_contact")
            tables["layers"].append(dict(row, publication_id=code, layer_id=row["id"], layer_function=function,
                note=row["layer_function"] if function != row["layer_function"] else None))
    for row in data["measurement"]:
        for code in records_by_pub[device_by_id[row["device_id"]]["publication_id"]]:
            kind = "J-V" if row["measurement_type"] == "JV" else row["measurement_type"]
            protocol = row["stability_protocol"]
            tables["measurements"].append(dict(row, publication_id=code, measurement_id=row["id"],
                measurement_type=choice(kind, "J-V", "MPP tracking", "EQE", "stability"),
                scan_direction=choice(row["scan_direction"], "reverse", "forward", "average"),
                intensity_unit=units(row["light_intensity_unit"]),
                mask_used=bool(row["mask_used"]) if row["mask_used"] is not None else None,
                certified=bool(row["certified"]) if row["certified"] is not None else None,
                stability_protocol=(choice(protocol, "ISOS-D", "ISOS-L", "ISOS-T") or "other") if protocol else None,
                note=protocol if protocol not in (None, "ISOS-D", "ISOS-L", "ISOS-T", "other") else None))

    sources = {s["id"]: s for s in data["source_reference"]}
    references = defaultdict(list)
    for link in data["result_source"]:
        references[link["result_id"]].append(sources[link["source_reference_id"]])
    candidates = {
        r["id"]:r for r in data["measurement_result"] if r["pdf_version_id"] in included and not r["deleted"]
        and (r["acquisition_method"] == "published_numeric" or r["value_state"] == "unparseable")
    }
    children = defaultdict(list)
    for record in candidates.values():
        parent = record["duplicate_of"]
        if parent and parent in candidates:
            children[parent].append(record)

    def values(record):
        return [s for s in references[record["id"]] if s["fragment_role"] == "value"]

    def add_result(record, source, duplicate=None):
        if source is None:
            return
        status = "extracted" if record["normalized_value"] is not None else "present_not_extractable"
        if status != "extracted" and record["value_state"] != "unparseable":
            return
        cited = record["result_origin"] == "cited"
        pub = publications.get(record["cited_publication_id"], {})
        citation = next((match.group() for text in [pub.get("corpus_reason") or "", *(
            s["source_fragment"] or "" for s in references[record["id"]] if s["source_type"] in ("table_cell", "caption")
            and s["fragment_role"] != "value"
        )] if (match := REFERENCE.search(text))), None)
        unit = units(record["normalized_unit"])
        tables["results"].append(dict(record, **source_fields(source),
            publication_id=included[record["pdf_version_id"]], result_id=record["id"],
            metric_value=record["normalized_value"], metric_unit=unit, normalized_unit=unit,
            aggregation=choice(record["aggregation"], "champion", "mean", "median"),
            device_id=None if cited else record["device_id"], measurement_id=None if cited else record["measurement_id"],
            cited_publication_id=(citation or pub.get("doi")) if cited else None,
            extraction_status=status, duplicate_of=duplicate))

    for record in candidates.values():
        if record["duplicate_of"]:
            continue
        linked = children[record["id"]]
        child_sources = {s["id"] for child in linked for s in values(child)}
        primary = first_source([s for s in values(record) if s["id"] not in child_sources])
        primary = primary or first_source(values(record)) or first_source(references[record["id"]])
        add_result(record, primary)
        seen = [primary] if primary else []
        for child in linked:
            if not same_result(record, child):
                continue
            anchor = first_source([s for s in values(child) if not any(overlapping(s, other) for other in seen)])
            if anchor is not None:
                mention = dict(child)
                for field in ("device_id", "measurement_id", "material_id", "cited_publication_id"):
                    if mention[field] is None:
                        mention[field] = record[field]
                if mention["aggregation"] == "unknown":
                    mention["aggregation"] = record["aggregation"]
                add_result(mention, anchor, duplicate=record["id"])
                seen.append(anchor)

    results_by_id = {r["result_id"]: r["publication_id"] for r in tables["results"]}
    for issue in issues:
        if issue["resolution_status"] != "open" or issue["severity"] not in ("warning", "error"):
            continue
        by_code = defaultdict(list)
        for result_id in issue["result_ids"]:
            if result_id in results_by_id:
                by_code[results_by_id[result_id]].append(result_id)
        for code, result_ids in by_code.items():
            source = first_source([s for s in issue["sources"] if included.get(s["pdf_version_id"]) == code
                                   and s["fragment_role"] == "value"])
            tables["issues"].append(dict(source_fields(source or {}), publication_id=code,
                issue_id=issue["id"], record_ids=result_ids, check_name=CHECK_NAMES.get(issue["rule_code"], issue["rule_code"]),
                description=issue["description"]))
    return tables


def submission_files(data, issues):
    def field(value):
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, list):
            return ";".join(map(str, value))
        if isinstance(value, float):
            return format(value, ".15g")
        return value

    for name, records in export_rows(data, issues).items():
        output = io.StringIO(newline="")
        columns = COLUMNS[name].split()
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        writer.writerows({column: field(record.get(column)) for column in columns} for record in records)
        yield name + ".csv", output.getvalue().encode("utf-8")
