"""Create and refresh derived metrics linked to the underlying plot and source."""
from . import domain as d
from .spectrum import REFERENCE, SOURCE, integrate_eqe
from .store import edit, insert, key, one, rows


def compute(plot, metric):
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
        if not ordered:
            raise ValueError("Нужна откалиброванная ВАХ")
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
    raise ValueError("Эта метрика не поддерживается для выбранного типа графика")


def publish(connection, plot, metric, refresh=False, coverage_confirmed=False, aggregation="unknown"):
    rid=key(plot["id"],"calculated",metric)
    current=one(connection,"SELECT * FROM measurement_result WHERE id=?",rid)
    if current and current["manual"]:
        return current
    try:
        value,unit,formula,inputs=compute(plot,metric)
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
    return one(connection,"SELECT * FROM measurement_result WHERE id=?",rid)
