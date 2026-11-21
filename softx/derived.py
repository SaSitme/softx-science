"""Create and refresh derived metrics linked to the underlying plot and source."""
import math
import re

from . import domain as d
from .spectrum import REFERENCE, SOURCE, integrate_eqe
from .store import edit, insert, key, one, rows


PLOT_METRICS = {
    "JV": ("Voc", "Jsc", "FF", "PCE"),
    "EQE": ("Jsc_EQE",),
    "stability": ("T80",),
}
RETENTION_LABEL = re.compile(r"\b(?:retention|retained\s+(?:PCE|efficiency)|normali[sz]ed\s+(?:PCE|efficiency)|relative\s+(?:PCE|efficiency)|PCE\s*/\s*PCE[_\s]?[0₀])\b", re.I)


def maximum_power(plot):
    voc = compute(plot, "Voc")[0]
    jsc = compute(plot, "Jsc")[0]
    if voc <= 0 or jsc == 0:
        raise ValueError("Для расчёта мощности нужны положительное Voc и ненулевое Jsc")
    axes = plot["axes"]
    branch = [(0, abs(jsc)), (voc, 0)]
    for point in plot["points"]:
        voltage = d.normalize("Voc", point["x"], axes["x"].get("unit"))[0]
        current = d.normalize("Jsc", point["y"], axes["y"].get("unit"))[0]
        if voltage is None or current is None or not 0 < voltage < voc:
            continue
        if current*jsc < 0:
            raise ValueError("Ток меняет знак между коротким замыканием и холостым ходом")
        branch.append((voltage, abs(current)))
    branch = sorted(set(branch))
    if len(branch) < 3 or len({v for v,_ in branch}) != len(branch):
        raise ValueError("Для максимальной мощности нужна однозначно оцифрованная рабочая ветвь ВАХ")
    candidates = [(v*j, v, j) for v,j in branch]
    for (v0,j0),(v1,j1) in zip(branch,branch[1:]):
        slope = (j1-j0)/(v1-v0)
        if slope < 0:
            voltage = (v0-j0/slope)/2
            if v0 < voltage < v1:
                current = j0+slope*(voltage-v0)
                candidates.append((voltage*current, voltage, current))
    power, voltage, current = max(candidates)
    if not 0 < power <= voc*abs(jsc):
        raise ValueError("Максимальная мощность не согласуется с Voc и Jsc этой кривой")
    return power, dict(voc=voc, jsc=jsc, vmpp=voltage, jmpp=current,
                       maximum_power_density_mW_cm2=power, uncertainty=None,
                       uncertainty_method="Погрешность положения максимума на оцифрованной кривой не оценена")


def retention_label(plot):
    text = " ".join(filter(None,(plot.get("caption_text"),plot["axes"].get("y",{}).get("label"))))
    return RETENTION_LABEL.search(text)


def compute(plot, metric, irradiance=None):
    if metric=="Jsc_EQE" and plot["plot_type"]=="EQE":
        value, interval=integrate_eqe(plot["points"],plot["axes"])
        return value,"mA/cm²","0.1 × q × ∫ EQE(λ) × Φ_ASTMG173(λ) dλ",{
            "reference":REFERENCE,"reference_url":SOURCE,"wavelength_nm":interval,"coverage":"observed_interval"}
    if metric=="T80" and plot["plot_type"]=="stability":
        axes=plot["axes"]
        if axes.get("y",{}).get("unit") not in ("%","1"):
            raise ValueError("По оси Y требуется доля от начального КПД")
        factor=1 if axes["y"]["unit"]=="1" else .01
        series=sorted((p["x"],p["y"]*factor) for p in plot["points"] if p["x"] is not None and p["y"] is not None)
        if len(series)<2 or abs(series[0][1]-1)>.02 or abs(series[0][0])>1e-9:
            raise ValueError("Для T80 нужна кривая удержания КПД от t = 0 и начального значения около 100%")
        if len(set(x for x,_ in series))!=len(series):
            raise ValueError("Для T80 нужны уникальные временные точки")
        for (x0,y0),(x1,y1) in zip(series,series[1:]):
            if y0>.8>=y1:
                value,u=d.normalize("T80",x0+(.8-y0)*(x1-x0)/(y1-y0),axes.get("x",{}).get("unit"))
                if value is None:
                    raise ValueError("Укажите единицу времени")
                return value,u,"t0 + (0.8 − R0) × (t1 − t0)/(R1 − R0)",{"threshold":.8,"bracket":[x0,x1]}
        raise ValueError("На наблюдаемом участке нет пересечения порога T80")
    if metric in ("Voc","Jsc") and plot["plot_type"]=="JV":
        axes=plot["axes"]
        solve,output=("y","x") if metric=="Voc" else ("x","y")
        ordered=sorted((p[solve],p[output]) for p in plot["points"] if p[solve] is not None and p[output] is not None)
        if len(ordered)<2 or d.normalize("Voc",1,axes.get("x",{}).get("unit"))[0] is None or d.normalize("Jsc",1,axes.get("y",{}).get("unit"))[0] is None:
            raise ValueError("Нужна откалиброванная ВАХ с единицами напряжения и плотности тока")
        zeroes=[(i,v) for i,(axis,v) in enumerate(ordered) if abs(axis)<1e-12]
        brackets=[(a,b) for a,b in zip(ordered,ordered[1:]) if a[0]<0<b[0]]
        if len(zeroes)+len(brackets)!=1:
            raise ValueError("Нужно одно однозначное пересечение нуля на оцифрованном участке")
        if zeroes:
            index,value=zeroes[0]
            pair=(ordered[index],ordered[index+1] if index+1<len(ordered) else ordered[index-1])
        else:
            pair=brackets[0]
            (a,b),(c,e)=pair
            value=b-a*(e-b)/(c-a)
        normalized,unit=d.normalize(metric,value,axes[output].get("unit"))
        if normalized is None:
            raise ValueError("Единица оси не переводится в целевую единицу метрики")
        (a,b),(c,e)=pair
        uncertainty=plot["uncertainty"]
        combined=uncertainty[output]+abs((e-b)/(c-a))*uncertainty[solve] if uncertainty and c!=a else None
        sigma=d.normalize(metric,combined,axes[output].get("unit"))[0]
        return normalized,unit,"Пересечение оси при 0, линейная интерполяция смежных точек",{
            "uncertainty":sigma,"uncertainty_method":"u_out + |местный наклон| × u_in" if sigma is not None else None}
    if metric in ("FF","PCE") and plot["plot_type"]=="JV":
        if metric=="PCE" and not irradiance:
            raise ValueError("Для КПД нужна указанная в PDF интенсивность освещения этого измерения")
        power,inputs=maximum_power(plot)
        if metric=="FF":
            return 100*power/(inputs["voc"]*abs(inputs["jsc"])),"%","100 × Pmax / (Voc × |Jsc|)",inputs
        value=100*power/irradiance["light_intensity"]
        if value>100:
            raise ValueError("Мощность по ВАХ превышает указанную интенсивность освещения")
        inputs.update(pin_mW_cm2=irradiance["light_intensity"],pin_source_id=irradiance["intensity_source_id"])
        return value,"%","100 × Pmax / Pin",inputs
    raise ValueError("Эта метрика не поддерживается для выбранного типа графика")


