"""OpenRouter requests for JSON answers: every AI step of the radio (picking
artists and writing the DJ's words, classifying rosters, filling in track
info, news) goes through ask_llm_json().

Each request and its outcome (which model answered, how long it took, what
was wrong with an unusable answer) is logged under the "llm" component.
"""

import json
import logging
import os
import time

import requests

log = logging.getLogger("llm")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL", "https://github.com/your-username/your-project")
# Seconds a whole OpenRouter request (including reading the answer) may take.
OPENROUTER_TIMEOUT = int(os.getenv("OPENROUTER_TIMEOUT", "60"))
# The same for background work nothing on air waits for (classifying
# artists, filling in track info): long lists take slow models a while.
OPENROUTER_BACKGROUND_TIMEOUT = int(os.getenv("OPENROUTER_BACKGROUND_TIMEOUT", "180"))
# "openrouter/free" routes each request to some free model, which now and
# then is one that can't follow the prompt (e.g. a content-safety classifier
# answering "User Safety: safe"), so unusable answers are retried.
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")
OPENROUTER_ATTEMPTS = int(os.getenv("OPENROUTER_ATTEMPTS", "3"))


def shorten(text, limit=500):
    """Trims long API payloads so a single bad response can't flood the log."""
    text = str(text)
    return text if len(text) <= limit else text[:limit] + f"... ({len(text)} chars)"


def call_openrouter_json(prompt, purpose, timeout=OPENROUTER_TIMEOUT, model=None, avoid=()):
    """Sends a prompt to OpenRouter and parses the response as JSON. The
    prompt must instruct the model to reply with raw JSON. `purpose` only
    labels the request in the logs. `model` asks for a specific model, with
    OpenRouter falling back to OPENROUTER_MODEL if it's unavailable. An
    answer from a model whose name starts with one of `avoid` is rejected
    (as unusable), e.g. small models openrouter/free picks that garble the
    station's language. Every failure is logged (under the "llm"
    component) before being raised."""
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY is missing. Check your .env file.")

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": OPENROUTER_SITE_URL,
        "X-Title": "AI Radio Project"
    }

    payload = {
        "model": model or OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": "You are a precise radio automation agent. You always output valid raw JSON."},
            {"role": "user", "content": prompt}
        ]
    }

    if model and model != OPENROUTER_MODEL:
        payload["models"] = [model, OPENROUTER_MODEL]
    log.info(f"Request ({purpose}), model {payload['model']}, prompt {len(prompt)} chars")
    started = time.monotonic()
    try:
        # requests' timeout only limits the wait for each next chunk, and
        # OpenRouter keeps sending whitespace while a model is still working,
        # so a slow model could hold a request for many minutes. Read the
        # body as it arrives and enforce `timeout` on the total.
        with requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=timeout,
            stream=True,
        ) as response:
            chunks = []
            for chunk in response.iter_content(chunk_size=8192):
                chunks.append(chunk)
                if time.monotonic() - started > timeout:
                    raise requests.Timeout(f"no complete answer within {timeout}s")
            body = b"".join(chunks).decode("utf-8", "replace")
    except requests.RequestException as e:
        log.warning(f"Request ({purpose}) failed after {time.monotonic() - started:.1f}s: {e}")
        raise
    elapsed = time.monotonic() - started

    try:
        result = json.loads(body)
    except ValueError:
        log.warning(
            f"Response ({purpose}) is not JSON: HTTP {response.status_code} after {elapsed:.1f}s: "
            f"{shorten(body)}"
        )
        raise ValueError(f"Failed to parse API response. Status: {response.status_code}, Text: {body}")

    # Check if the response contains the expected 'choices' key
    if 'choices' not in result:
        log.warning(
            f"API error ({purpose}): HTTP {response.status_code} after {elapsed:.1f}s: "
            f"{shorten(json.dumps(result.get('error', result), ensure_ascii=False))}"
        )
        raise KeyError("OpenRouter did not return 'choices'.")

    model = result.get("model", "?")
    if any(model.startswith(prefix) for prefix in avoid):
        log.warning(f"Rejected the answer ({purpose}) from {model}, a model to avoid for this")
        raise ValueError(f"{model} is on the list of models to avoid.")
    usage = result.get("usage") or {}
    log.info(
        f"Response ({purpose}) from {model} in {elapsed:.1f}s "
        f"(tokens: {usage.get('prompt_tokens', '?')} in, {usage.get('completion_tokens', '?')} out)"
    )

    raw_content = (result['choices'][0]['message'].get('content') or "").strip()
    if not raw_content:
        log.warning(f"Empty response ({purpose}) from {model}")
        raise ValueError(f"{model} returned an empty response.")

    # Models sometimes wrap the JSON in a markdown code block or some prose
    # anyway, or follow it with more text or even a second JSON object, so
    # parse the first complete object starting at the first "{". strict=False
    # accepts raw newlines inside strings (e.g. a multi-line DJ script).
    start = raw_content.find("{")
    try:
        parsed, _ = json.JSONDecoder(strict=False).raw_decode(raw_content[max(start, 0):])
    except json.JSONDecodeError as e:
        log.warning(f"Invalid JSON ({purpose}) from {model}: {e}: {shorten(raw_content)}")
        raise
    if not isinstance(parsed, dict):
        log.warning(f"JSON ({purpose}) from {model} is not an object: {shorten(raw_content)}")
        raise ValueError(f"{model} returned JSON that isn't an object.")
    return parsed


def ask_llm_json(prompt, purpose, validate, timeout=OPENROUTER_TIMEOUT, models=(), avoid=()):
    """call_openrouter_json(), retried until `validate(parsed)` accepts the
    answer (it raises ValueError/KeyError on an unusable one) and returns
    what the caller needs from it. Raises the last error if every attempt
    fails.

    `models` are preferred models, tried first, one attempt each, in order;
    then OPENROUTER_ATTEMPTS attempts go to OPENROUTER_MODEL. So preferred
    models that are down, slow or answering badly never eat into the usual
    attempts. Answers from `avoid` models are rejected (see
    call_openrouter_json)."""
    attempts = len(models) + OPENROUTER_ATTEMPTS
    for attempt in range(1, attempts + 1):
        model = models[attempt - 1] if attempt <= len(models) else None
        try:
            return validate(call_openrouter_json(prompt, purpose, timeout, model, avoid))
        except (requests.RequestException, ValueError, KeyError) as e:
            if attempt == attempts:
                raise
            log.info(f"Retrying ({purpose}), attempt {attempt + 1}/{attempts}, after: {e}")


def pick_by_indices(indices, candidates, purpose):
    """Maps the 1-based `selected_indices` an AI returned onto `candidates`,
    logging (and skipping) any that are malformed, out of range or repeated."""
    if not isinstance(indices, list):
        log.warning(f"'selected_indices' ({purpose}) is not a list: {shorten(indices)}")
        return []
    picked, invalid = [], []
    for i in indices:
        if isinstance(i, int) and 0 < i <= len(candidates) and candidates[i - 1] not in picked:
            picked.append(candidates[i - 1])
        else:
            invalid.append(i)
    if invalid:
        log.warning(
            f"Ignored {len(invalid)} invalid/duplicate index(es) ({purpose}) "
            f"out of 1..{len(candidates)}: {shorten(invalid, 200)}"
        )
    return picked
