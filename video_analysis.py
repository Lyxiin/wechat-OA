#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path

import asr_client
import stock_extractor
from video_analysis_core import (
    DEFAULT_COOKIES_FILE,
    DEFAULT_DB,
    DEFAULT_MODEL,
    DEFAULT_OUTPUT_DIR,
    ContentAnalyzer,
    PipelineRunner,
    VideoAnalysisStore,
    VideoSourceReader,
    YtDlpVideoDownloader,
)


def command_init_db(args):
    store = VideoAnalysisStore(output_dir=args.output_dir, db_path=args.db)
    try:
        store.initialize_db()
    finally:
        store.close()
    print(f"Video analysis database initialized: {Path(args.db).resolve()}")


def command_run(args):
    if args.env_file:
        asr_client.load_env_file(args.env_file)
        stock_extractor.load_env_file(args.env_file)
    urls = VideoSourceReader.read_sources(url=args.url, urls_file=args.urls)
    store = VideoAnalysisStore(output_dir=args.output_dir, db_path=args.db)
    try:
        store.initialize_db()
        downloader = YtDlpVideoDownloader(cookies_file=args.cookies_file)
        asr = asr_client.GroqASRClient()
        analyzer = ContentAnalyzer(model=args.model or os.environ.get("LLM_MODEL", DEFAULT_MODEL))
        runner = PipelineRunner(store=store, downloader=downloader, asr=asr, analyzer=analyzer)
        result = runner.run_urls(
            urls,
            force_download=args.force_download,
            force_transcribe=args.force_transcribe,
            force_analyze=args.force_analyze,
        )
    finally:
        store.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def build_parser():
    parser = argparse.ArgumentParser(
        description="Analyze publicly shared video content by extracting audio, transcribing, and summarizing."
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--env-file", default=".env")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-db", help="Create the video analysis SQLite schema.")
    init_parser.add_argument("--db", default=str(DEFAULT_DB))
    init_parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    init_parser.set_defaults(func=command_init_db)

    run_parser = subparsers.add_parser("run", help="Analyze one video URL or a URL list file.")
    run_parser.add_argument("--url", help="One public video or share URL.")
    run_parser.add_argument("--urls", help="Text file containing one public video URL per line.")
    run_parser.add_argument("--cookies-file", default=str(DEFAULT_COOKIES_FILE))
    run_parser.add_argument("--model", default=None)
    run_parser.add_argument("--force-download", action="store_true")
    run_parser.add_argument("--force-transcribe", action="store_true")
    run_parser.add_argument("--force-analyze", action="store_true")
    run_parser.set_defaults(func=command_run)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
