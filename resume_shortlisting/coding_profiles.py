"""Public coding-platform profile discovery (information only).

Finds a candidate's LeetCode / CodeChef / Codeforces profile and, optionally,
the statistics those platforms publish. Discovery is *link-based only*: a
profile is associated with a candidate when the candidate's own spreadsheet row
or resume contains the URL. Names are never used to guess a handle, because a
common name would silently attach a stranger's profile to a candidate.

Nothing in this module feeds the score, the recommendation, or any existing
matching logic — it is recruiter-facing evidence that sits alongside them. Every
network call is best-effort: a failure yields an "unavailable" status, never an
exception that could abort candidate processing.
"""

from __future__ import annotations

import re
import threading
import time

import requests

from . import config
from .models import CODING_PLATFORMS, CodingProfile
from .utils import get_logger

logger = get_logger("coding_profiles")

PLATFORM_LABELS: dict[str, str] = {
    "leetcode": "LeetCode",
    "codechef": "CodeChef",
    "codeforces": "Codeforces",
}

# Handle characters accepted by the three platforms.
_HANDLE = r"[A-Za-z0-9_.\-]+"

# leetcode.com/<handle> and leetcode.com/u/<handle> are both profile shapes.
_PROFILE_PATTERNS: dict[str, re.Pattern[str]] = {
    "leetcode": re.compile(
        rf"(?:https?://)?(?:www\.)?leetcode\.com/(?:u/)?({_HANDLE})", re.IGNORECASE
    ),
    "codechef": re.compile(
        rf"(?:https?://)?(?:www\.)?codechef\.com/users/({_HANDLE})", re.IGNORECASE
    ),
    "codeforces": re.compile(
        rf"(?:https?://)?(?:www\.)?codeforces\.com/profile/({_HANDLE})", re.IGNORECASE
    ),
}

# First path segments on leetcode.com that are site pages, not user profiles.
_LEETCODE_RESERVED = {
    "problems", "problemset", "contest", "contests", "discuss", "explore",
    "study-plan", "studyplan", "tag", "list", "submissions", "accounts",
    "interview", "jobs", "store", "subscribe", "profile", "articles",
    "playground", "company", "assessment", "circle", "notes",
}

_CANONICAL_URL = {
    "leetcode": "https://leetcode.com/u/{handle}/",
    "codechef": "https://www.codechef.com/users/{handle}",
    "codeforces": "https://codeforces.com/profile/{handle}",
}

# Human-readable statuses shown in the UI.
STATUS_NOT_FOUND = "No public profile link found"
STATUS_NOT_FETCHED = "Profile found; public stats not fetched"
STATUS_STATS_OK = "Profile found"
STATUS_STATS_PARTIAL = "Profile found; some public stats unavailable"
STATUS_STATS_UNAVAILABLE = "Profile found; public stats unavailable"


def _clean(handle: str) -> str:
    """Trim trailing punctuation a PDF text extraction leaves on a handle."""
    return handle.strip().strip(".,;:)]}’'\"").rstrip("/")


def find_profile_handles(text: str) -> dict[str, str]:
    """Return ``{platform: handle}`` for every profile URL present in ``text``."""
    found: dict[str, str] = {}
    if not text:
        return found
    for platform, pattern in _PROFILE_PATTERNS.items():
        for match in pattern.finditer(text):
            handle = _clean(match.group(1))
            if not handle:
                continue
            if platform == "leetcode" and handle.lower() in _LEETCODE_RESERVED:
                continue
            found[platform] = handle
            break
    return found


def _candidate_row_text(candidate) -> str:
    """Every value from the candidate's spreadsheet row, as one blob of text."""
    values: list[str] = []
    for holder in (getattr(candidate, "source_data", None), getattr(candidate, "extra", None)):
        for value in (holder or {}).values():
            if value is not None:
                values.append(str(value))
    return "\n".join(values)


