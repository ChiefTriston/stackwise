from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------
# Hardcoded paths
# ---------------------------------------------------------------------

ROOT = Path(r"C:\Users\trist\OneDrive\Documents\GitHub\AI-WIKI\automation\split_collector\YoutubeCollector")
VIDEO_DIR = ROOT / "vidoes"  # keeping your exact folder name
TRANSCRIPT_DIR = ROOT / "transcriptions"
MANIFEST_DIR = ROOT / "manifests"

# ── Match the exact RESEARCH_DIR used by pipeline_server.py ──
RESEARCH_DIR = ROOT.parent.parent / "research"

# ---------------------------------------------------------------------
# Hardcoded GPU path - set BEFORE torch/whisper import
# ---------------------------------------------------------------------

os.environ["CUDA_VISIBLE_DEVICES"] = "0"

import torch  # noqa: E402
import whisper  # noqa: E402

from query import (  # noqa: E402
    flatten_queries,
    rank_score,
    slugify,
    upload_date_sort_value,
    is_polarizing_title,
    MIN_VIEW_COUNT,
)


YT_DLP_BIN = "yt-dlp"
EXPECTED_GPU_SUBSTRING = "RTX 2050"


def ensure_dirs() -> None:
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)


def run_cmd(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def audio_path_for(tool_slug: str, video_id: str) -> Path:
    return VIDEO_DIR / f"{tool_slug}__{video_id}.mp3"


def transcript_path_for(tool_slug: str, video_id: str) -> Path:
    return TRANSCRIPT_DIR / f"{tool_slug}__{video_id}.txt"


def segment_json_path_for(tool_slug: str, video_id: str) -> Path:
    return TRANSCRIPT_DIR / f"{tool_slug}__{video_id}.segments.json"


def manifest_path_for(tool_slug: str, video_id: str) -> Path:
    return MANIFEST_DIR / f"{tool_slug}__{video_id}.json"


def selected_jobs_manifest_path(tool_slug: str) -> Path:
    return MANIFEST_DIR / f"{tool_slug}__selected_jobs.json"


def search_youtube(
    *,
    query_text: str,
    per_query_results: int,
    min_duration_seconds: int,
    recency_days: int,
) -> list[dict[str, Any]]:
    search_input = f"ytsearch{per_query_results}:{query_text}"

    cmd = [
        YT_DLP_BIN,
        "--flat-playlist",
        "--dump-single-json",
        "--dateafter",
        f"today-{max(1, recency_days)}days",
        "--match-filters",
        f"duration >= {min_duration_seconds} & !is_live",
        search_input,
    ]

    proc = run_cmd(cmd)
    if proc.returncode != 0:
        print(f"\nSEARCH FAILED:\n{proc.stderr.strip()}\n", file=sys.stderr)
        return []

    stdout = proc.stdout.strip()
    if not stdout:
        return []

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        print(f"\nBAD SEARCH JSON:\n{stdout[:1000]}\n", file=sys.stderr)
        return []

    entries = data.get("entries") or []
    results: list[dict[str, Any]] = []

    for idx, entry in enumerate(entries, start=1):
        video_id = entry.get("id")
        if not video_id:
            continue

        duration = int(entry.get("duration") or 0)
        if duration < min_duration_seconds:
            continue

        title = entry.get("title") or ""
        view_count = int(entry.get("view_count") or 0)

        if is_polarizing_title(title):
            print(f"  [SKIP] Polarizing title: {title[:80]}")
            continue

        if view_count > 0 and view_count < MIN_VIEW_COUNT:
            print(f"  [SKIP] Low views ({view_count:,}): {title[:60]}")
            continue

        results.append(
            {
                "video_id": video_id,
                "url": entry.get("url") or f"https://www.youtube.com/watch?v={video_id}",
                "title": title,
                "channel": entry.get("channel") or entry.get("uploader") or "",
                "uploader": entry.get("uploader") or entry.get("channel") or "",
                "upload_date": entry.get("upload_date"),
                "duration": duration,
                "view_count": view_count,
                "query_result_rank": idx,
            }
        )

    return results


def collect_jobs(
    *,
    tool_name: str,
    per_query_results: int,
    min_duration_minutes: int,
    recency_days: int,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}

    for q in flatten_queries(tool_name):
        bucket = q["bucket"]
        query_text = q["query_text"]
        query_index = q["query_index"]

        print(f"[SEARCH] {bucket} | q{query_index:02d} | {query_text}")

        results = search_youtube(
            query_text=query_text,
            per_query_results=per_query_results,
            min_duration_seconds=min_duration_minutes * 60,
            recency_days=recency_days,
        )

        for result in results:
            video_id = result["video_id"]

            if video_id not in merged:
                merged[video_id] = {
                    **result,
                    "matched_buckets": [bucket],
                    "matched_queries": [query_text],
                    "query_hits": [
                        {
                            "bucket": bucket,
                            "query_index": query_index,
                            "query_text": query_text,
                            "query_result_rank": result["query_result_rank"],
                        }
                    ],
                }
            else:
                row = merged[video_id]

                if bucket not in row["matched_buckets"]:
                    row["matched_buckets"].append(bucket)

                if query_text not in row["matched_queries"]:
                    row["matched_queries"].append(query_text)

                row["query_hits"].append(
                    {
                        "bucket": bucket,
                        "query_index": query_index,
                        "query_text": query_text,
                        "query_result_rank": result["query_result_rank"],
                    }
                )

    jobs = list(merged.values())

    jobs.sort(
        key=lambda x: rank_score(
            title=x.get("title") or "",
            matched_bucket_count=len(x.get("matched_buckets") or []),
            matched_query_count=len(x.get("matched_queries") or []),
            duration_seconds=int(x.get("duration") or 0),
            upload_date_value=upload_date_sort_value(x.get("upload_date")),
            view_count=int(x.get("view_count") or 0),
        ),
        reverse=True,
    )

    return jobs


def save_selected_jobs_manifest(tool_name: str, tool_slug: str, jobs: list[dict[str, Any]]) -> None:
    payload = {
        "tool_name": tool_name,
        "tool_slug": tool_slug,
        "generated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "jobs_found": len(jobs),
        "jobs": jobs,
    }
    selected_jobs_manifest_path(tool_slug).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def download_audio(
    *,
    tool_slug: str,
    job: dict[str, Any],
    min_duration_minutes: int,
    recency_days: int,
    manual: bool = False,
) -> Path | None:
    out_path = audio_path_for(tool_slug, job["video_id"])
    if out_path.exists():
        print(f"[SKIP DOWNLOAD] {out_path.name}")
        return out_path

    output_template = str(VIDEO_DIR / f"{tool_slug}__{job['video_id']}.%(ext)s")

    cmd = [
        YT_DLP_BIN,
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "-o", output_template,
        job["url"],
    ]

    if not manual:
        cmd.extend([
            "--dateafter", f"today-{max(1, recency_days)}days",
            "--match-filters", f"duration >= {min_duration_minutes * 60} & !is_live",
        ])

    print(f"[DOWNLOAD] {job.get('title', job['url'])}")
    proc = run_cmd(cmd)

    if proc.returncode != 0:
        print(f"\nDOWNLOAD FAILED for {job['video_id']}:\n{proc.stderr.strip()}\n", file=sys.stderr)
        return None

    return out_path if out_path.exists() else None


def verify_gpu() -> str:
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available. Refusing to run on CPU.")

    gpu_name = torch.cuda.get_device_name(0)
    if EXPECTED_GPU_SUBSTRING.lower() not in gpu_name.lower():
        raise RuntimeError(
            f"Unexpected GPU detected: {gpu_name}. "
            f"Expected something containing: {EXPECTED_GPU_SUBSTRING}"
        )

    print(f"[GPU] Using: {gpu_name}")
    return gpu_name


def load_whisper_model(model_name: str) -> whisper.Whisper:
    verify_gpu()
    print(f"[MODEL] Loading Whisper model: {model_name}")
    return whisper.load_model(model_name, device="cuda")


def transcribe_audio(
    *,
    model: whisper.Whisper,
    tool_slug: str,
    job: dict[str, Any],
    audio_path: Path,
) -> Path:
    transcript_path = transcript_path_for(tool_slug, job["video_id"])
    if transcript_path.exists():
        print(f"[SKIP TRANSCRIBE] {transcript_path.name}")
        return transcript_path

    segment_json_path = segment_json_path_for(tool_slug, job["video_id"])

    print(f"[TRANSCRIBE] {audio_path.name}")
    result = model.transcribe(
        str(audio_path),
        language="en",
        fp16=True,
        verbose=False,
    )

    transcript_text = (result.get("text") or "").strip()
    transcript_path.write_text(transcript_text, encoding="utf-8")

    segment_payload = {
        "tool_name": job.get("tool_name"),
        "tool_slug": tool_slug,
        "video_id": job.get("video_id"),
        "url": job.get("url"),
        "title": job.get("title"),
        "channel": job.get("channel"),
        "uploader": job.get("uploader"),
        "upload_date": job.get("upload_date"),
        "duration_seconds": job.get("duration"),
        "view_count": job.get("view_count", 0),
        "matched_buckets": job.get("matched_buckets", []),
        "matched_queries": job.get("matched_queries", []),
        "query_hits": job.get("query_hits", []),
        "audio_path": str(audio_path),
        "transcript_path": str(transcript_path),
        "segments": result.get("segments", []),
        "language": result.get("language"),
    }
    segment_json_path.write_text(
        json.dumps(segment_payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return transcript_path


def update_manifest(
    *,
    tool_name: str,
    tool_slug: str,
    job: dict[str, Any],
    audio_path: Path | None,
    transcript_path: Path | None,
) -> None:
    payload = {
        "tool_name": tool_name,
        "tool_slug": tool_slug,
        "video_id": job["video_id"],
        "url": job.get("url"),
        "title": job.get("title"),
        "channel": job.get("channel"),
        "uploader": job.get("uploader"),
        "upload_date": job.get("upload_date"),
        "duration_seconds": job.get("duration"),
        "view_count": job.get("view_count", 0),
        "matched_buckets": job.get("matched_buckets", []),
        "matched_queries": job.get("matched_queries", []),
        "query_hits": job.get("query_hits", []),
        "audio_path": str(audio_path) if audio_path else None,
        "transcript_path": str(transcript_path) if transcript_path else None,
        "last_updated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }

    manifest_path_for(tool_slug, job["video_id"]).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def load_youtube_override(slug: str) -> list[str]:
    """Load manual YouTube URLs from the override file created by pipeline_server.py."""
    override_path = RESEARCH_DIR / f"{slug}.youtube_override.json"
    if override_path.exists():
        try:
            data = json.loads(override_path.read_text(encoding="utf-8"))
            urls = data.get("youtube_urls", [])
            print(f"[OVERRIDE] Loaded {len(urls)} URLs from {override_path.name}")
            return urls
        except Exception as e:
            print(f"[WARN] Could not read override file {override_path}: {e}")
    return []


def build_manual_jobs(tool_name: str, tool_slug: str, youtube_urls: list[str]) -> list[dict]:
    """Build real job entries from exact URLs. Bypasses ALL search/ranking in query.py."""
    jobs = []
    for url in youtube_urls:
        cmd = [YT_DLP_BIN, "--dump-single-json", "--no-warnings", url]
        proc = run_cmd(cmd)

        if proc.returncode != 0:
            print(f"[WARN] Could not fetch metadata for {url}")
            continue

        try:
            data = json.loads(proc.stdout.strip())
        except json.JSONDecodeError:
            continue

        video_id = data.get("id")
        if not video_id:
            continue

        jobs.append({
            "video_id": video_id,
            "url": url,
            "title": data.get("title", "Unknown Title"),
            "channel": data.get("channel") or data.get("uploader") or "",
            "uploader": data.get("uploader") or data.get("channel") or "",
            "upload_date": data.get("upload_date"),
            "duration": int(data.get("duration") or 0),
            "view_count": int(data.get("view_count") or 0),
            "matched_buckets": ["manual"],
            "matched_queries": ["user override"],
            "query_hits": [],
            "manual": True,
        })

    # Sort newest first
    jobs.sort(key=lambda x: upload_date_sort_value(x.get("upload_date")), reverse=True)
    return jobs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool-name", required=False)
    parser.add_argument("--slug", default="")
    parser.add_argument("--per-query-results", type=int, default=8)
    parser.add_argument("--min-duration-minutes", type=int, default=30)
    parser.add_argument("--recency-days", type=int, default=90)
    parser.add_argument("--model", default="base", choices=["tiny", "base", "small"])
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--skip-transcribe", action="store_true")

    # ── NEW: Manual YouTube support ──
    parser.add_argument("--youtube-urls", nargs="*", default=None,
                        help="Manual YouTube URLs (bypasses discovery)")
    parser.add_argument("--youtube-only", action="store_true",
                        help="Run only YouTube ingest using .youtube_override.json if present")

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_dirs()

    tool_slug = args.slug.strip()
    tool_name = (args.tool_name or tool_slug).strip()
    if not tool_slug:
        tool_slug = slugify(tool_name)

    # ── Determine which URLs to use ──
    youtube_urls = args.youtube_urls

    if not youtube_urls and args.youtube_only:
        youtube_urls = load_youtube_override(tool_slug)
        if not youtube_urls:
            print(f"ERROR: --youtube-only was requested but no override file found for slug '{tool_slug}'")
            print(f"       Expected: {RESEARCH_DIR / f'{tool_slug}.youtube_override.json'}")
            return 1

    if youtube_urls:
        print(f"[MANUAL YOUTUBE] Using {len(youtube_urls)} provided URLs")
        jobs = build_manual_jobs(tool_name, tool_slug, youtube_urls)
    else:
        if not tool_name:
            print("ERROR: --tool-name (or --slug) is required for automatic discovery")
            return 1
        jobs = collect_jobs(
            tool_name=tool_name,
            per_query_results=args.per_query_results,
            min_duration_minutes=args.min_duration_minutes,
            recency_days=args.recency_days,
        )
        if args.limit > 0:
            jobs = jobs[: args.limit]

    for job in jobs:
        job["tool_name"] = tool_name
        job["tool_slug"] = tool_slug

    save_selected_jobs_manifest(tool_name, tool_slug, jobs)
    print(f"\n[SELECTED JOBS] {len(jobs)}")

    model = None
    if not args.skip_transcribe:
        model = load_whisper_model(args.model)

    # ── PER-VIDEO LOOP (unchanged) ──
    for idx, job in enumerate(jobs, start=1):
        print(f"\n[{idx}/{len(jobs)}] {job['video_id']} | {job['title']}")

        audio_path: Path | None = None
        transcript_path: Path | None = None

        if not args.skip_download:
            audio_path = download_audio(
                tool_slug=tool_slug,
                job=job,
                min_duration_minutes=args.min_duration_minutes,
                recency_days=args.recency_days,
                manual=job.get("manual", False),
            )
        else:
            audio_path = audio_path_for(tool_slug, job["video_id"])
            if not audio_path.exists():
                audio_path = None

        if audio_path is not None and not args.skip_transcribe:
            transcript_path = transcribe_audio(
                model=model,
                tool_slug=tool_slug,
                job=job,
                audio_path=audio_path,
            )

        update_manifest(
            tool_name=tool_name,
            tool_slug=tool_slug,
            job=job,
            audio_path=audio_path,
            transcript_path=transcript_path,
        )

    # ── FINAL AGGREGATION (runs ONCE after all videos) ──
    print(f"\n[AGGREGATE] Building final youtube_signals.json for {tool_slug}...")

    signals = {
        "tool_name": tool_name,
        "tool_slug": tool_slug,
        "generated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "source_count": len(jobs),
        "sources": []
    }

    for job in jobs:
        video_id = job["video_id"]

        # Load transcript text
        transcript_text = ""
        transcript_file = transcript_path_for(tool_slug, video_id)
        if transcript_file.exists():
            try:
                transcript_text = transcript_file.read_text(encoding="utf-8")
            except Exception:
                transcript_text = "[transcript load failed]"

        # Load segments (correctly extract the "segments" key)
        segments = []
        segment_file = segment_json_path_for(tool_slug, video_id)
        if segment_file.exists():
            try:
                segment_data = json.loads(segment_file.read_text(encoding="utf-8"))
                segments = segment_data.get("segments", [])
            except Exception:
                segments = []

        entry = {
            "video_id": video_id,
            "url": job.get("url"),
            "title": job.get("title"),
            "channel": job.get("channel"),
            "uploader": job.get("uploader"),
            "upload_date": job.get("upload_date"),
            "duration": job.get("duration"),
            "view_count": job.get("view_count", 0),
            "matched_buckets": job.get("matched_buckets", []),
            "matched_queries": job.get("matched_queries", []),
            "query_hits": job.get("query_hits", []),
            "audio_path": str(audio_path_for(tool_slug, video_id)),
            "transcript_path": str(transcript_file) if transcript_file.exists() else None,
            "transcript": transcript_text,
            "segments": segments,
        }
        signals["sources"].append(entry)

    signals_path = RESEARCH_DIR / f"{tool_slug}_youtube_signals.json"
    signals_path.write_text(
        json.dumps(signals, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    print(f"✅ Created {signals_path.name} with {len(jobs)} YouTube sources")

    print("\n[DONE]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())