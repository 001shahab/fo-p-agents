#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hosted-run behaviour for Hugging Face and third-party loggers.

The embedding and translation models live on the Hugging Face hub. On first
use, and whenever the library checks for updates, it probes every file a chat
model might have. ``chat_template.jinja`` is one of those: MiniLM and opus-mt
do not ship it, so the hub answers 404. ``httpx`` logs that at INFO, which
Render then shows as if the run had failed. It has not - the next request,
``special_tokens_map.json``, is the file the model actually needs and it
returns 200.

This module is imported by every agent and by the harness so that:

    * those HTTP probes are not printed
    * a model that is already in the local cache is loaded without contacting
      the hub at all
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict, Optional, Tuple


_HUB_ENV = {
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_DISABLE_PROGRESS_BARS": "1",
    "HF_HUB_DISABLE_EXPERIMENTAL_WARNING": "1",
    "HF_HUB_VERBOSITY": "error",
    "TRANSFORMERS_VERBOSITY": "error",
    "TOKENIZERS_PARALLELISM": "false",
}

_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "huggingface_hub",
    "huggingface_hub.utils",
    "huggingface_hub.file_download",
    "filelock",
    "urllib3",
    "transformers",
    "sentence_transformers",
    "torch",
)


def prepare_hub_environment() -> None:
    """Set hub environment variables before a model is loaded.

    Safe to call more than once. Child processes inherit the variables, which
    is how ``app.py`` on Render keeps agent subprocesses quiet.
    """
    for name, value in _HUB_ENV.items():
        os.environ.setdefault(name, value)
    # Before anything opens a connection, so the first model download is
    # verified the same way as every one after it.
    use_system_trust_store()


def use_system_trust_store() -> Optional[str]:
    """Verify TLS against the operating system's certificates, if we can.

    Corporate networks re-sign HTTPS with a root of their own, and Python does
    not look in the place that root is installed: it trusts certifi's bundle,
    which knows nothing about it. So the browser reaches the model hub and this
    does not, failing with "self-signed certificate in certificate chain".

    The usual remedy is to export the root by hand, concatenate it onto
    certifi's bundle and set two environment variables - platform-specific
    surgery that has to be repeated on every machine. truststore avoids all of
    it by verifying against the system store, which is where the corporate root
    already is, and is why the browser was fine.

    Optional on purpose. Where it is not installed nothing changes and the
    certifi behaviour applies, so a machine on an ordinary network needs
    nothing. Returns the backend in use, for a caller that wants to say so.
    """
    try:
        import truststore
    except ImportError:
        return None
    try:
        truststore.inject_into_ssl()
    except Exception:
        # Never worth failing a run over: the standard verification still works
        # everywhere the corporate root is not in the way.
        return None
    return "the operating system trust store"


def quiet_third_party_loggers() -> None:
    """Drop hub and HTTP client loggers below INFO.

    ``logging.basicConfig(level=INFO)`` otherwise promotes ``httpx`` to the
    same level as the agent's own progress lines.
    """
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.ERROR)


def configure_process_logging(verbose: bool) -> None:
    """stdout progress for the agent, silence for the hub."""
    prepare_hub_environment()
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    quiet_third_party_loggers()


def load_sentence_transformer(package: Any, name: str) -> Any:
    """Load a SentenceTransformer from cache when possible.

    A cached load never contacts the hub, so it never logs the 404 for
    ``chat_template.jinja``. The first run on a machine still downloads; the
    HTTP probes during that download stay hidden because the loggers are quiet.
    """
    prepare_hub_environment()
    quiet_third_party_loggers()
    try:
        return package.SentenceTransformer(name, local_files_only=True)
    except Exception:
        return package.SentenceTransformer(name)


# ---------------------------------------------------------------------------
# Chat model defaults shared by every agent
# ---------------------------------------------------------------------------

DEFAULT_OPENAI_MODEL = "gpt-5.6-luna"
DEFAULT_AZURE_MODEL = "openai.eu.gpt-5.6.luna"
DEFAULT_REASONING_EFFORT = "low"


def chat_completion_body(model: str, system_prompt: str, user_prompt: str,
                         omit_temperature: bool = False,
                         reasoning_effort: str = DEFAULT_REASONING_EFFORT,
                         reasoning_style: str = "effort") -> Dict[str, Any]:
    """Build a chat-completions JSON body for gpt-5.6-luna.

    ``reasoning_style`` is ``effort`` (OpenAI ``reasoning_effort``), ``nested``
    (Azure ``reasoning.effort``), or ``omit`` after the endpoint has rejected
    the parameter.
    """
    body: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    if not omit_temperature:
        body["temperature"] = 0
    effort = (reasoning_effort or "").strip().lower()
    if effort and effort not in {"none", "off", "0"}:
        if reasoning_style == "nested":
            body["reasoning"] = {"effort": effort}
        elif reasoning_style != "omit":
            body["reasoning_effort"] = effort
    return body


def retry_chat_body(status: int, error_text: str,
                    body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return an alternate body when the model rejects a generation parameter.

    gpt-5 family models commonly refuse ``temperature``. Azure and OpenAI also
    spell the reasoning control differently, so a 400 mentioning reasoning is
    retried with the other spelling before the field is dropped.
    """
    if status != 400:
        return None
    summary = (error_text or "").lower()
    if "temperature" in body and "temperature" in summary:
        return {key: value for key, value in body.items() if key != "temperature"}
    if "reasoning_effort" in body and "reasoning" in summary:
        effort = body["reasoning_effort"]
        alternate = {key: value for key, value in body.items() if key != "reasoning_effort"}
        alternate["reasoning"] = {"effort": effort}
        return alternate
    if isinstance(body.get("reasoning"), dict) and "reasoning" in summary:
        return {key: value for key, value in body.items() if key != "reasoning"}
    return None


def note_reasoning_retry(body: Dict[str, Any], retry: Dict[str, Any]
                         ) -> Tuple[bool, str]:
    """Describe how the client should remember a successful retry shape."""
    omit_temperature = "temperature" in body and "temperature" not in retry
    if "reasoning_effort" in body and "reasoning_effort" not in retry:
        style = "nested" if "reasoning" in retry else "omit"
        return omit_temperature, style
    if "reasoning" in body and "reasoning" not in retry:
        return omit_temperature, "omit"
    return omit_temperature, ""
