"""Bibliography from first-page typography, publisher lines, and PDF metadata."""
import re

DOI = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
PUBLISHED = re.compile(r"\b(?:copyright|published(?: online)?|опубликовано|©)\W{0,8}(?:\d{1,2}\s+\w+\s+)?((?:19|20)\d{2})\b", re.I)
DOI_MARKER = re.compile(r"\b(?:doi\s*[:=]|doi\.org/)\s*(" + DOI.pattern + ")", re.I)


def bibliographic_doi(metadata, lines):
    marked = DOI.search(str(metadata.get("/DOI", "")))
    if marked:
        return marked.group().rstrip(".,;").lower()
    # A DOI in first-page front matter can be in a footer or a citation sidebar.
    # Competing identifiers require an explicit journal metadata line.
    candidates = {}
    for line in lines:
        if line.page != 1:
            break
        for cell in line.cells:
            match = DOI_MARKER.search(cell.text)
            if match:
                doi = match[1].rstrip(".,;").lower()
                previous = candidates.get(doi, 0)
                weight = 2 if match.start() == 0 else 1
                candidates[doi] = max(previous, weight)
    if candidates:
        best = max(candidates.values())
        winners = [doi for doi, score in candidates.items() if score == best]
        if len(winners) == 1:
            return winners[0]
    return None


def front_matter(metadata, lines):
    first = [line for line in lines if line.page == 1 and not line.rotated and not line.image_annotation]
    title = str(metadata.get("/Title") or "").strip()
    if re.fullmatch(r"(?:[\w.-]+\.(?:pdf|docx?)|[\w.-]+\s+\d+\s*\.{2,}\s*\d+)", title, re.I):
        title = ""
    authors = str(metadata.get("/Author") or "").strip()
    fragments = []
    for line in first[:24]:
        text = line.text.strip()
        if len(line.cells) == 1 and len(text.split()) >= 3 and not re.match(
            r"(?:synthetic test|тестовый документ|abstract|keywords|original research|published|doi:|https?://)", text, re.I
        ):
            fragments.append((line.span.bbox[3] - line.span.bbox[1], line))
    if fragments:
        height, headline = max(fragments, key=lambda item: item[0])
        index = first.index(headline)
        start = index
        for previous in reversed(first[:index]):
            box, next_box = previous.span.bbox, first[start].span.bbox
            if abs(box[0] - next_box[0]) > 12 or box[1] - next_box[1] > height * 1.9 or box[3] - box[1] < height * .82:
                break
            start -= 1
        end = index + 1
        for following in first[end:]:
            box, previous_box = following.span.bbox, first[end - 1].span.bbox
            if abs(box[0] - previous_box[0]) > 12 or previous_box[1] - box[1] > height * 1.9 or box[3] - box[1] < height * .82:
                break
            end += 1
        if not title:
            title = " ".join(line.text for line in first[start:end])
        if not authors and end < len(first):
            candidate = first[end].text
            names = re.split(r"\s*,\s*|\s+and\s+", candidate)
            clean = [re.sub(r"\s*[\d*†‡,]+$", "", name).strip() for name in names]
            if len(clean) >= 2 and all(re.fullmatch(r"[^\W\d_]+(?:[-. ’'\w]+)?", name) and
                                       2 <= len(name.split()) <= 5 and name[0].isupper() for name in clean):
                authors = "; ".join(clean)
    year = next((m[1] for line in first if (m := PUBLISHED.search(line.text))), None)
    journal = next((m[1].strip() for line in first if (m := re.match(r"(?:Journal|Журнал)\s*:\s*(.+)", line.text, re.I))), None)
    if not journal:
        for line in first:
            for cell in line.cells:
                m = re.fullmatch(r"([A-Z][A-Za-z &-]+)\s*\|\s*www\.[\w.-]+", cell.text)
                if not m:
                    m = re.fullmatch(r"([A-Z][A-Za-z &-]+)\s+(?:19|20)\d{2},\s*\d+,\s*\d+\.? (?:https?://doi\.org/\S+)", cell.text)
                if m:
                    journal = m[1].strip()
                    break
            if journal:
                break
    return title or None, [x.strip() for x in re.split(r";|\band\b", authors) if x.strip()] or None, int(year) if year else None, journal
