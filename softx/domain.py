"""Thin-film PV registry. Parsing and storage consume this configuration."""
import re

AUTO = "Извлечено автоматически"
REVIEW = "Требует проверки"
CONFIRMED = "Подтверждено пользователем"
AI_EXTRACTION_METHODS = {"local":"local_ai_pdf_quote", "openai":"openai_pdf_quote",
                         "deepseek":"deepseek_pdf_quote", "deepseek_vision":"deepseek_vision"}
UNITS = {
    "PCE": {"%": 1, "1": 100}, "FF": {"%": 1, "1": 100}, "HI": {"%": 1, "1": 100},
    "Voc": {"V": 1, "mV": .001}, "Jsc": {"mA/cm²": 1, "A/m²": .1, "A/cm²": 1000},
    "Jsc_EQE": {"mA/cm²": 1, "A/m²": .1, "A/cm²": 1000},
    "Eg": {"eV": 1, "meV": .001}, "T80": {"h": 1, "min": 1/60, "s": 1/3600, "days": 24},
    "thickness": {"nm": 1, "µm": 1000, "Å": .1},
    "area": {"cm²": 1, "mm²": .01, "m²": 10000},
    "Pin": {"mW/cm²": 1, "W/m²": .1, "W/cm²": 1000},
    "Rs": {"Ω·cm²": 1, "Ω": 1}, "Rsh": {"Ω·cm²": 1, "Ω": 1},
}
METRICS = {
    "PCE": r"PCE|power conversion efficiency|[ηɳ]",
    "Voc": r"V[ _]*[oₒ][c꜀]|open[- ]circuit voltage",
    "Jsc_EQE": r"J[ _]*[sₛ][c꜀][ _-]*EQE|EQE[- ]integrated J[ _]*[sₛ][c꜀]",
    "Jsc": r"J[ _]*[sₛ][c꜀]|short[- ]circuit current density",
    "FF": r"FF|fill factor",
    "Rs": r"R[ _]?s(?:eries)?|series resistance",
    "Rsh": r"R[ _]?sh(?:unt)?|shunt resistance",
    "HI": r"HI|hysteresis index",
    "T80": r"T[ _]?80",
    "Eg": r"E[ _]?g|band gap|bandgap",
}
CLASSES = {
    "Перовскиты": r"perovskite|перовскит", "CIGS": r"CIGS|Cu\(In,Ga\)Se|copper indium gallium",
    "CdTe": r"CdTe|cadmium telluride", "Кестериты": r"kesterite|CZTS|Cu2ZnSn",
    "Sb₂Se₃": r"Sb[₂2]Se[₃3]|antimony selenide", "Тонкоплёночный Si": r"thin[- ]film silicon|a-Si|amorphous silicon|µc-Si",
}
UNIT_PATTERN = (r"m?V\b|m?eV\b|%|m?A\s*/\s*(?:cm|m)\s*(?:\^?2|²)|"
                r"m?A\s*[·⋅∙]?\s*(?:cm|m)\s*(?:[-−�][²2]|⁻²)|m?W\s*/\s*(?:cm|m)\s*(?:\^?2|²)|"
                r"[cm]m\s*(?:\^2|²|2)|m\s*(?:\^2|²|2)|nm\b|[µμu]m\b|Å|"
                r"hours?\b|days?\b|h\b|min\b|s\b|(?:[ΩΩ]|O(?=\s*cm))(?:\s*[·*]?\s*cm\s*(?:\^2|[²2]))?|dimensionless\b|fraction\b")
NUM = r"[+−-]?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?|[.,]\d+)(?:[Ee][+-]?\d+)?"


def normalize(metric, value, unit):
    if unit is None or value is None:
        return None, None
    clean = re.sub(r"\s+", "", unit.replace("Ω", "Ω")).replace("^2", "²").replace("cm2", "cm²").replace("mm2", "mm²").replace("/m2", "/m²")
    clean = re.sub(r"(m?A)[·⋅∙]?(cm|m)(?:[-−�][²2]|⁻²)", r"\1/\2²", clean)
    clean = {"μm": "µm", "um": "µm", "hour": "h", "hours": "h", "day": "days",
             "m2": "m²", "Ocm²": "Ω·cm²", "Ωcm²": "Ω·cm²", "Ω*cm²": "Ω·cm²"}.get(clean, clean)
    clean = {"dimensionless":"1","fraction":"1"}.get(clean.lower(),clean)
    factors = UNITS.get(metric, {})
    if clean in factors:
        target = clean if metric in ("Rs", "Rsh") else next(iter(factors))
        return value * factors[clean], target
    return None, None


def has_photovoltaic_context(text, material_class):
    return bool(re.search(r"\b(?:photovoltaics?|solar|PV)\s*(?:[-–]\s*)?(?:cells?|devices?|modules?|panels?)\b"
                          r"|\bphotovoltaic\b|\bAM\s*1[.,]5\s*G\b|фотоэлектрическ\w*|солнечн\w*\s+элемент", text, re.I)
                or (material_class and re.search(r"\bsingle[-\s]+junction\b", text, re.I)))


def classify(text, material_class):
    tandem = r"\btandem (?:solar cells?|devices?)\b|\b(?:two|multi)[- ]junction solar cells?"
    if re.search(tandem, text[:260], re.I) and not re.search(r"single[- ]junction", text[:260], re.I):
        return "excluded_tandem", "Тандемный или многопереходный прибор указан в начале публикации"
    if re.search(r"simulation.only|purely theoretical|no (?:devices?|samples?) (?:were |was )?fabricated|without experimental", text, re.I):
        return "excluded_theoretical", "Публикация явно указывает на расчётный характер работы"
    if not has_photovoltaic_context(text, material_class):
        return "needs_review", "В тексте не подтверждён фотоэлектрический контекст; извлечение солнечных метрик приостановлено"
    if material_class and re.search(r"fabricat|deposit|experimentally prepar", text, re.I) and re.search(r"\b(?:PCE|Voc|Jsc|FF)\b|power conversion efficiency", text, re.I):
        return "included", "Обнаружены изготовление тонкоплёночного прибора и фотоэлектрические данные"
    return "needs_review", "Недостаточно явных признаков изготовленного однопереходного тонкоплёночного прибора"
