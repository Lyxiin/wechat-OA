# Public Video Content Analysis Practice Project Design

Date: 2026-05-13

## Goal

Build a command-line practice project that analyzes publicly shared videos for learning and research use. The first version extracts audio from user-supplied public video links, transcribes speech, generates a general structured content analysis, and saves both human-readable files and SQLite index records.

The project must use only links and cookies already provided by the user. It must not attempt to bypass privacy, authentication, paywalls, or platform access controls.

## Approved Decisions

- Input mode: support both direct video/share links and future account-based batch collection, with direct URL lists as the first stable workflow.
- Analysis mode: use a general extensible structure with summary, topics, keywords, timeline, key points, entities, open questions, and an extension field for future domain-specific analysis.
- Persistence: write file artifacts for inspection and store SQLite records for indexing, status tracking, and later search or dashboards.
- Model integration: use adapter boundaries. The default adapters reuse the existing Groq Whisper ASR client and the existing OpenAI-style LLM configuration, while keeping room for local or alternate services later.
- User interface: command line first. A web viewer can be added later after the pipeline is reliable and tested.

## Recommended Approach

Add an independent `video_analysis.py` pipeline that reuses the existing ASR and Douyin/yt-dlp capabilities without coupling this learning project to the existing stock article pipeline.

This is preferred over expanding `douyin_pipeline.py` because it keeps account crawling, finance extraction, and general video analysis separate. It is also preferred over building a full plugin-style media framework because the first version should prioritize a working learning loop.

## Command-Line Interface

The first version should expose:

```powershell
python video_analysis.py init-db
python video_analysis.py run --urls urls.txt
python video_analysis.py run --url "https://..."
```

Optional rerun controls:

```powershell
python video_analysis.py run --urls urls.txt --force-download
python video_analysis.py run --urls urls.txt --force-transcribe
python video_analysis.py run --urls urls.txt --force-analyze
```

Expected default paths:

- Input cookies: `cookies/cookies.txt`, unless overridden by configuration or environment.
- Output directory: `video_analysis_data/`.
- SQLite database: `video_analysis_data/video_analysis.sqlite`.

## Architecture

The pipeline should be split into small units with clear interfaces:

- `VideoSourceReader`: reads one `--url` value or a `--urls` text file, normalizes whitespace, ignores blank/comment lines, and deduplicates links while preserving order.
- `VideoDownloader`: resolves video metadata with `yt-dlp`, downloads or extracts audio, and writes metadata and audio files. It should reuse the existing cookies handling pattern from `douyin_crawler.py`.
- `ASRAdapter`: transcribes audio. The default implementation wraps `asr_client.GroqASRClient`.
- `ContentAnalyzer`: sends transcript text and metadata to an OpenAI-style LLM and validates the returned JSON analysis.
- `VideoAnalysisStore`: writes SQLite rows and file artifacts, tracks status, and supports cache decisions.
- `PipelineRunner`: orchestrates per-video execution and keeps failures isolated.

## Data Flow

For each input link:

1. Normalize and deduplicate the URL.
2. Resolve metadata and canonical URL through `yt-dlp`.
3. Create a stable output folder under `video_analysis_data/<video_id>/`.
4. Download or reuse audio.
5. Transcribe or reuse transcript.
6. Analyze transcript or reuse analysis when the transcript hash matches.
7. Write `metadata.json`, `transcript.txt`, `analysis.json`, and `summary.md`.
8. Upsert an SQLite row with status, metadata, hashes, output paths, and error details.
9. Continue to the next video even if one video fails.

## File Artifacts

Each video folder should contain:

- `metadata.json`: title, author, source URL, canonical URL, publish time, duration, thumbnail URL, and raw selected metadata.
- `audio.mp3`: extracted audio when available.
- `transcript.txt`: plain text transcript.
- `analysis.json`: validated structured analysis.
- `summary.md`: human-readable result for quick review.

## Analysis JSON

The analyzer should produce this stable shape:

```json
{
  "summary": "Short overall summary.",
  "topics": ["topic"],
  "keywords": ["keyword"],
  "timeline": [
    {"time": "00:00", "event": "What happens or is discussed."}
  ],
  "key_points": ["important point"],
  "entities": ["person, organization, product, place, or concept"],
  "action_items": [],
  "open_questions": [],
  "extensions": {}
}
```

The implementation should validate that the top-level object exists, required list fields are lists, and `summary` is a string. If the model returns invalid JSON, the pipeline should save the raw response for debugging and mark only that video as failed.

## SQLite Storage

Use a lightweight table such as `video_items`:

- `id`
- `video_id`
- `source_url`
- `canonical_url`
- `title`
- `author`
- `publish_time`
- `duration`
- `thumbnail_url`
- `status`
- `error`
- `transcript_hash`
- `analysis_hash`
- `metadata_path`
- `audio_path`
- `transcript_path`
- `analysis_path`
- `summary_path`
- `created_at`
- `updated_at`

The database stores indexable metadata and paths. Full transcripts and full analysis JSON remain in files.

## Caching

Default behavior should avoid repeated network and model calls:

- Reuse audio if the expected audio file exists and has non-zero size.
- Reuse transcript if `transcript.txt` exists and is non-empty.
- Reuse analysis if `analysis.json` exists and the saved transcript hash matches the current transcript hash.

Rerun flags should override only their corresponding stage:

- `--force-download` downloads audio again.
- `--force-transcribe` reruns ASR from existing or newly downloaded audio.
- `--force-analyze` reruns LLM analysis from the current transcript.

## Error Handling

Failures should be isolated per video. A failed URL should not stop the rest of the batch.

Common errors should produce readable messages:

- Missing cookies file when a platform requires cookies.
- `yt-dlp` not installed or not runnable.
- Download or metadata resolution failed.
- Audio file was not created.
- ASR API key is missing or placeholder.
- ASR response is empty.
- LLM API key is missing or placeholder.
- LLM response is not valid JSON.

Each failed item should update SQLite with `status = failed`, store the error string, and continue processing remaining links.

## Testing Strategy

Default tests should avoid real network and real model calls. Use fake downloaders, fake ASR clients, and fake analyzers.

Coverage should include:

- URL file parsing, comment skipping, and deduplication.
- Output path generation and stable video folder naming.
- Audio, transcript, and analysis cache behavior.
- End-to-end pipeline orchestration with fake clients.
- Failure isolation when one URL fails.
- JSON analysis validation and invalid-response handling.
- SQLite upsert behavior.

Manual verification can cover real `yt-dlp`, cookies, Groq ASR, and LLM calls after unit tests pass.

## Acceptance Criteria

The first version is complete when:

- `python video_analysis.py init-db` creates the SQLite schema.
- `python video_analysis.py run --url "<public video url>"` creates file artifacts and a database row.
- `python video_analysis.py run --urls urls.txt` processes multiple links and reports per-video results.
- Existing audio, transcript, and analysis artifacts are reused unless force flags are provided.
- A fake-client test suite passes without network or API credentials.
- Real API credentials remain outside git and are loaded from `.env` or environment variables.

## Out of Scope for First Version

- Browser-based UI.
- Search dashboard.
- Cross-platform media abstraction beyond what the current `yt-dlp` flow naturally supports.
- Automated account crawling beyond retaining compatibility with the existing Douyin account pipeline.
- Domain-specific finance, sentiment, course-note, or knowledge-base extraction beyond the generic `extensions` field.
