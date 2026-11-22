"""Thin-film PV registry. Parsing and storage consume this configuration."""
import re

AUTO = "Извлечено автоматически"
REVIEW = "Требует проверки"
CONFIRMED = "Подтверждено пользователем"
AI_EXTRACTION_METHODS = {"local":"local_ai_pdf_quote", "openai":"openai_pdf_quote",
                         "deepseek":"deepseek_pdf_quote", "deepseek_vision":"deepseek_vision"}
UNITS = {
    "EQE": {"%": 1},
    "retained_PCE": {"%": 1},
    "Nt": {"cm⁻³": 1}, "tau": {"s": 1, "ms": 1e-3, "µs": 1e-6, "ns": 1e-9, "ps": 1e-12},
    "stabilized_PCE": {"%": 1}, "Vmpp": {"V": 1, "mV": .001},
    "Jmpp": {"mA/cm²": 1, "A/m²": .1, "A/cm²": 1000},
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
    "retained_PCE": r"(?:efficiency|PCE)\s+retention|retained\s+(?:efficiency|PCE)|(?:сохранени[ея]|удержани[ея]|остаточн\w+)\s+(?:исходного\s+)?(?:КПД|эффективност[иь])",
    "stabilized_PCE": r"stabili[sz]ed\s+(?:(?:power conversion )?efficiency|PCE)",
    "Vmpp": r"V[ _]*mpp", "Jmpp": r"J[ _]*mpp",
    "PCE": r"PCE|power conversion efficiency|[ηɳ]|КПД|коэффициент\w*\s+полезного\s+действия|эффективност[ьи]\s+(?:фотоэлектрического\s+)?преобразования",
    "Voc": r"V[ _]*[oₒ](?:[c꜀]|(?<=ₒ)�)|open[- ]circuit voltage|напряжени[ея]\s+холостого\s+хода",
    "Jsc_EQE": r"J[ _]*[sₛ](?:[c꜀]|(?<=ₛ)�)[ _-]*EQE|EQE[- ]integrated J[ _]*[sₛ](?:[c꜀]|(?<=ₛ)�)|плотност[ьи]\s+тока\s+короткого\s+замыкания\s+по\s+(?:ВКЭ|внешней\s+квантовой\s+эффективности)",
    "Jsc": r"J[ _]*[sₛ](?:[c꜀]|(?<=ₛ)�)|short[- ]circuit current density|плотност[ьи]\s+тока\s+короткого\s+замыкания",
    "FF": r"FF|fill factor|(?:фактор|коэффициент)\s+заполнения",
    "Rs": r"R[ _]?s(?:eries)?|series resistance|последовательн\w+\s+сопротивлени[ея]",
    "Rsh": r"R[ _]?sh(?:unt)?|shunt resistance|шунтирующ\w+\s+сопротивлени[ея]|сопротивлени[ея]\s+шунта",
    "HI": r"HI|hysteresis index|индекс\s+гистерезиса",
    "T80": r"T[ _]?80",
    "Eg": r"E[ _]?g|band gap|bandgap|ширин[аы]\s+запрещ[её]нной\s+зоны",
}
CLASSES = {
    "Перовскиты": r"perovskite|перовскит", "CIGS": r"CIGS|Cu\(In,Ga\)Se|copper indium gallium|селенид\w*\s+меди[,\s]+индия[,\s]+галлия",
    "CdTe": r"CdTe|cadmium telluride|теллурид\w*\s+кадмия", "Кестериты": r"kesterite|кестерит|CZTS|Cu2ZnSn",
    "Sb₂Se₃": r"Sb[₂2]Se[₃3]|antimony selenide|селенид\w*\s+сурьмы", "Тонкоплёночный Si": r"thin[- ]film silicon|a-Si|amorphous silicon|аморфн\w+\s+кремни[йя]|µc-Si",
}
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
RUSSIAN_AREA_UNIT = r"(?:[см]м|м)(?:\^?2|²)"
RUSSIAN_INTENSITY_UNIT = rf"(?:мВт|Вт)\s*/\s*{RUSSIAN_AREA_UNIT}"
RUSSIAN_UNIT = (
    rf"(?:мА|А)\s*/\s*{RUSSIAN_AREA_UNIT}|"
    r"(?:мА|А)\s*[·⋅∙]?\s*(?:см|м)\s*(?:[−-]2|⁻²)|"
    rf"{RUSSIAN_INTENSITY_UNIT}|Ом(?:\s*[·⋅*]?\s*{RUSSIAN_AREA_UNIT})?\b|"
    rf"{RUSSIAN_AREA_UNIT}|м?В\b|м?[эЭ][вВ]\b|нм\b|мкм\b|"
    r"мин\b|ч(?:ас(?:а|ов)?)?\b|сут(?:ки|ок)?\b|с\b"
)
RUSSIAN_UNIT_TOKENS = {
    "мА": "mA", "А": "A", "мВт": "mW", "Вт": "W", "мВ": "mV", "В": "V",
    "мэВ": "meV", "эВ": "eV", "нм": "nm", "мкм": "µm", "Ом": "Ω",
    "см": "cm", "мм": "mm", "м": "m", "мин": "min", "с": "s",
    "ч": "h", "час": "h", "часа": "h", "часов": "h",
    "сут": "days", "сутки": "days", "суток": "days",
}
UNIT_TOKEN = re.compile("|".join(sorted(RUSSIAN_UNIT_TOKENS, key=len, reverse=True)))
INTENSITY_UNIT = rf"mW\s*/\s*cm(?:\^?2|²)|W\s*/\s*m(?:\^?2|²)|{RUSSIAN_INTENSITY_UNIT}"
AREA_UNIT = rf"[cm]m[²2]|{RUSSIAN_AREA_UNIT}"
CURRENT_UNIT = r"(?:mA|A)\s*(?:/\s*(?:cm|m)(?:\^?2|²)|[·⋅∙]?\s*(?:cm|m)\s*(?:[−-]2|⁻²))"
THICKNESS_UNIT = r"nm|[µμu]m|Å|нм|мкм"
BANDGAP_UNIT = r"m?eV|м?[эЭ][вВ]"
UNIT_PATTERN = (RUSSIAN_UNIT + "|" + r"m?V\b|m?eV\b|%|m?A\s*/\s*(?:cm|m)\s*(?:\^?2|²)|"
                r"m?A\s*[·⋅∙]?\s*(?:cm|m)\s*(?:[-−�][²2]|⁻²)|m?W\s*/\s*(?:cm|m)\s*(?:\^?2|²)|"
                r"[cm]m\s*(?:\^2|²|2)|m\s*(?:\^2|²|2)|nm\b|[µμu]m\b|Å|"
                r"hours?\b|days?\b|h\b|min\b|s\b|(?:[ΩΩ]|O(?=\s*cm))(?:\s*[·*]?\s*cm\s*(?:\^2|[²2]))?|dimensionless\b|fraction\b")
