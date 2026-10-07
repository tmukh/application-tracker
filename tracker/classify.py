"""Decide what an email is. Two classifiers:

* OllamaClassifier: the local LLM, asked for JSON that must match a schema.
* KeywordClassifier: German/English phrase rules. It is the fallback when Ollama is down,
  and a cross-check on the LLM: when they disagree on something important, the email goes
  to the review queue instead of silently changing a status.
"""
import json
import re
from dataclasses import dataclass

import requests

from . import config
from .domains import company_from_domain

EVENT_TYPES = [
    "application_confirmation", "assessment", "interview_invite", "offer",
    "rejection", "follow_up", "other", "not_job",
]
DECISIVE = {"application_confirmation", "assessment", "interview_invite", "offer", "rejection"}


class LLMUnavailable(RuntimeError):
    """The model server cannot be reached right now. Emails stay queued and are retried later."""


@dataclass
class Result:
    event_type: str = "other"
    company: str = ""
    role: str = ""
    interview_at: str = ""
    summary: str = ""
    classifier: str = ""
    error: str = ""
    retryable: bool = False   # True = server down/busy, try again later instead of falling back


# ---------------------------------------------------------------- keywords
_RULES = [  # order matters: first match wins
    ("rejection", [
        r"\babsage\b", r"nicht (weiter )?berücksichtig", r"nicht weiter(verfolgen| im)", r"andere(n)? kandidat",
        r"für (einen )?andere(n)? (bewerber|kandidat)", r"leider (müssen|können|haben) wir",
        r"leider keine (passende|weitere)", r"entscheidung(en)? gegen", r"nicht in die engere",
        r"we regret", r"regret to inform", r"unfortunately,? (we|after|your)", r"not (be )?(moving|move|proceed)(ing)? forward",
        r"decided to (move|proceed) (forward )?with (other|another)", r"will not be (moving|proceeding)",
        r"other candidates", r"not selected",
    ]),
    ("offer", [r"\bjobangebot\b", r"vertragsangebot", r"arbeitsvertrag", r"job offer", r"offer letter", r"pleased to offer", r"wir freuen uns,? ihnen (ein|das) angebot"]),
    ("interview_invite", [
        r"einladung (zum|zu einem|zu einer)", r"vorstellungsgespräch", r"kennenlern", r"telefon(at|interview|ische)",
        r"\binterview\b", r"gespräch", r"invite you", r"schedule (a|an|our) (call|chat|interview)", r"calendly\.com",
        r"terminvorschlag", r"book a (time|slot)", r"video ?call", r"intro call",
    ]),
    ("assessment", [r"coding[- ]?challenge", r"take[- ]?home", r"assessment", r"hackerrank", r"codility", r"testaufgabe", r"fallstudie", r"case study", r"online[- ]?test", r"aufgabe"]),
    ("application_confirmation", [
        r"bewerbung[^.\n]{0,80}eingegangen", r"eingang ihrer bewerbung", r"bewerbung erhalten",
        r"vielen dank für ihre bewerbung", r"danke für (ihre|deine) bewerbung", r"danke für dein interesse",
        r"received your application", r"thank you for applying", r"thanks for applying", r"application (has been )?received",
        r"we('ve| have) received", r"your application (to|for|at|with)",
    ]),
]
_RULES = [(t, [re.compile(p, re.I) for p in pats]) for t, pats in _RULES]
_COMPANY_SUBJECT = [
    re.compile(r"(?:bewerbung|application|bewerbung als [^,]*?)\s*(?:bei|at|to|with|für|for)\s+(?:der |die |das |the )?([A-ZÄÖÜ][\w&.\-ÄÖÜäöüß ]{1,40}?)(?:\s*[-–|:(,!]|$)"),
    re.compile(r"\bat ([A-ZÄÖÜ][\w&.\-ÄÖÜäöüß ]{1,40}?)(?:\s*[-–|:(,!]|$)"),
    re.compile(r"\bbei (?:der |die |das )?([A-ZÄÖÜ][\w&.\-ÄÖÜäöüß ]{1,40}?)(?:\s*[-–|:(,!]|$)"),
]
_GENERIC_NAMES = re.compile(r"\b(recruiting|careers?|jobs?|hr|talent|people|team|karriere|bewerbung(en)?|no-?reply|noreply|human resources|personal)\b", re.I)
_ORG_WORDS = re.compile(r"\b(gmbh|ag|se|kg|ug|inc|ltd|llc|group|gruppe|recruiting|careers?|karriere|jobs|talent|hr)\b", re.I)


