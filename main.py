import html
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os
import re
import time
from collections import defaultdict, deque
from urllib.parse import quote
from xml.etree import ElementTree as ET

import httpx
import yfinance as yf
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from google import genai
from google.genai import types
from pydantic import BaseModel

import db
import docs
import radar
import mailer
import report

# Set these in your host's environment settings (never in the code):
#   GEMINI_API_KEY  = your key from Google AI Studio
#   GEMINI_MODEL    = the current Flash-Lite model ID shown in AI Studio
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_client = None
try:
    db.init()
except Exception as ex:
    print("Database not ready:", repr(ex))

SYSTEM = """You are Athena, a sharp strategy partner at a top consulting firm. Write like one: direct, structured, no filler. Never say "As an AI".
Rules:
1. Use only the SOURCES provided and your search results. Never invent figures.
2. Label every point "evidence" (directly supported by a listed source, citing its id) or "inference" (your reasoning, with confidence high, medium or low, citing the ids it rests on).
3. If the sources do not support a claim, say so plainly instead of guessing.
4. If there is no market data (private company), say metrics are unavailable."""

HITS = defaultdict(deque)


def get_client():
    global _client
    if _client is None:
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise HTTPException(500, "Server is missing GEMINI_API_KEY.")
        _client = genai.Client(api_key=key)
    return _client


def limit(request: Request, n=20, per=3600):
    fwd = request.headers.get("x-forwarded-for")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "x")
    q, now = HITS[(ip, n)], time.time()
    while q and now - q[0] > per:
        q.popleft()
    if len(q) >= n:
        raise HTTPException(429, "Too many requests. Please try again later.")
    q.append(now)


def resolve(name):
    try:
        for q in yf.Search(name, max_results=6).quotes:
            if q.get("quoteType") == "EQUITY":
                return q["symbol"], q.get("longname") or q.get("shortname") or name
    except Exception:
        pass
    return None, name


def get_metrics(sym):
    try:
        i = yf.Ticker(sym).info
        m = {
            "market_cap": i.get("marketCap"),
            "revenue_growth": i.get("revenueGrowth"),
            "operating_margin": i.get("operatingMargins"),
            "currency": i.get("currency"),
            "pe": i.get("trailingPE"),
        }
        return m if any(m[k] for k in ("market_cap", "revenue_growth", "operating_margin")) else {}
    except Exception:
        return {}


def get_news(name, n=10):
    url = f"https://news.google.com/rss/search?q={quote(name)}&hl=en-US&gl=US&ceid=US:en"
    try:
        r = httpx.get(url, timeout=10, follow_redirects=True)
        out = []
        for it in ET.fromstring(r.content).iter("item"):
            out.append({
                "title": html.unescape(it.findtext("title", "")),
                "url": it.findtext("link", ""),
                "publisher": it.findtext("source") or "",
            })
            if len(out) >= n:
                break
        return out
    except Exception:
        return []


def get_history(sym):
    try:
        h = yf.Ticker(sym).history(period="1y", interval="1wk")["Close"].dropna()
        return [[d.strftime("%Y-%m-%d"), round(float(v), 2)] for d, v in h.items()]
    except Exception:
        return []


def company_pack(name, tag):
    sym, full = resolve(name)
    metrics = get_metrics(sym) if sym else {}
    srcs = [{"id": f"{tag}{i + 1}", "kind": "news", **n} for i, n in enumerate(get_news(full, 5))]
    if metrics:
        srcs.append({"id": f"{tag}M", "kind": "metrics", "title": f"{full} market metrics", "publisher": "Yahoo Finance", "url": ""})
    return {"company": full, "ticker": sym, "metrics": metrics, "history": get_history(sym) if sym else [], "sources": srcs}


def gen(prompt, grounded=True):
    cfg = types.GenerateContentConfig(
        system_instruction=SYSTEM,
        temperature=0.3,
        tools=[types.Tool(google_search=types.GoogleSearch())] if grounded else None,
    )
    r = get_client().models.generate_content(model=MODEL, contents=prompt, config=cfg)
    web = []
    try:
        for c in r.candidates[0].grounding_metadata.grounding_chunks:
            web.append({"title": c.web.title, "url": c.web.uri})
    except Exception:
        pass
    return r.text or "", web


