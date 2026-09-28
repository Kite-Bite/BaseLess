"""
llm.py - the only module that talks to the LLM provider.

Division of labour (this is the important design point):
    Hindsight REFLECT does the diagnosis. It searches the bank, reads the past
    incident memories and reasons about them. This module does NOT diagnose -
    it takes reflect()'s answer plus the recalled memories and turns them into
    the three fields the UI renders: diagnosis, recalled_memories and
    confidence_note.

Why that split? It means the demo's intelligence is demonstrably Hindsight's.
The LLM is a formatter; if you take the same bank away the diagnosis visibly
gets worse, which is the whole point of the memory on/off comparison.

The provider is swappable via .env - any hosted OpenAI-compatible endpoint
(OpenAI, Gemini, Groq, OpenRouter, vLLM...). Only LLM_BASE_URL / LLM_API_KEY /
LLM_MODEL change, never the code.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")

# Request/timeout budget. The LLM is formatting only, so it should be fast; if
# it isn't, the diagnosis from reflect() is still worth showing without it.
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "45"))
LLM_MAX_RETRIES = 1  # one retry, on format errors and transient provider errors
RETRY_BACKOFF = 3.0  # seconds to wait before the one retry

_client: AsyncOpenAI | None = None


class LLMError(Exception):
    """Raised when the provider is down, rate-limited, or returns junk.

    main.py degrades gracefully on this: the diagnosis falls back to
    reflect()'s raw text plus a confidence note saying the formatter was
    unavailable, so the demo never hard-fails on the LLM.
    """


def get_client() -> AsyncOpenAI:
    """Shared AsyncOpenAI client (async so it can run alongside Hindsight calls)."""
    global _client
    if not LLM_BASE_URL or not LLM_API_KEY or not LLM_MODEL:
        raise LLMError(
            "LLM_BASE_URL / LLM_API_KEY / LLM_MODEL missing - copy .env.example to .env"
        )
    if _client is None:
        _client = AsyncOpenAI(
            base_url=LLM_BASE_URL,
            api_key=LLM_API_KEY,
            timeout=LLM_TIMEOUT,
            max_retries=0,  # we do our own retry, on format errors only
        )
    return _client


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------
# Two jobs for the model: (1) compress reflect()'s reasoning into a tight
# on-call answer, (2) judge how much weight the memory actually carries. We
# ask for JSON so the UI can render sections, and we validate it ourselves
# because "OpenAI-compatible" does not guarantee "follows the schema".
_SYSTEM_PROMPT = """You are the formatting layer of Blameless, an on-call incident analyst \
that remembers past incidents. Hindsight has already produced the diagnosis by reasoning over \
a memory bank of past incidents. Your job is NOT to diagnose from scratch and NOT to invent \
evidence - it is to present that reasoning clearly for an engineer mid-incident.

Rules:
- Ground everything in the provided reflect analysis and memories. Never invent a cause, a \
log line, or a past incident that is not in the input.
- Blameless postmortem style: discuss systems, configuration, and process. Never attribute \
cause to a person, and never use words like "forgot", "careless" or "negligent".
- Be concrete and short. An on-call engineer needs what to check next, not an essay.
- If the memory provides no relevant prior incident, say so plainly and give a generic \
first-response checklist instead of pretending there is a precedent.

Return ONLY a JSON object with exactly these keys:
{
  "diagnosis": "2-5 sentences: most likely root cause, why the evidence fits, and the \
concrete fix. Use \\n\\n between the root-cause and the fix.",
  "confidence_note": "1-2 sentences on how much weight the memory carries, explicitly \
naming which past incidents were used, or saying the bank held no relevant precedent.",
  "suspected_causes": ["ordered list of 2-4 candidate causes, most likely first"]
}"""


def _build_user_prompt(
    title: str,
    symptoms: str,
    system_name: str,
    reflect_text: str,
    memories: list[dict[str, Any]],
    memory_enabled: bool,
) -> str:
    """Assemble the user-side prompt from the incident and the Hindsight output."""
    if memory_enabled and memories:
        memory_block = "\n\n".join(
            f"[{i + 1}] (document={m.get('document_id') or 'unknown'}, "
            f"severity={m.get('metadata', {}).get('severity', '?')}, "
            f"system={m.get('metadata', {}).get('system', '?')})\n{m.get('text', '')}"
            for i, m in enumerate(memories[:8])
        )
        memory_section = f"RECALLED PAST INCIDENTS FROM MEMORY ({len(memories)}):\n{memory_block}"
    else:
        memory_section = (
            "RECALLED PAST INCIDENTS FROM MEMORY: none. The memory bank is empty or holds "
            "nothing relevant to these symptoms. You have no precedent to reason from."
        )

    return f"""NEW INCIDENT
title: {title}
system: {system_name}

SYMPTOMS (as reported by the on-call engineer):
{symptoms}

{memory_section}

HINDSIGHT REFLECT ANALYSIS (the diagnosis, produced by reasoning over the bank above):
{reflect_text or "(Hindsight returned no analysis - answer from first principles only)"}