def _guess_company(subject: str, from_name: str, from_addr: str) -> str:
    for rx in _COMPANY_SUBJECT:
        m = rx.search(subject)
        if m:
            return m.group(1).strip(" .-")
    dom = company_from_domain(from_addr)
    if dom:
        return dom  # the employer's own domain beats a display name, which is often just a recruiter's name
    # Webmail or job-platform sender: the display name only counts if it looks like an organisation, not a person
    if from_name and _ORG_WORDS.search(from_name):
        name = re.sub(r"\s+", " ", re.sub(r"[|,\-–·]+", " ", _GENERIC_NAMES.sub("", from_name))).strip()
        if len(name) > 1:
            return name
    return ""


_ROLE_PATTERNS = [
    re.compile(r"\bbewerbung\s+als\s+([^\n.,:;()]{3,60})", re.I),
    re.compile(r"\bapplication\s+(?:for|as)\s+(?:the\s+|an?\s+)?(?:position\s+of\s+|role\s+of\s+)?([^\n.,:;()]{3,60})", re.I),
    re.compile(r"\bapplied\s+(?:for|as)\s+(?:the\s+|an?\s+)?([^\n.,:;()]{3,60})", re.I),
    re.compile(r"\bstelle\s+(?:als\s+)?[\"„“']?([^\n.,:;()\"“”']{3,60})", re.I),
    re.compile(r"\bbewerbung[^:\n]{0,40}:\s*([^\n]{3,60})$", re.I),
]
_ROLE_TAIL = re.compile(r"\s+(position|role|stelle|bei|at|in|wurde|ist|has|is)\b.*$", re.I)


def _guess_role(subject: str, body: str) -> str:
    for text in (subject, body[:800]):
        for rx in _ROLE_PATTERNS:
            m = rx.search(text)
            if m:
                role = _ROLE_TAIL.sub("", m.group(1)).strip(" -–\"'")
                if role:
                    return role
    return ""


class KeywordClassifier:
    name = "keywords"

    def classify(self, em: dict) -> Result:
        text = f"{em.get('subject', '')}\n{em.get('body', '')[:3000]}"
        subj_only = em.get("subject", "")
        etype = "other"
        for t, pats in _RULES:
            if any(p.search(text) for p in pats):
                etype = t
                break
        # newsletters and job alerts mention "Bewerbung" too
        if etype == "other" and re.search(r"newsletter|job ?alert|neue stellen|unsubscribe|abbestellen", text, re.I):
            etype = "not_job"
        company = _guess_company(subj_only, em.get("from_name", ""), em.get("from_addr", ""))
        role = _guess_role(subj_only, em.get("body", "")) if etype == "application_confirmation" else ""
        return Result(event_type=etype, company=company, role=role, classifier=self.name,
                      summary=f"(keyword match) {subj_only[:80]}")


