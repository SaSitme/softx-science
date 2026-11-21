"""Anchor DeepSeek's interpretation of images to locally recognized PDF fragments."""
import base64
import io
import json
import logging
import os
import subprocess
from urllib.error import URLError

from PIL import Image

from . import domain as d
from .extract import (METRIC, UNIT, VALUE, column_distance, has_nonmeasurement_tail,
                      measurement_cues, table_headers)
from .local_ai import PERFORMANCE, deepseek_response
from .pdf import draw


LOG = logging.getLogger(__name__)


def image_url(path, meta, box):
    with Image.open(io.BytesIO(draw(path, "png", width=1650))) as page:
        a,b,c,d = meta["crop_box"]
        left,bottom,right,top = box
        sx,sy = page.width/(c-a),page.height/(d-b)
        image = page.crop(tuple(round(v) for v in ((left-a)*sx,(d-top)*sy,(right-a)*sx,(d-bottom)*sy)))
        image.thumbnail((1400,1400))
        output = io.BytesIO()
        image.convert("RGB").save(output,"JPEG",quality=88)
    return "data:image/jpeg;base64,"+base64.b64encode(output.getvalue()).decode("ascii")


def region_candidates(lines, figure, covered):
    candidates, units = [], []
    for line in lines:
        for cell in line.cells:
            units.extend((line,cell.part(*unit.span())) for unit in UNIT.finditer(cell.text))
    for line in lines:
        for cell in line.cells:
            for value in VALUE.finditer(cell.text):
                near = cell.text[max(0,value.start()-55):value.start()]
                if figure:
                    if not (METRIC.search(near) or any(cue in near.lower() for cues in PERFORMANCE.values() for cue in cues)):
                        continue
                elif value.start() != 0:
                    continue
                span = cell.part(*value.span())
                if covered(line,span):
                    continue
                unit_ids = [index for index,(unit_line,unit) in enumerate(units)
                            if (unit_line is line and unit.bbox[0]>=span.bbox[0] and unit.bbox[0]-span.bbox[2]<22)
                            or (not figure and unit_line is not line and unit.bbox[1]>span.bbox[1]
                                and column_distance(unit,span)<30)]
                if unit_ids:
                    candidates.append((line,span,value,unit_ids))
    return candidates[:30],units


def image_has_evidence(metric, line, span, value, unit_line, unit, lines, figure):
    if unit_line is line:
        for cell in line.cells:
            match = next((u for u in UNIT.finditer(cell.text) if cell.part(*u.span()).bbox==unit.bbox),None)
            if match:
                for number in VALUE.finditer(cell.text):
                    if (number["n"]==value["n"] and cell.part(*number.span()).bbox==span.bbox
                            and not has_nonmeasurement_tail(cell.text,match)
                            and any(code==metric for code,_ in measurement_cues(cell.text,number,match))):
                        return True
    if figure:
        return False
    for _,header,code in table_headers(unit_line).values():
        if (code!=metric or column_distance(header,span)>30
                or not header.bbox[0]<=unit.bbox[0]<=unit.bbox[2]<=header.bbox[2]):
            continue
        # A later header in the same column starts a different group of rows.
        if not any(span.bbox[1]<other.span.bbox[1]<header.bbox[1] and any(
            column_distance(candidate,span)<25 for _,candidate,_ in table_headers(other).values()
        ) for other in lines):
            return True
    return False


def vision_suggestions(regions, *, covered):
    try:
        limit = max(0,int(os.getenv("SOFTX_DEEPSEEK_MAX_IMAGES","3")))
    except ValueError:
        return []
    accepted, sent = [], 0
    for path,meta,box,figure,lines in regions:
        if sent >= limit:
            break
        candidates,units = region_candidates(lines,figure,covered)
        if not candidates:
            continue
        used_units = {i for _,_,_,options in candidates for i in options}
        prompt = json.dumps({
            "metrics":list(d.METRICS),
            "candidates":[{"id":index,"printed":span.text,"row":line.text,"possible_unit_ids":options}
                          for index,(line,span,_,options) in enumerate(candidates)],
            "units":{index:units[index][1].text for index in sorted(used_units)},
        },ensure_ascii=False)
        fragments = [span.bbox for _,span,_,_ in candidates]+[units[i][1].bbox for i in used_units]
        left,bottom,right,top = box
        crop = (max(left,min(b[0] for b in fragments)-65),max(bottom,min(b[1] for b in fragments)-28),
                min(right,max(b[2] for b in fragments)+65),min(top,max(b[3] for b in fragments)+42))
        try:
            url = image_url(path,meta,crop)
            sent += 1
            answer = deepseek_response(prompt,image=url)
        except (URLError,TimeoutError,subprocess.SubprocessError,OSError,ValueError,KeyError,IndexError,TypeError) as exc:
            LOG.warning("DeepSeek image pass unavailable (%s)",getattr(exc,"code",type(exc).__name__))
            break
        if not isinstance(answer,dict):
            continue
        for index,(line,span,value,options) in enumerate(candidates):
            choice = answer.get(str(index))
            if not isinstance(choice,dict):
                continue
            metric,unit_id = choice.get("metric"),choice.get("unit")
            if not isinstance(metric,str) or metric not in d.METRICS or type(unit_id) is not int or unit_id not in options:
                continue
            unit_line,unit = units[unit_id]
            if d.normalize(metric,1,unit.text)[0] is not None and image_has_evidence(
                metric,line,span,value,unit_line,unit,lines,figure
            ):
                accepted.append((metric,line,span,value,unit_line,unit))
    return accepted
