"""Safe exports of cached resumes belonging to current-run candidates."""
from __future__ import annotations
import io
import zipfile
from pathlib import Path
from . import config
from .models import ScoreResult
from .utils import resume_cache_path, safe_filename

def profile_path(result: ScoreResult) -> Path | None:
    candidates = [resume_cache_path(result.candidate.resume_url, result.candidate.name, suffix) for suffix in (".pdf", ".docx", ".doc")]
    path = next((item for item in candidates if item.is_file()), candidates[0])
    try:
        path.resolve().relative_to(config.RESUMES_DIR.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None

def profile_name(result: ScoreResult, index: int = 0) -> str:
    path = profile_path(result)
    suffix = path.suffix.lower() if path else ".pdf"
    return f"{safe_filename(result.candidate.display_name)}_{index}_Resume{suffix}"

def profiles_zip(results: list[ScoreResult]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, result in enumerate(results, 1):
            path = profile_path(result)
            if path:
                archive.writestr(f"shortlisted_profiles/{profile_name(result, index)}", path.read_bytes())
    return buffer.getvalue()
