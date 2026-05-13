#!/usr/bin/env python3
import os
from pathlib import Path

import requests


DEFAULT_GROQ_API_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
DEFAULT_GROQ_ASR_MODEL = "whisper-large-v3-turbo"
PLACEHOLDER_API_KEYS = {"replace_with_your_key", "your_key", "gsk-xxx"}


def load_env_file(env_path=".env"):
    env_path = Path(env_path)
    loaded = {}
    if not env_path.exists():
        return loaded
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


class GroqASRClient:
    def __init__(
        self,
        api_key=None,
        api_url=None,
        model=None,
        language="zh",
        response_format="json",
        timeout=300,
        session=None,
    ):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY", "")
        if not self.api_key or self.api_key in PLACEHOLDER_API_KEYS:
            raise RuntimeError("GROQ_API_KEY is required for Groq Whisper ASR")
        self.api_url = api_url or os.environ.get("GROQ_API_URL", DEFAULT_GROQ_API_URL)
        self.model = model or os.environ.get("GROQ_ASR_MODEL", DEFAULT_GROQ_ASR_MODEL)
        self.language = language or os.environ.get("GROQ_ASR_LANGUAGE", "zh")
        self.response_format = response_format
        self.timeout = timeout
        self.session = session or requests

    def transcribe(self, audio_path, prompt=None):
        audio_path = Path(audio_path)
        if not audio_path.exists():
            raise FileNotFoundError(audio_path)

        headers = {"Authorization": f"Bearer {self.api_key}"}
        data = {
            "model": self.model,
            "language": self.language,
            "response_format": self.response_format,
        }
        if prompt:
            data["prompt"] = prompt

        with audio_path.open("rb") as audio_file:
            files = {"file": (audio_path.name, audio_file)}
            resp = self.session.post(
                self.api_url,
                headers=headers,
                data=data,
                files=files,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        text = payload.get("text", "")
        if not text.strip():
            raise RuntimeError(f"ASR returned empty text for {audio_path}")
        return text.strip()