def parse(text):
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(t[t.find("{"): t.rfind("}") + 1])


def clean(items, valid):
    """Enforce honesty: Evidence without a real source becomes low-confidence Inference."""
    out = []
    for it in items or []:
        ids = [s for s in it.get("sources", []) if s in valid]
        kind, conf = it.get("type"), it.get("confidence")
        if kind == "evidence" and not ids:
            kind, conf = "inference", "low"
        if kind != "evidence":
            kind = "inference"
            conf = conf if conf in ("high", "medium", "low") else "medium"
        out.append({
            "text": str(it.get("text", "")),
            "type": kind,
            "confidence": conf if kind == "inference" else None,
            "sources": ids,
        })
    return out


def brief_prompt(company, ticker, metrics, srcs):
    lines = "\n".join(f"[{s['id']}] {s['title']} ({s.get('publisher') or s['kind']})" for s in srcs)
    return f"""Company: {company} ({ticker or 'no public ticker found'})
SOURCES:
{lines or '(none found)'}
MARKET METRICS (source M): {json.dumps(metrics)}

Return ONLY JSON, no markdown, in this shape:
{{"headline":"one sentence, the main takeaway","spoken":"a 70-word spoken briefing, plain sentences, no ids or markdown","pulse":[3 items],"risks":[3 items],"opportunities":[2 or 3 items]}}
Each item: {{"text":"under 28 words","type":"evidence or inference","confidence":"high|medium|low|null","sources":["S1"]}}
Opportunities must be inference."""


class BriefReq(BaseModel):
    company: str


class ChatReq(BaseModel):
    question: str
    brief: dict


def build_brief(raw):
    name = raw.strip()[:80]
    if not name:
        raise HTTPException(400, "Enter a company name.")
    sym, full = resolve(name)
    metrics = get_metrics(sym) if sym else {}
    srcs = [{"id": f"S{i + 1}", "kind": "news", **n} for i, n in enumerate(get_news(full))]
    if metrics:
        srcs.append({"id": "M", "kind": "metrics", "title": "Market metrics", "publisher": "Yahoo Finance", "url": ""})
    try:
        text, web = gen(brief_prompt(full, sym, metrics, srcs))
        data = parse(text)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "Athena could not produce a briefing. Please try again.")
    valid = {s["id"] for s in srcs}
    return {
        "company": full,
        "ticker": sym,
        "metrics": metrics,
        "headline": str(data.get("headline", "")),
        "spoken": str(data.get("spoken", "")),
        "history": get_history(sym) if sym else [],
        "pulse": clean(data.get("pulse"), valid),
        "risks": clean(data.get("risks"), valid),
        "opportunities": clean(data.get("opportunities"), valid),
        "sources": srcs,
        "web": web[:8],
    }


@app.post("/api/brief")
def brief(req: BriefReq, request: Request):
    limit(request)
    return build_brief(req.company)


@app.post("/api/chat")
def chat(req: ChatReq, request: Request):
    limit(request)
    keys = ("company", "ticker", "metrics", "headline", "pulse", "risks", "opportunities", "sources")
    ctx = json.dumps({k: req.brief.get(k) for k in keys})[:12000]
    prompt = (
        f"BRIEFING (your only evidence):\n{ctx}\n\nQuestion: {req.question[:500]}\n"
        "Answer in under 120 words. Mark claims as Evidence [source id] or Inference (confidence). "
        "If the briefing cannot support the answer, say so and say what to look up."
    )
    try:
        text, _ = gen(prompt, grounded=False)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "Athena could not answer. Please try again.")
    return {"answer": text}


class CmpReq(BaseModel):
    companies: list[str]


