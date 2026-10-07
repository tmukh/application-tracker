"""Which sender domains say something about the employer, and which do not."""

# Webmail providers: the domain tells you nothing about the company.
GENERIC_DOMAINS = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "web.de", "gmx.de", "gmx.net",
                   "t-online.de", "yahoo.com", "icloud.com", "proton.me", "protonmail.com", "posteo.de", "mailbox.org"}

# Platforms that send mail on behalf of many employers.
ATS_DOMAINS = (
    "greenhouse.io", "greenhouse-mail.io", "lever.co", "personio.de", "personio.com", "workday.com",
    "myworkday.com", "smartrecruiters.com", "join.com", "softgarden.io", "softgarden.de", "successfactors.com",
    "successfactors.eu", "ashbyhq.com", "recruitee.com", "teamtailor.com", "workable.com", "bamboohr.com",
    "rexx-systems.com", "hirehive.com", "jobvite.com", "icims.com", "taleo.net", "onlyfy.jobs", "prescreenapp.io",
    "d-vinci.de", "cornerjob.com", "linkedin.com", "stepstone.de", "xing.com", "indeed.com", "welcometothejungle.com",
)


def employer_domain(addr: str) -> str:
    """The sender's domain if it identifies an employer, else ''."""
    d = addr.split("@")[-1].lower() if "@" in addr else ""
    if not d or d in GENERIC_DOMAINS or any(d == x or d.endswith("." + x) for x in ATS_DOMAINS):
        return ""
    return d


def company_from_domain(addr: str) -> str:
    d = employer_domain(addr)
    if not d:
        return ""
    parts = d.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "gov"):  # example.co.uk
        return parts[-3].capitalize()
    return (parts[-2] if len(parts) >= 2 else parts[0]).capitalize()
