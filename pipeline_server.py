#!/usr/bin/env python3
"""
pipeline_server.py
------------------
Local HTTP server powering the Stackwise Pipeline Control Center.
Runs on localhost:8420 — no external access.

Wraps all pipeline operations as JSON API endpoints:
  POST /api/collect          — run source collection
  POST /api/step-prompt      — generate synthesis prompt for step 1-4
  POST /api/step-upload      — validate + merge a step payload into staging
  POST /api/build            — build page from completed 4-step staging
  POST /api/boost-prompt     — generate research booster prompt for weak sources
  POST /api/boost-upload     — apply booster output + reset staging to step 1
  POST /api/deploy           — git add + commit + push
  POST /api/lock             — lock sections in content_locks.json
  POST /api/unlock           — unlock sections
  POST /api/rebuild          — rebuild HTML from structured JSON
  GET  /api/tools            — list all tools with status
  GET  /api/tool/<slug>      — detailed tool status
  GET  /api/logs/<slug>      — get pipeline logs

Usage:
  python pipeline_server.py
  → Open http://localhost:8420
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

def _clean_url(url: str) -> str:
    """Strip tracking parameters (utm_*, gclid, gbraid, etc.) from URLs."""
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.query:
        return url
    # Keep only non-tracking params
    tracking_prefixes = ('utm_', 'gclid', 'gclsrc', 'gbraid', 'gad_', 'fbclid', 'mc_', 'ref', 'source')
    params = parse_qs(parsed.query, keep_blank_values=True)
    clean_params = {k: v for k, v in params.items() if not any(k.startswith(p) for p in tracking_prefixes)}
    from urllib.parse import urlencode
    clean_query = urlencode(clean_params, doseq=True) if clean_params else ""
    clean = parsed._replace(query=clean_query)
    result = clean.geturl()
    # Remove trailing ? if no params left
    return result.rstrip("?")


# ── Path Configuration ────────────────────────────────────────────────────────
# Resolve relative to this script's location

_THIS_DIR = Path(__file__).resolve().parent

# Expect this structure:
#   automation/split_collector/pipeline_server.py   ← this file
#   automation/split_collector/collector_parts/     ← package
#   automation/research/                            ← outputs
#   (sibling) Stackwise/                            ← deploy repo

COLLECTOR_PARTS = _THIS_DIR / "collector_parts"
RESEARCH_DIR = _THIS_DIR.parent / "research"
CONTENT_LOCKS_PATH = COLLECTOR_PARTS / "content_locks.json"
URL_LOCKS_PATH = COLLECTOR_PARTS / "url_locks.json"

# The collect_tool_sources.py entry point
COLLECT_SCRIPT = _THIS_DIR / "collect_tool_sources.py"

# ── YouTube ingest (correct location) ─────────────────────────────────────
YOUTUBE_INGEST_SCRIPT = _THIS_DIR / "YoutubeCollector" / "youtube_ingest.py"

# ── YouTube Collector paths (must match youtube_ingest.py exactly) ─────────────
YOUTUBE_ROOT = _THIS_DIR / "YoutubeCollector"
YOUTUBE_VIDEO_DIR = YOUTUBE_ROOT / "vidoes"          # note the exact typo "vidoes"
YOUTUBE_TRANSCRIPT_DIR = YOUTUBE_ROOT / "transcriptions"
YOUTUBE_MANIFEST_DIR = YOUTUBE_ROOT / "manifests"

# Stackwise repo for deploy
STACKWISE_REPO = Path(os.environ.get(
    "STACKWISE_REPO",
    str(_THIS_DIR.parent.parent.parent / "Stackwise")
))

PORT = 8420
ALL_SECTIONS = [
    "overview", "quick_verdict", "pricing", "user_signals",
    "best_fit", "alternatives", "workflow", "illustrative_output",
]

# ── Job tracking ──────────────────────────────────────────────────────────────

_jobs: dict[str, dict] = {}  # slug -> {status, log, started, ...}
_job_lock = threading.Lock()


def _log(slug: str, msg: str):
    with _job_lock:
        if slug not in _jobs:
            _jobs[slug] = {"status": "running", "log": [], "started": datetime.now().isoformat()}
        _jobs[slug]["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def _set_status(slug: str, status: str):
    with _job_lock:
        if slug in _jobs:
            _jobs[slug]["status"] = status


# ── File helpers ──────────────────────────────────────────────────────────────

def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _load_json(path: Path) -> dict | list:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _find_sources_json(slug: str) -> Path | None:
    p = RESEARCH_DIR / f"{slug}.split.sources.json"
    return p if p.exists() else None


def _find_structured_json(slug: str) -> Path | None:
    p = RESEARCH_DIR / f"{slug}.split.sources_profile_structured.json"
    return p if p.exists() else None


def _find_youtube_json(slug: str) -> Path | None:
    p = RESEARCH_DIR / f"{slug}_youtube_signals.json"
    return p if p.exists() else None


def _find_html(slug: str) -> Path | None:
    p = RESEARCH_DIR / f"{slug}.html"
    if p.exists():
        return p
    # Also check Stackwise repo
    p2 = STACKWISE_REPO / f"{slug}.html"
    return p2 if p2.exists() else None


def _file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.md5(path.read_bytes()).hexdigest()


def _file_mtime(path: Path) -> str:
    if not path.exists():
        return ""
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %I:%M %p")


# ── Tool discovery ────────────────────────────────────────────────────────────

def _discover_tools() -> list[dict]:
    """Scan research dir and locks to build tool list."""
    tools = {}
    locks = _load_json(CONTENT_LOCKS_PATH)
    url_locks = _load_json(URL_LOCKS_PATH)

    # From sources JSON files
    for p in RESEARCH_DIR.glob("*.split.sources.json"):
        slug = p.name.replace(".split.sources.json", "")
        if slug not in tools:
            data = _load_json(p)
            tools[slug] = {
                "slug": slug,
                "name": data.get("tool_name", slug.replace("-", " ").title()),
                "official_url": data.get("official_url_hint", ""),
            }

    # From active/recent jobs (tools being collected that don't have files yet)
    with _job_lock:
        for slug, job in _jobs.items():
            if slug not in tools:
                tools[slug] = {
                    "slug": slug,
                    "name": job.get("tool_name", slug.replace("-", " ").title()),
                    "official_url": job.get("official_url", ""),
                }

    # From content_locks
    for slug in locks:
        if slug not in tools:
            tools[slug] = {
                "slug": slug,
                "name": slug.replace("-", " ").title(),
                "official_url": "",
            }

    # From url_locks
    for slug, lock_data in url_locks.items():
        if isinstance(lock_data, dict):
            if slug not in tools:
                tools[slug] = {
                    "slug": slug,
                    "name": slug.replace("-", " ").title(),
                    "official_url": lock_data.get("try_url", ""),
                }
            elif not tools[slug].get("official_url"):
                tools[slug]["official_url"] = lock_data.get("try_url", "")

    # Enrich each tool with status
    result = []
    for slug, info in sorted(tools.items()):
        sources_path = _find_sources_json(slug)
        structured_path = _find_structured_json(slug)
        html_path = _find_html(slug)
        yt_path = _find_youtube_json(slug)

        locked = locks.get(slug, []) if isinstance(locks, dict) else []
        if isinstance(locked, dict):
            locked = locked.get("locked_sections", [])

        # Detect manual changes: compare structured JSON hash to HTML hash
        has_manual_changes = False
        if structured_path and html_path:
            struct_mtime = structured_path.stat().st_mtime if structured_path.exists() else 0
            html_mtime = html_path.stat().st_mtime if html_path.exists() else 0
            if html_mtime > struct_mtime + 5:  # HTML edited after structured JSON
                has_manual_changes = True

        url_lock = url_locks.get(slug, {}) if isinstance(url_locks, dict) else {}

        # Read staging file to surface step progress in the UI
        staging = _load_staging(slug)
        staging_current_step = staging.get("current_step", 0) if staging else 0
        staging_complete = _staging_is_complete(staging) if staging else False
        has_staging = any(staging.get(k) is not None for k in (
            "pricing", "user_signals", "workflow", "alternatives",
            "illustrative_output", "overview", "quick_verdict", "best_fit",
        )) if staging else False
        boost_path = RESEARCH_DIR / f"{slug}.split.boost.json"
        has_booster = boost_path.exists()

        # Cheap source quality summary (color + score) for the tool card badge.
        # The full breakdown is available via /api/source-quality/<slug>.
        sq_color = "missing"
        sq_score = 0
        if sources_path:
            try:
                sq = _score_source_quality(slug)
                sq_color = sq.get("overall", "missing")
                sq_score = sq.get("score", 0)
            except Exception:
                pass

        result.append({
            "slug": slug,
            "name": info["name"],
            "official_url": info.get("official_url", ""),
            "pricing_url": url_lock.get("pricing_url", "") if isinstance(url_lock, dict) else "",
            "has_sources": bool(sources_path),
            "has_structured": bool(structured_path),
            "has_html": bool(html_path),
            "has_youtube": bool(yt_path),
            "locked_sections": locked,
            "has_manual_changes": has_manual_changes,
            "sources_date": _file_mtime(sources_path) if sources_path else "",
            "structured_date": _file_mtime(structured_path) if structured_path else "",
            "html_date": _file_mtime(html_path) if html_path else "",
            "has_staging": has_staging,
            "staging_current_step": staging_current_step,
            "staging_complete": staging_complete,
            "has_booster": has_booster,
            "source_quality": sq_color,   # "green" | "yellow" | "red" | "missing"
            "source_quality_score": sq_score,
            "status": _get_tool_status(slug, sources_path, structured_path, html_path, has_manual_changes, staging_current_step, staging_complete),
            "job": _jobs.get(slug, {}).get("status", ""),
        })

    return result


def _get_tool_status(slug, sources_path, structured_path, html_path, has_manual_changes,
                     staging_current_step=0, staging_complete=False):
    job_status = _jobs.get(slug, {}).get("status", "")
    if job_status == "running":
        return "running"
    if job_status == "failed":
        return "failed"
    if has_manual_changes:
        return "manual_edits"
    if html_path:
        return "complete"
    if structured_path:
        return "needs_build"
    # Staging in progress (new 4-step flow)
    if sources_path and staging_current_step > 0 and not staging_complete:
        return f"step_{staging_current_step}_of_4"
    if sources_path and staging_complete:
        return "staging_complete"
    if sources_path:
        return "needs_synthesis"
    return "new"


# ── Source quality scoring ────────────────────────────────────────────────────
#
# After collection finishes, score the source base on six dimensions to give
# the operator a red/yellow/green visibility signal BEFORE they invest 4
# paste cycles into a doomed pipeline. The score is intentionally transparent
# (no opaque magic) so it can be argued with and refined.
#
# Six dimensions, each scored 0 (red) / 1 (yellow) / 2 (green):
#   1. Official source        — has the tool's own product page
#   2. Pricing source quality — verified pricing page vs forum thread
#   3. Editorial coverage     — count + tier of named editorial outlets
#   4. Domain diversity       — distinct review domains, not stuffed bucket
#   5. Community signal       — at least one forum/discussion source
#   6. YouTube balance        — YouTube supplementary, not dominant
#
# Final color is the WORST of the six (one red kills the whole grade) but the
# modal shows the breakdown so the operator knows what to fix.

# Tier 1: high-credibility editorial outlets with real review process
_TIER_1_DOMAINS = {
    "arstechnica.com", "wired.com", "nytimes.com", "theverge.com",
    "technologyreview.com", "ieee.org", "stratechery.com",
    "404media.co", "theinformation.com", "newyorker.com",
    "wsj.com", "ft.com", "bloomberg.com", "economist.com",
    "anthropic.com",  # Treat the vendor's own deep-content as tier 1 for vendor-fact verification only
}

# Tier 2: solid tech trade press
_TIER_2_DOMAINS = {
    "pcmag.com", "zdnet.com", "techradar.com", "techcrunch.com",
    "venturebeat.com", "theregister.com", "tomsguide.com", "engadget.com",
    "computerworld.com", "infoworld.com", "cnet.com", "fastcompany.com",
    "wired.co.uk", "cnbc.com", "axios.com", "businessinsider.com",
    "forbes.com", "tomshardware.com", "androidauthority.com",
    "digitaltrends.com", "lifehacker.com", "makeuseof.com",
}

# Verified pricing source domains (the tool's own pricing page is best)
_PRICING_TIER_1_PATTERNS = ("/pricing", "pricing.")  # any URL with /pricing or pricing. subdomain
_PRICING_BAD_DOMAINS = {
    "news.ycombinator.com", "reddit.com", "old.reddit.com",
}

# Community sources (legit forums/discussions)
_COMMUNITY_DOMAINS = {
    "news.ycombinator.com", "reddit.com", "old.reddit.com",
    "github.com", "stackoverflow.com", "lobste.rs",
    "lemmy.world", "lemmy.ml", "tildes.net",
}


def _domain_of(url: str) -> str:
    """Extract bare domain (no www, no path) from a URL."""
    if not url:
        return ""
    try:
        netloc = urlparse(url).netloc
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def _classify_editorial_domain(domain: str) -> str:
    """Return 'tier1', 'tier2', or 'tier3' for an editorial domain."""
    if not domain:
        return "tier3"
    if domain in _TIER_1_DOMAINS:
        return "tier1"
    if domain in _TIER_2_DOMAINS:
        return "tier2"
    return "tier3"


def _score_source_quality(slug: str) -> dict:
    """Score the source base for a tool. Returns a structured report.

    Returns:
        {
            "slug": str,
            "overall": "green" | "yellow" | "red" | "missing",
            "score": int (0-12),
            "max_score": 12,
            "dimensions": [{name, color, score, detail}, ...],
            "suggestions": [str, ...],   # search queries to fix gaps
            "tool_name": str,
        }
    """
    sources_path = _find_sources_json(slug)
    if not sources_path:
        return {
            "slug": slug,
            "overall": "missing",
            "score": 0,
            "max_score": 12,
            "dimensions": [],
            "suggestions": ["Run source collection first."],
            "tool_name": slug,
        }

    try:
        data = _load_json(sources_path)
    except Exception as e:
        return {
            "slug": slug,
            "overall": "missing",
            "score": 0,
            "max_score": 12,
            "dimensions": [],
            "suggestions": [f"Sources file unreadable: {e}"],
            "tool_name": slug,
        }

    tool_name = data.get("tool_name", slug)
    sources = data.get("selected_sources", [])

    # Bucket sources by their selected_bucket
    by_bucket: dict[str, list[dict]] = {}
    for s in sources:
        b = s.get("selected_bucket", "unknown")
        by_bucket.setdefault(b, []).append(s)

    # Helper for collecting domains in a bucket
    def domains_in(bucket: str) -> list[str]:
        return [_domain_of(s.get("url", "")) for s in by_bucket.get(bucket, [])]

    # Check if YouTube signals exist
    yt_path = _find_youtube_json(slug)
    has_youtube = bool(yt_path)
    youtube_count = 0
    if has_youtube:
        try:
            yt_data = _load_json(yt_path)
            youtube_count = yt_data.get("source_count", len(yt_data.get("sources", [])))
        except Exception:
            pass

    dimensions = []
    suggestions = []

    # ── Dimension 1: Official source ──
    official_count = len(by_bucket.get("official", []))
    if official_count >= 1:
        dim_score = 2
        dim_color = "green"
        dim_detail = f"{official_count} official source(s)"
    else:
        dim_score = 0
        dim_color = "red"
        dim_detail = "No official source page collected"
        suggestions.append(f'site:{tool_name.lower().replace(" ", "")}.com "{tool_name}" overview')
    dimensions.append({
        "name": "Official source",
        "color": dim_color,
        "score": dim_score,
        "detail": dim_detail,
    })

    # ── Dimension 2: Pricing source quality ──
    pricing_sources = by_bucket.get("pricing", [])
    pricing_score = 0
    pricing_detail = "No pricing source"
    if pricing_sources:
        # Check if any pricing source is a verified pricing page
        has_verified_pricing = False
        for s in pricing_sources:
            url = (s.get("url") or "").lower()
            if any(pat in url for pat in _PRICING_TIER_1_PATTERNS):
                has_verified_pricing = True
                break
        # Check if pricing source is just a forum thread (bad)
        all_forum_pricing = all(
            _domain_of(s.get("url", "")) in _PRICING_BAD_DOMAINS
            for s in pricing_sources
        )
        if has_verified_pricing:
            pricing_score = 2
            pricing_detail = f"Verified pricing page (one of {len(pricing_sources)} sources)"
        elif all_forum_pricing:
            pricing_score = 0
            pricing_detail = f"All {len(pricing_sources)} pricing source(s) are forum threads — not authoritative"
            suggestions.append(f'"{tool_name}" pricing site:{tool_name.lower().replace(" ", "")}.com')
        else:
            pricing_score = 1
            pricing_detail = f"{len(pricing_sources)} pricing source(s) but no official /pricing page"
            suggestions.append(f'"{tool_name}" official pricing page')
    else:
        suggestions.append(f'"{tool_name}" pricing 2025 plans tiers')
    pricing_color = {2: "green", 1: "yellow", 0: "red"}[pricing_score]
    dimensions.append({
        "name": "Pricing source",
        "color": pricing_color,
        "score": pricing_score,
        "detail": pricing_detail,
    })

    # ── Dimension 3: Editorial coverage (tier-aware) ──
    review_sources = by_bucket.get("reviews", [])
    review_domains = [_domain_of(s.get("url", "")) for s in review_sources]
    review_tiers = [_classify_editorial_domain(d) for d in review_domains]
    tier1_count = review_tiers.count("tier1")
    tier2_count = review_tiers.count("tier2")
    tier3_count = review_tiers.count("tier3")
    weighted = tier1_count * 1.0 + tier2_count * 0.7 + tier3_count * 0.3

    if weighted >= 2.5 and tier1_count + tier2_count >= 2:
        ed_score = 2
        ed_color = "green"
        ed_detail = f"{tier1_count} tier-1, {tier2_count} tier-2, {tier3_count} tier-3 reviews"
    elif weighted >= 1.5 or (tier1_count + tier2_count) >= 1:
        ed_score = 1
        ed_color = "yellow"
        ed_detail = f"{tier1_count} tier-1, {tier2_count} tier-2, {tier3_count} tier-3 reviews — thin"
        suggestions.append(f'"{tool_name}" review (site:arstechnica.com OR site:theverge.com OR site:wired.com OR site:techcrunch.com)')
    else:
        ed_score = 0
        ed_color = "red"
        ed_detail = f"Only {tier3_count} low-tier reviews — no credible editorial coverage"
        suggestions.append(f'"{tool_name}" review (site:arstechnica.com OR site:theverge.com OR site:wired.com)')
        suggestions.append(f'"{tool_name}" 2025 review (site:pcmag.com OR site:zdnet.com OR site:techradar.com)')
    dimensions.append({
        "name": "Editorial coverage",
        "color": ed_color,
        "score": ed_score,
        "detail": ed_detail,
    })

    # ── Dimension 4: Domain diversity ──
    distinct_review_domains = len(set(d for d in review_domains if d))
    if distinct_review_domains >= 3:
        div_score = 2
        div_color = "green"
        div_detail = f"{distinct_review_domains} distinct review domains"
    elif distinct_review_domains >= 2:
        div_score = 1
        div_color = "yellow"
        div_detail = f"Only {distinct_review_domains} distinct review domains"
    else:
        div_score = 0
        div_color = "red"
        div_detail = f"Only {distinct_review_domains} distinct review domain(s) — bucket is stuffed or empty"
        suggestions.append(f'"{tool_name}" review (different sources for diverse perspectives)')
    dimensions.append({
        "name": "Domain diversity",
        "color": div_color,
        "score": div_score,
        "detail": div_detail,
    })

    # ── Dimension 5: Community signal ──
    discussion_sources = by_bucket.get("discussions", [])
    distinct_community = len(set(_domain_of(s.get("url", "")) for s in discussion_sources if s.get("url")))
    if distinct_community >= 2:
        com_score = 2
        com_color = "green"
        com_detail = f"{len(discussion_sources)} community sources from {distinct_community} domains"
    elif distinct_community == 1 or len(discussion_sources) >= 1:
        com_score = 1
        com_color = "yellow"
        com_detail = f"{len(discussion_sources)} community source(s), {distinct_community} distinct domain(s)"
    else:
        com_score = 0
        com_color = "red"
        com_detail = "No community/forum discussion sources"
        suggestions.append(f'"{tool_name}" reddit OR "hacker news" experience')
        suggestions.append(f'site:reddit.com "{tool_name}" honest review')
    dimensions.append({
        "name": "Community signal",
        "color": com_color,
        "score": com_score,
        "detail": com_detail,
    })

    # ── Dimension 6: YouTube balance (not dominance) ──
    # Count "real text" sources (not youtube). If YouTube has more entries
    # than the editorial buckets combined, the source base is YouTube-dominant.
    text_source_count = (
        len(by_bucket.get("official", []))
        + len(by_bucket.get("reviews", []))
        + len(by_bucket.get("workflow", []))
        + len(by_bucket.get("alternatives", []))
        + len(by_bucket.get("discussions", []))
    )
    if not has_youtube:
        yt_score = 2
        yt_color = "green"
        yt_detail = "No YouTube sources (text-only base)"
    elif youtube_count <= text_source_count * 0.4:
        yt_score = 2
        yt_color = "green"
        yt_detail = f"{youtube_count} YouTube source(s), supplementary to {text_source_count} text sources"
    elif youtube_count <= text_source_count * 0.8:
        yt_score = 1
        yt_color = "yellow"
        yt_detail = f"{youtube_count} YouTube vs {text_source_count} text — significant YouTube weight"
    else:
        yt_score = 0
        yt_color = "red"
        yt_detail = f"{youtube_count} YouTube vs {text_source_count} text — YouTube-dominant evidence base"
        suggestions.append(f'"{tool_name}" written review (not video) 2025')
    dimensions.append({
        "name": "YouTube balance",
        "color": yt_color,
        "score": yt_score,
        "detail": yt_detail,
    })

    # ── Compute overall ──
    total_score = sum(d["score"] for d in dimensions)
    colors = [d["color"] for d in dimensions]
    if "red" in colors:
        overall = "red"
    elif "yellow" in colors:
        overall = "yellow"
    else:
        overall = "green"

    # Deduplicate suggestions while preserving order
    seen = set()
    unique_suggestions = []
    for sug in suggestions:
        if sug not in seen:
            seen.add(sug)
            unique_suggestions.append(sug)

    return {
        "slug": slug,
        "tool_name": tool_name,
        "overall": overall,
        "score": total_score,
        "max_score": 12,
        "dimensions": dimensions,
        "suggestions": unique_suggestions,
    }


# ── Pipeline operations ───────────────────────────────────────────────────────

def _run_youtube_ingest(slug: str) -> bool:
    """Dedicated synchronous YouTube ingest for existing tools.
    Waits for completion — no race condition with UI.
    Uses UTF-8 encoding to prevent charmap errors on Windows."""
    if not YOUTUBE_INGEST_SCRIPT.exists():
        _log(slug, f"ERROR: youtube_ingest.py not found at {YOUTUBE_INGEST_SCRIPT}")
        return False

    _log(slug, "▶ Starting YouTube-only ingest (existing tool mode)")
    cmd = [
        sys.executable,
        str(YOUTUBE_INGEST_SCRIPT),
        "--slug", slug,
        "--youtube-only"
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(_THIS_DIR),
            env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
        )
        for line in proc.stdout:
            _log(slug, f"[YT] {line.rstrip()}")
        proc.wait()

        if proc.returncode == 0:
            _log(slug, "✅ YouTube ingest complete")
            return True
        else:
            _log(slug, f"⚠ YouTube ingest exited with code {proc.returncode}")
            return False
    except Exception as e:
        _log(slug, f"❌ YouTube ingest failed: {e}")
        return False

def _cleanup_youtube_files(slug: str) -> None:
    """Clean ALL previous YouTube artifacts for this slug before re-ingest.
    Called ONLY on existing-tool Edit YouTube flow."""
    _log(slug, "🧹 Cleaning old YouTube files before re-ingest...")

    signals_path = RESEARCH_DIR / f"{slug}_youtube_signals.json"
    if signals_path.exists():
        signals_path.unlink()
        _log(slug, f"Deleted {signals_path.name}")

    for directory in (YOUTUBE_MANIFEST_DIR, YOUTUBE_VIDEO_DIR, YOUTUBE_TRANSCRIPT_DIR):
        if not directory.exists():
            continue
        for file in directory.glob(f"{slug}__*"):
            try:
                file.unlink()
                _log(slug, f"Deleted {file.name}")
            except Exception as e:
                _log(slug, f"⚠ Could not delete {file.name}: {e}")

    selected_jobs = YOUTUBE_MANIFEST_DIR / f"{slug}__selected_jobs.json"
    if selected_jobs.exists():
        selected_jobs.unlink()
        _log(slug, f"Deleted {selected_jobs.name}")

    _log(slug, "✅ YouTube cleanup complete — ready for fresh ingest")


def _run_collect(slug: str, tool_name: str, official_url: str, pricing_url: str, youtube_urls: list[str] | None = None):
    """New tool flow: sources → YouTube ingest → reset staging."""
    youtube_urls = [u.strip() for u in (youtube_urls or []) if u.strip()][:3]

    def _worker():
        if youtube_urls:
            override_path = RESEARCH_DIR / f"{slug}.youtube_override.json"
            _save_json(override_path, {"youtube_urls": youtube_urls})
            _log(slug, f"[YouTube] Saved {len(youtube_urls)} manual URLs")

        _log(slug, f"Starting collection for {tool_name}")
        _set_status(slug, "running")
        with _job_lock:
            _jobs[slug]["tool_name"] = tool_name
            _jobs[slug]["official_url"] = official_url

        if not COLLECT_SCRIPT.exists():
            _log(slug, f"ERROR: collect_tool_sources.py not found at {COLLECT_SCRIPT}")
            _log(slug, f"  Expected location: {COLLECT_SCRIPT}")
            _log(slug, f"  Server directory: {_THIS_DIR}")
            _log(slug, "  Make sure pipeline_server.py is in automation/split_collector/")
            _set_status(slug, "failed")
            return

        cmd = [
            sys.executable, str(COLLECT_SCRIPT),
            "--tool-name", tool_name,
            "--slug", slug,
            "--official-url", official_url,
            "--pricing-url", pricing_url,
            "--force",
            "--collect-only",
        ]
        _log(slug, f"Working dir: {_THIS_DIR}")
        _log(slug, f"Script: {COLLECT_SCRIPT}")
        _log(slug, f"Python: {sys.executable}")
        _log(slug, f"Command: {' '.join(cmd)}")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(_THIS_DIR),
                env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
            )
            for line in proc.stdout:
                _log(slug, line.rstrip())
            proc.wait()

            if proc.returncode == 0:
                _log(slug, "\n✅ Source collection complete")

                if youtube_urls:
                    _cleanup_youtube_files(slug)
                    if _run_youtube_ingest(slug):
                        _log(slug, "✅ YouTube signals integrated")
                    else:
                        _log(slug, "⚠ YouTube ingest failed — continuing anyway")
                elif (RESEARCH_DIR / f"{slug}.youtube_override.json").exists():
                    _cleanup_youtube_files(slug)
                    if (RESEARCH_DIR / f"{slug}.youtube_override.json").exists():
                        (RESEARCH_DIR / f"{slug}.youtube_override.json").unlink()
                    _log(slug, "YouTube signals cleared during re-collect")

                success, msg = _reset_staging(slug)
                if success:
                    _log(slug, msg)
                _set_status(slug, "complete")
            else:
                _log(slug, f"\n❌ Collection failed (exit code {proc.returncode})")
                _set_status(slug, "failed")
        except Exception as e:
            _log(slug, f"ERROR: {e}")
            _log(slug, traceback.format_exc())
            _set_status(slug, "failed")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def _run_youtube_edit(slug: str, youtube_urls: list[str]):
    youtube_urls = [u.strip() for u in (youtube_urls or []) if u.strip()][:3]

    def _worker():
        _set_status(slug, "running")
        _log(slug, f"[YouTube Edit] Starting refresh with {len(youtube_urls)} URL(s)")

        try:
            _cleanup_youtube_files(slug)
            override_path = RESEARCH_DIR / f"{slug}.youtube_override.json"

            with _job_lock:
                if slug not in _jobs:
                    _jobs[slug] = {"status": "running", "log": [], "started": datetime.now().isoformat()}
                _jobs[slug]["tool_name"] = _jobs.get(slug, {}).get("tool_name", slug.replace("-", " ").title())

            if youtube_urls:
                _save_json(override_path, {"youtube_urls": youtube_urls})
                _log(slug, f"[YouTube] Saved override with {len(youtube_urls)} URL(s)")

                success = _run_youtube_ingest(slug)
                if not success:
                    _set_status(slug, "failed")
                    return
            else:
                if override_path.exists():
                    override_path.unlink()
                    _log(slug, "Deleted youtube_override.json (YouTube signals cleared)")
                else:
                    _log(slug, "No override file to delete")

            ok, msg = _reset_staging(slug)
            _log(slug, msg)
            _set_status(slug, "complete" if ok else "failed")

        except Exception as e:
            _log(slug, f"ERROR: {e}")
            _log(slug, traceback.format_exc())
            _set_status(slug, "failed")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def _staging_path(slug: str) -> Path:
    """Path to the in-progress staging JSON for a tool."""
    return RESEARCH_DIR / f"{slug}.split.staging.json"


def _load_staging(slug: str) -> dict:
    """Load the staging file for a slug, or return a fresh empty staging dict."""
    path = _staging_path(slug)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    # Fresh staging: all step output slots empty, current_step=0 means nothing done yet
    return {
        "slug": slug,
        "current_step": 0,
        "steps_completed": [],
        # Step output slots — None until populated by the corresponding step upload
        "pricing": None,
        "user_signals": None,
        "workflow": None,
        "alternatives": None,
        "illustrative_output": None,
        "overview": None,
        "quick_verdict": None,
        "best_fit": None,
    }


def _save_staging(slug: str, staging: dict) -> None:
    """Write the staging file atomically."""
    path = _staging_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(staging, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _reset_staging(slug: str) -> tuple[bool, str]:
    """Wipe the staging file for a tool, forcing a fresh 4-step synthesis on
    the next prompt run. Sources are NOT touched. The structured JSON and HTML
    from the previous synthesis are also NOT touched (they'll be overwritten
    when build runs after step 4). Returns (success, message).
    """
    path = _staging_path(slug)
    try:
        if path.exists():
            path.unlink()
            return True, f"Staging wiped for {slug}. Ready to re-run from step 1."
        return True, f"No staging file existed for {slug}. Ready to start fresh."
    except Exception as e:
        return False, f"Failed to reset staging: {type(e).__name__}: {e}"


def _staging_is_complete(staging: dict) -> bool:
    """Return True if all 4 steps have been uploaded."""
    required_sections = {
        "pricing", "user_signals",
        "workflow", "alternatives", "illustrative_output",
        "overview", "quick_verdict", "best_fit",
    }
    return all(staging.get(k) is not None for k in required_sections)


def _next_step_for_slug(slug: str) -> int:
    """Return the next step number the operator should run (1-4), or 5 if all done."""
    staging = _load_staging(slug)
    return staging.get("current_step", 0) + 1


def _build_research_booster_prompt(slug: str) -> str:
    """Generate a targeted research prompt for the LLM to fix weak source dimensions."""
    report = _score_source_quality(slug)
    if report.get("overall") == "green":
        return "✅ Sources are already strong — no booster needed."

    weak_dims = [d for d in report.get("dimensions", []) if d.get("color") in ("red", "yellow")]
    suggestions = report.get("suggestions", [])
    tool_name = report.get("tool_name", slug.replace("-", " ").title())

    system = """You are an expert researcher for Stackwise tool profiles.
