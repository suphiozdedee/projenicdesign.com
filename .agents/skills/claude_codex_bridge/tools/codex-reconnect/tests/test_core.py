from __future__ import annotations

import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_ROOT))

from codex_reconnect.cli import build_parser
from codex_reconnect.network import (
    DEFAULT_OPENAI_PROBE_URL,
    ProbeResult,
    Readiness,
    classify_readiness,
    probe_https,
    resolve_primary_probe_url,
)
from codex_reconnect.paths import default_state_dir
from codex_reconnect.policy import codex_error_class, full_jitter_delay
from codex_reconnect.protocol import JsonlAppServer, ProtocolError


FAKE_BRIDGE_SERVER = Path(__file__).with_name("fake_bridge_app_server.py")


class NamingTests(unittest.TestCase):
    def test_public_cli_and_state_directory_use_codex_reconnect(self) -> None:
        self.assertEqual(build_parser().prog, "codex-reconnect")
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": temporary}, clear=True):
                self.assertEqual(
                    default_state_dir(), Path(temporary) / "codex-reconnect"
                )

    def test_ccb_runtime_uses_project_scoped_reconnect_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime_dir = Path(temporary) / "provider-runtime" / "codex"
            with mock.patch.dict(
                os.environ,
                {
                    "CCB_SESSION_FILE": str(Path(temporary) / ".ccb-session"),
                    "CODEX_RUNTIME_DIR": str(runtime_dir),
                },
                clear=True,
            ):
                self.assertEqual(default_state_dir(), runtime_dir / "reconnect")


class NetworkTests(unittest.TestCase):
    def test_primary_probe_prefers_active_provider_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = Path(temporary)
            (codex_home / "config.toml").write_text(
                'model_provider = "custom"\n'
                '[model_providers.custom]\n'
                'base_url = "https://provider.example.test/v1"\n',
                encoding="utf-8",
            )
            self.assertEqual(
                resolve_primary_probe_url(
                    codex_home,
                    environment={"OPENAI_BASE_URL": "https://ambient.example.test"},
                ),
                "https://provider.example.test/v1",
            )

    def test_primary_probe_falls_back_to_environment_then_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = Path(temporary)
            self.assertEqual(
                resolve_primary_probe_url(
                    codex_home,
                    environment={"OPENAI_BASE_URL": "https://ambient.example.test"},
                ),
                "https://ambient.example.test",
            )
            self.assertEqual(
                resolve_primary_probe_url(codex_home), DEFAULT_OPENAI_PROBE_URL
            )

    def test_explicit_probe_override_remains_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = Path(temporary)
            (codex_home / "config.toml").write_text(
                'model_provider = "custom"\n'
                '[model_providers.custom]\n'
                'base_url = "https://provider.example.test/v1"\n',
                encoding="utf-8",
            )
            self.assertEqual(
                resolve_primary_probe_url(
                    codex_home,
                    configured_url="https://explicit.example.test/health",
                ),
                "https://explicit.example.test/health",
            )

    def test_http_error_still_proves_https_reachability(self) -> None:
        def fail_with_http(request: object, **kwargs: object) -> object:
            raise urllib.error.HTTPError(
                "https://example.test/probe", 429, "limited", hdrs=None, fp=None
            )

        result = probe_https("https://example.test/probe", open_url=fail_with_http)
        self.assertTrue(result.reachable)
        self.assertEqual(result.status, 429)

    def test_openai_probe_is_authoritative(self) -> None:
        primary = ProbeResult("https://openai.test", True, 405, 0.1)
        public = ProbeResult("https://public.test", False, None, 0.1, "blocked")
        self.assertEqual(classify_readiness(primary, public), Readiness.READY)
        self.assertEqual(
            classify_readiness(
                ProbeResult("https://openai.test", False, None, 0.1, "down"),
                ProbeResult("https://public.test", True, 204, 0.1),
            ),
            Readiness.UPSTREAM_UNAVAILABLE,
        )


class PolicyTests(unittest.TestCase):
    def test_structured_error_class_and_jitter(self) -> None:
        self.assertEqual(
            codex_error_class({"codexErrorInfo": {"responseStreamDisconnected": {}}}),
            "responseStreamDisconnected",
        )
        self.assertEqual(
            codex_error_class({"codexErrorInfo": "serverOverloaded"}),
            "serverOverloaded",
        )
        self.assertEqual(full_jitter_delay(3, random_value=lambda: 0.5), 4.0)


class ProtocolTests(unittest.TestCase):
    def test_installed_shape_handshake(self) -> None:
        with JsonlAppServer([sys.executable, str(FAKE_BRIDGE_SERVER)]) as transport:
            result = transport.initialize(
                client_name="codex-reconnect-test",
                client_version="test",
                experimental_api=True,
            )
        self.assertEqual(result["userAgent"], "fake-bridge/1")

    def test_malformed_jsonl_fails_closed(self) -> None:
        command = [sys.executable, "-c", "print('{not-json', flush=True)"]
        with JsonlAppServer(command) as transport:
            with self.assertRaises(ProtocolError):
                transport.initialize(
                    client_name="codex-reconnect-test",
                    client_version="test",
                    timeout=2,
                )
