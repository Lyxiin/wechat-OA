import os
import shutil
import unittest
from pathlib import Path

import asr_client


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload or {"text": "识别文本"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, headers=None, data=None, files=None, timeout=None):
        self.calls.append({
            "url": url,
            "headers": headers,
            "data": data,
            "files": files,
            "timeout": timeout,
        })
        return FakeResponse()


class AsrClientTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / ".test_tmp" / "asr_client"
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.audio = self.root / "sample.mp3"
        self.audio.write_bytes(b"fake audio bytes")

    def tearDown(self):
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_groq_client_rejects_missing_api_key(self):
        old_value = os.environ.get("GROQ_API_KEY")
        try:
            os.environ.pop("GROQ_API_KEY", None)

            with self.assertRaisesRegex(RuntimeError, "GROQ_API_KEY"):
                asr_client.GroqASRClient()
        finally:
            if old_value is not None:
                os.environ["GROQ_API_KEY"] = old_value

    def test_groq_client_posts_audio_to_transcription_endpoint(self):
        session = FakeSession()
        client = asr_client.GroqASRClient(api_key="gsk-test", session=session)

        text = client.transcribe(self.audio)

        self.assertEqual(text, "识别文本")
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["url"], "https://api.groq.com/openai/v1/audio/transcriptions")
        self.assertEqual(call["headers"]["Authorization"], "Bearer gsk-test")
        self.assertEqual(call["data"]["model"], "whisper-large-v3-turbo")
        self.assertEqual(call["data"]["language"], "zh")
        self.assertEqual(call["data"]["response_format"], "json")
        self.assertIn("file", call["files"])


if __name__ == "__main__":
    unittest.main()
