from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader


def extract_text_from_pdf(pdf_path: str) -> str:
    p = Path(pdf_path)
    if not p.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    if p.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a .pdf file, got: {pdf_path}")

    reader = PdfReader(str(p))
    parts: list[str] = []
    for page in reader.pages:
        txt = page.extract_text() or ""
        txt = txt.replace("\x00", "")
        if txt.strip():
            parts.append(txt)
    return "\n\n".join(parts).strip()