Format this into the JSON object described in your instructions."""


# --------------------------------------------------------------------------
# JSON extraction / validation
# --------------------------------------------------------------------------
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json(raw: str) -> dict[str, Any]:
    """Pull a JSON object out of a model response.

    Hosted providers wrap JSON in prose, fence it in ```json, or prefix it with
    "Here is the JSON:". Rather than trusting the provider to be tidy, we strip
    fences, find the outermost braces, and fall back to the first balanced
    object. Raises LLMError if nothing parses - the caller retries.
    """
    text = (raw or "").strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()

    start = text.find("{")
    if start == -1:
        raise LLMError("no JSON object in response")

    # Walk braces to find the matching close, ignoring braces inside strings.
    depth, in_str, escaped = 0, False, False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start : idx + 1])
                except json.JSONDecodeError as exc:
                    raise LLMError(f"invalid JSON: {exc}") from exc
                if not isinstance(parsed, dict):
                    raise LLMError("parsed JSON is not an object")
                return parsed

    raise LLMError("unbalanced JSON object in response")


def _validate(parsed: dict[str, Any]) -> dict[str, Any]:
    """Coerce a parsed object into the shape the UI expects.

    Missing/odd fields become sensible defaults instead of an error - a
    diagnosis with a missing confidence note is far more useful to an on-call
    engineer than no diagnosis at all. Only a missing diagnosis is fatal.
    """
    diagnosis = parsed.get("diagnosis")
    if not isinstance(diagnosis, str) or not diagnosis.strip():
        raise LLMError("response missing 'diagnosis'")

    causes = parsed.get("suspected_causes")
    if isinstance(causes, str):
        causes = [c.strip() for c in causes.split("\n") if c.strip()]
    if not isinstance(causes, list):
        causes = []

    confidence = parsed.get("confidence_note")
    if not isinstance(confidence, str) or not confidence.strip():
        confidence = "Confidence not stated by the formatter."

    return {
        "diagnosis": diagnosis.strip(),
        "confidence_note": confidence.strip(),
        "suspected_causes": [str(c).strip() for c in causes if str(c).strip()][:5],
    }


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def _is_retryable(exc: Exception) -> bool:
    """Transient provider problems are worth one more shot; config errors are not.

    A 404 (model retired) or 401 (bad key) will fail identically forever, so
    retrying just delays a clear error message.
    """
    text = str(exc)
    if "NotFoundError" in type(exc).__name__ or " 404" in text:
        return False
    if "AuthenticationError" in type(exc).__name__ or " 401" in text or " 403" in text:
        return False
    return True


async def format_diagnosis(
    title: str,
    symptoms: str,
    system_name: str,
    reflect_text: str,
    memories: list[dict[str, Any]],
    memory_enabled: bool,
) -> dict[str, Any]:
    """Turn Hindsight's analysis into {diagnosis, confidence_note, suspected_causes}.

    Retries ONCE on a format error (unparseable JSON / missing field) with the
    error appended to the prompt - providers are stochastic, so one retry is
    usually enough. Also retries once on a transient provider failure (429/503/
    timeout) after a short backoff, because those are the common real-world
    case and a formatter outage should not cost the demo its structured output.
    Config errors (401/403/404) are not retried; they raise immediately.
    """
    client = get_client()
    user_prompt = _build_user_prompt(
        title, symptoms, system_name, reflect_text, memories, memory_enabled
    )
    last_error: Exception | None = None

    for attempt in range(LLM_MAX_RETRIES + 1):
        attempt_prompt = user_prompt
        if isinstance(last_error, LLMError):
            # Format problem: tell the model what went wrong, then retry once.
            attempt_prompt = (
                f"{user_prompt}\n\nYour previous response could not be parsed: {last_error}. "
                "Reply with ONLY the raw JSON object, no prose and no code fences."
            )
        try:
            response = await client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": attempt_prompt},
                ],
                temperature=0.2,  # low: this is formatting grounded text, not ideation
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or ""
            return _validate(extract_json(raw))
        except LLMError as exc:
            last_error = exc
            print(f"[llm] format error (attempt {attempt + 1}): {exc}", flush=True)
            continue
        except Exception as exc:  # noqa: BLE001 - network, auth, rate limit, etc.
            if attempt == 0 and _is_retryable(exc):
                print(f"[llm] transient provider error, retrying once: {type(exc).__name__}", flush=True)
                await asyncio.sleep(RETRY_BACKOFF)
                last_error = exc
                continue
            raise LLMError(_friendly_error(exc)) from exc

    raise LLMError(f"LLM returned unusable output after {LLM_MAX_RETRIES + 1} attempts: {last_error}")


def _friendly_error(exc: Exception) -> str:
    """Turn an SDK exception into something worth reading at 3am."""
    name = type(exc).__name__
    text = str(exc)
    if "RateLimit" in name or "429" in text:
        return f"LLM provider rate-limited the request ({name}). Try again shortly."
    if "APIStatusError" in name and ("401" in text or "403" in text):
        return f"LLM provider rejected the API key ({name}). Check LLM_API_KEY."
    if "APIConnectionError" in name or "Connect" in name:
        return f"Could not reach the LLM provider at {LLM_BASE_URL} ({name})."
    if isinstance(exc, asyncio.TimeoutError) or "Timeout" in name:
        return f"LLM provider timed out after {LLM_TIMEOUT}s."
    return f"LLM provider error ({name}): {text[:200]}"


async def close_client() -> None:
    """Close the OpenAI httpx client on shutdown."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None