# --------------------------------------------------------------------------- #
# Public statistics (best-effort)
# --------------------------------------------------------------------------- #
def _session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update({"User-Agent": config.USER_AGENT})
    return sess


_LEETCODE_QUERY = """
query candidateStats($username: String!) {
  matchedUser(username: $username) {
    submitStatsGlobal { acSubmissionNum { difficulty count } }
  }
  userContestRanking(username: $username) { rating }
}
"""


def _leetcode_stats(sess: requests.Session, handle: str) -> tuple[int | None, int | None]:
    """Solved-problem count and contest rating from the public GraphQL API."""
    response = sess.post(
        "https://leetcode.com/graphql",
        json={"query": _LEETCODE_QUERY, "variables": {"username": handle}},
        timeout=config.CODING_PROFILE_TIMEOUT,
    )
    payload = (response.json() or {}).get("data") or {}
    solved = rating = None
    user = payload.get("matchedUser")
    if user:
        counts = ((user.get("submitStatsGlobal") or {}).get("acSubmissionNum")) or []
        for entry in counts:
            if str(entry.get("difficulty", "")).lower() == "all":
                solved = int(entry["count"])
                break
    ranking = payload.get("userContestRanking")
    if ranking and ranking.get("rating") is not None:
        rating = int(round(float(ranking["rating"])))
    return solved, rating


_CODECHEF_RATING_RE = re.compile(r'class="rating-number"[^>]*>\s*(\d+)')
_CODECHEF_SOLVED_RE = re.compile(
    r"Total Problems Solved:\s*(?:<[^>]+>\s*)*(\d+)", re.IGNORECASE
)


def _codechef_stats(sess: requests.Session, handle: str) -> tuple[int | None, int | None]:
    """CodeChef publishes no API, so read the two numbers off the public page."""
    response = sess.get(
        f"https://www.codechef.com/users/{handle}",
        timeout=config.CODING_PROFILE_TIMEOUT,
    )
    if response.status_code != 200:
        return None, None
    html = response.text
    rating_match = _CODECHEF_RATING_RE.search(html)
    solved_match = _CODECHEF_SOLVED_RE.search(html)
    return (
        int(solved_match.group(1)) if solved_match else None,
        int(rating_match.group(1)) if rating_match else None,
    )


# Codeforces answers a burst of calls with a non-JSON error page: the API
# allows roughly one call every two seconds, shared across all callers.
_CODEFORCES_CALL_INTERVAL = 2.2
_CODEFORCES_LOCK = threading.Lock()
_codeforces_next_call = 0.0
# Submission history is large; a very prolific handle can exceed even the
# extended read timeout, in which case the solved count stays unavailable.
_CODEFORCES_HISTORY_LIMIT = 2000


def _codeforces_get(sess: requests.Session, path: str, params: dict, timeout: int) -> list:
    """Call one Codeforces API method, respecting the shared rate limit.

    The lock is held across the request so concurrent candidate workers queue
    rather than trip the limit; this path only runs when stats are opted into.
    """
    global _codeforces_next_call
    with _CODEFORCES_LOCK:
        wait = _codeforces_next_call - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            response = sess.get(
                f"https://codeforces.com/api/{path}", params=params, timeout=timeout
            )
        finally:
            _codeforces_next_call = time.monotonic() + _CODEFORCES_CALL_INTERVAL
    payload = response.json()
    if payload.get("status") != "OK":
        raise ValueError(payload.get("comment") or "Codeforces API error")
    return payload.get("result") or []


