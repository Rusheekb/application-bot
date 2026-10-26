"""Which application sites the auto-apply agent can work with (kept free of Playwright imports)."""

from __future__ import annotations

# Hosted application forms that don't require an account. Workday, iCIMS, Taleo and similar
# sites make you sign in first, so the agent hands those back to you.
AUTO_APPLY_ATS = {
    "greenhouse",
    "lever",
    "ashby",
    "workable",
    "smartrecruiters",
    "recruitee",
    "breezy",
    "jazzhr",
}
LOGIN_WALL_ATS = {"workday", "icims", "taleo", "successfactors", "linkedin", "indeed"}


def apply_support(ats: str | None) -> str:
    """'supported', 'login' (needs an account; the agent will likely stop) or 'unknown'."""
    if ats in AUTO_APPLY_ATS:
        return "supported"
    if ats in LOGIN_WALL_ATS:
        return "login"
    return "unknown"
