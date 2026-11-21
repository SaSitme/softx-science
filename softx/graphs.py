"""Locate embedded graph figures and recover vector series in canonical user space."""
import io
import math
import re
import xml.etree.ElementTree as ET

from PIL import Image

from .derived import RETENTION_LABEL
from .domain import NUM, UNIT_PATTERN
from .extract import DEVICE, SCAN, VALUE, number
from .pdf import draw, page_file, union
from .store import data_dir, insert, key

TYPES = {
    "JV": r"\b(?:J\s*[-–−]?\s*V|I\s*[-–−]\s*V)\b|(?:photo)?current(?:[\s-]+density)?\s*(?:[-–−/]|versus|vs\.?)[\s-]*voltage|current.density.{0,20}voltage",
    "EQE": r"\bEQE\b|external quantum efficiency",
    "stability": r"\bstability\b|ageing|aging|\bT80\b",
    "thermal": r"anneal|heat treatment|thermal|temperature",
    "scatter": r"scatter|distribution|individual data",
    "PCE_vs_param": r"(?:PCE|efficiency).*(?:versus| vs\.? |as a function of|dependence)",
}
CAPTION = re.compile(r"^(?:Figure|Fig\.?)\s*(\d+)\s*(?:\(([a-z])\))?\s*[.:]?", re.I)
FIGURE_HEADING = re.compile(r"^(?:Figure|Fig\.?)\s*\d+\s*(?:\([a-zA-Z]\))?\s*[.:|–—]\s+|^FIGURE\s+\d+\s+")


def projection(axes, points):
    def coordinate(axis, raw):
        if not axis or axis.get("p0") is None or axis.get("p1") is None or axis.get("v0") is None or axis.get("v1") is None:
            return None
        p0, p1, v0, v1 = (axis[name] for name in ("p0", "p1", "v0", "v1"))
        if p0 == p1 or v0 == v1:
            raise ValueError("Для калибровки нужны две разные координаты и два разных значения")
        t = (raw-p0)/(p1-p0)
        if axis.get("scale") == "log":
            if v0 <= 0 or v1 <= 0:
                raise ValueError("Для логарифмической шкалы нужны положительные значения")
            return math.exp(math.log(v0) + t*(math.log(v1)-math.log(v0)))
        return v0+t*(v1-v0)

    values = [{"px":p["px"], "py":p["py"], "x":coordinate(axes.get("x"),p["px"]),
               "y":coordinate(axes.get("y"),p["py"])} for p in points]
    if any(not math.isfinite(v) for p in values for v in p.values() if v is not None):
        raise ValueError("Физические координаты выходят за числовой диапазон")
    return values


def uncertainty(axes, points, width):
    if not width or not points or any(axes.get(axis,{}).get("scale") != "linear" or points[0].get(axis) is None for axis in ("x","y")):
        return None, "Оценка не определена. Нет толщины восстановленного штриха или линейной калибровки обеих осей."
    error = {}
    for axis in ("x","y"):
        by = axes[axis]
        error[axis] = abs((by["v1"]-by["v0"])/(by["p1"]-by["p0"])) * width/2
    return error, "Полуширина штриха в координатах PDF, умноженная на модуль наклона шкалы. Вклад исходного эксперимента и ошибки калибровки не оценён."


def vectors(root, meta):
    x0, _, _, y1 = meta["crop_box"]
    unit = meta["user_unit"]
    found = []
    for element in root.iter():
        if element.tag not in ("stroke_path", "fill_path"):
            continue
        transform = list(map(float, element.get("transform","1 0 0 1 0 0").split()))
        a,b,c,d,e,f = transform
        scale = math.hypot(a,b)/unit
        for_coordinates = []
        current = []
        closed = False
        for segment in element:
            if segment.tag == "moveto" and current:
                for_coordinates.append((current,closed))
                current,closed = [],False
            if segment.tag in ("moveto","lineto","curveto"):
                x,y = float(segment.get("x",segment.get("x3","0"))),float(segment.get("y",segment.get("y3","0")))
                current.append((x0+(a*x+c*y+e)/unit,y1-(b*x+d*y+f)/unit))
            if segment.tag == "closepath":
                closed = True
        if current:
            for_coordinates.append((current,closed))
        for coords, is_closed in for_coordinates:
            if len(coords) < 2:
                continue
            bbox = union([(x,y,x,y) for x,y in coords])
            found.append({"points":coords, "closed":is_closed, "bbox":bbox,
                          "width":float(element.get("linewidth","0"))*scale,
                          "color":element.get("color", "0"), "stroke":element.tag=="stroke_path"})
    return found