def _codeforces_stats(sess: requests.Session, handle: str) -> tuple[int | None, int | None]:
    """Rating from ``user.info``; solved count from distinct accepted problems.

    The two calls are independent: losing one still reports the other.
    """
    rating = solved = None
    try:
        result = _codeforces_get(
            sess, "user.info", {"handles": handle}, config.CODING_PROFILE_TIMEOUT
        )
        value = result[0].get("rating") if result else None
        rating = int(value) if value is not None else None
    except Exception as exc:  # noqa: BLE001 - rating is optional detail
        logger.debug("Codeforces rating unavailable for %s: %s", handle, exc)

    try:
        submissions = _codeforces_get(
            sess,
            "user.status",
            {"handle": handle, "from": 1, "count": _CODEFORCES_HISTORY_LIMIT},
            config.CODING_PROFILE_TIMEOUT * 3,
        )
        solved = len({
            (
                (submission.get("problem") or {}).get("contestId"),
                (submission.get("problem") or {}).get("index"),
            )
            for submission in submissions
            if submission.get("verdict") == "OK"
        })
    except Exception as exc:  # noqa: BLE001 - solved count is optional detail
        logger.debug("Codeforces submission history unavailable for %s: %s", handle, exc)
    return solved, rating


def _fetcher(platform: str):
    """Resolve the per-platform stats function (looked up at call time)."""
    return {
        "leetcode": _leetcode_stats,
        "codechef": _codechef_stats,
        "codeforces": _codeforces_stats,
    }.get(platform)


def fetch_stats(
    platform: str, handle: str, sess: requests.Session | None = None
) -> tuple[int | None, int | None, str]:
    """Return ``(problems_solved, rating, status)`` for one platform handle.

    Never raises: any network/parsing problem becomes an "unavailable" status.
    """
    fetcher = _fetcher(platform)
    if fetcher is None:
        return None, None, STATUS_STATS_UNAVAILABLE
    own_session = sess is None
    try:
        if sess is None:
            sess = _session()
        solved, rating = fetcher(sess, handle)
    except Exception as exc:  # noqa: BLE001 - stats are never worth a failure
        logger.info("%s stats unavailable for %s: %s", platform, handle, exc)
        return None, None, STATUS_STATS_UNAVAILABLE
    finally:
        if own_session and sess is not None:
            sess.close()

    if solved is None and rating is None:
        return None, None, STATUS_STATS_UNAVAILABLE
    status = STATUS_STATS_OK if (solved is not None and rating is not None) else STATUS_STATS_PARTIAL
    return solved, rating, status


# --------------------------------------------------------------------------- #
# Entry point used by the pipeline
# --------------------------------------------------------------------------- #
def discover_coding_profiles(
    candidate, resume_text: str = "", *, fetch_public_stats: bool = False
) -> dict[str, CodingProfile]:
    """Build the per-platform coding-profile map for one candidate.

    Discovery order: the candidate's spreadsheet row first (an explicitly
    supplied link is the strongest signal), then the extracted resume text.
    Platforms with no link are still returned, flagged ``profile_found=False``.
    """
    from_sheet = find_profile_handles(_candidate_row_text(candidate))
    from_resume = find_profile_handles(resume_text)

    profiles: dict[str, CodingProfile] = {}
    sess: requests.Session | None = None
    if fetch_public_stats:
        try:
            sess = _session()
        except Exception as exc:  # noqa: BLE001 - fall back to link-only evidence
            logger.info("Coding-profile stats session unavailable: %s", exc)
    try:
        for platform in CODING_PLATFORMS:
            handle = from_sheet.get(platform) or from_resume.get(platform)
            if not handle:
                profiles[platform] = CodingProfile(
                    platform=platform, profile_found=False, status=STATUS_NOT_FOUND
                )
                continue

            source = "spreadsheet" if platform in from_sheet else "resume"
            profile = CodingProfile(
                platform=platform,
                profile_url=_CANONICAL_URL[platform].format(handle=handle),
                profile_found=True,
                handle=handle,
                source=source,
                status=STATUS_NOT_FETCHED,
            )
            if fetch_public_stats and sess is not None:
                solved, rating, status = fetch_stats(platform, handle, sess)
                profile.problems_solved = solved
                profile.rating = rating
                profile.status = status
            profiles[platform] = profile
    finally:
        if sess is not None:
            sess.close()
    return profiles
