"""Diagnostic value matching on real dev PDFs. Does not supply labels to extraction."""
import csv
import json
import sys
from collections import Counter
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from softx.store import db,rows
root=Path('/home/danya/Изображения/dev_gold')
with (root/'results.csv').open(encoding='utf-8-sig') as f:
    gold=list(csv.DictReader(f))
metrics={'PCE','Voc','Jsc','FF','Rs','Rsh','HI','Jsc_EQE','T80'}
def token(metric,value,unit):
    try:value=round(float(value),5)
    except (TypeError,ValueError):return None
    return metric,value,(unit or '').replace('²','2')
with db() as c:
    predicted=rows(c,"SELECT r.*,v.filename FROM measurement_result r JOIN pdf_version v ON v.id=r.pdf_version_id WHERE v.filename LIKE 'D%.pdf'")
report=[]
for publication in sorted({g['publication_id'] for g in gold}):
    expected=Counter(t for g in gold if g['publication_id']==publication and g['metric_name'] in metrics and g['extraction_status']=='extracted'
                     if (t:=token(g['metric_name'],g['normalized_value'],g['normalized_unit'])))
    actual=Counter(t for r in predicted if r['filename']==publication+'.pdf' and r['metric_name'] in metrics
                   if (t:=token(r['metric_name'],r['normalized_value'],r['normalized_unit'])))
    matches=sum((actual&expected).values())
    report.append(dict(publication=publication,matches=matches,predicted=sum(actual.values()),expected=sum(expected.values()),
                       missing=[dict(metric=k[0],value=k[1],unit=k[2],count=n) for k,n in (expected-actual).items()]))
Path('export/dev_comparison.json').write_text(json.dumps({'criterion':'One-to-one metric, normalized value (5 decimal places), unit and publication. Device, aggregation and source accuracy are not covered by this diagnostic.','publications':report},ensure_ascii=False,indent=2))
print([(r['publication'],r['matches'],r['predicted'],r['expected']) for r in report])
print('TOTAL',*[sum(r[k] for r in report) for k in ('matches','predicted','expected')])
