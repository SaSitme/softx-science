"""Recover the visible path of one colored curve from a user-selected seed."""
import io
from collections import defaultdict, deque
from statistics import median

from PIL import Image

from .pdf import draw


def trace_curve(path, page_box, box, seed):
    left,bottom,right,top = page_box
    a,b,c,d = box
    with Image.open(io.BytesIO(draw(path,"png",width=1300))) as rendered:
        sx,sy = rendered.width/(right-left), rendered.height/(top-bottom)
        x0,y0,x1,y1 = [round(v) for v in ((a-left)*sx,(top-d)*sy,(c-left)*sx,(top-b)*sy)]
        image = rendered.crop((x0,y0,x1,y1)).convert("RGB")
    pixels = image.load()
    w,h = image.size
    x,y = round((seed[0]-left)*sx)-x0,round((top-seed[1])*sy)-y0
    nearby = [(u,v) for u in range(max(0,x-5),min(w,x+6))
              for v in range(max(0,y-5),min(h,y+6)) if max(pixels[u,v])-min(pixels[u,v])>=55]
    if not nearby:
        raise ValueError("Щёлкните по цветной кривой в выделенной области")
    start = min(nearby,key=lambda pos:(pos[0]-x)**2+(pos[1]-y)**2)
    color = pixels[start]
    pending,seen = deque([start]),{start}
    columns = defaultdict(list)
    visited = 0
    while pending:
        u,v = pending.popleft()
        columns[u].append(v)
        visited += 1
        if visited > w*h*.12:
            raise ValueError("Цвет совпадает с фоном или заливкой; выберите отдельную линию")
        for du,dv in ((-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)):
            n,m = u+du,v+dv
            if not (0<=n<w and 0<=m<h) or (n,m) in seen:
                continue
            seen.add((n,m))
            other = pixels[n,m]
            if max(other)-min(other)>=35 and sum((p-q)**2 for p,q in zip(color,other))<=80**2:
                pending.append((n,m))
    if len(columns)<max(12,w*.12):
        raise ValueError("Не удалось проследить достаточно длинную непрерывную кривую")
    return [{"px":min(c,max(a,left+(x0+u+.5)/sx)), "py":min(d,max(b,top-(y0+median(vs)+.5)/sy))}
            for u,vs in sorted(columns.items()) if max(vs)-min(vs)<max(6,h*.12)]
