"""GitHub link discovery and validation (Steps 9-10).

For every GitHub URL found in a resume we determine:
    * kind   : profile (github.com/user) vs repository (github.com/user/repo)
    * status : Working / Broken / Private / Not Found

Repositories are validated through the GitHub REST API when possible (it cleanly
distinguishes 404 "not found" from 403 "rate limited/private"); profiles and any
other URLs fall back to a plain HTTP request. An optional ``GITHUB_TOKEN`` raises
the API rate limit, which matters at 1000+ resumes.
"""

from __future__ import annotations

import re

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from . import config
from .models import GithubKind, GithubLink, GithubStatus
from .utils import get_logger

logger = get_logger("github_checker")

# github.com/<owner>[/<repo>][/...] — capture owner and optional repo.
_GH_PATH_RE = re.compile(
    r"github\.com/([A-Za-z0-9_.-]+)(?:/([A-Za-z0-9_.-]+))?",
    re.IGNORECASE,
)

# Reserved first-path segments that are not user profiles.
_RESERVED = {
    "orgs", "features", "topics", "collections", "sponsors", "about",
    "pricing", "marketplace", "explore", "notifications", "settings", "login",
}


def classify_kind(url: str) -> tuple[GithubKind, str, str]:
    """Return (kind, owner, repo) for a GitHub URL."""
    match = _GH_PATH_RE.search(url)
    if not match:
        return GithubKind.UNKNOWN, "", ""
    owner = match.group(1) or ""
    repo = (match.group(2) or "").removesuffix(".git")
    if owner.lower() in _RESERVED:
        return GithubKind.UNKNOWN, owner, repo
    if repo:
        return GithubKind.REPOSITORY, owner, repo
    return GithubKind.PROFILE, owner, ""


def _session() -> requests.Session:
    sess = requests.Session()
    headers = {"User-Agent": config.USER_AGENT}
    if config.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"
    sess.headers.update(headers)
    return sess


@retry(
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    stop=stop_after_attempt(config.GITHUB_RETRIES),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
def _request(sess: requests.Session, method: str, url: str) -> requests.Response:
    return sess.request(method, url, timeout=config.GITHUB_TIMEOUT, allow_redirects=True)


def _validate_repo_via_api(
    sess: requests.Session, owner: str, repo: str
) -> GithubStatus:
    """Validate a repository through the GitHub API."""
    api_url = f"https://api.github.com/repos/{owner}/{repo}"
    resp = _request(sess, "GET", api_url)
    code = resp.status_code
    if code == 200:
        # Private repos are not returned by the public API without access;
        # a 200 here means public + accessible.
        return GithubStatus.WORKING
    if code == 404:
        # Could be genuinely missing OR private (API hides private as 404 when
        # unauthenticated). Fall back to the web page to disambiguate.
        return _disambiguate_404(sess, f"https://github.com/{owner}/{repo}")
    if code in (403, 429):
        # Rate limited — fall back to a web check rather than guessing.
        logger.warning("GitHub API rate limited; falling back to web check")
        return _validate_via_web(sess, f"https://github.com/{owner}/{repo}")
    return GithubStatus.BROKEN


def _disambiguate_404(sess: requests.Session, web_url: str) -> GithubStatus:
    """A repo API 404 may be 'not found' or 'private' — check the web page."""
    try:
        resp = _request(sess, "GET", web_url)
    except requests.RequestException:
        return GithubStatus.NOT_FOUND
    if resp.status_code == 200:
        # Page loads but API 404'd -> private/redirect edge; treat as Private.
        return GithubStatus.PRIVATE
    if resp.status_code == 404:
        return GithubStatus.NOT_FOUND
    return GithubStatus.BROKEN


def _validate_via_web(sess: requests.Session, url: str) -> GithubStatus:
    """Validate any GitHub URL (profile or repo) via a plain HTTP request."""
    try:
        resp = _request(sess, "GET", url)
    except requests.Timeout:
        return GithubStatus.BROKEN
    except requests.ConnectionError:
        return GithubStatus.BROKEN
    except requests.RequestException:
        return GithubStatus.BROKEN

    if resp.status_code == 200:
        return GithubStatus.WORKING
    if resp.status_code == 404:
        return GithubStatus.NOT_FOUND
    if resp.status_code in (401, 403):
        return GithubStatus.PRIVATE
    return GithubStatus.BROKEN


def validate_url(url: str, sess: requests.Session | None = None) -> GithubLink:
    """Validate a single GitHub URL and return a :class:`GithubLink`."""
    own_session = sess is None
    sess = sess or _session()
    kind, owner, repo = classify_kind(url)

    try:
        if kind is GithubKind.REPOSITORY:
            status = _validate_repo_via_api(sess, owner, repo)
        else:
            status = _validate_via_web(sess, url)
    except Exception as exc:  # noqa: BLE001 - never let a link check crash a run
        logger.warning("GitHub validation error for %s: %s", url, exc)
        status = GithubStatus.BROKEN
    finally:
        if own_session:
            sess.close()

    logger.info("GitHub %s [%s] -> %s", url, kind.value, status.value)
    return GithubLink(url=url, kind=kind, status=status)


def validate_urls(urls: list[str]) -> list[GithubLink]:
    """Validate every GitHub URL for one candidate, reusing a session."""
    if not urls:
        return []
    sess = _session()
    try:
        return [validate_url(url, sess) for url in urls]
    finally:
        sess.close()


def summarize(links: list[GithubLink]) -> GithubStatus:
    """Collapse multiple link statuses into one headline status (Step 10).

    Precedence: any Working -> Working; else Broken/Private/NotFound as present.
    """
    if not links:
        return GithubStatus.NONE
    statuses = {link.status for link in links}
    for preferred in (
        GithubStatus.WORKING,
        GithubStatus.PRIVATE,
        GithubStatus.NOT_FOUND,
        GithubStatus.BROKEN,
    ):
        if preferred in statuses:
            return preferred
    return GithubStatus.BROKEN