BASE_NUM = r"[+−-]?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?|[.,]\d+)(?:[Ee][+-]?\d+)?"


POWER_OF_TEN = r"\s*[×x·]\s*10\s*(?:\^\s*[+−-]?\d{1,2}(?!\d)|[⁺⁻]?[⁰¹²³⁴⁵⁶⁷⁸⁹]{1,2}(?![⁰¹²³⁴⁵⁶⁷⁸⁹]))"
NUM = rf"{BASE_NUM}(?:{POWER_OF_TEN})?"
PV_JSC = re.compile(r"(?:J[ _]*[sₛ](?:[c꜀]|(?<=ₛ)�)|short[- ]circuit current density)[^\n;]{0,100}m?A\s*[/·]?\s*(?:cm|m)",re.I)
PV_VOC = re.compile(r"(?:V[ _]*[oₒ](?:[c꜀]|(?<=ₒ)�)|open[- ]circuit voltage)[^\n;]{0,85}(?:\bm?V\b|(?<=\d)m?V\b)",re.I)
PV_EFFICIENCY = re.compile(r"(?:\b(?:PCE|FF|fill factor)\b|[ηɳ])[^\n;]{0,40}%",re.I)


def normalize(metric, value, unit):
    if unit is None or value is None:
        return None, None
    if re.fullmatch(r"м?[эЭ][вВ]", unit):
        unit = unit.lower().replace("в", "В")
    if re.fullmatch(RUSSIAN_UNIT, unit):
        unit = UNIT_TOKEN.sub(lambda match: RUSSIAN_UNIT_TOKENS[match.group()], unit)
        unit = unit.replace("⋅", "·")
    clean = re.sub(r"\s+", "", unit.replace("Ω", "Ω")).replace("^2", "²").replace("cm2", "cm²").replace("mm2", "mm²").replace("/m2", "/m²")
    clean = re.sub(r"(m?A)[·⋅∙]?(cm|m)(?:[-−�][²2]|⁻²)", r"\1/\2²", clean)
    clean = {"μm": "µm", "um": "µm", "hour": "h", "hours": "h", "day": "days",
             "m2": "m²", "Ocm²": "Ω·cm²", "Ωcm²": "Ω·cm²", "Ω*cm²": "Ω·cm²"}.get(clean, clean)
    clean = {"dimensionless":"1","fraction":"1"}.get(clean.lower(),clean)
    if metric == "Nt" and re.fullmatch(r"cm(?:\^?[-−]3|⁻³)|1/cm(?:\^?3|³)",clean):
        clean = "cm⁻³"
    if metric == "tau":
        clean = clean.replace("μs","µs").replace("us","µs")
    factors = UNITS.get(metric, {})
    if clean in factors:
        target = clean if metric in ("Rs", "Rsh") else next(iter(factors))
        return value * factors[clean], target
    return None, None


