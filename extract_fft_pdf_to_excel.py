import io
import re
import sys
from dataclasses import dataclass, asdict
from typing import List, Optional, Tuple

import requests
import pdfplumber
import pandas as pd


@dataclass
class MatchRow:
    date: Optional[str]
    lieu: Optional[str]
    equipe_dom: Optional[str]
    equipe_ext: Optional[str]
    match_type: Optional[str]
    match_index: Optional[str]
    joueur_dom: Optional[str]
    joueur_ext: Optional[str]
    score: Optional[str]


def fetch_pdf_bytes(source: str, timeout_sec: int = 30) -> bytes:
    if source.startswith("file://"):
        path = source[len("file://") :]
        with open(path, "rb") as f:
            return f.read()
    # If it's a bare filesystem path
    if not (source.startswith("http://") or source.startswith("https://")):
        with open(source, "rb") as f:
            return f.read()
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/pdf,application/*;q=0.9,*/*;q=0.8",
    }
    resp = requests.get(source, headers=headers, timeout=timeout_sec, allow_redirects=True)
    resp.raise_for_status()
    return resp.content


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages_text: List[str] = []
        for page in pdf.pages:
            # Use extract_text with layout tolerance to preserve order
            txt = page.extract_text(x_tolerance=1.5, y_tolerance=2.0) or ""
            pages_text.append(txt)
    return "\n".join(pages_text)


def clean_text(text: str) -> str:
    # Normalize whitespace and dashes
    text = text.replace("\u00A0", " ")
    text = re.sub(r"[\t\r]+", " ", text)
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def find_header_info(text: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    # Attempt to extract date (dd/mm/yyyy) anywhere
    date_match = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", text)
    date_val = date_match.group(1) if date_match else None

    # Try to find line with team vs team
    # Accept "Club A - Club B" or "Club A vs Club B"
    team_match = re.search(r"(?m)^(?P<home>.+?)\s*(?:[-–]|vs)\s*(?P<away>.+?)$", text)
    equipe_dom = team_match.group("home").strip() if team_match else None
    equipe_ext = team_match.group("away").strip() if team_match else None

    # Try to find "Lieu" or a line starting with "Lieu :" or "Site :"
    lieu_match = re.search(r"(?mi)^(?:Lieu|Site)\s*[:\-]\s*(.+)$", text)
    lieu_val = lieu_match.group(1).strip() if lieu_match else None

    return date_val, lieu_val, equipe_dom, equipe_ext


def parse_matches(text: str, defaults: Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]) -> List[MatchRow]:
	date_val, lieu_val, equipe_dom, equipe_ext = defaults
	lines = text.splitlines()
	rows: List[MatchRow] = []

	# Flexible patterns for Singles and Doubles lines
	# Accepts e.g.: "Simple 1: Player A vs Player B Score: 6-4 4-6 10-7"
	# Label "Score" is optional, we also accept "Résultat/Resultat" variants.
	score_label = r"(?:(?:[Ss]core|R(?:ésultat|esultat))\s*[:\-])?"
	set_scores = r"([0-9]{1,2}-[0-9]{1,2}(?:\s+[0-9]{1,2}-[0-9]{1,2}){0,3})"
	player_sep = r"(?:vs|contre|[-–])"

	single_re = re.compile(
		rf"(?mi)^(?:Simple|SIMPLE|S)\s*(\d+)?\s*[:\-]?\s*(.+?)\s+{player_sep}\s+(.+?)\s*{score_label}\s*{set_scores}$"
	)
	double_re = re.compile(
		rf"(?mi)^(?:Double|DOUBLE|D)\s*(\d+)?\s*[:\-]?\s*(.+?)\s+{player_sep}\s+(.+?)\s*{score_label}\s*{set_scores}$"
	)

	# Also try compact rows: "PlayerHome - PlayerAway 6-4 4-6 10-7"
	table_row_re = re.compile(rf"(?mi)^(.+?)\s+[-–]\s+(.+?)\s+{set_scores}$")

	for line in lines:
		l = line.strip()
		if not l:
			continue

		m = single_re.search(l)
		if m:
			rows.append(
				MatchRow(
					date=date_val,
					lieu=lieu_val,
					equipe_dom=equipe_dom,
					equipe_ext=equipe_ext,
					match_type="Simple",
					match_index=(m.group(1) or "").strip() or None,
					joueur_dom=m.group(2).strip(),
					joueur_ext=m.group(3).strip(),
					score=re.sub(r"\s+", " ", (m.group(4) or "").strip()) or None,
				)
			)
			continue

		m = double_re.search(l)
		if m:
			rows.append(
				MatchRow(
					date=date_val,
					lieu=lieu_val,
					equipe_dom=equipe_dom,
					equipe_ext=equipe_ext,
					match_type="Double",
					match_index=(m.group(1) or "").strip() or None,
					joueur_dom=m.group(2).strip(),
					joueur_ext=m.group(3).strip(),
					score=re.sub(r"\s+", " ", (m.group(4) or "").strip()) or None,
				)
			)
			continue

		m = table_row_re.search(l)
		if m:
			rows.append(
				MatchRow(
					date=date_val,
					lieu=lieu_val,
					equipe_dom=equipe_dom,
					equipe_ext=equipe_ext,
					match_type=None,
					match_index=None,
					joueur_dom=m.group(1).strip(),
					joueur_ext=m.group(2).strip(),
					score=re.sub(r"\s+", " ", (m.group(3) or "").strip()) or None,
				)
			)

	# Deduplicate obvious duplicates
	uniq: List[MatchRow] = []
	seen = set()
	for r in rows:
		key = (r.match_type or "", r.match_index or "", r.joueur_dom or "", r.joueur_ext or "", r.score or "")
		if key in seen:
			continue
		seen.add(key)
		uniq.append(r)

	return uniq