def frames(paths):
    candidates = []
    vertical, horizontal = [], []
    for path in paths:
        if not path["stroke"]:
            continue
        x0,y0,x1,y1 = path["bbox"]
        if path["closed"] and len(path["points"]) in (4,5) and x1-x0 >= 65 and y1-y0 >= 45:
            if all(min(abs(x-x0),abs(x-x1),abs(y-y0),abs(y-y1)) < .8 for x,y in path["points"]):
                candidates.append(path["bbox"])
        if x1-x0 >= 65 and y1-y0 < 1:
            horizontal.append(path["bbox"])
        if y1-y0 >= 45 and x1-x0 < 1:
            vertical.append(path["bbox"])
    for x0,_,x1,y in horizontal[:120]:
        for x,y0,_,y1 in vertical[:120]:
            if abs(x0-x)<2 and abs(y-y0)<2:
                candidates.append([x0,y0,x1,y1])
    unique = []
    for box in candidates:
        if not any(max(abs(a-b) for a,b in zip(box, earlier)) < 3 for earlier in unique):
            unique.append(box)
    return unique


def number_ticks(lines, paths, box, axis):
    x0,y0,x1,y1 = box
    ticks = []
    for line in lines:
        for span in line.cells:
            match = VALUE.fullmatch(span.text)
            if not match or any(match[k] is not None for k in ("op","spread","end")):
                continue
            l,b,r,t = span.bbox
            if axis == "x" and y0-48 < t < y0-2 and x0-15 < (l+r)/2 < x1+15:
                pos = (l+r)/2
            elif axis == "y" and x0-55 < r < x0-2 and y0-11 < (b+t)/2 < y1+11:
                pos = (b+t)/2
            else:
                continue
            marks = [p["bbox"] for p in paths if len(p["points"])==2]
            if axis == "x":
                coords = [a for a,b,c,d in marks if abs(a-c)<.8 and 1<d-b<13 and min(abs(b-y0),abs(d-y0))<1.6]
            else:
                coords = [b for a,b,c,d in marks if abs(b-d)<.8 and 1<c-a<13 and min(abs(a-x0),abs(c-x0))<1.6]
            closest = min(coords, key=lambda v:abs(v-pos), default=pos)
            ticks.append((closest if abs(closest-pos)<11 else pos, number(match["n"])))
    return sorted(set(ticks))


def axis_fit(ticks, unit):
    axis = dict(p0=None,p1=None,v0=None,v1=None,scale="linear",unit=unit)
    if len(ticks)<2:
        return axis
    p0,v0=ticks[0]
    p1,v1=ticks[-1]
    if p0==p1 or v0==v1:
        return axis
    axis.update(p0=p0,p1=p1,v0=v0,v1=v1)
    if len(ticks)>=3:
        def relative(transform):
            low,high=transform(v0),transform(v1)
            return max(abs((transform(v)-low)/(high-low)-(p-p0)/(p1-p0)) for p,v in ticks)
        linear = relative(float)
        log = relative(math.log) if all(v>0 for _,v in ticks) else 1
        if log < .035 and linear > .07:
            axis["scale"]="log"
        elif linear > .08:
            axis.update(p0=None,p1=None,v0=None,v1=None)
    return axis


def save_crop(version, page, meta, box):
    directory = data_dir() / version
    ident = key(version,page,box)+".png"
    file = directory / ident
    if not file.exists():
        with Image.open(io.BytesIO(draw(page_file(directory,page), "png", width=1500))) as image:
            x0, y0, x1, y1 = meta["crop_box"]
            sx,sy=image.width/(x1-x0),image.height/(y1-y0)
            a,b,c,d=box
            coords=(max(0,round((a-x0)*sx)),max(0,round((y1-d)*sy)),
                    min(image.width,round((c-x0)*sx)),min(image.height,round((y1-b)*sy)))
            if coords[2]<=coords[0] or coords[3]<=coords[1]:
                return None
            temporary = file.with_suffix(".tmp")
            image.crop(coords).save(temporary,format="PNG")
            temporary.replace(file)
    return ident


def raster_boxes(root, meta):
    left,bottom,right,top = meta["crop_box"]
    unit = meta["user_unit"]
    for element in root.iter("fill_image"):
        if "alpha" not in element.attrib or "transform" not in element.attrib:
            continue
        a,b,c,d,e,f = map(float,element.get("transform").split())
        xs=[left+x/unit for x in (e,a+e,c+e,a+c+e)]
        ys=[top-y/unit for y in (f,b+f,d+f,b+d+f)]
        box=[max(left,min(xs)),max(bottom,min(ys)),min(right,max(xs)),min(top,max(ys))]
        if box[2]-box[0]>=85 and box[3]-box[1]>=50:
            yield box