def has_photovoltaic_context(text, material_class):
    return bool(re.search(r"\b(?:photovoltaics?|solar|PV)\s*(?:[-–]\s*)?(?:cells?|devices?|modules?|panels?)\b"
                          r"|\bphotovoltaics?\b|\bAM\s*1[.,]5\s*G\b|фотоэлектрическ\w*|солнечн\w*\s+(?:элемент|батаре)|фотовольтаик\w*|фотопреобразовател\w*", text, re.I)
                or (material_class and re.search(r"\bsingle[-\s]+junction\b", text, re.I)) or (PV_JSC.search(text) and PV_VOC.search(text) and PV_EFFICIENCY.search(text)))


def classify(text, material_class):
    tandem = r"\btandem (?:solar cells?|devices?)\b|\b(?:two|multi)[- ]junction solar cells?|(?:тандемн|многопереходн|двухпереходн)\w+\s+(?:(?:тонкопл[её]ночн|перовскитн|солнечн)\w+\s+){0,2}(?:элемент|фотоэлемент|батаре|фотопреобразовател)\w*"
    if re.search(tandem, text[:260], re.I) and not re.search(r"single[- ]junction|однопереходн\w*", text[:260], re.I):
        return "excluded_tandem", "Тандемный или многопереходный прибор указан в начале публикации"
    if re.search(r"simulation.only|purely theoretical|no (?:devices?|samples?) (?:were |was )?fabricated|without experimental|(?:без\s+изготовления|не\s+были\s+изготовлены|чисто\s+теоретическ\w+)", text, re.I):
        return "excluded_theoretical", "Публикация явно указывает на расчётный характер работы"
    if not has_photovoltaic_context(text, material_class):
        return "needs_review", "В тексте не подтверждён фотоэлектрический контекст; извлечение солнечных метрик приостановлено"
    if re.search(r"SYNTHETIC TEST DOCUMENT[^\n]{0,85}FICTIONAL|Synthetic document[^\n]{0,40}values are fictional|ТЕСТОВЫЙ ДОКУМЕНТ[^\n]{0,85}ВЫМЫШЛЕН",text,re.I):
        return "needs_review", "Тестовый PDF с вымышленными значениями"
    if material_class and re.search(r"fabricat|deposit|experimentally prepar|изготовлен\w*|экспериментально\s+получен\w*", text, re.I) and re.search(r"\b(?:PCE|Voc|Jsc|FF|КПД)\b|power conversion efficiency|фактор заполнения", text, re.I):
        return "included", "Обнаружены изготовление тонкоплёночного прибора и фотоэлектрические данные"
    return "needs_review", "Недостаточно явных признаков изготовленного однопереходного тонкоплёночного прибора"