@app.post("/api/compare")
def compare(req: CmpReq, request: Request):
    limit(request)
    names = [c.strip()[:80] for c in req.companies if c.strip()][:3]
    if len(names) < 2:
        raise HTTPException(400, "Pick at least two companies.")
    packs = [company_pack(n, "ABC"[i]) for i, n in enumerate(names)]
    ctx = "\n\n".join(
        f"{p['company']} ({p['ticker'] or 'no ticker'}), metrics: {json.dumps(p['metrics'])}\n"
        + "\n".join(f"[{s['id']}] {s['title']}" for s in p["sources"])
        for p in packs
    )
    prompt = f"""Compare these companies for a decision-maker.
{ctx}

Return ONLY JSON, no markdown:
{{"verdict":"one sentence on the biggest difference","spoken":"a 70-word spoken comparison, plain sentences, no ids","differences":[3 or 4 items]}}
Each item: {{"text":"under 28 words","type":"evidence or inference","confidence":"high|medium|low|null","sources":["A1"]}}
Use only the sources and metrics above. Say plainly when data is missing for a company."""
    try:
        text, _ = gen(prompt, grounded=False)
        data = parse(text)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "Athena could not compare these companies. Please try again.")
    valid = {s["id"] for p in packs for s in p["sources"]}
    return {
        "packs": packs,
        "verdict": str(data.get("verdict", "")),
        "spoken": str(data.get("spoken", "")),
        "differences": clean(data.get("differences"), valid),
    }


BRIEF_KEYS = ("company", "ticker", "metrics", "headline", "pulse", "risks", "opportunities", "sources")

CONSULT = {
    "tree": """Build an issue tree for this question: "<<Q>>".
Return ONLY JSON: {"question":"...","branches":[{"name":"short, mutually exclusive driver","hypothesis":"testable, under 20 words","data":"what to check, under 15 words","confidence":"high|medium|low","children":[{"name":"sub-driver","hypothesis":"under 20 words","data":"under 15 words"}]}],"so_what":"one sentence: the first hypothesis to test"}
Use 3 or 4 branches and at most 2 children each. Everything here is hypothesis, not fact.""",
    "interview": """Prepare someone for an interview or first meeting with this company.
Return ONLY JSON: {"talking_points":[3 items],"questions_to_ask":[4 sharp, specific questions],"likely_case":{"prompt":"a realistic case question about this company","first_step":"how to start structuring it"}}
Each talking point: {"text":"under 28 words","type":"evidence or inference","confidence":"high|medium|low|null","sources":["S1"]}""",
    "case_new": """Write ONE short consulting practice case set at this company. Pick market sizing or profitability at random.
Return ONLY JSON: {"kind":"market sizing or profitability","prompt":"3 to 4 sentences stating the client's problem and the ask","hint":"one line on what a good structure covers"}""",
    "case_review": """Case given to a student: <<Q>>
Student's answer: <<A>>
Review like a consulting interviewer. Return ONLY JSON: {"scores":{"structure":1 to 5,"quant":1 to 5,"insight":1 to 5},"strengths":[2 short items],"gaps":[2 or 3 short items],"better_structure":"3 to 5 clauses separated by semicolons","next_drill":"one sentence"}""",
}


class ConsultReq(BaseModel):
    mode: str
    brief: dict
    question: str = ""
    answer: str = ""


@app.post("/api/consult")
def consult(req: ConsultReq, request: Request):
    limit(request)
    if req.mode not in CONSULT:
        raise HTTPException(400, "Unknown service.")
    if req.mode == "case_review" and len(req.answer.strip()) < 20:
        raise HTTPException(400, "Write a fuller answer first.")
    ctx = json.dumps({k: req.brief.get(k) for k in BRIEF_KEYS})[:12000]
    task = CONSULT[req.mode].replace("<<Q>>", req.question[:600]).replace("<<A>>", req.answer[:2500])
    try:
        text, _ = gen(f"BRIEFING (your only evidence):\n{ctx}\n\n{task}", grounded=False)
        data = parse(text)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "Athena could not finish that. Please try again.")
    if req.mode == "interview":
        valid = {s.get("id") for s in req.brief.get("sources", [])}
        data["talking_points"] = clean(data.get("talking_points"), valid)
    return data