def raster_kind(caption):
    for kind in ("JV","EQE"):
        if re.search(TYPES[kind],caption,re.I):
            return kind
    if re.search(TYPES["stability"],caption,re.I) and re.search(r"test|curve|under|time|operat|shelf|retention|T80",caption,re.I):
        return "stability"
    if re.search(r"(?:temperature|anneal).*(?:dependence|variation|vs\.? |versus|as a function)|(?:dependence|variation|vs\.? |versus|as a function).*(?:temperature|anneal)",caption,re.I):
        return "thermal"
    if re.search(TYPES["PCE_vs_param"],caption,re.I):
        return "PCE_vs_param"
    if re.search(r"scatter\s+(?:plot|data)|\b(?:PCE|Voc|Jsc)\s+(?:distribution|histogram|box plot)",caption,re.I):
        return "scatter"
    return None


def save_raster_figures(extraction, meta, root, captions):
    images = list(raster_boxes(root,meta))
    for caption,label,kind in captions:
        x0,_,x1,top=caption.span.bbox
        candidates=[box for box in images if -4<=box[1]-top<=55 and
                    min(x1,box[2])-max(x0,box[0])>=.4*min(x1-x0,box[2]-box[0])]
        if not candidates:
            continue
        box=min(candidates,key=lambda b:(abs(b[1]-top),-(b[2]-b[0])*(b[3]-b[1])))
        ident=key(extraction.version,meta["page"],label[1],box,"raster_figure")
        crop=save_crop(extraction.version,meta["page"],meta,box)
        if not crop:
            continue
        insert(extraction.db,"plot_dataset",id=ident,publication_id=extraction.pub,pdf_version_id=extraction.version,
               source_page=meta["page"],source_figure=label[1],panel_id=label[2],plot_type=kind,plot_bbox=box,
               caption_text=caption.text,crop_image=crop,axes={axis:axis_fit([],None) for axis in "xy"},
               points=[],digitization_method="raster_figure",series_label="Исходный рисунок",
               uncertainty_method="Для изображения ещё не заданы точки и калибровка осей.")
        for ref in figure_sources(extraction,meta,box,caption,label):
            insert(extraction.db,"plot_source",plot_id=ident,source_reference_id=ref)


def automatic(extraction, pages, *, raster_only=False):
    for meta in pages:
        page = meta["page"]
        lines = [line for line in extraction.lines if line.page == page]
        captions = []
        for line in lines:
            match = CAPTION.match(line.text)
            kind = next((k for k, pattern in TYPES.items() if re.search(pattern,line.text,re.I)),None) if match else None
            captions.append((line,match,kind))
        graph_captions=[caption for caption in captions if caption[2]]
        raster_captions=[(line,label,kind) for line,label,_ in captions
                         if FIGURE_HEADING.match(line.text) and (kind:=raster_kind(line.text))]
        if not raster_captions and (raster_only or not graph_captions):
            continue
        path = page_file(data_dir()/extraction.version,page)
        root = ET.fromstring(draw(path,"trace"))
        save_raster_figures(extraction,meta,root,raster_captions)
        if raster_only:
            continue
        items = vectors(root,meta)
        for box in frames(items):
            center = (box[1]+box[3])/2
            line, label, kind = min(graph_captions, key=lambda c:abs((c[0].span.bbox[1]+c[0].span.bbox[3])/2-center))
            if abs(line.span.bbox[1]-center)>max(200,(box[3]-box[1])*1.7):
                continue
            produce(extraction,meta,box,items,lines,line,label,kind)


def units_for_plot(kind, lines, box):
    x0,y0,x1,y1=box
    near=[s.text for line in lines for s in line.cells
          if s.bbox[0]<=x1+25 and s.bbox[2]>=x0-85 and y0-85<=s.bbox[1]<=y1+60]
    text=" ".join(near)
    if kind == "JV":
        x=re.search(r"(?:voltage|potential|\bV)\s*[\[(]\s*(m?V)\s*[\])]",text,re.I)
        y=re.search(r"(?:current density|\bJ)\s*[\[(]\s*((?:m?A)\s*/\s*(?:cm|m)\^?[²2])",text,re.I)
        return x[1] if x else None, y[1].replace("^2","²").replace("cm2","cm²").replace("/m2","/m²") if y else None
    axis_patterns={
        "EQE": (r"(?:wavelength|λ)\s*[\[(]\s*(nm|[µμu]m)", r"(?:EQE|efficiency)\s*[\[(]\s*(%|1)"),
        "stability": (r"(?:time|duration)\s*[\[(]\s*(h|days?|min|s)\b",r"(?:PCE|retention|efficiency)\s*[\[(]\s*(%|1)"),
        "thermal": (r"(?:temperature|annealing)\s*[\[(]\s*(°?C|K)\b",r"(?:PCE|efficiency)\s*[\[(]\s*(%)"),
        "PCE_vs_param": (rf"(?:thickness|concentration|time|loading)\s*[\[(]\s*({UNIT_PATTERN}|mg/mL|M)",r"(?:PCE|efficiency)\s*[\[(]\s*(%)"),
        "scatter": (r"(?:voltage|\bVoc)\s*[\[(]\s*(m?V)",r"(?:PCE|efficiency)\s*[\[(]\s*(%)"),
    }
    matches=[re.search(pattern,text,re.I) for pattern in axis_patterns[kind]]
    return tuple(match[1].replace("μ","µ") if match else None for match in matches)


