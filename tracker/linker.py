"""Decide which application an email belongs to, and what status the emails add up to."""
import re
from difflib import SequenceMatcher

from .domains import GENERIC_DOMAINS, employer_domain

_SUFFIX = {"gmbh", "ag", "se", "kg", "ug", "mbh", "inc", "ltd", "llc", "co", "corp", "gruppe", "group",
           "holding", "und", "the", "der", "die", "das", "limited", "sa", "bv", "nv", "plc", "ev", "gbr"}
_ROLE_NOISE = re.compile(r"\(\s*(m|w|f|d|x|all genders|gn|mwd|m/w/d|w/m/d|f/m/d|m/f/d|f/m/x|x/f/m)[^)]*\)|\ball genders\b|\b[mwfdx]/[mwfdx](/[mwfdx])?\b", re.I)

RANK = {"applied": 1, "assessment": 2, "interview": 3, "offer": 4}
EVENT_TO_STATUS = {"application_confirmation": "applied", "assessment": "assessment",
                   "interview_invite": "interview", "offer": "offer"}


def norm_company(name: str) -> str:
    toks = [t for t in re.split(r"[^\w]+", (name or "").lower()) if t and t not in _SUFFIX]
    return " ".join(toks)


def same_company(a: str, b: str) -> bool:
    na, nb = norm_company(a), norm_company(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = sorted((na, nb), key=len)
    return len(short) >= 5 and long_.startswith(short)  # "bundesdruckerei" ~ "bundesdruckerei berlin"


def norm_role(role: str) -> str:
    r = _ROLE_NOISE.sub(" ", role or "")
    return re.sub(r"\s+", " ", re.sub(r"[^\w+#. ]+", " ", r.lower())).strip()


def roles_similar(a: str, b: str, strict: bool = False) -> bool:
    """Loose (default): is this reply probably about that job? Strict: is it the very same job title?
    Strict is used for confirmations, so 'Cloud Engineer' and 'Cloud Software Engineer' stay two applications."""
    na, nb = norm_role(a), norm_role(b)
    if not na or not nb:
        return not strict  # nothing to compare: fine for a reply, but never merge two confirmations on that basis
    ratio = SequenceMatcher(None, na, nb).ratio()
    if strict:
        return ratio >= 0.9
    if ratio >= 0.6:
        return True
    ta, tb = set(na.split()), set(nb.split())
    if not (ta and tb):
        return False
    small, big = sorted((ta, tb), key=len)
    if len(small) >= 2 and small <= big:  # "DevOps Engineer" inside "DevOps Engineer Berlin Platform Team"
        return True
    return len(ta & tb) / len(ta | tb) >= 0.5


def role_score(a: str, b: str) -> float:
    return SequenceMatcher(None, norm_role(a), norm_role(b)).ratio()


def useful_domain(addr: str) -> str:
    return employer_domain(addr)


_SHARED_MAILBOX = re.compile(r"(no-?reply|donotreply|notification|mailer|alert|bounce|system|robot|survey|^jobs?$|^careers?$|"
                             r"^recruiting$|^hr$|talent|karriere|bewerbung|^info$|support|workday|personio)", re.I)
_NEW_APP_ON_MISMATCH = {"interview_invite", "assessment", "offer", "rejection"}
_DECISIVE = {"application_confirmation", "assessment", "interview_invite", "offer", "rejection"}
_POSITIVE = {"interview_invite", "assessment", "offer"}


_COMMON_WORDS = {"recruiting", "recruiter", "recruitment", "careers", "talent", "notifications", "noreply", "support", "service",
                 "team", "hiring", "bewerbung", "karriere", "personal", "candidate", "jobs", "emea", "europe", "global"}


def _person_tokens(local: str, domain: str, company: str) -> set[str]:
    """Distinctive name-like pieces of an address, e.g. 'c.a.traistaru' -> {'traistaru'}."""
    own = norm_company(company).replace(" ", "")
    out = set()
    for t in re.split(r"[^a-z]+", local.lower()):
        if len(t) >= 6 and t not in _COMMON_WORDS and t not in domain and not (own and (t in own or own in t)):
            out.add(t)
    return out


def personal_sender_apps(conn, em: dict) -> list[int]:
    """Applications that this sender, or the same person writing from another address, already wrote about.
    Shared mailboxes (noreply@, jobs@, notifications@) write about many applications, so they say nothing; neither
    does your own webmail address."""
    addr = (em.get("from_addr") or "").lower()
    if not addr or "@" not in addr or _SHARED_MAILBOX.search(addr.split("@")[0]) or addr.split("@")[1] in GENERIC_DOMAINS:
        return []
    local, domain = addr.split("@")
    mine = _person_tokens(local, domain, em.get("company", ""))
    hits: set[int] = set()
    rows = conn.execute("SELECT lower(from_addr) a, application_id FROM emails WHERE application_id IS NOT NULL AND id != ? AND from_addr LIKE '%@%'",
                        (em.get("id", -1),)).fetchall()
    for r in rows:
        other = r["a"]
        if other == addr:
            hits.add(r["application_id"])
            continue
        o_local, o_domain = other.split("@", 1)
        if o_domain in GENERIC_DOMAINS or _SHARED_MAILBOX.search(o_local):
            continue
        theirs = _person_tokens(o_local, o_domain, em.get("company", ""))
        # the same surname inside the other address: c.a.traistaru@ and ...corinatraistaru@
        if any(t in local or t in mine for t in theirs) or any(t in o_local for t in mine):
            hits.add(r["application_id"])
    return sorted(hits)


def open_as_of(conn, a, em: dict) -> bool:
    """Was this application still waiting for an answer when this email was sent? A mail inviting you to an
    interview is not about an application that had been rejected two weeks earlier."""
    events = [r["event_type"] for r in conn.execute(
        "SELECT event_type FROM emails WHERE application_id=? AND sent_at < ? AND event_type IN "
        "('application_confirmation','assessment','interview_invite','offer','rejection') ORDER BY sent_at",
        (a["id"], em.get("sent_at") or "9999"))]
    if not events:
        return a["source"] == "email" or a["status"] not in ("rejected", "withdrawn")
    return derive_status(events, "applied") != "rejected"


def started_before(conn, a, em: dict) -> bool:
    """Mail cannot be about an application that did not exist yet. Only trusted for applications created from mail,
    since the dates of imported or hand-typed ones are whatever you typed."""
    if a["source"] != "email":
        return True
    # only confirmations date the start: other mail may have been linked later or by hand
    first = conn.execute("SELECT MIN(sent_date) FROM emails WHERE application_id=? AND event_type='application_confirmation'", (a["id"],)).fetchone()[0]
    dates = [d for d in (a["applied_date"], first) if d]
    return not dates or min(dates) <= (em.get("sent_date") or "9999")


def find_application(conn, em: dict):
    """Returns (application_id or None, review_reason). None means: create a new application."""
    # 1. a reply in the same email thread
    refs = [r for r in (em.get("refs") or "").split() if r]
    if refs:
        q = ",".join("?" * len(refs))
        row = conn.execute(f"SELECT application_id FROM emails WHERE message_id IN ({q}) AND application_id IS NOT NULL LIMIT 1", refs).fetchone()
        if row:
            return row["application_id"], ""

    apps = [a for a in conn.execute("SELECT * FROM applications ORDER BY COALESCE(applied_date,'') DESC, id DESC").fetchall()
            if started_before(conn, a, em)]
    cands = [a for a in apps if em.get("company") and same_company(a["company"], em["company"])]
    # 2. no application under that employer name: fall back to the sender's own domain
    dom = useful_domain(em.get("from_addr", ""))
    if not cands and dom:
        by_domain = [a for a in apps if dom in (a["domains"] or "").split()]
        if not em.get("company"):
            cands = by_domain
        else:  # the employer is spelled differently than before: accept only when the job title agrees as well
            cands = [a for a in by_domain if em.get("role") and a["role"] and roles_similar(a["role"], em["role"])]

    if em["event_type"] == "application_confirmation":
        # a confirmation is a new application unless the same role is already tracked
        for a in cands:
            # imported rows have hand-typed, often shortened roles, so they get the loose comparison
            if roles_similar(a["role"], em.get("role", ""), strict=a["source"] != "import"):
                return a["id"], ""
        return None, ""

    # 3. an invitation arrives, but every application at that company was already rejected: look for an open one
    #    that this sender's domain is connected to (Avanade's recruiter writes from an Accenture address)
    if em["event_type"] in _POSITIVE and dom and cands and not any(open_as_of(conn, a, em) for a in cands):
        cands = cands + [a for a in apps if a not in cands and dom in (a["domains"] or "").split() and open_as_of(conn, a, em)]
    # 4. the same person already wrote about one application: a strong hint
    aff_ids = set(personal_sender_apps(conn, em))
    pool = list(cands) + [a for a in apps if a["id"] in aff_ids and a not in cands]
    if not pool:
        return None, ""

    role = em.get("role", "")
    is_open = {a["id"]: open_as_of(conn, a, em) for a in pool}

    def score(a):
        if role and a["role"]:
            # how close the titles are; "similar" titles never score below 0.6, and an exact match still beats a near one
            s = role_score(a["role"], role)
            if roles_similar(a["role"], role):
                s = max(s, 0.6)
        else:
            s = 0.4  # nothing to compare
        return s + (0.5 if a["id"] in aff_ids else 0) + (0.3 if is_open[a["id"]] else 0)

    ranked = sorted(pool, key=score, reverse=True)  # stable: newer applications win ties
    best = ranked[0]
    # A different job title is a different application: "Site Reliability Engineer" is not "DevOps Engineer",
    # even when it is the only application at that company.
    if role and best["role"] and not roles_similar(best["role"], role):
        if em["event_type"] in _NEW_APP_ON_MISMATCH:
            return None, ""
        return None, ""  # a follow-up about another job: leave it unlinked rather than pin it on the wrong one
    # An invitation for a job whose application was already rejected is a new application unless the title is
    # (nearly) identical: msg rejected "Software Engineer Banking", then invited him to "Software Engineer Public Sector".
    if (em["event_type"] in _POSITIVE and not is_open[best["id"]] and role and best["role"]
            and role_score(best["role"], role) < 0.8):
        return None, ""
    ties = [a for a in ranked if score(best) - score(a) < 0.05]
    if len(ties) > 1:
        still_open = [a for a in ties if is_open[a["id"]]]
        if len(still_open) == 1:
            return still_open[0]["id"], ""  # only one of them can still be waiting for an answer
        if em["event_type"] in _DECISIVE:
            return best["id"], f"{len(pool)} applications at this company, please check the assignment"
    return best["id"], ""


def derive_status(events: list[str], current: str) -> str:
    """events: event types in time order. Rejection closes an application; a later positive mail reopens it."""
    state = None
    for t in events:
        if t == "rejection":
            state = "rejected"
        elif t in EVENT_TO_STATUS:
            new = EVENT_TO_STATUS[t]
            if state in (None, "rejected") or RANK[new] > RANK.get(state, 0):
                state = new
    return state or current
