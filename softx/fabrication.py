"""Evidence-backed absorber recipes and layer sequences in fabrication sections."""
import re
from dataclasses import dataclass

from . import domain

CHEM = r"(?:[A-Z][a-z]?[0-9₀-₉]*){2,}"
FORMULA = re.compile(r"(?<!\w)(?=[A-Za-z0-9₀-₉]*\d|[A-Za-z0-9₀-₉]*[₀-₉])" + CHEM + r"(?!\w)")
DOPANT = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*M\s+(" + CHEM + r")(?!\w)")
PRECURSOR = re.compile(r"(?<!\w)(" + CHEM + r")\s*\(\s*(\d+(?:\.\d+)?)\s*M\s*\)")
DIGITS = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")
HEADING = re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+)?(?:(?:device|solar cell)\s+fabrication|fabrication of (?:solar cells|devices)|device preparation)\s*$", re.I)
SECTION = re.compile(r"^\d+(?:\.\d+)+\.?\s+[A-Z]|^(?:characterization|measurements|results(?: and discussion)?|references)\s*$", re.I)
STACK = re.compile(r"(?<!\w)(?:glass\s*/\s*)?(?:FTO|ITO|AZO)\s*/\s*[-\w()₂₃ ]{2,25}\s*/\s*[-\w()₂₃ ]{2,36}\s*/\s*[-\w()₂₃ ]{2,24}\s*/\s*(?:Ag|Au|Al|Cu|carbon)(?!\w)", re.I)
FRONT = re.compile(r"\b(FTO|ITO|AZO)\b(?=.{0,15}\b(?:glass|substrate))")
ETL = re.compile(r"\b(?:compact\s+)?((?-i:" + CHEM + r"))(?=\s+(?:blocking\s+layer|electron transport layer))"
                 r"|(?:electron transport layer|ETL)\s+(?:solution\s+)?\([^()]*?\b((?-i:" + CHEM + r"))\)", re.I)
HTL = re.compile(r"\b(P3HT|PTAA|PEDOT:PSS|spiro[-–]OMeTAD|NiO[x]?)\b", re.I)
METAL = re.compile(r"\b(Ag|Au|Al|Cu|silver|gold|aluminum|copper)\b", re.I)
ANNEAL = re.compile(r"anneal\w*\s+(?:at\s+)?(\d+\s*[°◦º]\s*C)(?:\s+for\s+((?:\d+\s*(?:min|hours?|h|s)\b)|an?\s+hour))?", re.I)


@dataclass
class Passage:
    lines: list

    @property
    def text(self):
        return " ".join(line.text for line in self.lines)

    def sources(self, start, end):
        offset = 0
        for line in self.lines:
            last = offset + len(line.text)
            if start < last and end > offset:
                yield line, line.span.part(max(start - offset, 0), min(end, last) - offset)
            offset = last + 1


def fabrication_passage(lines):
    for index, heading in enumerate(lines):
        if not HEADING.fullmatch(heading.text):
            continue
        body = []
        for line in lines[index + 1:index + 55]:
            if line.page != heading.page:
                break
            if abs(line.span.bbox[0] - heading.span.bbox[0]) > 36:
                continue
            if SECTION.match(line.text):
                break
            body.append(line)
        if body:
            return Passage(body)
    return Passage([])


def absorber_recipe(title, material_class, passage):
    recipe = list(PRECURSOR.finditer(passage.text))
    if material_class == "Перовскиты" and len(recipe) >= 3 and re.search(r"perovskite precursor solution", passage.text, re.I):
        components = [f"{m[1]} {m[2]} M" for m in recipe]
        return f"Перовскит ({', '.join(components)})", None, components
    if material_class == "Перовскиты":
        for context in (title or "", passage.text):
            for match in FORMULA.finditer(context):
                formula = match[0].translate(DIGITS)
                if re.search(r"(?:Pb|Sn).*[IBrCl]", formula) and re.search(r"(?:MA|FA|Cs|Rb)", formula):
                    return formula, formula, None
    for name, formula in (("CdTe", "CdTe"), ("Sb₂Se₃", "Sb2Se3")):
        if material_class == name:
            return name, formula, None
    return None, None, None


def identify_layers(passage, stacks, absorber):
    text = passage.text
    front, etl, htl = FRONT.search(text), ETL.search(text), HTL.search(text)
    contact = None
    for m in METAL.finditer(text):
        context = text[max(0, m.start() - 42):m.end() + 72]
        if re.search(r"evaporat|electrode", context, re.I):
            contact = m
    if front and etl and htl and contact:
        etl_material = etl[1] or etl[2]
        if etl[1] and etl[0].lower().startswith("compact"):
            etl_material = "compact " + etl_material
        # The recipe gives the sequence and the layer-specific process context.
        absorber_match = re.search(r"perovskite precursor solution|\b" + re.escape(absorber or "Перовскиты") + r"\b", text, re.I)
        if absorber_match and etl.start() < absorber_match.start() < htl.start():
            etl_start = max(front.end(), text.rfind(". ", 0, etl.start()) + 2)
            absorber_end = htl.start()
            contact_start = max(htl.end(), text.rfind(". ", 0, contact.start()) + 2)
            return [
                ("front_contact", front[1], front.span()),
                ("ETL", etl_material, (etl_start, absorber_match.start())),
                ("absorber", absorber or absorber_match[0], (absorber_match.start(), absorber_end)),
                ("HTL", htl[0], (htl.start(), contact_start)),
                ("back_contact", {"silver": "Ag", "gold": "Au", "aluminum": "Al", "copper": "Cu"}.get(contact[0].lower(), contact[0]),
                 (contact_start, min(len(text), contact.end() + 72))),
            ]
    if stacks:
        stack, line = stacks
        materials = [item.strip() for item in stack[0].split("/") if item.strip().lower() != "glass"]
        if len(materials) == 5 and re.search(r"ETL|electron transport", materials[1], re.I) and re.search(
                r"perovskite|absorber", materials[2], re.I):
            return list(zip(("front_contact", "ETL", "absorber", "HTL", "back_contact"),
                            materials, [None] * 5))
    return []


def layer_properties(text):
    method = re.search(r"spin[- ]coat\w*(?:\s+at\s+[\d,]+\s*rpm\s+for\s+\d+\s*s)?|thermal(?:ly)?\s+evaporat\w*|immersed\s+in\s+[^.;]{1,105}", text, re.I)
    anneal = ANNEAL.search(text)
    atmosphere = re.search(r"(?:nitrogen|N2|argon)\s+glove\s*box|(?:approximately\s+)?\d+\s*%\s*RH", text, re.I)
    thickness = re.search(rf"(?<![\w.])({domain.NUM})\s*(nm|[µμu]m)\s*[- ]?\s*thick\b", text, re.I)
    return dict(
        deposition_method=method[0] if method else None,
        annealing_temperature=anneal[1] if anneal else None,
        annealing_time=anneal[2] if anneal else None,
        atmosphere=atmosphere[0] if atmosphere else None,
        layer_thickness=float(thickness[1].replace(",", ".")) if thickness else None,
        thickness_unit=thickness[2] if thickness else None,
    )