Your ONLY job is to fill the specific weak dimensions listed below with authoritative, up-to-date information.
Do NOT write the final profile yet. Focus exclusively on the gaps."""

    user = f"""Tool: {tool_name}
Slug: {slug}

WEAK DIMENSIONS THAT MUST BE FIXED:
"""
    for d in weak_dims:
        user += f"• {d['name']}: {d['detail']}\n"

    if suggestions:
        user += "\nSuggested searches you should use:\n"
        for i, q in enumerate(suggestions, 1):
            user += f"{i}. {q}\n"

    user += """
Research the official site and recent authoritative sources.
Extract concrete facts, pricing tiers, user quotes, etc.

Return ONLY this exact JSON (no extra text):

{
  "pricing": { ... full pricing section exactly like Step 1 would produce ... },
  "editorial_coverage": ["excerpt 1", "excerpt 2", ...],
  "community_signal": ["key user quote 1", "key user quote 2", ...],
  "sources_found": [{"title": "...", "url": "...", "snippet": "..."}, ...]
}
"""

    return f"=== SYSTEM PROMPT ===\n\n{system}\n\n=== USER PROMPT ===\n\n{user}"


def _build_step_prompt(slug: str, step: int) -> str:
    """Build the prompt text for a specific step in the 4-step synthesis flow.

    Returns the fully assembled system + user prompt ready to paste into an LLM.
    """
    if step not in (1, 2, 3, 4):
        return f"ERROR: step must be 1, 2, 3, or 4 (got {step})"

    sources_path = _find_sources_json(slug)
    if not sources_path:
        return f"ERROR: No sources JSON found for {slug}"

    # Ensure the collector_parts path is importable
    if str(COLLECTOR_PARTS) not in sys.path:
        sys.path.insert(0, str(COLLECTOR_PARTS))

    # Force a fresh reload of build_synthesis_prompt in case the module was
    # imported earlier (e.g., by a previous request running the old monolith
    # code). Without this, sys.modules can cache a stale version that lacks
    # STEP_REGISTRY and the new per-step builders.
    try:
        if 'build_synthesis_prompt' in sys.modules:
            import importlib
            bsp_module = importlib.reload(sys.modules['build_synthesis_prompt'])
        else:
            import build_synthesis_prompt as bsp_module
        STEP_REGISTRY = bsp_module.STEP_REGISTRY
        _fmt_source_rich = bsp_module._fmt_source_rich
        _fmt_youtube_rich = bsp_module._fmt_youtube_rich
    except Exception as e:
        # Include the full traceback in the error so the frontend can show it
        import traceback
        return (
            f"ERROR: could not import build_synthesis_prompt: {type(e).__name__}: {e}\n"
            f"COLLECTOR_PARTS={COLLECTOR_PARTS}\n"
            f"sys.path[0:3]={sys.path[0:3]}\n"
            f"{traceback.format_exc()}"
        )

    if step not in STEP_REGISTRY:
        return f"ERROR: step {step} not in STEP_REGISTRY"

    step_info = STEP_REGISTRY[step]

    # Load sources and format by bucket
    try:
        data = _load_json(sources_path)
        tool_name = data.get("tool_name", slug)
        official_url = data.get("official_url_hint", "")
        sources = data.get("selected_sources", [])

        sources_by_bucket: dict[str, list[str]] = {}
        for s in sources:
            bucket = s.get("selected_bucket", "unknown")
            sources_by_bucket.setdefault(bucket, []).append(_fmt_source_rich(s))
    except Exception as e:
        return f"ERROR: could not load sources: {type(e).__name__}: {e}"

    # Load YouTube signals for step 2 only
    youtube_text = ""
    if step == 2:
        yt_path = _find_youtube_json(slug)
        if yt_path:
            try:
                yt_data = _load_json(yt_path)
                youtube_text = _fmt_youtube_rich(yt_data)
            except Exception as e:
                print(f"  ⚠ could not load YouTube signals: {e}")

    # Load prior step outputs for steps 3 and 4
    prior_outputs: dict = {}
    if step_info["needs_prior"]:
        staging = _load_staging(slug)
        current_step = staging.get("current_step", 0)
        if current_step < step - 1:
            return (
                f"ERROR: step {step} requires steps 1..{step - 1} to be complete first. "
                f"Current progress: step {current_step} of 4 done."
            )
        # Pull sections from all prior completed steps
        for prior_step in range(1, step):
            for key in STEP_REGISTRY[prior_step]["expected_keys"]:
                if key in staging and staging[key] is not None:
                    prior_outputs[key] = staging[key]

    # Dispatch to the right builder
    builder = step_info["builder"]
    try:
        if step == 1:
            # Load booster if it exists (shows the raw boosted pricing in Step 1)
            boost_path = RESEARCH_DIR / f"{slug}.split.boost.json"
            booster_data = None
            if boost_path.exists():
                try:
                    booster_data = _load_json(boost_path)
                except Exception:
                    pass
            user_prompt = builder(tool_name, slug, sources_by_bucket, official_url, booster_data)
        elif step == 2:
            user_prompt = builder(tool_name, slug, sources_by_bucket, youtube_text, official_url)
        else:
            user_prompt = builder(tool_name, slug, sources_by_bucket, prior_outputs, official_url)
    except Exception as e:
        return f"ERROR: prompt builder failed: {type(e).__name__}: {e}\n{traceback.format_exc()}"

    system_prompt = step_info["system"]
    return f"=== SYSTEM PROMPT ===\n\n{system_prompt}\n\n=== USER PROMPT ===\n\n{user_prompt}"


def _upload_step(slug: str, step: int, payload: dict) -> tuple[bool, str]:
    """Validate and merge a step's JSON output into the staging file.

    Returns (success, message). On success, advances current_step in staging.
    On failure, staging is not modified.
    """
    if step not in (1, 2, 3, 4):
        return False, f"Invalid step: {step}"

    if str(COLLECTOR_PARTS) not in sys.path:
        sys.path.insert(0, str(COLLECTOR_PARTS))

    try:
        from build_synthesis_prompt import (
            STEP_REGISTRY,
            validate_step_output,
            extract_step_sections,
        )
    except Exception as e:
        return False, f"Could not import validation helpers: {e}"

    # Load current staging
    staging = _load_staging(slug)
    current_step = staging.get("current_step", 0)

    # Enforce linear order: step N requires current_step == N - 1
    if step != current_step + 1:
        return False, (
            f"Out-of-order upload: you're trying to upload step {step} but "
            f"current progress is step {current_step} of 4. Upload step {current_step + 1} first."
        )

    # Hard validation: payload must contain exactly the expected keys
    valid, err = validate_step_output(step, payload)
    if not valid:
        return False, err

    # Extract the relevant sections (handles wrapped and unwrapped shapes)
    sections = extract_step_sections(step, payload)

    # Merge into staging
    for key, value in sections.items():
        staging[key] = value

    staging["current_step"] = step
    completed = staging.get("steps_completed", [])
    step_name = STEP_REGISTRY[step]["name"]
    if step_name not in completed:
        completed.append(step_name)
    staging["steps_completed"] = completed

    _save_staging(slug, staging)

    keys_list = ", ".join(sorted(sections.keys()))
    next_step = step + 1 if step < 4 else None
    if next_step:
        return True, f"Step {step} ({step_name}) accepted: {keys_list}. Next: step {next_step}."
    else:
        return True, f"Step {step} ({step_name}) accepted: {keys_list}. All 4 steps complete — ready to build."


def _build_page_from_staging(slug: str) -> tuple[bool, str]:
    """Merge the completed staging file into the structured JSON format and build HTML.

    This replaces the old _build_page flow that took a full structured JSON from
    the upload modal. Now the upload is per-step, and build happens at the end
    once all 4 steps are uploaded.
    """
    staging = _load_staging(slug)
    if not _staging_is_complete(staging):
        missing = [k for k in ("pricing", "user_signals", "workflow", "alternatives",
                                "illustrative_output", "overview", "quick_verdict", "best_fit")
                    if not staging.get(k)]
        return False, f"Cannot build: staging is incomplete. Missing: {', '.join(missing)}"

    if str(COLLECTOR_PARTS) not in sys.path:
        sys.path.insert(0, str(COLLECTOR_PARTS))

    try:
        from build_synthesis_prompt import merge_staging_to_structured

        # Resolve tool_name from the sources file (the staging file may not have it)
        sources_path = _find_sources_json(slug)
        tool_name = slug
        if sources_path:
            sources_data = _load_json(sources_path)
            tool_name = sources_data.get("tool_name", slug)

        structured = merge_staging_to_structured(tool_name, slug, staging)

        # Save to the canonical location where page_writer expects it
        structured_path = RESEARCH_DIR / f"{slug}.split.sources_profile_structured.json"
        _save_json(structured_path, structured)

        from collector_parts.page_writer import write_page
        html_path = str(RESEARCH_DIR / f"{slug}.html")
        write_page(str(structured_path), html_path)
        return True, html_path
    except Exception as e:
        return False, f"Build failed: {e}\n{traceback.format_exc()}"


def _build_page(slug: str, structured_json: dict) -> tuple[bool, str]:
    """Legacy: save structured JSON and build HTML page.

    Kept for backwards compatibility with any tools that already have a
    completed structured JSON from the old monolith flow. New tools should
    use the 4-step flow via _build_page_from_staging.
    """
    try:
        structured_path = RESEARCH_DIR / f"{slug}.split.sources_profile_structured.json"
        _save_json(structured_path, structured_json)

        sys.path.insert(0, str(COLLECTOR_PARTS.parent))
        from collector_parts.page_writer import write_page

        html_path = str(RESEARCH_DIR / f"{slug}.html")
        write_page(str(structured_path), html_path)
        return True, html_path
    except Exception as e:
        return False, f"Build failed: {e}\n{traceback.format_exc()}"


def _deploy(slug: str, commit_msg: str = "") -> tuple[bool, str]:
    """Deploy HTML to GitHub and update index.html tool listing."""
    html_path = _find_html(slug)
    if not html_path:
        html_path = RESEARCH_DIR / f"{slug}.html"
    if not html_path.exists():
        return False, f"No HTML file found for {slug}"

    if not STACKWISE_REPO.exists():
        return False, f"Stackwise repo not found at {STACKWISE_REPO}"

    if not commit_msg:
        commit_msg = f"Update {slug} tool page"

    try:
        # 1. Copy tool HTML to repo
        dest = STACKWISE_REPO / html_path.name
        dest.write_bytes(html_path.read_bytes())

        # 2. Update index.html with the tool entry.
        # Snapshot index.html mtime/hash so we can tell if it actually changed
        # on disk — this is more reliable than parsing the return string.
        index_path = STACKWISE_REPO / "index.html"
        index_hash_before = _file_hash(index_path) if index_path.exists() else ""
        try:
            index_msg = _update_index(slug)
        except Exception as e:
            index_msg = f"index update FAILED with exception: {type(e).__name__}: {e}"
            _log(slug, f"  ⚠ {index_msg}")
            _log(slug, traceback.format_exc())
        if index_msg:
            print(f"  Index: {index_msg}")
            _log(slug, f"  Index: {index_msg}")

        index_hash_after = _file_hash(index_path) if index_path.exists() else ""
        index_changed = bool(index_hash_before != index_hash_after and index_hash_after)
        if index_changed:
            _log(slug, f"  Index: index.html modified on disk (will include in commit)")
        else:
            _log(slug, f"  Index: index.html unchanged on disk")

        # 3. Git add, commit, push — include index.html if it actually changed
        files_to_add = [html_path.name]
        if index_changed:
            files_to_add.append("index.html")
        for f in files_to_add:
            subprocess.run(["git", "-C", str(STACKWISE_REPO), "add", f], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(STACKWISE_REPO), "commit", "-m", commit_msg],
            check=True, capture_output=True,
        )
        subprocess.run(["git", "-C", str(STACKWISE_REPO), "push"], check=True, capture_output=True)
        deploy_msg = f"Deployed {html_path.name} to GitHub"
        if index_changed:
            deploy_msg += f" + index updated"
        elif index_msg:
            deploy_msg += f" (index: {index_msg})"
        return True, deploy_msg
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", errors="replace")
        if "nothing to commit" in stderr:
            return True, "No changes to deploy"
        return False, f"Git error: {stderr[:300]}"


# ── Index update helpers ──────────────────────────────────────────────────────

# Category inference: map keywords found in overview/verdict to index categories.
# The index uses these cats: Writing, Coding, Automation, Research, Meetings,
# Productivity, Video, Image, Marketing, SEO.
_CAT_KEYWORDS = {
    "Coding": ["code", "coding", "developer", "IDE", "programming", "terminal", "VS Code", "codebase", "refactor", "git"],
    "Writing": ["writing", "grammar", "content", "copywriting", "blog", "article", "paraphras"],
    "Research": ["research", "search", "citation", "knowledge", "compare", "analysis"],
    "Automation": ["automation", "automate", "workflow", "agent", "no-code", "pipeline", "script"],
    "Productivity": ["productivity", "workspace", "project management", "organize", "notes", "task"],
    "Meetings": ["meeting", "transcri", "recording", "conference", "call"],
    "Video": ["video", "avatar", "animation"],
    "Image": ["image", "photo", "headshot", "graphic", "visual", "design"],
    "Marketing": ["marketing", "email", "campaign", "ad creative", "advertising"],
    "SEO": ["SEO", "search engine optimization", "on-page"],
}

# Primary category labels (singular) — used for the `cat` field
_CAT_LABELS = {
    "Coding": "Code editor",
    "Writing": "Writing assistant",
    "Research": "AI search",
    "Automation": "AI automation",
    "Productivity": "Productivity tool",
    "Meetings": "Meeting transcription",
    "Video": "Video creation",
    "Image": "Image generation",
    "Marketing": "Marketing tool",
    "SEO": "SEO & content",
}


def _infer_categories(structured: dict) -> tuple[str, list[str]]:
    """Infer (primary_cat_label, [filter_cats]) from the structured profile JSON.

    Returns e.g. ("AI assistant", ["Coding", "Productivity", "Research"]).
    Falls back to ("AI tool", ["Productivity"]) if nothing matches.
    """
    sections = structured.get("sections", {})
    # Build a big text blob to scan
    text_parts = [
        sections.get("overview", ""),
        str(sections.get("quick_verdict", {}).get("verdict", "")),
    ]
    # Add workflow loop for richer signal
    wf = sections.get("workflow", {})
    if isinstance(wf, dict):
        text_parts.append(wf.get("loop", ""))
    text = " ".join(text_parts).lower()

    hits: dict[str, int] = {}
    for cat, keywords in _CAT_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw.lower() in text)
        if score > 0:
            hits[cat] = score

    if not hits:
        return "AI tool", ["Productivity"]

    # Sort by score, take top 3
    ranked = sorted(hits, key=lambda c: hits[c], reverse=True)
    filter_cats = ranked[:3]
    primary = ranked[0]

    # If the tool spans 3+ categories with similar scores, it's a general-purpose
    # AI assistant, not a specialized tool. Use "AI assistant" as the label.
    if len(ranked) >= 3 and hits[ranked[2]] >= hits[ranked[0]] * 0.4:
        cat_label = "AI assistant"
    else:
        cat_label = _CAT_LABELS.get(primary, "AI tool")

    return cat_label, filter_cats


def _build_tool_entry(slug: str, structured: dict) -> str:
    """Build a single JS object literal for the tools array in index.html."""
    tool_name = structured.get("tool_name", slug.replace("-", " ").title())
    sections = structured.get("sections", {})

    # Verdict — use the quick_verdict.verdict, truncated to keep the index clean
    verdict_full = ""
    qv = sections.get("quick_verdict", {})
    if isinstance(qv, dict):
        verdict_full = qv.get("verdict", "")
    # Take the first sentence as the index verdict
    if verdict_full:
        first_sentence = re.split(r'(?<=[.!?])\s+', verdict_full.strip())[0]
        # Truncate to ~60 chars for the card if needed
        if len(first_sentence) > 80:
            first_sentence = first_sentence[:77].rsplit(" ", 1)[0] + "…"
        verdict = first_sentence
    else:
        verdict = "See full profile"

    # Confidence
    conf_raw = ""
    if isinstance(qv, dict):
        conf_raw = str(qv.get("confidence", "")).strip().lower()
    conf = {"high": "h", "medium": "m", "low": "l"}.get(conf_raw, "m")

    # Categories
    cat_label, filter_cats = _infer_categories(structured)

    href = f"{slug}.html"

    # Escape quotes in strings for JS
    def _js_esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    cats_str = ",".join(f'"{_js_esc(c)}"' for c in filter_cats)
    return (
        f'  {{ name:"{_js_esc(tool_name)}", cat:"{_js_esc(cat_label)}", '
        f'cats:[{cats_str}], verdict:"{_js_esc(verdict)}", '
        f'conf:"{conf}", href:"{_js_esc(href)}" }}'
    )


def _update_index(slug: str) -> str:
    """Add or update the tool entry in index.html's tools array.

    Returns a human-readable status message, or empty string if index
    could not be updated.
    """
    index_path = STACKWISE_REPO / "index.html"
    if not index_path.exists():
        return "index.html not found in repo — skipped"

    # Load the structured JSON for this tool to get metadata
    structured_path = _find_structured_json(slug)
    if not structured_path:
        return f"no structured JSON for {slug} — index not updated"

    structured = _load_json(structured_path)
    if not structured:
        return f"could not read structured JSON for {slug}"

    new_entry = _build_tool_entry(slug, structured)
    href_pattern = f"{slug}.html"

    index_text = index_path.read_text(encoding="utf-8")

    # Find the tools array: "const tools = [" ... "];"
    # We'll look for the pattern and work with it
    array_start_match = re.search(r'const\s+tools\s*=\s*\[', index_text)
    if not array_start_match:
        return "could not find 'const tools = [' in index.html — skipped"

    array_start = array_start_match.end()

    # Find the matching closing "];" — account for nested brackets in objects
    depth = 1
    pos = array_start
    while pos < len(index_text) and depth > 0:
        if index_text[pos] == '[':
            depth += 1
        elif index_text[pos] == ']':
            depth -= 1
        pos += 1
    array_end = pos - 1  # position of the closing ]

    array_content = index_text[array_start:array_end]

    # Check if this tool already exists (by href)
    existing_pattern = re.compile(
        r'\{[^}]*href\s*:\s*"' + re.escape(href_pattern) + r'"[^}]*\}',
        re.DOTALL,
    )
    existing_match = existing_pattern.search(array_content)

    if existing_match:
        # Replace the existing entry
        old_entry = existing_match.group(0)
        # Preserve the indentation of the old entry
        new_array_content = array_content.replace(old_entry, new_entry.strip())
        new_index = index_text[:array_start] + new_array_content + index_text[array_end:]
        index_path.write_text(new_index, encoding="utf-8")
        return f"updated existing entry for {slug} in index.html"
    else:
        # Append a new entry at the end of the array
        # Find the last entry (last } before the closing ])
        last_brace = array_content.rfind("}")
        if last_brace == -1:
            # Empty array — just insert
            insert_content = f"\n{new_entry}\n"
        else:
            # Add after the last entry with a comma
            insert_content = array_content[:last_brace + 1] + ",\n" + new_entry
            insert_content += array_content[last_brace + 1:]
            new_index = index_text[:array_start] + insert_content + index_text[array_end:]
            index_path.write_text(new_index, encoding="utf-8")
            return f"added {slug} to index.html tools array"

    return ""


def _lock_sections(slug: str, sections: list[str]) -> tuple[bool, str]:
    locks = _load_json(CONTENT_LOCKS_PATH)
    if not isinstance(locks, dict):
        locks = {}
    current = set(locks.get(slug, []))
    for s in sections:
        if s in ALL_SECTIONS:
            current.add(s)
    locks[slug] = sorted(current)
    _save_json(CONTENT_LOCKS_PATH, locks)
    return True, f"Locked: {', '.join(sorted(current))}"


def _unlock_sections(slug: str, sections: list[str]) -> tuple[bool, str]:
    locks = _load_json(CONTENT_LOCKS_PATH)
    if not isinstance(locks, dict):
        locks = {}
    current = set(locks.get(slug, []))
    for s in sections:
        current.discard(s)
    locks[slug] = sorted(current)
    _save_json(CONTENT_LOCKS_PATH, locks)
    return True, f"Remaining locks: {', '.join(sorted(current)) or '(none)'}"


# ── HTTP Handler ──────────────────────────────────────────────────────────────

class PipelineHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, status=200):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length)

    def _read_json(self) -> dict:
        return json.loads(self._read_body())

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/" or path == "/dashboard":
            self._serve_dashboard()
        elif path == "/api/tools":
            self._send_json({"tools": _discover_tools()})
        elif path.startswith("/api/tool/"):
            slug = path.split("/")[-1]
            tools = _discover_tools()
            tool = next((t for t in tools if t["slug"] == slug), None)
            if tool:
                self._send_json(tool)
            else:
                self._send_json({"error": "Not found"}, 404)

        elif path.startswith("/api/source-quality/"):
            slug = path.split("/")[-1]
            if not slug:
                self._send_json({"error": "slug required"}, 400)
                return
            self._send_json(_score_source_quality(slug))

        elif path.startswith("/api/youtube-override/"):
            slug = path.split("/")[-1]
            override_path = RESEARCH_DIR / f"{slug}.youtube_override.json"
            data = _load_json(override_path)
            self._send_json({"youtube_urls": data.get("youtube_urls", [])})
            return

        # ── RESTORED: live logging and staging status ──
        elif path.startswith("/api/logs/"):
            slug = path.split("/")[-1]
            with _job_lock:
                job = _jobs.get(slug, {"status": "idle", "log": []})
            self._send_json({
                "status": job["status"],
                "log": job.get("log", [])
            })
            return

        elif path.startswith("/api/staging/"):
            slug = path.split("/")[-1]
            staging = _load_staging(slug)
            self._send_json(staging)
            return

        else:
                        self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/collect":
            body = self._read_json()
            tool_name = body.get("tool_name", "")
            slug = body.get("slug", "") or _slugify(tool_name)
            official_url = _clean_url(body.get("official_url", ""))
            pricing_url = _clean_url(body.get("pricing_url", ""))
            youtube_urls = body.get("youtube_urls", []) or []
            if not tool_name:
                self._send_json({"error": "tool_name required"}, 400)
                return
            _run_collect(slug, tool_name, official_url, pricing_url, youtube_urls)
            self._send_json({"ok": True, "slug": slug})
            return

        elif path == "/api/step-prompt":
            # Build the prompt for a specific step in the 4-step flow
            try:
                body = self._read_json()
                slug = body.get("slug", "")
                step = body.get("step", 0)
                # Accept step as int or stringified int
                try:
                    step = int(step)
                except (TypeError, ValueError):
                    self._send_json({"error": f"step must be an integer, got {step!r}"}, 400)
                    return
                if not slug:
                    self._send_json({"error": "slug is required"}, 400)
                    return
                if step not in (1, 2, 3, 4):
                    self._send_json({"error": f"step must be 1, 2, 3, or 4 (got {step})"}, 400)
                    return
                prompt = _build_step_prompt(slug, step)
                self._send_json({"ok": True, "prompt": prompt, "step": step})
            except Exception as e:
                err_msg = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
                print(f"  ⚠ /api/step-prompt crashed: {err_msg}")
                try:
                    self._send_json({"error": f"Server exception: {type(e).__name__}: {e}"}, 500)
                except Exception:
                    pass

        elif path == "/api/step-upload":
            # Validate and merge a step's JSON output into staging
            body = self._read_json()
            slug = body.get("slug", "")
            step = body.get("step", 0)
            payload = body.get("payload", {})
            if not slug or step not in (1, 2, 3, 4) or not isinstance(payload, dict):
                self._send_json({"error": "slug, step (1-4), and payload (object) required"}, 400)
                return
            ok, msg = _upload_step(slug, step, payload)
            if ok:
                staging = _load_staging(slug)
                self._send_json({
                    "ok": True,
                    "message": msg,
                    "current_step": staging.get("current_step", 0),
                    "next_step": (staging.get("current_step", 0) + 1) if staging.get("current_step", 0) < 4 else None,
                    "is_complete": _staging_is_complete(staging),
                })
            else:
                self._send_json({"ok": False, "message": msg}, 400)

        elif path == "/api/build":
            # Build HTML from the completed staging file (runs after all 4 steps)
            body = self._read_json()
            slug = body.get("slug", "")
            if not slug:
                self._send_json({"error": "slug required"}, 400)
                return
            ok, msg = _build_page_from_staging(slug)
            self._send_json({"ok": ok, "message": msg})

        elif path == "/api/resynthesize":
            # Wipe the staging file so the next prompt run starts fresh from
            # step 1. Sources, structured JSON, and HTML are NOT touched —
            # the structured JSON gets overwritten when build runs after step 4.
            # Used to apply prompt-version updates to existing tools without
            # re-collecting sources.
            body = self._read_json()
            slug = body.get("slug", "")
            if not slug:
                self._send_json({"error": "slug required"}, 400)
                return
            sources_path = _find_sources_json(slug)
            if not sources_path:
                self._send_json({
                    "ok": False,
                    "message": f"No sources file found for {slug}. Re-collect first before re-synthesizing."
                }, 400)
                return
            ok, msg = _reset_staging(slug)
            self._send_json({"ok": ok, "message": msg})

        elif path == "/api/deploy":
            body = self._read_json()
            slug = body.get("slug", "")
            commit_msg = body.get("commit_message", "")
            ok, msg = _deploy(slug, commit_msg)
            self._send_json({"ok": ok, "message": msg})

        elif path == "/api/lock":
            body = self._read_json()
            slug = body.get("slug", "")
            sections = body.get("sections", [])
            ok, msg = _lock_sections(slug, sections)
            self._send_json({"ok": ok, "message": msg})

        elif path == "/api/unlock":
            body = self._read_json()
            slug = body.get("slug", "")
            sections = body.get("sections", [])
            ok, msg = _unlock_sections(slug, sections)
            self._send_json({"ok": ok, "message": msg})

        elif path == "/api/rebuild":
            body = self._read_json()
            slug = body.get("slug", "")
            structured_path = _find_structured_json(slug)
            if not structured_path:
                self._send_json({"error": f"No structured JSON for {slug}"}, 404)
                return
            try:
                sys.path.insert(0, str(COLLECTOR_PARTS.parent))
                from collector_parts.page_writer import write_page
                html_path = str(RESEARCH_DIR / f"{slug}.html")
                write_page(str(structured_path), html_path)
                self._send_json({"ok": True, "message": f"Rebuilt {slug}.html"})
            except Exception as e:
                self._send_json({"ok": False, "message": str(e)}, 500)

        elif path == "/api/open-folder":
            body = self._read_json()
            slug = body.get("slug", "")
            sources_path = _find_sources_json(slug)
            yt_path = _find_youtube_json(slug)
            # Open the research folder in Windows Explorer
            folder = str(RESEARCH_DIR)
            try:
                if os.name == "nt":
                    os.startfile(folder)
                else:
                    subprocess.Popen(["xdg-open", folder])
                self._send_json({"ok": True, "message": f"Opened {folder}",
                    "files": {
                        "sources": str(sources_path) if sources_path else None,
                        "youtube": str(yt_path) if yt_path else None,
                        "folder": folder,
                    }
                })
            except Exception as e:
                self._send_json({"ok": False, "message": str(e)})

        elif path == "/api/files":
            body = self._read_json()
            slug = body.get("slug", "")
            sources_path = _find_sources_json(slug)
            structured_path = _find_structured_json(slug)
            yt_path = _find_youtube_json(slug)
            html_path = _find_html(slug)
            self._send_json({
                "sources": str(sources_path) if sources_path else None,
                "structured": str(structured_path) if structured_path else None,
                "youtube": str(yt_path) if yt_path else None,
                "html": str(html_path) if html_path else None,
                "folder": str(RESEARCH_DIR),
            })

        elif path == "/api/youtube-edit":
            body = self._read_json()
            slug = body.get("slug")
            raw_urls = body.get("youtube_urls", []) or []
            youtube_urls = [u.strip() for u in raw_urls if u.strip()][:3]

            if not slug:
                self._send_json({"ok": False, "message": "slug is required"}, status=400)
                return

            _run_youtube_edit(slug, youtube_urls)
            self._send_json({
                "ok": True,
                "message": f"YouTube refresh started for {slug}",
                "slug": slug
            })
            return

        elif path == "/api/boost-prompt":
            # Generate research booster prompt for weak sources
            body = self._read_json()
            slug = body.get("slug", "")
            if not slug:
                self._send_json({"error": "slug required"}, 400)
                return
            prompt = _build_research_booster_prompt(slug)
            self._send_json({"ok": True, "prompt": prompt, "slug": slug})

        elif path == "/api/boost-upload":
            # Accept LLM research booster output, save it, reset staging, and prepare Step 1
            body = self._read_json()
            slug = body.get("slug", "")
            payload = body.get("payload", {})
            if not slug or not isinstance(payload, dict):
                self._send_json({"error": "slug and payload object required"}, 400)
                return

            # Same source check used by /api/resynthesize
            sources_path = _find_sources_json(slug)
            if not sources_path:
                self._send_json({
                    "ok": False,
                    "message": f"No sources file found for {slug}. Re-collect first before applying a booster."
                }, 400)
                return

            # Save booster data
            boost_path = RESEARCH_DIR / f"{slug}.split.boost.json"
            _save_json(boost_path, payload)

            # Reset staging so the 4-step flow starts fresh
            ok_reset, msg_reset = _reset_staging(slug)
            if not ok_reset:
                self._send_json({
                    "ok": False,
                    "message": msg_reset,
                    "boosted_sections": list(payload.keys()),
                    "auto_reset": False
                }, 500)
                return

            # Optional: re-merge boosted pricing into the fresh staging file
            if payload.get("pricing"):
                staging = _load_staging(slug)
                staging["pricing"] = payload["pricing"]
                _save_staging(slug, staging)

            _log(slug, "Booster applied → staging reset → ready for Step 1")

            self._send_json({
                "ok": True,
                "message": "Booster applied. Staging reset. Ready for Step 1.",
                "boosted_sections": list(payload.keys()),
                "next_step": 1,
                "auto_start": True
            })

        else:
            self._send_json({"error": "Not found"}, 404)

    def _serve_dashboard(self):
        """Serve the inline dashboard HTML."""
        self._send_html(DASHBOARD_HTML)

    def log_message(self, format, *args):
        # Suppress default access logs (too noisy)
        pass


# ── Dashboard HTML ────────────────────────────────────────────────────────────


# ── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Stackwise Pipeline</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Outfit:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg: #08080a;
  --bg2: #0f0f12;
  --bg3: #161619;
  --bg-elevated: #1a1a1e;
  --border: rgba(255,255,255,0.055);
  --border2: rgba(255,255,255,0.09);
  --text: #f0f0f3;
  --text2: rgba(240,240,243,0.58);
  --text3: rgba(240,240,243,0.3);
  --accent: #818cf8;
  --accent2: #6366f1;
  --green: #34d399;
  --green-dim: rgba(52,211,153,0.15);
  --green-b: rgba(52,211,153,0.25);
  --red: #fb7185;
  --red-dim: rgba(251,113,133,0.1);
  --red-b: rgba(251,113,133,0.22);
  --amber: #fbbf24;
  --amber-dim: rgba(251,191,36,0.1);
  --amber-b: rgba(251,191,36,0.22);
  --blue: #60a5fa;
  --blue-dim: rgba(96,165,250,0.1);
  --blue-b: rgba(96,165,250,0.22);
  --r: 8px;
  --r-lg: 12px;
}

html { font-size: 15px; -webkit-font-smoothing: antialiased; }
body { background: var(--bg); color: var(--text); font-family: 'Outfit', sans-serif; min-height: 100vh; }
button { font-family: inherit; cursor: pointer; border: none; background: none; }
input, textarea { font-family: inherit; }

/* ── TOPBAR ── */
.topbar {
  display: flex; align-items: center; gap: 16px;
  padding: 16px 32px; border-bottom: 1px solid var(--border);
  background: var(--bg2); position: sticky; top: 0; z-index: 50;
}
.logo { font: 600 15px/1 'JetBrains Mono', monospace; color: var(--text); letter-spacing: -0.04em; }
.logo span { color: var(--accent); }

.add-wrap { display: flex; gap: 8px; flex: 1; max-width: 440px; }
.add-input {
  flex: 1; padding: 9px 14px; font-size: 13px;
  background: var(--bg3); color: var(--text); border: 1px solid var(--border2);
  border-radius: var(--r); outline: none; transition: border 0.15s;
}
.add-input:focus { border-color: var(--accent); }
.add-input::placeholder { color: var(--text3); }
.add-btn {
  padding: 9px 20px; font-size: 13px; font-weight: 600;
  background: var(--accent2); color: #fff; border-radius: var(--r);
  transition: background 0.15s; white-space: nowrap;
}
.add-btn:hover { background: var(--accent); }

.top-right { margin-left: auto; display: flex; gap: 8px; align-items: center; }
.refresh-btn {
  font: 500 12px 'JetBrains Mono', monospace; color: var(--text3);
  padding: 6px 14px; border: 1px solid var(--border); border-radius: var(--r);
  transition: all 0.15s;
}
.refresh-btn:hover { color: var(--text2); border-color: var(--border2); }

/* ── SUMMARY STRIP ── */
.summary {
  display: flex; gap: 28px; padding: 12px 32px;
  border-bottom: 1px solid var(--border); background: var(--bg);
}
.sum { font: 400 12px 'JetBrains Mono', monospace; color: var(--text3); }
.sum b { color: var(--text2); font-weight: 500; }

/* ── TABLE ── */
.tw { padding: 0 32px 60px; }
table { width: 100%; border-collapse: collapse; margin-top: 12px; }
th {
  font: 500 10px/1 'JetBrains Mono', monospace; color: var(--text3);
  text-transform: uppercase; letter-spacing: 0.08em;
  padding: 12px 14px; text-align: left;
  border-bottom: 1px solid var(--border);
  position: sticky; top: 53px; background: var(--bg); z-index: 10;
}
td { font-size: 13px; padding: 14px; border-bottom: 1px solid var(--border); vertical-align: top; }
tr:hover td { background: rgba(255,255,255,0.012); }

.t-name { font-weight: 500; color: var(--text); margin-bottom: 2px; }
.t-url { font: 400 11px 'JetBrains Mono', monospace; color: var(--text3); max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.t-date { font: 400 12px 'JetBrains Mono', monospace; color: var(--text3); }

/* ── STATUS PILL ── */
.pill {
  display: inline-flex; align-items: center; gap: 6px;
  font: 500 11px/1 'Outfit', sans-serif; padding: 5px 12px;
  border-radius: 20px; white-space: nowrap;
}
.pill-green { background: var(--green-dim); color: var(--green); border: 1px solid var(--green-b); }
.pill-red { background: var(--red-dim); color: var(--red); border: 1px solid var(--red-b); }
.pill-amber { background: var(--amber-dim); color: var(--amber); border: 1px solid var(--amber-b); }
.pill-blue { background: var(--blue-dim); color: var(--blue); border: 1px solid var(--blue-b); }
.pill-neutral { background: var(--bg3); color: var(--text3); border: 1px solid var(--border2); }
.pill::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }

/* ── NEXT STEP ── */
.next-step { font-size: 12px; color: var(--text2); line-height: 1.5; max-width: 220px; }
.next-step em { font-style: normal; color: var(--accent); font-weight: 500; }

/* ── LOCK CHIPS ── */
.locks { display: flex; flex-wrap: wrap; gap: 4px; }
.lock-chip {
  font: 400 10px 'JetBrains Mono', monospace; padding: 2px 8px;
  background: var(--green-dim); color: var(--green); border-radius: 4px;
}

/* ── ACTION BTNS ── */
.acts { display: flex; gap: 6px; flex-wrap: wrap; }
.ab {
  font: 500 11px/1 'Outfit', sans-serif; padding: 6px 14px;
  border-radius: 6px; transition: all 0.12s; white-space: nowrap;
}
.ab-primary { background: var(--accent2); color: #fff; }
.ab-primary:hover { background: var(--accent); }
.ab-deploy { background: var(--green-dim); color: var(--green); border: 1px solid var(--green-b); }
.ab-deploy:hover { background: rgba(52,211,153,0.22); }
.ab-warn { background: var(--red-dim); color: var(--red); border: 1px solid var(--red-b); }
.ab-warn:hover { background: rgba(251,113,133,0.18); }
.ab-ghost { color: var(--text3); border: 1px solid var(--border2); }
.ab-ghost:hover { color: var(--text2); border-color: var(--text3); }

/* ── MODALS ── */
.overlay {
  display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.65);
  z-index: 100; align-items: center; justify-content: center;
  backdrop-filter: blur(6px);
}
.overlay.open { display: flex; }
.overlay.open > .modal { position: relative; }
.modal {
  background: var(--bg2); border: 1px solid var(--border2);
  border-radius: var(--r-lg); padding: 28px 30px; width: 520px; max-width: 94vw;
  max-height: 88vh; overflow-y: auto; animation: modalIn 0.18s ease-out;
}
@keyframes modalIn { from { opacity: 0; transform: translateY(8px) scale(0.98); } }
.modal-lg { width: 680px; }

.m-title { font-size: 17px; font-weight: 600; margin-bottom: 20px; letter-spacing: -0.02em; }
.m-label {
  font: 500 10px 'JetBrains Mono', monospace; color: var(--text3);
  text-transform: uppercase; letter-spacing: 0.07em; margin-bottom: 6px;
}
.m-input {
  width: 100%; padding: 10px 14px; font-size: 13px;
  background: var(--bg3); color: var(--text); border: 1px solid var(--border);
  border-radius: var(--r); margin-bottom: 16px; outline: none;
}
.m-input:focus { border-color: var(--accent); }
.m-textarea {
  width: 100%; padding: 12px 14px; font: 400 12px 'JetBrains Mono', monospace;
  background: var(--bg3); color: var(--text); border: 1px solid var(--border);
  border-radius: var(--r); margin-bottom: 16px; outline: none;
  resize: vertical; min-height: 220px; line-height: 1.6;
}
.m-textarea:focus { border-color: var(--accent); }
.m-foot { display: flex; gap: 10px; justify-content: flex-end; margin-top: 6px; }
.m-cancel { font-size: 13px; padding: 8px 18px; background: var(--bg3); color: var(--text2); border-radius: var(--r); }
.m-submit { font-size: 13px; font-weight: 600; padding: 8px 22px; background: var(--accent2); color: #fff; border-radius: var(--r); }
.m-submit:hover { background: var(--accent); }

.m-hint { font-size: 12px; color: var(--text3); margin-bottom: 16px; line-height: 1.5; }

/* ── PROMPT VIEWER ── */
.prompt-box {
  background: #0a0a0e; border: 1px solid var(--border); border-radius: var(--r);
  padding: 16px; font: 400 11px/1.65 'JetBrains Mono', monospace;
  color: var(--text2); white-space: pre-wrap; word-break: break-word;
  max-height: 460px; overflow-y: auto;
}

/* ── STEP MODAL PROGRESS DOTS ── */
.step-dots {
  display: grid; grid-template-columns: repeat(4, 1fr);
  gap: 8px; margin-bottom: 4px;
  padding: 12px; border-radius: var(--r);
  background: var(--bg3); border: 0.5px solid var(--border);
}
.step-dot {
  display: flex; flex-direction: column; align-items: center; gap: 6px;
  padding: 8px 4px; border-radius: 6px;
  text-align: center; transition: background .15s;
}
.step-dot-num {
  width: 28px; height: 28px; border-radius: 50%;
  background: var(--bg); border: 1px solid var(--border);
  color: var(--text3); font: 500 12px 'JetBrains Mono', monospace;
  display: flex; align-items: center; justify-content: center;
  transition: all .15s;
}
.step-dot-label {
  font: 500 10px 'JetBrains Mono', monospace;
  color: var(--text3); letter-spacing: 0.04em;
  text-transform: uppercase;
}
.step-dot.done .step-dot-num {
  background: var(--green-dim); border-color: var(--green-b); color: var(--green);
}
.step-dot.done .step-dot-label { color: var(--text2); }
.step-dot.current .step-dot-num {
  background: var(--accent); border-color: var(--accent); color: #fff;
  box-shadow: 0 0 0 3px rgba(138,125,247,0.18);
}
.step-dot.current .step-dot-label { color: var(--text); }

/* Step status badges on tool cards */
.step-badge {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 3px 9px; border-radius: 999px;
  background: var(--accent-light); border: 0.5px solid var(--accent-border);
  color: var(--accent); font: 500 10px 'JetBrains Mono', monospace;
  letter-spacing: 0.03em; text-transform: uppercase;
}
.step-badge-dot {
  width: 5px; height: 5px; border-radius: 50%; background: var(--accent);
  animation: pulse 2s infinite;
}
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

/* ── SOURCE QUALITY BADGE ── */
.sq-badge {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 3px 9px; border-radius: 999px;
  font: 500 10px 'JetBrains Mono', monospace;
  letter-spacing: 0.03em; text-transform: uppercase;
  cursor: pointer; border: 0.5px solid;
  transition: filter .12s;
}
.sq-badge:hover { filter: brightness(1.15); }
.sq-badge .sq-dot { width: 6px; height: 6px; border-radius: 50%; }
.sq-badge.sq-green { background: rgba(74,222,128,0.12); border-color: rgba(74,222,128,0.4); color: #4ade80; }
.sq-badge.sq-green .sq-dot { background: #4ade80; }
.sq-badge.sq-yellow { background: rgba(251,191,36,0.12); border-color: rgba(251,191,36,0.4); color: #fbbf24; }
.sq-badge.sq-yellow .sq-dot { background: #fbbf24; }
.sq-badge.sq-red { background: rgba(248,113,113,0.12); border-color: rgba(248,113,113,0.4); color: #f87171; }
.sq-badge.sq-red .sq-dot { background: #f87171; }
.sq-badge.sq-missing { background: var(--bg3); border-color: var(--border); color: var(--text3); }
.sq-badge.sq-missing .sq-dot { background: var(--text3); }

/* ── BOOSTED BADGE ── */
.boosted-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 9px;
  border-radius: 999px;
  font: 500 10px 'JetBrains Mono', monospace;
  letter-spacing: 0.03em;
  text-transform: uppercase;
  background: rgba(251,191,36,0.12);
  border: 0.5px solid rgba(251,191,36,0.4);
  color: #fbbf24;
}

/* Source quality modal — dimension breakdown */
.sq-grid {
  display: flex; flex-direction: column; gap: 8px;
  margin: 14px 0;
}
.sq-row {
  display: grid; grid-template-columns: 14px 150px 50px 1fr;
  gap: 12px; align-items: center;
  padding: 12px 14px; border-radius: var(--r);
  background: var(--bg3); border: 0.5px solid var(--border);
}
.sq-row .sq-row-dot { width: 8px; height: 8px; border-radius: 50%; }
.sq-row.sq-green .sq-row-dot { background: #4ade80; }
.sq-row.sq-yellow .sq-row-dot { background: #fbbf24; }
.sq-row.sq-red .sq-row-dot { background: #f87171; }
.sq-row .sq-name { font: 500 12px 'JetBrains Mono', monospace; color: var(--text); }
.sq-row .sq-score { font: 500 11px 'JetBrains Mono', monospace; color: var(--text3); }
.sq-row .sq-detail { font-size: 12px; color: var(--text2); line-height: 1.5; }

.sq-suggestions {
  margin-top: 18px; padding: 14px;
  background: var(--bg3); border: 0.5px solid var(--border);
  border-radius: var(--r);
}
.sq-suggestions-title {
  font: 500 11px 'JetBrains Mono', monospace; color: var(--text3);
  text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 10px;
}
.sq-suggestion {
  display: flex; gap: 8px; align-items: flex-start;
  padding: 8px 10px; margin-bottom: 6px; border-radius: 6px;
  background: var(--bg2); border: 0.5px solid var(--border);
  font: 400 11px/1.5 'JetBrains Mono', monospace; color: var(--text2);
  cursor: pointer; transition: background .12s;
}
.sq-suggestion:hover { background: var(--bg); }
.sq-suggestion .sq-num { color: var(--text3); flex-shrink: 0; }
.sq-suggestion .sq-query { flex: 1; word-break: break-all; }
.sq-suggestion .sq-copy { color: var(--accent); flex-shrink: 0; font-size: 10px; }

/* ── SECTION CHECKS ── */
.sec-row { display: flex; align-items: center; gap: 10px; padding: 7px 0; }
.sec-row input[type=checkbox] { accent-color: var(--accent); width: 16px; height: 16px; }
.sec-row label { font: 400 13px 'JetBrains Mono', monospace; color: var(--text2); cursor: pointer; }

/* ── LOG ── */
.log-box {
  background: var(--bg3); border: 1px solid var(--border); border-radius: var(--r);
  padding: 14px; font: 400 11px/1.7 'JetBrains Mono', monospace;
  color: var(--text2); max-height: 320px; overflow-y: auto;
}

/* ── TOAST ── */
.toast {
  position: fixed; bottom: 24px; right: 24px; padding: 12px 22px;
  background: var(--bg-elevated); border: 1px solid var(--border2);
  border-radius: var(--r); font: 500 13px 'Outfit', sans-serif;
  color: var(--text); z-index: 200; opacity: 0; transform: translateY(8px);
  transition: all 0.22s; pointer-events: none;
}
.toast.show { opacity: 1; transform: translateY(0); }
.toast.ok { border-color: var(--green-b); }
.toast.err { border-color: var(--red-b); color: var(--red); }

/* ── RUNNING INDICATOR ── */
.running-bar {
  display: none; padding: 12px 32px;
  background: var(--blue-dim); border-bottom: 1px solid var(--blue-b);
  font-size: 13px; color: var(--blue); align-items: center; gap: 12px;
}
.running-bar.show { display: flex; }
.spinner {
  width: 14px; height: 14px; border: 2px solid var(--blue-b);
  border-top-color: var(--blue); border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.running-bar .view-log {
  margin-left: auto; font: 500 11px 'JetBrains Mono', monospace;
  color: var(--blue); text-decoration: underline; cursor: pointer;
}

/* ── EMPTY STATE ── */
.empty-state {
  text-align: center; padding: 80px 20px; color: var(--text3);
}
.empty-state h3 { font-size: 18px; font-weight: 500; color: var(--text2); margin-bottom: 8px; }
.empty-state p { font-size: 13px; max-width: 360px; margin: 0 auto; line-height: 1.6; }
</style>
</head>
<body>

<!-- TOPBAR -->
<div class="topbar">
  <div class="logo">Stackwise <span>Pipeline</span></div>
  <div class="add-wrap">
    <input class="add-input" id="toolInput" placeholder="Type a tool name to add..." />
    <button class="add-btn" onclick="openNewTool()">+ Add Tool</button>
  </div>
  <div class="top-right">
    <button class="refresh-btn" onclick="refresh()">↻ Refresh</button>
  </div>
</div>

<!-- SUMMARY -->
<div class="summary" id="summary"></div>

<!-- RUNNING BAR -->
<div class="running-bar" id="runningBar">
  <div class="spinner"></div>
  <span id="runningText">Collecting sources...</span>
  <span class="view-log" id="runningLogBtn" onclick="">View log</span>
</div>

<!-- TABLE -->
<div class="tw">
  <table>
    <thead><tr>
      <th>Tool</th>
      <th>Last Updated</th>
      <th>Status</th>
      <th>Next Step</th>
      <th>Protected Sections</th>
      <th>Actions</th>
    </tr></thead>
    <tbody id="tbody"></tbody>
  </table>
  <div id="emptyState" class="empty-state" style="display:none;">
    <h3>No tools yet</h3>
    <p>Type a tool name above and click "+ Add Tool" to start your first collection.</p>
  </div>
</div>

<!-- MODALS -->

<!-- New Tool -->
<div class="overlay" id="mNew">
  <div class="modal">
    <div class="m-title">Add a new tool</div>
    <div class="m-hint">We'll collect sources from the web, YouTube reviews, and the official site.</div>
    <div class="m-label">Tool Name</div>
    <input class="m-input" id="fName" placeholder="e.g. Grammarly" />
    <div class="m-label">Official Website</div>
    <input class="m-input" id="fUrl" placeholder="https://www.grammarly.com/" />
    <div class="m-label">Pricing Page (optional)</div>
    <input class="m-input" id="fPricing" placeholder="https://www.grammarly.com/plans" />

    <div class="m-label">YouTube URLs <span style="font-size:10px;color:var(--text3)">(one per line, max 3 — optional)</span></div>
    <textarea class="m-textarea" id="fYoutube" rows="3" placeholder="https://www.youtube.com/watch?v=..."></textarea>
    <div class="m-label">Pipeline Mode</div>
    <div style="display:flex; gap:8px; margin-bottom:20px;">
      <button class="ab" id="modeManual" onclick="setMode('manual')" style="flex:1; padding:12px; border-radius:var(--r); text-align:left; border:1px solid var(--accent); background:var(--blue-dim);">
        <div style="font-weight:600; font-size:13px; color:var(--text); margin-bottom:4px;">Manual (recommended)</div>
        <div style="font-size:11px; color:var(--text2); line-height:1.4;">Collect sources, then you send a prompt to any LLM and upload the response.</div>
      </button>
      <button class="ab" id="modeApi" onclick="setMode('api')" style="flex:1; padding:12px; border-radius:var(--r); text-align:left; border:1px solid var(--border2); background:transparent;">
        <div style="font-weight:600; font-size:13px; color:var(--text); margin-bottom:4px;">Full Auto (API key needed)</div>
        <div style="font-size:11px; color:var(--text2); line-height:1.4;">Runs the entire pipeline automatically using the Claude API.</div>
      </button>
    </div>
    <div class="m-foot">
      <button class="m-cancel" onclick="hideModal('mNew')">Cancel</button>
      <button class="m-submit" onclick="startCollect()">Start Collecting</button>
    </div>
  </div>
</div>

<!-- Step modal — unified prompt + upload for the 4-step synthesis flow -->
<div class="overlay" id="mStep">
  <div class="modal modal-lg">
    <div class="m-title">
      <span id="stepTitle">Step ? of 4</span> — <span id="stepSlug"></span>
    </div>
    <div class="m-hint" id="stepHint">Copy the prompt below into your LLM, then paste the JSON response back here. The upload is strict — the JSON must contain exactly the sections this step produces.</div>

    <!-- Step progress dots -->
    <div class="step-dots" id="stepDots">
      <div class="step-dot" data-step="1"><div class="step-dot-num">1</div><div class="step-dot-label">Pricing</div></div>
      <div class="step-dot" data-step="2"><div class="step-dot-num">2</div><div class="step-dot-label">Signals</div></div>
      <div class="step-dot" data-step="3"><div class="step-dot-num">3</div><div class="step-dot-label">Structure</div></div>
      <div class="step-dot" data-step="4"><div class="step-dot-num">4</div><div class="step-dot-label">Decision</div></div>
    </div>

    <!-- Prompt display -->
    <div class="m-label" style="margin-top:14px;">Prompt to paste into your LLM</div>
    <div class="prompt-box" id="stepPromptText">Loading prompt…</div>
    <div style="display:flex; gap:8px; margin-top:8px;">
      <button class="ab ab-ghost" onclick="copyStepPrompt()">Copy prompt</button>
      <span id="stepCopyMsg" style="font-size:12px; color:var(--text3); align-self:center;"></span>
    </div>

    <!-- Upload textarea -->
    <div class="m-label" style="margin-top:18px;">Paste the LLM's JSON response here</div>
    <textarea class="m-textarea" id="stepJson" placeholder='{"pricing": "..."} or {"user_signals": {...}} etc.' style="min-height:160px;"></textarea>
    <div id="stepUploadMsg" style="font-size:12px; margin-top:6px; min-height:16px;"></div>

    <div class="m-foot">
      <button class="m-cancel" onclick="hideModal('mStep')">Close</button>
      <button class="m-submit" id="stepSubmitBtn" onclick="submitStep()">Upload & advance</button>
    </div>
  </div>
</div>

<!-- Source Quality Modal — visibility into how strong the source base is -->
<div class="overlay" id="mSourceQuality">
  <div class="modal modal-lg">
    <div class="m-title">
      Source quality — <span id="sqTool"></span>
      <span id="sqOverallBadge" style="margin-left:10px;"></span>
    </div>
    <div class="m-hint">A breakdown of how strong the collected source base is across six dimensions. The overall grade is the worst dimension. Click any suggested query to copy it for manual collection.</div>

    <div class="sq-grid" id="sqGrid">
      <div style="color:var(--text3); font: 400 12px 'JetBrains Mono', monospace; padding: 16px;">Loading…</div>
    </div>

    <div class="sq-suggestions" id="sqSuggestionsBox" style="display:none;">
      <div class="sq-suggestions-title">Suggested search queries to fix the gaps</div>
      <div id="sqSuggestionsList"></div>
    </div>

        <!-- Research Booster Button -->
    <div id="boosterSection" style="margin: 20px 0; padding: 16px; background: var(--bg3); border-radius: var(--r); border: 1px solid var(--amber-b); display: none;">
      <div style="font: 500 12px ''JetBrains Mono'', monospace; color: var(--amber); margin-bottom: 8px;">🔥 SOURCES ARE WEAK — BOOST RECOMMENDED</div>
      <button onclick="generateBoosterPrompt()" 
              style="width:100%; padding: 14px; background: var(--amber); color: #000; font-weight: 600; border-radius: var(--r); font-size: 13px;">
        Generate Research Booster Prompt
      </button>
      <div style="font-size:11px; color:var(--text3); text-align:center; margin-top:8px;">
        This will create a special prompt that fixes the weak dimensions before Step 1
      </div>
    </div>
    <div class="m-foot">
      <button class="m-cancel" onclick="hideModal('mSourceQuality')">Close</button>
    </div>
  </div>
</div>

<!-- Deploy -->
<div class="overlay" id="mDeploy">
  <div class="modal">
    <div class="m-title">Push to GitHub — <span id="dSlug"></span></div>
    <div class="m-hint">This will copy the HTML page to your Stackwise repo and push it live.</div>
    <div class="m-label">Commit Message</div>
    <input class="m-input" id="dMsg" />
    <div class="m-foot">
      <button class="m-cancel" onclick="hideModal('mDeploy')">Cancel</button>
      <button class="ab ab-deploy m-submit" style="background:var(--green-dim); color:var(--green); border:1px solid var(--green-b);" onclick="deploy()">Push to GitHub</button>
    </div>
  </div>
</div>

<!-- Lock -->
<div class="overlay" id="mLock">
  <div class="modal">
    <div class="m-title">Protect your edits — <span id="lSlug"></span></div>
    <div class="m-hint">Check the sections you've manually improved. Protected sections won't be overwritten when you re-run the pipeline.</div>
    <div id="lChecks"></div>
    <div class="m-foot">
      <button class="m-cancel" onclick="hideModal('mLock')">Cancel</button>
      <button class="m-submit" onclick="saveLocks()">Save</button>
    </div>
  </div>
</div>

<!-- Log -->
<div class="overlay" id="mLog">
  <div class="modal modal-lg">
    <div class="m-title">Collection log — <span id="lgSlug"></span></div>
    <div class="log-box" id="lgText">No activity yet.</div>
    <div class="m-foot">
      <button class="m-cancel" onclick="hideModal('mLog')">Close</button>
    </div>
  </div>
</div>

<!-- Files Modal -->
<div class="overlay" id="mFiles">
  <div class="modal">
    <div class="m-title">Pipeline files -- <span id="fSlug"></span></div>
    <div class="m-hint">These are the files for this tool. Click to open the folder in Explorer.</div>
    <div id="filesList" style="margin-bottom:16px;"></div>
    <div class="m-foot">
      <button class="m-cancel" onclick="hideModal('mFiles')">Close</button>
      <button class="m-submit" onclick="openFolder()">Open in Explorer</button>
    </div>
  </div>
</div>

<!-- Toast -->
<div class="toast" id="toast"></div>

<script>
let cur = '';
let cur_step = 0;
const SECS = ['overview','quick_verdict','pricing','user_signals','best_fit','alternatives','workflow','illustrative_output'];
const SEC_LABELS = {
  overview: 'Overview', quick_verdict: 'Quick Verdict', pricing: 'Pricing',
  user_signals: 'User Signals', best_fit: 'Best Fit', alternatives: 'Alternatives',
  workflow: 'Workflow', illustrative_output: 'Example Output'
};

// ── API ──
async function api(url, method='GET', body=null) {
  const o = { method, headers: {'Content-Type':'application/json'} };
  if (body) o.body = JSON.stringify(body);
  return (await fetch(url, o)).json();
}

// ── Helpers ──
function h(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function showModal(id) { document.getElementById(id).classList.add('open'); }
function hideModal(id) { document.getElementById(id).classList.remove('open'); }
function toast(msg, ok=true) {
  const el = document.getElementById('toast');
  el.textContent = msg; el.className = 'toast show ' + (ok ? 'ok' : 'err');
  setTimeout(() => el.className = 'toast', 3500);
}
function ago(dateStr) {
  if (!dateStr) return '—';
  // Simple relative time from date string like "2026-04-04 01:15 AM"
  try {
    const d = new Date(dateStr);
    const now = new Date();
    const diff = (now - d) / 1000;
    if (diff < 60) return 'Just now';
    if (diff < 3600) return Math.floor(diff/60) + ' min ago';
    if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
    if (diff < 604800) return Math.floor(diff/86400) + 'd ago';
    return dateStr;
  } catch { return dateStr; }
}

// ── Status mapping (human-readable) ──
function statusPill(t) {
  if (t.job === 'running') return '<span class="pill pill-blue">Collecting sources…</span>';
  if (t.status && t.status.startsWith('step_')) {
    const doneCount = t.staging_current_step || 0;
    return '<span class="pill pill-amber">Step ' + doneCount + '/4 — ' + (STEP_NAMES[doneCount] || '?') + ' done</span>';
  }
  switch(t.status) {
    case 'complete': return '<span class="pill pill-green">Complete</span>';
    case 'manual_edits': return '<span class="pill pill-red">Manual edits made</span>';
    case 'needs_build': return '<span class="pill pill-amber">Ready to build</span>';
    case 'staging_complete': return '<span class="pill pill-amber">4/4 done — ready to build</span>';
    case 'needs_synthesis': return '<span class="pill pill-amber">Ready for step 1</span>';
    case 'failed': return '<span class="pill pill-red">Collection failed</span>';
    case 'new': return '<span class="pill pill-neutral">Not started</span>';
    default: return '<span class="pill pill-neutral">' + h(t.status) + '</span>';
  }
}

function nextStep(t) {
  if (t.job === 'running') return '<div class="next-step">Sources are being collected. This takes a few minutes.</div>';
  if (t.status && t.status.startsWith('step_')) {
    const doneCount = t.staging_current_step || 0;
    const nextN = doneCount + 1;
    const nextName = STEP_NAMES[nextN] || '?';
    return '<div class="next-step">Continue with <em>Step ' + nextN + ' — ' + nextName + '</em>. Paste the prompt into your LLM, then upload the JSON response.</div>';
  }
  switch(t.status) {
    case 'needs_synthesis':
      return '<div class="next-step">Start the 4-step synthesis: <em>Generate Prompt 1 (Pricing)</em>, paste into your LLM, upload the response.</div>';
    case 'staging_complete':
      return '<div class="next-step">All 4 steps uploaded. <em>Build Page</em> to generate the final HTML.</div>';
    case 'needs_build':
      return '<div class="next-step"><em>Build Page</em> to generate the final HTML from the existing structured JSON.</div>';
    case 'manual_edits':
      return '<div class="next-step"><em>Lock your edits</em> so they\'re protected, then deploy.</div>';
    case 'complete':
      return '<div class="next-step" style="color:var(--text3);">All done. Deploy when ready.</div>';
    case 'failed':
      return '<div class="next-step" style="color:var(--red)">Something went wrong. <em>View the error log</em> to see what happened.</div>';
    case 'new':
      return '<div class="next-step"><em>Start collection</em> to gather sources.</div>';
    default:
      return '<div class="next-step">—</div>';
  }
}

function actions(t) {
  let b = [];
  const s = t.slug, n = h(t.name);

  // Source quality badge — shown first when sources exist, before any action button
  if (t.has_sources && t.source_quality && t.source_quality !== 'missing') {
    const sqColor = t.source_quality;  // green/yellow/red
    const sqLabel = {green:'Strong sources', yellow:'Thin sources', red:'Weak sources'}[sqColor] || 'Sources';
    const sqScore = t.source_quality_score || 0;
    b.push(`<span class="sq-badge sq-${sqColor}" onclick="openSourceQuality('${s}')" title="Click for breakdown"><span class="sq-dot"></span>${sqLabel} ${sqScore}/12</span>`);
  }

  if (t.has_booster) {
    b.push('<span class="boosted-badge">🔥 BOOSTED</span>');
  }

  // Primary action based on status
  if (t.job === 'running') {
    b.push(`<button class="ab ab-ghost" onclick="openLog('${s}')">View Log</button>`);
  } else if (t.status === 'failed' || t.job === 'failed') {
    b.push(`<button class="ab ab-warn" onclick="openLog('${s}')">View Error</button>`);
    b.push(`<button class="ab ab-primary" onclick="rerun('${s}','${n}')">Try Again</button>`);
  } else if (t.status && t.status.startsWith('step_')) {
    // Mid-flow: show step badge + continue button for the next step
    const doneCount = t.staging_current_step || 0;
    const nextStep = doneCount + 1;
    const nextName = STEP_NAMES[nextStep] || '?';
    b.push(`<span class="step-badge"><span class="step-badge-dot"></span>Step ${doneCount}/4 done</span>`);
    b.push(`<button class="ab ab-primary" onclick="openStep('${s}', ${nextStep})">Generate Prompt ${nextStep} (${nextName})</button>`);
  } else if (t.status === 'staging_complete') {
    b.push(`<button class="ab ab-primary" onclick="buildFromStaging('${s}')">Build Page (all 4 steps done)</button>`);
  } else if (t.status === 'needs_synthesis') {
    // Fresh collection, no steps started yet — begin at step 1
    b.push(`<button class="ab ab-primary" onclick="openStep('${s}', 1)">Generate Prompt 1 (Pricing)</button>`);
  } else if (t.status === 'needs_build') {
    // Legacy: has structured JSON but no HTML. Rebuild from structured.
    b.push(`<button class="ab ab-primary" onclick="rebuild('${s}')">Build Page</button>`);
  } else if (t.status === 'manual_edits') {
    b.push(`<button class="ab ab-warn" onclick="openLock('${s}')">Lock My Edits</button>`);
    b.push(`<button class="ab ab-deploy" onclick="openDeploy('${s}','${n}')">Deploy</button>`);
  } else if (t.status === 'complete') {
    b.push(`<button class="ab ab-deploy" onclick="openDeploy('${s}','${n}')">Deploy</button>`);
  }

  // File access - always show if sources exist
  if (t.has_sources) {
    b.push(`<button class="ab ab-ghost" onclick="openFiles('${s}')">Files</button>`);
    b.push(`<button class="ab ab-ghost" onclick="openYoutubeEdit('${s}')">Edit YouTube</button>`);
  } else if (t.status === 'new') {
    b.push(`<button class="ab ab-primary" onclick="rerun('${s}','${n}')">Collect Sources</button>`);
  }

  // Secondary actions (always available for tools with data)
  if (t.has_html && t.status !== 'manual_edits') {
    b.push(`<button class="ab ab-ghost" onclick="openLock('${s}')">Locks</button>`);
  }
  if (t.has_structured) {
    b.push(`<button class="ab ab-ghost" onclick="rebuild('${s}')">Rebuild</button>`);
  }
  // Re-synthesize: wipe staging and walk back through the 4 prompt steps.
  // Available on any tool with collected sources, since it doesn't matter
  // whether the tool was previously built via the new flow or the old monolith.
  if (t.has_sources && t.job !== 'running') {
    b.push(`<button class="ab ab-ghost" onclick="resynthesize('${s}','${n}')">Re-synthesize</button>`);
  }

  return b.join('');
}

// ── Render ──
async function refresh() {
  const data = await api('/api/tools');
  const tools = data.tools || [];

  // Summary
  const total = tools.length;
  const done = tools.filter(t => t.status === 'complete').length;
  const edits = tools.filter(t => t.status === 'manual_edits').length;
  const running = tools.filter(t => t.job === 'running').length;
  document.getElementById('summary').innerHTML =
    `<div class="sum"><b>${total}</b> tools</div>` +
    `<div class="sum"><b>${done}</b> complete</div>` +
    (edits ? `<div class="sum" style="color:var(--red)"><b>${edits}</b> need locking</div>` : '') +
    (running ? `<div class="sum" style="color:var(--blue)"><b>${running}</b> running</div>` : '');

  // Table
  const tbody = document.getElementById('tbody');
  const empty = document.getElementById('emptyState');

  if (!tools.length) {
    tbody.innerHTML = '';
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';

  tbody.innerHTML = tools.map(t => {
    const locks = t.locked_sections.length
      ? '<div class="locks">' + t.locked_sections.map(s => `<span class="lock-chip">${SEC_LABELS[s]||s}</span>`).join('') + '</div>'
      : '<span style="font-size:12px;color:var(--text3)">None</span>';

    const lastDate = t.html_date || t.structured_date || t.sources_date || '';

    return `<tr>
      <td><div class="t-name">${h(t.name)}</div><div class="t-url">${h(t.official_url||'')}</div></td>
      <td><span class="t-date">${ago(lastDate)}</span></td>
      <td>${statusPill(t)}</td>
      <td>${nextStep(t)}</td>
      <td>${locks}</td>
      <td><div class="acts">${actions(t)}</div></td>
    </tr>`;
  }).join('');
}

// ── New Tool ──
let pipelineMode = 'manual';

function setMode(mode) {
  pipelineMode = mode;
  const manual = document.getElementById('modeManual');
  const api = document.getElementById('modeApi');
  if (mode === 'manual') {
    manual.style.borderColor = 'var(--accent)';
    manual.style.background = 'var(--blue-dim)';
    api.style.borderColor = 'var(--border2)';
    api.style.background = 'transparent';
  } else {
    api.style.borderColor = 'var(--accent)';
    api.style.background = 'var(--blue-dim)';
    manual.style.borderColor = 'var(--border2)';
    manual.style.background = 'transparent';
  }
}

function openNewTool() {
  const v = document.getElementById('toolInput').value.trim();
  document.getElementById('fName').value = v;
  document.getElementById('fUrl').value = '';
  document.getElementById('fPricing').value = '';
  setMode('manual');
  showModal('mNew');
  (v ? document.getElementById('fUrl') : document.getElementById('fName')).focus();
}

async function startCollect() {
  const name = document.getElementById('fName').value.trim();
  const url = document.getElementById('fUrl').value.trim();
  const pricing = document.getElementById('fPricing').value.trim();
  const youtubeRaw = document.getElementById('fYoutube').value.trim();
  const youtube_urls = youtubeRaw
    ? youtubeRaw.split('\n').map(l => l.trim()).filter(l => l).slice(0, 3)
    : [];

  if (!name) { toast('Enter a tool name', false); return; }
  hideModal('mNew');
  document.getElementById('toolInput').value = '';

  const slug = name.toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'');

  const bar = document.getElementById('runningBar');
  bar.classList.add('show');
  document.getElementById('runningText').textContent = 'Collecting sources for ' + name + '…';
  document.getElementById('runningLogBtn').onclick = () => openLog(slug);

  toast('Started collecting sources for ' + name);

  await api('/api/collect', 'POST', {
    tool_name: name,
    official_url: url,
    pricing_url: pricing,
    youtube_urls: youtube_urls,
    mode: pipelineMode
  });

  setTimeout(refresh, 800);
  poll(slug);
}

async function rerun(slug, name) {
  const t = await api('/api/tool/' + slug);
  const bar = document.getElementById('runningBar');
  bar.classList.add('show');
  document.getElementById('runningText').textContent = 'Re-collecting sources for ' + name + '\u2026';
  document.getElementById('runningLogBtn').onclick = () => openLog(slug);
  toast('Re-collecting sources for ' + name + '\u2026');
  await api('/api/collect', 'POST', { tool_name: name, slug, official_url: t.official_url || '', pricing_url: t.pricing_url || '' });
  setTimeout(refresh, 800);
  poll(slug);
}

function poll(slug) {
  const fn = async () => {
    const d = await api('/api/logs/' + slug);
    if (d.status === 'running') {
      // Update running bar with latest log line
      const logs = d.log || [];
      if (logs.length) {
        const last = logs[logs.length - 1];
        document.getElementById('runningText').textContent = last.substring(last.indexOf(']') + 2) || 'Working…';
      }
      setTimeout(fn, 2500);
      refresh();
    } else {
      document.getElementById('runningBar').classList.remove('show');
      refresh();
      if (d.status === 'complete') {
        toast('Collection finished! Next: generate a prompt and send it to your LLM.');
      } else {
        const logs = (await api('/api/logs/' + slug)).log || []; const last = logs.slice(-2).map(l => l.substring(l.indexOf(']') + 2)).join(' '); toast('Collection failed: ' + (last || 'check error log'), false);
      }
    }
  };
  setTimeout(fn, 2000);
}

// ── Step flow (4-step split synthesis) ──
const STEP_NAMES = {1:'Pricing', 2:'User Signals', 3:'Structure', 4:'Decision'};

function _updateStepDots(currentStep, targetStep) {
  // currentStep = staging.current_step (0-4, how many steps are done)
  // targetStep = the step the modal is showing (1-4)
  document.querySelectorAll('#mStep .step-dot').forEach(dot => {
    const n = parseInt(dot.dataset.step, 10);
    dot.classList.remove('done', 'current');
    if (n <= currentStep) dot.classList.add('done');
    if (n === targetStep) dot.classList.add('current');
  });
}

async function openStep(slug, stepNum) {
  cur = slug;
  document.getElementById('stepSlug').textContent = slug;
  document.getElementById('stepTitle').textContent = 'Step ' + stepNum + ' of 4 — ' + STEP_NAMES[stepNum];
  document.getElementById('stepPromptText').textContent = 'Loading prompt…';
  document.getElementById('stepJson').value = '';
  document.getElementById('stepUploadMsg').textContent = '';
  document.getElementById('stepUploadMsg').style.color = 'var(--text3)';
  document.getElementById('stepCopyMsg').textContent = '';

  // Fetch current staging state + the step prompt in parallel
  const stagingReq = api('/api/staging/' + slug);
  const promptReq = api('/api/step-prompt', 'POST', { slug, step: stepNum });

  stagingReq.then(s => {
    _updateStepDots(s.current_step || 0, stepNum);
  }).catch(() => {
    _updateStepDots(0, stepNum);
  });

  showModal('mStep');
  cur_step = stepNum;

  try {
    const d = await promptReq;
    if (d && d.prompt) {
      document.getElementById('stepPromptText').textContent = d.prompt;
    } else if (d && d.error) {
      // Backend sent a 400 with an error message — show it
      document.getElementById('stepPromptText').textContent = 'Backend error: ' + d.error;
    } else if (d && d.message) {
      document.getElementById('stepPromptText').textContent = 'Backend message: ' + d.message;
    } else {
      // Last resort — dump whatever came back so we can see it
      document.getElementById('stepPromptText').textContent = 'Could not generate prompt. Raw response: ' + JSON.stringify(d);
    }
  } catch (e) {
    document.getElementById('stepPromptText').textContent = 'Network error loading prompt: ' + e;
  }
}

function copyStepPrompt() {
  const text = document.getElementById('stepPromptText').textContent || '';
  if (!text.trim() || text === 'Loading prompt…') {
    toast('Prompt still loading', false);
    return;
  }
  navigator.clipboard.writeText(text).then(() => {
    const msg = document.getElementById('stepCopyMsg');
    msg.textContent = '✓ Copied to clipboard';
    msg.style.color = 'var(--green)';
    setTimeout(() => { msg.textContent = ''; }, 2500);
  });
}

async function submitStep() {
  const raw = document.getElementById('stepJson').value.trim();
  const msgEl = document.getElementById('stepUploadMsg');
  if (!raw) {
    msgEl.textContent = 'Paste the LLM JSON response first.';
    msgEl.style.color = 'var(--red, #f87171)';
    return;
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    msgEl.textContent = 'Invalid JSON — check for missing commas or brackets.';
    msgEl.style.color = 'var(--red, #f87171)';
    return;
  }

  const btn = document.getElementById('stepSubmitBtn');
  btn.disabled = true;
  btn.textContent = 'Validating…';
  msgEl.textContent = '';

  try {
    const d = await api('/api/step-upload', 'POST', {
      slug: cur, step: cur_step, payload: parsed
    });
    if (d.ok) {
      msgEl.textContent = '✓ ' + (d.message || 'Accepted');
      msgEl.style.color = 'var(--green)';
      _updateStepDots(d.current_step || 0, d.next_step || cur_step);

      if (d.is_complete) {
        // All 4 steps done — offer to build
        setTimeout(() => {
          hideModal('mStep');
          toast('All 4 steps complete — building page…');
          buildFromStaging(cur);
        }, 900);
      } else if (d.next_step) {
        // Auto-advance to next step after brief success pause
        setTimeout(() => {
          openStep(cur, d.next_step);
        }, 1000);
      }
    } else {
      msgEl.textContent = '✗ ' + (d.message || 'Upload rejected');
      msgEl.style.color = 'var(--red, #f87171)';
    }
  } catch (e) {
    msgEl.textContent = '✗ Network error: ' + e;
    msgEl.style.color = 'var(--red, #f87171)';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Upload & advance';
  }
  refresh();
}

async function buildFromStaging(slug) {
  try {
    const d = await api('/api/build', 'POST', { slug });

    if (!d.ok) {
      toast(d.message || 'Build failed', false);
      refresh();
      return;
    }

    const deploy = await api('/api/deploy', 'POST', { slug });

    if (deploy.ok) {
      toast(deploy.message || 'Page built and deployed!', true);
    } else {
      toast('Build succeeded, but deploy failed: ' + (deploy.message || 'Deploy failed'), false);
    }

    refresh();
  } catch (e) {
    toast('Network error during build/deploy: ' + e, false);
    refresh();
  }
}

// ── Source Quality ──
async function openSourceQuality(slug) {
  cur = slug;
  document.getElementById('sqTool').textContent = slug;
  document.getElementById('sqOverallBadge').innerHTML = '';
  document.getElementById('sqGrid').innerHTML = '<div style="color:var(--text3); font: 400 12px monospace; padding: 16px;">Loading…</div>';
  document.getElementById('sqSuggestionsBox').style.display = 'none';
  showModal('mSourceQuality');

  let report;
  try {
    report = await api('/api/source-quality/' + slug);
  } catch (e) {
    document.getElementById('sqGrid').innerHTML = '<div style="color:var(--red, #f87171); padding: 16px;">Failed to load: ' + e + '</div>';
    return;
  }

  if (report.error) {
    document.getElementById('sqGrid').innerHTML = '<div style="color:var(--red, #f87171); padding: 16px;">' + h(report.error) + '</div>';
    return;
  }

  // Header badge — overall grade
  const overall = report.overall || 'missing';
  const overallLabel = {green:'STRONG', yellow:'THIN', red:'WEAK', missing:'NO SOURCES'}[overall] || overall.toUpperCase();
  document.getElementById('sqOverallBadge').innerHTML =
    '<span class="sq-badge sq-' + overall + '"><span class="sq-dot"></span>' + overallLabel +
    ' &nbsp; ' + (report.score || 0) + '/' + (report.max_score || 12) + '</span>';

  // Dimension breakdown grid
  const grid = document.getElementById('sqGrid');
  if (!report.dimensions || report.dimensions.length === 0) {
    grid.innerHTML = '<div style="color:var(--text3); padding:16px;">No dimensions to show. ' + h(report.suggestions ? report.suggestions[0] || '' : '') + '</div>';
  } else {
    grid.innerHTML = report.dimensions.map(d => {
      const colorClass = 'sq-' + (d.color || 'missing');
      return '<div class="sq-row ' + colorClass + '">' +
        '<div class="sq-row-dot"></div>' +
        '<div class="sq-name">' + h(d.name) + '</div>' +
        '<div class="sq-score">' + (d.score || 0) + '/2</div>' +
        '<div class="sq-detail">' + h(d.detail || '') + '</div>' +
      '</div>';
    }).join('');
  }

  // Suggestions list
  const suggestions = report.suggestions || [];
  const sugBox = document.getElementById('sqSuggestionsBox');
  const sugList = document.getElementById('sqSuggestionsList');
  // Show booster button only when sources are weak
  const isWeak = (report.overall === 'red' || report.overall === 'yellow');
  const boosterEl = document.getElementById('boosterSection');
  if (boosterEl) boosterEl.style.display = isWeak ? 'block' : 'none';
  if (suggestions.length === 0) {
    sugBox.style.display = 'none';
  } else {
    sugBox.style.display = 'block';
    sugList.innerHTML = suggestions.map((q, i) => {
      // Escape the query for safe storage in a data attribute
      const safeQuery = q.replace(/"/g, '&quot;').replace(/'/g, "&#39;");
      return '<div class="sq-suggestion" onclick="copySuggestion(this)" data-query="' + safeQuery + '">' +
        '<span class="sq-num">' + (i + 1) + '.</span>' +
        '<span class="sq-query">' + h(q) + '</span>' +
        '<span class="sq-copy">copy</span>' +
      '</div>';
    }).join('');
  }
}

function copySuggestion(el) {
  const query = el.getAttribute('data-query') || '';
  // Decode the entities we encoded above
  const decoded = query.replace(/&quot;/g, '"').replace(/&#39;/g, "'");
  navigator.clipboard.writeText(decoded).then(() => {
    const original = el.querySelector('.sq-copy').textContent;
    el.querySelector('.sq-copy').textContent = '✓ copied';
    el.querySelector('.sq-copy').style.color = 'var(--green)';
    setTimeout(() => {
      el.querySelector('.sq-copy').textContent = original;
      el.querySelector('.sq-copy').style.color = '';
    }, 1500);
  });
}


// ── Deploy ──
function openDeploy(slug, name) {
  cur = slug;
  document.getElementById('dSlug').textContent = slug;
  document.getElementById('dMsg').value = 'Update ' + name + ' tool page';
  showModal('mDeploy');
}
async function deploy() {
  hideModal('mDeploy');
  toast('Deploying…');
  const d = await api('/api/deploy', 'POST', { slug: cur, commit_message: document.getElementById('dMsg').value.trim() });
  toast(d.ok ? 'Deployed!' : (d.message || 'Deploy failed'), d.ok);
  refresh();
}

// ── Rebuild ──
async function rebuild(slug) {
  toast('Rebuilding page…');
  const d = await api('/api/rebuild', 'POST', { slug });
  toast(d.ok ? 'Page rebuilt' : (d.message || 'Rebuild failed'), d.ok);
  refresh();
}

// ── Re-synthesize ──
// Wipes the staging file and immediately opens step 1, so the operator can
// re-run the 4-step synthesis on existing sources with the latest prompts.
async function resynthesize(slug, name) {
  toast('Wiping staging for ' + name + '…');
  const d = await api('/api/resynthesize', 'POST', { slug });
  if (!d.ok) {
    toast(d.message || 'Re-synthesize failed', false);
    return;
  }
  // Refresh tool list so the card flips to step_1_of_4 status, then open the
  // step 1 modal. Order matters: refresh first so the modal sees the new state.
  await refresh();
  openStep(slug, 1);
}

// ── Lock ──
function openLock(slug) {
  cur = slug;
  document.getElementById('lSlug').textContent = slug;
  api('/api/tool/' + slug).then(t => {
    const locked = new Set(t.locked_sections || []);
    document.getElementById('lChecks').innerHTML = SECS.map(s =>
      `<div class="sec-row"><input type="checkbox" id="lk_${s}" ${locked.has(s)?'checked':''} /><label for="lk_${s}">${SEC_LABELS[s]||s}</label></div>`
    ).join('');
  });
  showModal('mLock');
}
async function saveLocks() {
  const on = SECS.filter(s => document.getElementById('lk_'+s)?.checked);
  const off = SECS.filter(s => !document.getElementById('lk_'+s)?.checked);
  hideModal('mLock');
  if (on.length) await api('/api/lock', 'POST', { slug: cur, sections: on });
  if (off.length) await api('/api/unlock', 'POST', { slug: cur, sections: off });
  toast('Locks saved');
  refresh();
}

// ── Log ──
async function openLog(slug) {
  document.getElementById('lgSlug').textContent = slug;
  showModal('mLog');
  const d = await api('/api/logs/' + slug);
  document.getElementById('lgText').textContent = (d.log||[]).join('\n') || 'No activity yet.';
}


/* ── Research Booster Functions ── */
async function generateBoosterPrompt() {
  if (!cur) return;
  const btnSection = document.getElementById('boosterSection');
  btnSection.style.opacity = '0.5';
  btnSection.style.pointerEvents = 'none';

  try {
    const d = await api('/api/boost-prompt', 'POST', { slug: cur });
    if (d.ok && d.prompt) {
      hideModal('mSourceQuality');
      cur_step = 0;
      document.getElementById('stepTitle').innerHTML = '🔥 Research Booster — ' + cur;
      document.getElementById('stepSlug').textContent = cur;
      document.getElementById('stepPromptText').textContent = d.prompt;
      document.getElementById('stepHint').innerHTML = 
        'Copy this booster prompt into your LLM.<br><strong>It will return extra authoritative data</strong> to fix the weak sources.';
      document.getElementById('stepJson').value = '';
      document.getElementById('stepUploadMsg').innerHTML = 
        '<span style="color:var(--amber)">Paste the booster JSON here (it will reset staging and open Step 1)</span>';
      document.getElementById('stepSubmitBtn').innerHTML = 'Upload Booster Data';
      document.getElementById('stepSubmitBtn').onclick = submitBooster;
      showModal('mStep');
    } else {
      toast('Failed to generate booster prompt', false);
    }
  } catch (e) {
    toast('Error generating booster: ' + e, false);
  } finally {
    btnSection.style.opacity = '1';
    btnSection.style.pointerEvents = 'auto';
  }
}

async function submitBooster() {
  const raw = document.getElementById('stepJson').value.trim();
  const msgEl = document.getElementById('stepUploadMsg');
  if (!raw) {
    msgEl.textContent = 'Paste the booster JSON first.';
    msgEl.style.color = 'var(--red)';
    return;
  }

  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    msgEl.textContent = 'Invalid JSON';
    msgEl.style.color = 'var(--red)';
    return;
  }

  const btn = document.getElementById('stepSubmitBtn');
  let switchedToStepMode = false;
  btn.disabled = true;
  btn.textContent = 'Applying booster...';

  try {
    const d = await api('/api/boost-upload', 'POST', { slug: cur, payload: parsed });
    if (!d.ok) {
      msgEl.textContent = '✗ ' + (d.message || 'Upload failed');
      msgEl.style.color = 'var(--red)';
      return;
    }

    msgEl.innerHTML = '✅ <strong>Booster applied!</strong><br>Loading Step 1 (Pricing)...';
    msgEl.style.color = 'var(--green)';

    // Reconfigure the existing step modal back into normal Step 1 mode
    document.getElementById('stepTitle').textContent = 'Step 1 of 4 — Pricing';
    document.getElementById('stepHint').innerHTML =
      'Copy this prompt into your LLM, then paste back ONLY the JSON response.<br><strong>The upload is strict — the JSON must contain exactly the sections this step produces.</strong>';
    document.getElementById('stepPromptText').textContent = 'Loading prompt…';
    document.getElementById('stepJson').value = '';
    document.getElementById('stepUploadMsg').textContent = '';
    document.getElementById('stepUploadMsg').style.color = 'var(--text3)';
    document.getElementById('stepCopyMsg').textContent = '';
    btn.textContent = 'Upload & advance';
    btn.onclick = submitStep;
    btn.disabled = false;
    switchedToStepMode = true;

    // Keep the user inside the step modal and open Step 1 immediately
    await openStep(cur, 1);
  } catch (e) {
    msgEl.textContent = 'Network error';
    msgEl.style.color = 'var(--red)';
  } finally {
    if (!switchedToStepMode) {
      btn.disabled = false;
      btn.textContent = 'Upload Booster Data';
      btn.onclick = submitBooster;
    }
  }
}


function openYoutubeEdit(slug) {
  cur = slug;
  document.getElementById('ytSlug').textContent = slug;
  api('/api/youtube-override/' + slug).then(d => {
    document.getElementById('ytUrls').value = (d.youtube_urls || []).join('\n');
  }).catch(() => {
    document.getElementById('ytUrls').value = '';
  });
  showModal('mYoutubeEdit');
}

async function saveYoutubeEdit() {
  const raw = document.getElementById('ytUrls').value.trim();
  const youtube_urls = raw ? raw.split('\n').map(l => l.trim()).filter(l => l).slice(0,3) : [];
  hideModal('mYoutubeEdit');

  const bar = document.getElementById('runningBar');
  bar.classList.add('show');
  document.getElementById('runningText').textContent = 'Refreshing YouTube for ' + cur + '…';
  document.getElementById('runningLogBtn').onclick = () => openLog(cur);

  toast('Started YouTube refresh for ' + cur);

  const d = await api('/api/youtube-edit', 'POST', { slug: cur, youtube_urls });
  if (d.ok) {
    setTimeout(refresh, 800);
    poll(cur);
  } else {
    toast(d.message || 'Failed', false);
  }
}

refresh();
setInterval(refresh, 12000);
</script>
<!-- YouTube Edit Modal -->
<div class="overlay" id="mYoutubeEdit">
  <div class="modal">
    <div class="m-title">Edit YouTube URLs — <span id="ytSlug"></span></div>
    <div class="m-hint">One URL per line (max 3). This will update the override and re-run ingest.</div>
    <div class="m-label">YouTube URLs</div>
    <textarea class="m-textarea" id="ytUrls" rows="4" placeholder="https://youtube.com/watch?v=..."></textarea>
    <div class="m-foot">
      <button class="m-cancel" onclick="hideModal('mYoutubeEdit')">Cancel</button>
      <button class="m-submit" onclick="saveYoutubeEdit()">Save & Re-ingest</button>
    </div>
  </div>
</div>
</body>
</html>"""