# ------------------------------------------------------------------ ollama
SCHEMA = {
    "type": "object",
    "properties": {
        "event_type": {"type": "string", "enum": EVENT_TYPES},
        "company": {"type": "string"},
        "role": {"type": "string"},
        "interview_at": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["event_type", "company", "role", "interview_at", "summary"],
}

SYSTEM = """You sort emails from a job seeker's application inbox. Emails are German or English.
Return JSON only.

event_type, pick exactly one:
- application_confirmation: acknowledgement that the application arrived (automatic or human)
- assessment: asks the candidate to do a coding challenge, take-home task, test or questionnaire
- interview_invite: invites to or schedules any interview or call, including a first call with a recruiter
- offer: a job offer or contract
- rejection: the application is declined or will not be pursued
- follow_up: another message inside an ongoing application (questions, document requests, status update, rescheduling)
- other: job related but none of the above
- not_job: not about the candidate's own applications (newsletters, job alerts, marketing, security notices)

Rules:
- A polite "leider"/"unfortunately" is only a rejection if the application itself is declined. Rescheduling or "unfortunately the slot is taken" is follow_up.
- A survey or feedback request ("How was your recruiting experience?"), a career-portal welcome or account mail, and a job alert are never an assessment or an interview: use other (or not_job for alerts).
- company: the employer the candidate applied to. Not the recruiting platform (Personio, Greenhouse, Join, SmartRecruiters ...) and not a generic sender name. If a staffing agency writes on behalf of a client and names the client, use the client. Empty string if unknown.
- role: the job title as written, without (m/w/d). Empty string if unknown.
- interview_at: ISO 8601 date or datetime if the email proposes or confirms ONE specific time, else an empty string. Never invent a date.
- summary: one short English sentence."""


class OllamaClassifier:
    name = "ollama"

    def __init__(self, url: str = None, model: str = None, timeout: int = None):
        self.url = (url or config.OLLAMA_URL).rstrip("/")
        self.model = model or config.OLLAMA_MODEL
        self.timeout = timeout or config.LLM_TIMEOUT_S

    def check(self) -> str:
        """Raise a clear error if Ollama is not usable. Returns the model name that will be used."""
        try:
            r = requests.get(f"{self.url}/api/tags", timeout=5)
            r.raise_for_status()
        except Exception as e:
            raise LLMUnavailable(f"Cannot reach Ollama at {self.url}. Is it running? ({e})")
        models = [m["name"] for m in r.json().get("models", [])]
        if not models:
            raise RuntimeError("Ollama is running but has no models. Run: ollama pull qwen2.5:7b")
        if not self.model:
            self.model = models[0]
        elif self.model not in models and f"{self.model}:latest" not in models:
            raise RuntimeError(f"Model '{self.model}' is not installed. Installed: {', '.join(models)}")
        return self.model

    def classify(self, em: dict) -> Result:
        body = (em.get("body") or "")[: config.MAX_BODY_CHARS]
        user = (f"From: {em.get('from_name', '')} <{em.get('from_addr', '')}>\n"
                f"Date: {em.get('sent_date', '')}\nSubject: {em.get('subject', '')}\n\n{body}")
        payload = {
            "model": self.model, "stream": False, "format": SCHEMA,
            "options": {"temperature": 0},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        }
        try:
            r = requests.post(f"{self.url}/api/chat", json=payload, timeout=self.timeout)
            r.raise_for_status()
            data = json.loads(r.json()["message"]["content"])
            et = data.get("event_type", "other")
            if et not in EVENT_TYPES:
                et = "other"
            return Result(event_type=et, company=str(data.get("company", "")).strip(),
                          role=str(data.get("role", "")).strip(),
                          interview_at=str(data.get("interview_at", "")).strip(),
                          summary=str(data.get("summary", "")).strip(), classifier=f"ollama:{self.model}")
        except (requests.ConnectionError, requests.Timeout) as e:
            return Result(classifier=f"ollama:{self.model}", error=f"{type(e).__name__}: {e}", retryable=True)
        except requests.HTTPError as e:
            busy = e.response is not None and e.response.status_code >= 500
            return Result(classifier=f"ollama:{self.model}", error=f"{type(e).__name__}: {e}", retryable=busy)
        except Exception as e:  # unreadable or off-schema answer: not worth retrying
            return Result(classifier=f"ollama:{self.model}", error=f"{type(e).__name__}: {e}")


def get_classifier(name: str = None):
    name = name or config.CLASSIFIER
    if name == "keywords":
        return None  # pipeline uses the keyword classifier alone
    clf = OllamaClassifier()
    clf.check()
    return clf
