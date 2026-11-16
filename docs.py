"""Document intelligence: read a PDF, answer with page citations, verify quotes against the page text."""
import base64
import io
import re

MAX_BYTES = 8_000_000
MAX_PAGES = 150


def extract(b64):
    from pypdf import PdfReader
    raw = base64.b64decode(b64)
    if len(raw) > MAX_BYTES:
        raise ValueError("The file is larger than 8 MB.")
    r = PdfReader(io.BytesIO(raw))
    if r.is_encrypted:
        raise ValueError("Password-protected PDFs are not supported.")
    pages = [(p.extract_text() or "").strip() for p in r.pages[:MAX_PAGES]]
    if sum(len(p) for p in pages) < 300:
        raise ValueError("No readable text found. Scanned PDFs are not supported yet.")
    return pages, len(r.pages)


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def verify(quote, page_text):
    q = norm(quote)
    return len(q.split()) >= 3 and q in norm(page_text)


def pick(pages, question, budget=150_000):
    if sum(len(p) for p in pages) <= budget:
        return list(range(len(pages)))
    words = {w for w in norm(question).split() if len(w) > 3}
    normed = [norm(p) for p in pages]
    top = sorted(range(len(pages)), key=lambda i: -sum(normed[i].count(w) for w in words))[:15]
    return sorted(top)


def context(pages, idx):
    return "\n\n".join(f"[PAGE {i + 1}]\n{pages[i][:6000]}" for i in idx)


def _cite(c, pages):
    try:
        p = int(c.get("page", 0))
    except Exception:
        return None
    if not 1 <= p <= len(pages):
        return None
    q = str(c.get("quote", ""))[:200]
    return {"page": p, "quote": q, "verified": verify(q, pages[p - 1])}


def ask(name, pages, question, ask_json):
    idx = pick(pages, question)
    prompt = (f'DOCUMENT "{name}":\n{context(pages, idx)}\n\nQuestion: {question[:500]}\n'
              "Answer using ONLY the document above. Return ONLY JSON: "
              '{"answer":"under 120 words, plain sentences","found":true,"citations":[{"page":1,"quote":"exact words copied from that page, 5 to 20 words"}]}\n'
              "If the document does not answer the question, set found to false, say so in the answer and return no citations.")
    d = ask_json(prompt)
    cites = [c for c in (_cite(x, pages) for x in (d.get("citations") or [])[:4]) if c]
    return {"answer": str(d.get("answer", "")), "found": bool(d.get("found", True)), "citations": cites, "pages_read": len(idx)}


def brief(name, pages, ask_json):
    idx = pick(pages, "key dates obligations penalties termination payment risks")
    prompt = (f'DOCUMENT "{name}":\n{context(pages, idx)}\n\n'
              "Extract what a busy manager needs. Return ONLY JSON: "
              '{"type":"contract|policy|report|other","summary":"3 plain sentences",'
              '"dates":[{"text":"...","page":1,"quote":"exact words from that page, 5 to 20 words"}],'
              '"obligations":[same shape],"risks":[same shape],"numbers":[same shape]}\n'
              "Up to 5 items per list. Use only the document. Leave a list empty if nothing applies.")
    d = ask_json(prompt)
    out = {"type": str(d.get("type", "other")), "summary": str(d.get("summary", "")), "pages_read": len(idx)}
    for k in ("dates", "obligations", "risks", "numbers"):
        items = []
        for x in (d.get(k) or [])[:5]:
            c = _cite(x, pages)
            if c:
                items.append({"text": str(x.get("text", "")), **c})
        out[k] = items
    return out
