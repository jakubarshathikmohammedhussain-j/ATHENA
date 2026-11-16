import json
import os
import secrets
import time

from sqlalchemy import create_engine, text

url = os.getenv("DATABASE_URL", "sqlite:///athena.db")
if url.startswith("postgres://"):
    url = url.replace("postgres://", "postgresql+psycopg://", 1)
elif url.startswith("postgresql://"):
    url = url.replace("postgresql://", "postgresql+psycopg://", 1)
engine = create_engine(url, pool_pre_ping=True)


def init():
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE IF NOT EXISTS subs (id TEXT PRIMARY KEY, kind TEXT, email TEXT, params TEXT, "
            "sched TEXT, confirmed INTEGER DEFAULT 0, token TEXT, created DOUBLE PRECISION, "
            "last_sent DOUBLE PRECISION DEFAULT 0, seen TEXT DEFAULT '[]')"))
        c.execute(text("CREATE TABLE IF NOT EXISTS docs (id TEXT PRIMARY KEY, name TEXT, pages TEXT, created DOUBLE PRECISION)"))
        c.execute(text(
            "CREATE TABLE IF NOT EXISTS briefs (id TEXT PRIMARY KEY, title TEXT, data TEXT, created DOUBLE PRECISION)"))


def _rows(res):
    return [dict(r._mapping) for r in res]


def count_subs(email=None):
    with engine.begin() as c:
        if email:
            return c.execute(text("SELECT COUNT(*) FROM subs WHERE email=:e"), {"e": email}).scalar()
        return c.execute(text("SELECT COUNT(*) FROM subs")).scalar()


def add_sub(kind, email, params, sched):
    sid, token = secrets.token_hex(6), secrets.token_urlsafe(18)
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO subs (id, kind, email, params, sched, token, created) "
            "VALUES (:i,:k,:e,:p,:s,:t,:c)"),
            {"i": sid, "k": kind, "e": email, "p": json.dumps(params), "s": json.dumps(sched), "t": token, "c": time.time()})
    return sid, token


def confirm(token):
    with engine.begin() as c:
        r = c.execute(text("UPDATE subs SET confirmed=1, last_sent=:n WHERE token=:t AND confirmed=0"),
                      {"n": time.time(), "t": token})
        return r.rowcount > 0


def get_by_token(token):
    with engine.begin() as c:
        rows = _rows(c.execute(text("SELECT * FROM subs WHERE token=:t"), {"t": token}))
    return rows[0] if rows else None


def unsub(token):
    with engine.begin() as c:
        return c.execute(text("DELETE FROM subs WHERE token=:t"), {"t": token}).rowcount > 0


def confirmed():
    with engine.begin() as c:
        return _rows(c.execute(text("SELECT * FROM subs WHERE confirmed=1")))


def mark_sent(sid, seen=None):
    with engine.begin() as c:
        if seen is None:
            c.execute(text("UPDATE subs SET last_sent=:n WHERE id=:i"), {"n": time.time(), "i": sid})
        else:
            c.execute(text("UPDATE subs SET last_sent=:n, seen=:s WHERE id=:i"),
                      {"n": time.time(), "s": json.dumps(seen), "i": sid})


def save_brief(title, data):
    bid = secrets.token_hex(5)
    with engine.begin() as c:
        c.execute(text("INSERT INTO briefs (id, title, data, created) VALUES (:i,:t,:d,:c)"),
                  {"i": bid, "t": title, "d": json.dumps(data), "c": time.time()})
    return bid


def get_brief(bid):
    with engine.begin() as c:
        r = c.execute(text("SELECT data FROM briefs WHERE id=:i"), {"i": bid}).first()
    return json.loads(r[0]) if r else None


def latest_brief(title):
    with engine.begin() as c:
        r = c.execute(text("SELECT data FROM briefs WHERE title=:t ORDER BY created DESC LIMIT 1"), {"t": title}).first()
    return json.loads(r[0]) if r else None


def save_doc(name, pages):
    did = secrets.token_urlsafe(12)
    with engine.begin() as c:
        c.execute(text("INSERT INTO docs (id, name, pages, created) VALUES (:i,:n,:p,:c)"),
                  {"i": did, "n": name, "p": json.dumps(pages), "c": time.time()})
    return did


def get_doc(did):
    with engine.begin() as c:
        r = c.execute(text("SELECT name, pages FROM docs WHERE id=:i"), {"i": did}).first()
    return (r[0], json.loads(r[1])) if r else (None, None)


def delete_doc(did):
    with engine.begin() as c:
        return c.execute(text("DELETE FROM docs WHERE id=:i"), {"i": did}).rowcount > 0


def purge_docs(max_age=86400):
    with engine.begin() as c:
        c.execute(text("DELETE FROM docs WHERE created < :t"), {"t": time.time() - max_age})
