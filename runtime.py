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

import json
import logging
import os
import re
import sys
from pathlib import Path
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


# ---------------------------------------------------------------------------
# The .env file and the credentials in it
# ---------------------------------------------------------------------------
#
# Every script reads the same .env, so the parsing lives here rather than in
# five copies: a file that authenticates one agent has to authenticate all of
# them, and that only holds if they all read it the same way.

_COMMENT_TAIL = re.compile(r"\s+#.*$")

# The variables each backend's key is read from, in the order they are tried, so
# a failure can name them rather than leave the reader to guess which spelling
# was expected.
KEY_VARIABLES: Dict[str, Tuple[str, ...]] = {
    "azure": ("AZURE_OPENAI_API_KEY", "AZURE_API_KEY", "OPENAI_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
}

# Measured, not guessed: the 4,543-line run of September 2026 cost $7.24 across
# the four agents, all but two cents of it Agent 1, which reads every line. The
# figure is here so that the cost of a run over the full extract can be stated
# before it starts, a hundred thousand lines being rather more than anybody
# should meet by surprise on an invoice.
ESTIMATED_COST_PER_ROW = 0.0016


def parse_dotenv(path: Path) -> Dict[str, str]:
    """Parse a ``.env`` file into a dictionary; later assignments win.

    Written by hand rather than pulled from a package because the format is
    trivial and the agents are expected to run on machines where installing an
    extra dependency needs an approval. Later assignments winning matches the
    shell and python-dotenv, which matters because the working ``.env`` on this
    project defines one key twice.

    Three concessions to how the file is written in practice, each of which has
    the same failure mode if it is not made: the file looks correct and a
    variable in it silently does not exist.

    It is read as ``utf-8-sig``, because an editor on Windows can save with a
    byte order mark, and the mark would otherwise be read as the first three
    characters of the first key's name. ``export`` and ``set`` prefixes are
    dropped, both of which arrive when lines are copied out of shell notes. And
    an unquoted trailing comment is treated as a comment, as python-dotenv does,
    which is what stops ``KEY=abc  # the PwC key`` from sending the words after
    the hash to the endpoint as part of the credential.
    """
    values: Dict[str, str] = {}
    if not Path(path).is_file():
        return values

    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        lowered = line.lower()
        if lowered.startswith("export ") or lowered.startswith("set "):
            line = line.partition(" ")[2].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            # One matched pair of quotes; anything else is part of the value.
            value = value[1:-1]
        else:
            # A hash with no space in front of it is a character in the value,
            # which is how a key or a password is allowed to contain one.
            value = _COMMENT_TAIL.sub("", value).strip()
        if key:
            values[key] = value
    return values


# The variables a real environment is allowed to override the file with. Listed
# rather than taking all of os.environ, so that an unrelated variable of the same
# shape cannot reach the model configuration.
MODEL_ENV_NAMES: Tuple[str, ...] = (
    "AZURE_ENABLE", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
    "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_BASE_URL", "AZURE_OPENAI_MODEL",
    "AZURE_API_KEY", "AZURE_BASE_URL", "BASE_URL", "MODEL_NAME",
    "LLM_BATCH_SIZE", "LLM_TIMEOUT", "LLM_MAX_REQUESTS", "LLM_REASONING_EFFORT",
    "LLM_SPEND_LIMIT", "LLM_INPUT_COST_PER_MTOK", "LLM_OUTPUT_COST_PER_MTOK",
)


def model_environment(env_path: Path) -> Dict[str, str]:
    """The ``.env`` file, with real environment variables laid over it.

    Real variables win, which is what allows a scheduled job to override a
    developer's local settings. Empty ones do not: an exported variable with
    nothing in it is how a shell remembers that a name was mentioned, not an
    instruction to forget the key in the file. Letting it win means
    ``set OPENAI_API_KEY=`` on Windows, or an unset variable in a wrapper
    script, blanks a perfectly good credential and the run reports that no key
    was found while looking straight at one.
    """
    env = parse_dotenv(env_path)
    for name in MODEL_ENV_NAMES:
        value = os.environ.get(name)
        if value is not None and value.strip():
            env[name] = value
    return env


def env_flag(value: Optional[str], default: bool = False) -> bool:
    """Interpret the usual spellings of a boolean environment variable."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"}


def resolve_credentials(env: Dict[str, str]) -> Dict[str, str]:
    """Work out which endpoint to call and with what, from the environment.

    ``AZURE_ENABLE`` chooses between the PwC GenAI shared service and the public
    OpenAI API. The two credential sets are read from separate variables so both
    can be configured at once. The direct-OpenAI branch deliberately refuses to
    inherit ``BASE_URL``: that variable points at the shared service on this
    project, and inheriting it would transmit a personal OpenAI key to an
    internal endpoint.

    Shared by the agents and by the preflight check in ``all_agents.py``, which
    is the point. A preflight that resolved the endpoint even slightly
    differently from the agent it is vouching for would be worse than none: it
    would report a working connection the agent then fails to make.
    """
    if env_flag(env.get("AZURE_ENABLE"), False):
        return {
            "backend": "azure",
            "api_key": (env.get("AZURE_OPENAI_API_KEY")
                        or env.get("AZURE_API_KEY")
                        or env.get("OPENAI_API_KEY")
                        or ""),
            "base_url": (env.get("AZURE_OPENAI_BASE_URL")
                         or env.get("AZURE_BASE_URL")
                         or env.get("BASE_URL")
                         or "https://genai-sharedservice-emea.pwcinternal.com"
                            "/v1/chat/completions"),
            "model": (env.get("AZURE_OPENAI_MODEL")
                      or env.get("MODEL_NAME")
                      or DEFAULT_AZURE_MODEL),
            "reasoning_effort": (env.get("LLM_REASONING_EFFORT")
                                 or DEFAULT_REASONING_EFFORT).strip().lower(),
        }
    return {
        "backend": "openai",
        "api_key": env.get("OPENAI_API_KEY") or "",
        "base_url": env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1",
        "model": env.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL,
        "reasoning_effort": (env.get("LLM_REASONING_EFFORT")
                             or DEFAULT_REASONING_EFFORT).strip().lower(),
    }


def chat_endpoint(base_url: str) -> str:
    """Full chat-completions URL.

    The shared service is usually configured with the complete endpoint while
    the public API is configured with the ``/v1`` root, so both spellings are
    accepted and normalised here rather than at each call site.
    """
    url = (base_url or "").rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    return f"{url}/chat/completions"


def missing_key_message(backend: str, env_path: Path) -> str:
    """Explain that the model was asked for and no credential was found.

    This used to be a warning, and the run continued on the local stack. That
    is the wrong default at this size: every call site falls back to a
    deterministic answer, so the output has all its columns and looks finished,
    and the only sign that the model never ran is that the descriptions are
    phrases rather than sentences. One such run over the full extract took
    hours and had to be thrown away. Refusing to start costs a second.
    """
    variables = ", ".join(KEY_VARIABLES.get(backend, ("OPENAI_API_KEY",)))
    lines = [
        f"The language model is switched on and no API key was found for the "
        f"{backend} backend, so there is nothing to authenticate with.",
        "",
        f"Looked for {variables}",
        f"  in {env_path}",
        "  and in the environment.",
        "",
    ]
    if backend == "azure":
        lines += [
            "AZURE_ENABLE is on, so the key wanted is the one for the PwC GenAI "
            "shared service. A .env for that machine reads:",
            "",
            "  AZURE_ENABLE=true",
            "  AZURE_OPENAI_API_KEY=<the shared-service key>",
            "  AZURE_OPENAI_MODEL=openai.eu.gpt-5.6.luna",
            "  AZURE_OPENAI_BASE_URL=https://genai-sharedservice-emea."
            "pwcinternal.com/v1/chat/completions",
        ]
    else:
        lines += [
            "AZURE_ENABLE is off or absent, so the key wanted is a personal "
            "OpenAI one and requests would go to api.openai.com. That host is "
            "usually unreachable from a corporate network: if this is the PwC "
            "machine, the line missing from .env is AZURE_ENABLE=true, which "
            "switches to the shared service.",
        ]
    lines += [
        "",
        "Add the key, or pass --no-llm to run on the local stack alone.",
    ]
    return "\n".join(lines)


def probe_chat_endpoint(endpoint: str, api_key: str, model: str,
                        timeout: int = 30) -> Optional[str]:
    """Send the smallest useful request. Returns None when it worked.

    Worth a request of its own before any work starts. A key can be present and
    still be rejected, revoked or pointed at a deployment that does not exist,
    and because every call site treats no answer as "the model had nothing to
    add", the run completes with the AI columns filled by the fallback. Finding
    that out at the end costs the whole run; finding it out here costs a second.

    The return value is a sentence naming what went wrong, ready to print.
    """
    import urllib.error
    import urllib.request

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        # The shared service authenticates by api-key rather than by bearer
        # token. Sending both is accepted by each and saves branching.
        "api-key": api_key,
    }
    body = chat_completion_body(
        model, 'Reply with the JSON object {"ok": true} and nothing else.', "ok")

    def send(payload: Dict[str, Any]) -> Tuple[int, str]:
        request = urllib.request.Request(
            endpoint, data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as handle:
                return handle.status, handle.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", errors="replace")

    try:
        status, text = send(body)
        retry = retry_chat_body(status, text, body)
        if retry is not None:
            # The endpoint refused a generation parameter rather than the
            # request. The clients handle that themselves; it is not a failure.
            status, text = send(retry)
    except Exception as error:  # noqa: BLE001 - the reason is what is wanted
        detail = str(error)
        if "CERTIFICATE_VERIFY_FAILED" in detail or "certificate" in detail.lower():
            return (f"the endpoint could not be verified ({detail}). Install "
                    f"truststore so Python trusts the same certificates the "
                    f"browser does: pip install truststore")
        return f"the endpoint could not be reached ({detail})"

    if status == 200:
        return None
    summary = " ".join((text or "")[:300].split())
    if status in (401, 403):
        return (f"the endpoint rejected the key with HTTP {status}. The key is "
                f"present but not accepted: {summary}")
    if status == 404:
        return (f"the endpoint has no deployment named {model!r} (HTTP 404). "
                f"Check the model name: {summary}")
    if status == 429:
        return (f"the endpoint is rate limiting before the run has begun "
                f"(HTTP 429): {summary}")
    return f"the endpoint answered HTTP {status}: {summary}"


class SpendLimitReached(SystemExit):
    """The authorised model spend was reached and nobody could be asked.

    Derived from SystemExit rather than Exception on purpose. The agents wrap
    row processing in broad handlers so that one malformed line cannot stop a
    run, and an exception raised here would be swallowed by one of them and the
    run would continue with the model off. SystemExit is not an Exception, so it
    passes through those handlers, prints its message and stops the run.
    """


def estimated_model_cost(rows: int) -> float:
    """What the four agents are likely to spend reading this many lines."""
    return max(0, rows) * ESTIMATED_COST_PER_ROW


def budget_for_rows(rows: int, floor: float) -> float:
    """A budget that covers this many lines, rounded to a tidy figure.

    A quarter more than the estimate, because the measured rate came from one
    dataset and the repair pass fires on the lines that need it, not on a fixed
    share of them.
    """
    covering = estimated_model_cost(rows) * 1.25
    if covering <= floor:
        return floor
    step = 5.0 if covering < 100 else 25.0
    return float(int(covering / step + 0.999) * step)
