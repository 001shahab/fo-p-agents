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
from typing import Any


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
