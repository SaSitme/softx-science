"""Generate the machine-readable schema passport from the SQLite contract."""
import json
from pathlib import Path
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from softx import domain, rules
from softx.store import TABLES

connection=sqlite3.connect(":memory:")
connection.row_factory=sqlite3.Row
connection.executescript((ROOT/"softx"/"schema.sql").read_text())

entities={}
for table in TABLES:
    columns={}
    relationships={}
    for row in connection.execute(f"PRAGMA foreign_key_list({table})"):
        relationships[row["from"]]={"table":row["table"],"column":row["to"]}
    for row in connection.execute(f"PRAGMA table_info({table})"):
        name=row["name"]
        columns[name]={"type":row["type"],"nullable":not (row["notnull"] or row["pk"]),
                       "default_sql":row["dflt_value"]}
        if name in relationships:
            columns[name]["references"]=relationships[name]
    entities[table]={"primary_key":[row["name"] for row in sorted(connection.execute(f"PRAGMA table_info({table})"),key=lambda r:r["pk"]) if row["pk"]],
                     "columns":columns}

enums={
  "publication.corpus_status":["included","excluded_tandem","excluded_theoretical","needs_review"],
  "pdf_version.processing_status":["queued","processing","completed","partial","failed"],
  "pdf_version.pv_extraction_enabled":[0,1],
  "device.device_role":["control","target","record"],
  "device.architecture":["n-i-p","p-i-n","substrate","superstrate"],
  "measurement.scan_direction":["forward","reverse"],
  "measurement_result.result_origin":["primary","cited"],
  "measurement_result.aggregation":["champion","mean","median","single","unknown"],
  "measurement_result.value_state":["numeric","numeric_zero","missing","ambiguous","unparseable"],
  "measurement_result.acquisition_method":["published_numeric","digitized_plot","calculated","assumption"],
  "measurement_result.extraction_status":[domain.AUTO,domain.CONFIRMED,domain.REVIEW],
  "source_reference.source_type":["text","table_cell","table_header","footnote","figure","caption"],
  "source_reference.fragment_role":["value","unit","sample_label","condition"],
  "plot_dataset.plot_type":["JV","EQE","PCE_vs_param","thermal","stability","scatter"],
  "validation_issue.severity":["error","warning","info","not_applicable"],
  "validation_issue.resolution_status":["open","passed","resolved","not_applicable","superseded"]}
for name,values in enums.items():
    table,column=name.split(".")
    entities[table]["columns"][column]["enum"]=values

units={metric:{"target":next(iter(factors)), "factor_by_unit":factors} for metric,factors in domain.UNITS.items()}
for metric in ("Rs","Rsh"):
    units[metric]={"target":None,"accepted_distinct_units":["Ω·cm²","Ω"]}
for table,column,metric in (("device","active_area","area"),("device_layer","layer_thickness","thickness"),
                            ("material","bandgap_value","Eg"),("measurement","light_intensity","Pin"),
                            ("measurement_result","time_point","T80")):
    entities[table]["columns"][column]["normalized_dimension"]=units[metric]["target"]

data={
    "schema_version":3,"database":"SQLite","documentation":"DATA_PASSPORT.md",
    "entities":entities,
    "metric_registry":domain.METRICS,"normalization":units,
    "canonical_pdf":{"bbox_order":["x_min","y_min","x_max","y_max"],"space":"unrotated_pdf_user_space",
                     "page_number_origin":1,"y_direction":"up","page_box":"crop_intersect_media"},
    "invariants":{"immutable_result_fields":["raw_value","raw_unit","raw_record"],"idempotence_key":"pdf_version.sha256",
                  "plot_is_approximate":True,"condition_defaults":None},
    "rules":{code:{"name":name} for code,name in rules.RULES.items()},
    "comparison":{"area_relative_tolerance":rules.AREA_TOL,"spectrum":"AM1.5G","pin":{"value":100,"unit":"mW/cm²"},
                  "criteria":["same_material_class","same_device_type","same_metric_and_unit","same_area_definition",
                              "explicit_aggregation","explicit_scan_direction","primary","included"],
                  "separate_views":["certified","stability"],"stability_extra":["stability_protocol","stress_conditions"]},
}
data["rules"]["PCE_CONSISTENCY"].update(relative_threshold=rules.PCE_TOL,denominator="abs(PCE_calc)",operator=">")
data["rules"]["EQE_JSC"].update(relative_threshold=rules.EQE_TOL,denominator="abs(Jsc_JV)",operator=">")
data["rules"]["PHYSICAL_RANGE"].update(PCE={"min":0,"max":100},FF={"min_exclusive":0,"max":100},Jsc={"max_abs":"Pin/Eg"})
data["rules"]["VOC_BANDGAP"].update(Voc={"max_exclusive":"Eg/q"})
data["rules"]["SHOCKLEY_QUEISSER"].update(model="AM1.5G detailed balance",T_reference_K=298.15)
data["rules"]["BATCH_CHAMPION"].update(min_difference=0,max_difference="spread_if_available")
data["rules"]["SCAN_ISOLATION"].update(operator="equal")
data["rules"]["CROSS_SOURCE"].update(max_difference="sum_of_half_rounding_steps_and_digitization_uncertainties")
(ROOT/"data_passport.json").write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n")