def write_excel(rows: List[MatchRow], out_path: str) -> None:
    df = pd.DataFrame([asdict(r) for r in rows])
    # Ensure column order for Canva schema
    cols = [
        "date",
        "lieu",
        "equipe_dom",
        "equipe_ext",
        "match_type",
        "match_index",
        "joueur_dom",
        "joueur_ext",
        "score",
    ]
    for c in cols:
        if c not in df.columns:
            df[c] = None
    df = df[cols]

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Données")


def parse_matches_from_tables(pdf_bytes: bytes, defaults: Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]) -> List[MatchRow]:
    """Fallback parser that scans detected tables and tries to extract match rows.

    This is helpful for PDFs (e.g., Ten'Up exports) where text extraction loses structure.
    """
    date_val, lieu_val, equipe_dom, equipe_ext = defaults
    rows: List[MatchRow] = []

    # Re-create the same flexible patterns as in parse_matches
    score_label = r"(?:(?:[Ss]core|R(?:ésultat|esultat))\s*[:\-])?"
    set_scores = r"([0-9]{1,2}-[0-9]{1,2}(?:\s+[0-9]{1,2}-[0-9]{1,2}){0,3})"
    player_sep = r"(?:vs|contre|[-–])"

    single_re = re.compile(
        rf"(?mi)^(?:Simple|SIMPLE|S)\s*(\d+)?\s*[:\-]?\s*(.+?)\s+{player_sep}\s+(.+?)\s*{score_label}\s*{set_scores}$"
    )
    double_re = re.compile(
        rf"(?mi)^(?:Double|DOUBLE|D)\s*(\d+)?\s*[:\-]?\s*(.+?)\s+{player_sep}\s+(.+?)\s*{score_label}\s*{set_scores}$"
    )
    table_row_re = re.compile(rf"(?mi)^(.+?)\s+[-–]\s+(.+?)\s+{set_scores}$")

    def try_parse_line(candidate: str) -> Optional[MatchRow]:
        l = candidate.strip()
        if not l:
            return None
        m = single_re.search(l)
        if m:
            return MatchRow(
                date=date_val,
                lieu=lieu_val,
                equipe_dom=equipe_dom,
                equipe_ext=equipe_ext,
                match_type="Simple",
                match_index=(m.group(1) or "").strip() or None,
                joueur_dom=m.group(2).strip(),
                joueur_ext=m.group(3).strip(),
                score=re.sub(r"\s+", " ", (m.group(4) or "").strip()) or None,
            )
        m = double_re.search(l)
        if m:
            return MatchRow(
                date=date_val,
                lieu=lieu_val,
                equipe_dom=equipe_dom,
                equipe_ext=equipe_ext,
                match_type="Double",
                match_index=(m.group(1) or "").strip() or None,
                joueur_dom=m.group(2).strip(),
                joueur_ext=m.group(3).strip(),
                score=re.sub(r"\s+", " ", (m.group(4) or "").strip()) or None,
            )
        m = table_row_re.search(l)
        if m:
            return MatchRow(
                date=date_val,
                lieu=lieu_val,
                equipe_dom=equipe_dom,
                equipe_ext=equipe_ext,
                match_type=None,
                match_index=None,
                joueur_dom=m.group(1).strip(),
                joueur_ext=m.group(2).strip(),
                score=re.sub(r"\s+", " ", (m.group(3) or "").strip()) or None,
            )
        return None

    # Try table extraction with different strategies
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for settings in (
                {"vertical_strategy": "lines", "horizontal_strategy": "lines", "snap_tolerance": 3, "intersection_tolerance": 3},
                {"vertical_strategy": "text", "horizontal_strategy": "text"},
            ):
                try:
                    tables = page.extract_tables(table_settings=settings) or []
                except Exception:
                    tables = []
                if not tables:
                    continue
                for tbl in tables:
                    if not tbl:
                        continue
                    for raw_row in tbl:
                        if not raw_row:
                            continue
                        cells = [c.strip() for c in raw_row if c and isinstance(c, str)]
                        if not cells:
                            continue
                        # Primary candidate: all cells joined
                        candidates = [" ".join(cells)]
                        # If row looks like [Label/Type, Home, Away, Score]
                        if len(cells) >= 4:
                            candidates.append(f"{cells[0]} {cells[1]} - {cells[2]} {cells[3]}")
                        # If row looks like [Home, Away, Score]
                        if len(cells) >= 3:
                            candidates.append(f"{cells[0]} - {cells[1]} {cells[2]}")

                        parsed: Optional[MatchRow] = None
                        for cand in candidates:
                            parsed = try_parse_line(cand)
                            if parsed is not None:
                                rows.append(parsed)
                                break

    # Deduplicate
    uniq: List[MatchRow] = []
    seen = set()
    for r in rows:
        key = (r.match_type or "", r.match_index or "", r.joueur_dom or "", r.joueur_ext or "", r.score or "")
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    return uniq


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: python extract_fft_pdf_to_excel.py <pdf_or_url> <output_excel_path> [dump_text_path]")
        return 2

    url = sys.argv[1]
    out_path = sys.argv[2]

    pdf_bytes = fetch_pdf_bytes(url)
    raw_text = extract_text_from_pdf(pdf_bytes)
    text = clean_text(raw_text)

    # Optional: dump cleaned text for debugging/inspection
    if len(sys.argv) >= 4:
        dump_path = sys.argv[3]
        try:
            with open(dump_path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass

    date_val, lieu_val, equipe_dom, equipe_ext = find_header_info(text)
    text_rows = parse_matches(text, (date_val, lieu_val, equipe_dom, equipe_ext))
    table_rows = parse_matches_from_tables(pdf_bytes, (date_val, lieu_val, equipe_dom, equipe_ext))

    # Merge and deduplicate
    all_rows: List[MatchRow] = []
    seen_keys = set()
    for r in text_rows + table_rows:
        key = (r.match_type or "", r.match_index or "", r.joueur_dom or "", r.joueur_ext or "", r.score or "")
        if key in seen_keys:
            continue
        seen_keys.add(key)
        all_rows.append(r)

    # If no rows were parsed, still create a one-line header row with metadata for inspection
    if not all_rows:
        all_rows = [
            MatchRow(
                date=date_val,
                lieu=lieu_val,
                equipe_dom=equipe_dom,
                equipe_ext=equipe_ext,
                match_type=None,
                match_index=None,
                joueur_dom=None,
                joueur_ext=None,
                score=None,
            )
        ]

    write_excel(all_rows, out_path)
    print(f"Wrote Excel with {len(all_rows)} row(s) to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