# ── Server startup ────────────────────────────────────────────────────────────

def main():
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)

    # Validate paths
    if not COLLECT_SCRIPT.exists():
        print(f"  ⚠ WARNING: collect_tool_sources.py not found at {COLLECT_SCRIPT}")
        print(f"    Collection will fail. Is pipeline_server.py in the right directory?")
    else:
        print(f"  ✅ Collector:    {COLLECT_SCRIPT}")
    if not COLLECTOR_PARTS.exists():
        print(f"  ⚠ WARNING: collector_parts/ not found at {COLLECTOR_PARTS}")
    else:
        print(f"  ✅ Package:      {COLLECTOR_PARTS}")

    print(f"\n  ╔══════════════════════════════════════════╗")
    print(f"  ║  Stackwise Pipeline Control Center       ║")
    print(f"  ║  http://localhost:{PORT}                   ║")
    print(f"  ╚══════════════════════════════════════════╝")
    print(f"\n  Research dir:  {RESEARCH_DIR}")
    print(f"  Locks:         {CONTENT_LOCKS_PATH}")
    print(f"  Deploy repo:   {STACKWISE_REPO}")
    print(f"  {'✅ Repo exists' if STACKWISE_REPO.exists() else '⚠ Repo not found — deploy will fail'}")
    print()

    server = HTTPServer(("127.0.0.1", PORT), PipelineHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down...")
        server.server_close()


if __name__ == "__main__":
    main()




