def publish(connection, plot, metric, refresh=False, coverage_confirmed=False, aggregation="unknown"):
    rid=key(plot["id"],"calculated",metric)
    current=one(connection,"SELECT * FROM measurement_result WHERE id=?",rid)
    if current and current["manual"]:
        return current
    irradiance=one(connection,"""SELECT m.light_intensity,m.intensity_source_id FROM measurement m
        JOIN source_reference s ON s.id=m.intensity_source_id
        WHERE m.id=? AND m.device_id=? AND m.measurement_type='JV' AND m.light_intensity>0
          AND m.light_intensity_unit='mW/cm²' AND s.pdf_version_id=?""",
        plot["measurement_id"],plot["device_id"],plot["pdf_version_id"]) if metric=="PCE" else None
    try:
        value,unit,formula,inputs=compute(plot,metric,irradiance)
        if not math.isfinite(value):
            raise ValueError("Результат выходит за числовой диапазон")
        if metric=="Jsc_EQE":
            if refresh and current:
                previous=current["calculation_inputs"] or {}
                coverage_confirmed=previous.get("coverage_confirmed",False) and previous.get("wavelength_nm")==inputs["wavelength_nm"]
            inputs["coverage_confirmed"]=coverage_confirmed
        fields=dict(aggregation=current["aggregation"] if refresh and current else aggregation, metric_value=value, metric_unit=unit, normalized_value=value, normalized_unit=unit,
                    value_state="numeric_zero" if value==0 else "numeric", formula=formula, calculation_inputs=inputs,
                    extraction_status=d.REVIEW)
    except ValueError as exc:
        if not refresh or not current:
            raise
        fields=dict(metric_value=None, normalized_value=None, value_state="unparseable", extraction_status=d.REVIEW,
                    calculation_inputs={"error":str(exc)})
    device=one(connection,"SELECT material_id FROM device WHERE id=?",plot["device_id"])
    fields.update(device_id=plot["device_id"],measurement_id=plot["measurement_id"],material_id=device["material_id"] if device else None)
    if current:
        edit(connection,"measurement_result",current,fields,"softx-calculation")
    else:
        insert(connection,"measurement_result", id=rid,publication_id=plot["publication_id"],pdf_version_id=plot["pdf_version_id"],
               metric_name=metric,raw_record={},acquisition_method="digitized_plot" if metric in ("Voc","Jsc") else "calculated",
               extraction_method="plot_calculation",**fields)
        insert(connection,"result_plot",result_id=rid,plot_id=plot["id"])
    for source in rows(connection,"SELECT source_reference_id FROM plot_source WHERE plot_id=?",plot["id"]):
        insert(connection,"result_source",result_id=rid,**source)
    if irradiance and fields["metric_value"] is not None:
        insert(connection,"result_source",result_id=rid,source_reference_id=irradiance["intensity_source_id"])
    return one(connection,"SELECT * FROM measurement_result WHERE id=?",rid)


def extract_plot_results(connection, plot):
    existing = {row["metric_name"] for row in rows(connection,"""SELECT r.metric_name FROM measurement_result r
        JOIN result_plot rp ON r.id=rp.result_id WHERE rp.plot_id=?""",plot["id"])}
    enabled = one(connection,"SELECT pv_extraction_enabled FROM pdf_version WHERE id=?",plot["pdf_version_id"])
    metrics = PLOT_METRICS.get(plot["plot_type"],()) if enabled["pv_extraction_enabled"] != 0 else ()
    for metric in sorted(existing.union(metrics)):
        if metric not in existing and metric=="T80" and not retention_label(plot):
            continue
        try:
            publish(connection,plot,metric,refresh=True)
        except ValueError:
            # An ambiguous crossing or missing prerequisite provides no numeric result.
            if metric in existing:
                raise