# ---------- Services: scheduled emails, job alerts, saved briefs ----------
BASE = (os.getenv("BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
CRON_SECRET = os.getenv("CRON_SECRET", "")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


class SubReq(BaseModel):
    kind: str
    email: str
    params: dict
    sched: dict
    consent: bool = False


def clean_list(v, n, maxlen):
    return [str(x).strip()[:maxlen] for x in (v or []) if str(x).strip()][:n]


def msg_page(title, text):
    return HTMLResponse(report.shell(title, f"<h2>{title}</h2><p>{text}</p><p><a href='/'>Back to Athena</a></p>", "Athena"))


def parse_sched(raw):
    try:
        s = {"day": int(raw["day"]), "hour": int(raw["hour"]), "tz": str(raw["tz"])}
        ZoneInfo(s["tz"])
        assert 0 <= s["day"] <= 6 and 0 <= s["hour"] <= 23
        return s
    except Exception:
        raise HTTPException(400, "Check the day, time and timezone.")


def clean_suppliers(lst):
    out = []
    for x in (lst or [])[:10]:
        try:
            name, loc = str(x.get("name", "")).strip()[:80], str(x.get("location", "")).strip()[:80]
            spend = max(0.0, min(float(x.get("spend") or 0), 1e12))
        except Exception:
            continue
        if name and loc:
            out.append({"name": name, "location": loc, "spend": spend})
    return out


@app.post("/api/subscribe")
def subscribe(req: SubReq, request: Request):
    limit(request, n=5, per=3600)
    email = req.email.strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise HTTPException(400, "Enter a valid email address.")
    if not req.consent:
        raise HTTPException(400, "Please tick the consent box.")
    if req.kind == "brief":
        p = {"companies": clean_list(req.params.get("companies"), 3, 80)}
        if not p["companies"]:
            raise HTTPException(400, "Add at least one company.")
    elif req.kind == "radar":
        p = {"suppliers": clean_suppliers(req.params.get("suppliers"))}
        if not p["suppliers"]:
            raise HTTPException(400, "Add at least one supplier with a location.")
    else:
        raise HTTPException(400, "Unknown service.")
    s = parse_sched(req.sched)
    if db.count_subs(email) >= 3:
        raise HTTPException(400, "Limit: 3 subscriptions per email.")
    if db.count_subs() >= int(os.getenv("MAX_SUBS", "200")):
        raise HTTPException(503, "Sign-ups are full right now.")
    _, token = db.add_sub(req.kind, email, p, s)
    link = f"{BASE}/api/confirm?t={token}"
    try:
        mailer.send(email, "Confirm your Athena subscription", report.shell(
            "Confirm", f"<p>Tap to confirm your subscription:</p><p><a href='{link}'>Confirm subscription</a></p>"
                       "<p>If this was not you, ignore this email.</p>", "Athena"))
    except Exception as ex:
        print("confirm email failed:", repr(ex))
        db.unsub(token)
        raise HTTPException(502, "Could not send the confirmation email. Try again later.")
    return {"ok": True, "message": "Check your inbox and tap the confirm link."}


@app.get("/api/confirm")
def confirm(t: str):
    ok = db.confirm(t)
    return msg_page("Confirmed" if ok else "Link not valid",
                    "You are subscribed. Every email has an unsubscribe link." if ok else "This link was already used or is not valid.")


@app.get("/api/unsub")
def unsub(t: str):
    ok = db.unsub(t)
    return msg_page("Unsubscribed" if ok else "Link not valid",
                    "You are unsubscribed and your details are deleted." if ok else "This link was already used or is not valid.")


@app.get("/b/{bid}")
def saved(bid: str):
    d = db.get_brief(bid)
    if not d:
        raise HTTPException(404, "Briefing not found.")
    return HTMLResponse(report.radar_page(d) if d.get("kind") == "radar" else report.page_html(d))


def last_slot(now, day, hour, tz):
    n = now.astimezone(ZoneInfo(tz))
    s = n.replace(hour=hour, minute=0, second=0, microsecond=0) - timedelta(days=(n.weekday() - day) % 7)
    if s > n:
        s -= timedelta(days=7)
    return s.timestamp()


def is_due(sub, now):
    if sub["kind"] not in ("brief", "radar"):
        return False
    s = json.loads(sub["sched"])
    return last_slot(now, s["day"], s["hour"], s["tz"]) > sub["last_sent"]


def ask_json(prompt):
    text, _ = gen(prompt, grounded=False)
    return parse(text)


def send_brief_sub(sub, p):
    sections, links = [], []
    for name in p["companies"][:3]:
        prev = db.latest_brief(name.lower())
        cur = build_brief(name)
        bid = db.save_brief(name.lower(), cur)
        sections.append(report.company_html(cur, report.changes(prev, cur)))
        links.append((cur["company"], f"{BASE}/b/{bid}"))
    mailer.send(sub["email"], "Your Athena briefing: " + ", ".join(p["companies"]),
                report.email_html(sections, links, f"{BASE}/api/unsub?t={sub['token']}"))
    db.mark_sent(sub["id"])


def send_radar_sub(sub, p):
    key = "radar:" + sub["id"]
    prev = db.latest_brief(key)
    res = radar.build(p["suppliers"], ask_json)
    res["kind"] = "radar"
    bid = db.save_brief(key, res)
    mailer.send(sub["email"], "Your Athena supplier risk radar",
                report.radar_email(res, report.radar_changes(prev, res), f"{BASE}/b/{bid}", f"{BASE}/api/unsub?t={sub['token']}"))
    db.mark_sent(sub["id"])


def run_due(subs):
    for sub in subs:
        try:
            p = json.loads(sub["params"])
            (send_radar_sub if sub["kind"] == "radar" else send_brief_sub)(sub, p)
        except Exception as ex:
            print("send failed", sub["id"], repr(ex))
            db.mark_sent(sub["id"])


@app.post("/api/cron")
def cron(request: Request, bg: BackgroundTasks):
    if not CRON_SECRET or request.headers.get("x-cron-secret") != CRON_SECRET:
        raise HTTPException(401, "Unauthorized.")
    now = datetime.now(timezone.utc)
    due = [x for x in db.confirmed() if is_due(x, now)][:10]
    bg.add_task(run_due, due)
    return {"queued": len(due)}


class RadarReq(BaseModel):
    suppliers: list[dict]


@app.post("/api/radar")
def radar_api(req: RadarReq, request: Request):
    limit(request, n=6, per=3600)
    sup = clean_suppliers(req.suppliers)
    if not sup:
        raise HTTPException(400, "Add at least one supplier with a location.")
    try:
        res = radar.build(sup, ask_json)
    except Exception as ex:
        print("radar failed:", repr(ex))
        raise HTTPException(502, "The radar could not run. Please try again.")
    res["kind"] = "radar"
    res["id"] = db.save_brief("radar", res)
    return res


class DocUp(BaseModel):
    name: str
    data: str


class DocQ(BaseModel):
    doc_id: str
    question: str = ""


def load_doc(did):
    name, pages = db.get_doc(did)
    if not pages:
        raise HTTPException(404, "This document has expired. Please upload it again.")
    return name, pages


@app.post("/api/doc/upload")
def doc_upload(req: DocUp, request: Request):
    limit(request, n=8, per=3600)
    db.purge_docs()
    try:
        pages, total = docs.extract(req.data)
    except ValueError as ex:
        raise HTTPException(400, str(ex))
    except Exception:
        raise HTTPException(400, "Could not read this PDF.")
    name = req.name.strip()[:120] or "Document"
    return {"doc_id": db.save_doc(name, pages), "name": name, "pages": len(pages), "total_pages": total}


@app.post("/api/doc/brief")
def doc_brief(req: DocQ, request: Request):
    limit(request, n=12, per=3600)
    name, pages = load_doc(req.doc_id)
    try:
        return docs.brief(name, pages, ask_json)
    except Exception as ex:
        print("doc brief failed:", repr(ex))
        raise HTTPException(502, "Athena could not read this document. Please try again.")


@app.post("/api/doc/ask")
def doc_ask(req: DocQ, request: Request):
    limit(request, n=40, per=3600)
    if not req.question.strip():
        raise HTTPException(400, "Type a question.")
    name, pages = load_doc(req.doc_id)
    try:
        return docs.ask(name, pages, req.question, ask_json)
    except Exception as ex:
        print("doc ask failed:", repr(ex))
        raise HTTPException(502, "Athena could not answer. Please try again.")


@app.post("/api/doc/delete")
def doc_delete(req: DocQ, request: Request):
    limit(request, n=20, per=3600)
    return {"deleted": db.delete_doc(req.doc_id)}


def page(name):
    return FileResponse(os.path.join(os.path.dirname(__file__), name))


@app.get("/radar")
def radar_page():
    return page("radar.html")


@app.get("/documents")
def documents_page():
    return page("documents.html")


@app.get("/shared.css")
def shared_css():
    return page("shared.css")


@app.get("/services")
def services():
    return FileResponse(os.path.join(os.path.dirname(__file__), "services.html"))


@app.get("/")
def home():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))