def figure_sources(extraction,meta,box,caption,label):
    source=extraction.ref(caption,caption.span,"condition","caption",figure=label[1])
    source_figure=key(extraction.version,meta["page"],box,"figure")
    insert(extraction.db,"source_reference",id=source_figure,publication_id=extraction.pub,pdf_version_id=extraction.version,
           source_page=meta["page"],source_figure=label[1],source_fragment=caption.text,bbox=box,source_type="figure",fragment_role="condition")
    return source,source_figure


def produce(extraction, meta, box, items, lines, caption, label, kind):
    x0,y0,x1,y1=box
    units=units_for_plot(kind,lines,box)
    axes={axis:axis_fit(number_ticks(lines,items,box,axis),unit) for axis,unit in zip("xy",units)}
    if kind=="stability":
        # Identify the retention axis in or beside this panel, excluding distant captions.
        nearby = " ".join(s.text for line in lines for s in line.cells
                          if x0-85 <= s.bbox[0] < x0 and y0 <= s.bbox[1] <= y1+60)
        if match := RETENTION_LABEL.search(nearby):
            axes["y"]["label"] = match[0]
    series = []
    for p in items:
        a,b,c,d=p["bbox"]
        if p["stroke"] and not p["closed"] and len(p["points"])>=3 and (
                a>=x0-1 and b>=y0-1 and c<=x1+1 and d<=y1+1
                and c-a>=(x1-x0)*.12 and d-b>=1.5):
            series.append((p["points"],p["width"],p["color"]))
    markers={}
    for p in items:
        a,b,c,d=p["bbox"]
        if p["closed"] and len(p["points"])>=3 and 1<c-a<13 and 1<d-b<13 and x0<=a<c<=x1 and y0<=b<d<=y1:
            group=markers.setdefault(p["color"],[])
            center=((a+c)/2,(b+d)/2)
            if not any(abs(x-center[0])+abs(y-center[1])<.6 for x,y in group):
                group.append(center)
    for color, points in markers.items():
        if len(points)>=2 and not any(existing[2]==color for existing in series):
            series.append((points,None,color))
    if not series:
        series=[([],None,"")]

    legend = [s.text for line in lines for s in line.cells if x0<s.bbox[0]<x1 and y0<s.bbox[1]<y1
              and re.search(r"[A-Za-z]",s.text)]
    sources=figure_sources(extraction,meta,box,caption,label)
    named=re.search(r"\b(?:for|of)\s+(?:(?:device|sample|cell)\s+)?([\w.-]+)\b",caption.text,re.I)
    device=extraction.devices.get(named[1]) if named else None
    scan=SCAN.search(caption.text)
    measurement=extraction.measurement(device,scan[1].lower(),extraction.conditions_for(caption),kind=kind) if device and scan and len(series)==1 else None
    for index,(raw,width,color) in enumerate(series):
        ident=key(extraction.version,meta["page"],box,"series",index)
        points=projection(axes,[dict(px=x,py=y) for x,y in raw])
        estimate,method=uncertainty(axes,points,width)
        insert(extraction.db,"plot_dataset",id=ident,publication_id=extraction.pub,pdf_version_id=extraction.version,
               source_page=meta["page"],source_figure=label[1],panel_id=label[2],plot_type=kind,plot_bbox=box,
               caption_text=caption.text,legend_text=" · ".join(legend) or None,crop_image=save_crop(extraction.version,meta["page"],meta,box),
               axes=axes,points=points,digitization_method="vector_pdf" if raw else "manual_pending",
               stroke_width_pdf=width,uncertainty=estimate,uncertainty_method=method,device_id=device if len(series)==1 else None,
               measurement_id=measurement,series_label=f"Серия {index+1}")
        for ref in sources:
            insert(extraction.db,"plot_source",plot_id=ident,source_reference_id=ref)
