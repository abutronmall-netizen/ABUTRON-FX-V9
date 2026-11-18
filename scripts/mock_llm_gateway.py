#!/usr/bin/env python3
"""Mock OpenAI-compatible LLM gateway for staging validation.

Speaks the standard ``/v1/chat/completions`` protocol that the V9
Gemma + Debate advisors expect. It responds to every request with a
well-formed JSON advisory so the ``LLMReasoningBackend`` and the
multi-agent debate path can be validated end-to-end without a real
LLM model or external API.

This is a **staging test fixture** — it produces deterministic,
advisory-only responses. It never places orders and touches no
production services. Run it standalone (``python3 scripts/
mock_llm_gateway.py [port]``) or import ``MockLLMGateway`` to run it
in-process as a threaded HTTP server.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional


class _MockHandler(BaseHTTPRequestHandler):
    """HTTP handler for the mock OpenAI-compatible gateway."""

    # Silence default logging for clean test output.
    def log_message(self, fmt, *args):  # noqa: A003
        pass

    server_version = "MockLLMGateway/1.0"

    def do_POST(self):  # noqa: N802
        """Handle POST /v1/chat/completions (OpenAI-compatible)."""
        if self.path.rstrip("/") not in ("/v1/chat/completions",
                                         "/chat/completions"):
            self.send_error(404, "not found")
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            payload = {}

        # Determine the response based on the system prompt / role.
        messages = payload.get("messages", [])
        system_prompt = ""
        user_prompt = ""
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if role == "system":
                system_prompt = content
            elif role == "user":
                user_prompt = content

        response = _build_advisory(system_prompt, user_prompt, payload)
        body = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        """Health check endpoint."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b'{"status":"ok"}')))
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')


def _build_advisory(system_prompt: str, user_prompt: str,
                    payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build a deterministic advisory JSON response.

    The mock inspects the system prompt to decide whether this is a
    single-advisor call, a debate agent call, or the manager synthesis,
    and returns an appropriate signal/confidence/narrative.
    """
    model = payload.get("model", "mock-model")
    sp_lower = (system_prompt or "").lower()

    # Manager synthesis: aggregate debate into a final call.
    if "research manager" in sp_lower or "manager" in sp_lower:
        content = json.dumps({
            "signal": "BUY",
            "confidence": 0.62,
            "narrative": "Manager synthesis: bull case dominant; "
                         "conviction supported by trend + orderflow.",
        })
        return _openai_response(model, content)

    # Risk debators: conservative stance, moderate confidence.
    if "risk" in sp_lower or "aggressive" in sp_lower or "conservative" in sp_lower or "neutral" in sp_lower:
        # Conservative/risk roles skew toward caution.
        if "conservative" in sp_lower:
            content = json.dumps({
                "signal": "NEUTRAL",
                "confidence": 0.45,
                "narrative": "Conservative debator: edge thin; "
                             "prefer tight risk management.",
            })
        else:
            content = json.dumps({
                "signal": "BUY",
                "confidence": 0.55,
                "narrative": "Risk debator: direction plausible, "
                             "manage exposure.",
            })
        return _openai_response(model, content)

    # Bull / bear debate agents.
    if "bull" in sp_lower:
        content = json.dumps({
            "signal": "BUY",
            "confidence": 0.70,
            "narrative": "Bull: upward momentum, orderflow supportive, "
                         "breakout risk to the upside.",
        })
        return _openai_response(model, content)
    if "bear" in sp_lower:
        content = json.dumps({
            "signal": "SELL",
            "confidence": 0.50,
            "narrative": "Bear: counter-argument — mean reversion risk, "
                         "spread widening.",
        })
        return _openai_response(model, content)

    # Default: single Gemma advisor call.
    content = json.dumps({
        "signal": "BUY",
        "confidence": 0.65,
        "narrative": "Gemma advisory: trend-aligned signal with "
                     "supportive orderflow; manage risk per gate.",
    })
    return _openai_response(model, content)


def _openai_response(model: str, content: str) -> Dict[str, Any]:
    """Wrap a content string in the OpenAI chat-completion envelope."""
    return {
        "id": "chatcmpl-mock-001",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                  "total_tokens": 0},
    }


class MockLLMGateway:
    """Threaded HTTP server wrapping the mock gateway.

    Use as a context manager for in-process staging validation, or call
    ``start()`` / ``stop()`` for manual control.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._server = ThreadingHTTPServer((host, port), _MockHandler)
        self._thread: Optional[threading.Thread] = None
        self.host = host
        self.port = self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    def start(self) -> str:
        """Start the server in a background thread; return base_url."""
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        """Stop the server."""
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> "MockLLMGateway":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Mock OpenAI-compatible LLM gateway")
    ap.add_argument("--port", type=int, default=11434,
                    help="port to listen on (default 11434)")
    ap.add_argument("--host", default="0.0.0.0",
                    help="host to bind (default 0.0.0.0)")
    args = ap.parse_args()
    gateway = MockLLMGateway(host=args.host, port=args.port)
    url = gateway.start()
    print(f"Mock LLM gateway listening on {url}")
    print(f"  POST {url}/chat/completions  (OpenAI-compatible)")
    try:
        # Keep the server running until interrupted.
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        gateway.stop()
        print("stopped.")


if __name__ == "__main__":
    main()
