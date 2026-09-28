"""
memory.py - THE ONLY MODULE THAT TALKS TO HINDSIGHT.

Everything Blameless knows lives in a Hindsight bank ("memory bank"). This file
is the single wrapper around the Hindsight Python client so that the rest of the
app (main.py, llm.py) never imports `hindsight_client` directly. That gives us
one place to log, time, trace and error-handle every memory operation - and one
place to explain in a talk.

The four Hindsight primitives Blameless uses
---------------------------------------------
1. create_bank()  -> (startup) declare the bank's mission + disposition once.
2. retain()       -> (learning loop) write a resolved incident into memory.
3. recall()       -> (diagnosis) fetch raw past memories relevant to symptoms.
4. reflect()      -> (diagnosis) ask Hindsight to *reason* over those memories
                      and answer a natural-language question. This is the main
                      diagnosis path: Hindsight does the thinking, the LLM in
                      llm.py only formats the answer for humans.

Why async (`a*`) methods?
-------------------------
The sync helpers (client.retain/recall/reflect) are implemented with
`loop.run_until_complete()`. If you call them from inside a running event loop
(e.g. an `async def` FastAPI route) Python raises "event loop is already
running". FastAPI's async routes DO run in a loop, so we use the async
counterparts (aretain/arecall/areflect) and run them concurrently with the
OpenAI call. See https://docs.hindsight.vectorize.io/sdks/python
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from hindsight_client import Hindsight

load_dotenv()

# --------------------------------------------------------------------------
# Configuration (all swappable via .env - no code changes to change providers)
# --------------------------------------------------------------------------
HINDSIGHT_BASE_URL = os.getenv("HINDSIGHT_BASE_URL", "")
HINDSIGHT_API_KEY = os.getenv("HINDSIGHT_API_KEY", "")
HINDSIGHT_BANK_ID = os.getenv("HINDSIGHT_BANK_ID", "blameless")

# Seconds before we give up on a Hindsight call. Hindsight Cloud is a network
# service; a slow call should degrade the demo, not hang it.
HINDSIGHT_TIMEOUT = float(os.getenv("HINDSIGHT_TIMEOUT", "60"))

# How many recent calls to keep for the GET /trace panel (the "Memory Trace"
# side panel in the UI). Small ring buffer: enough for a demo, bounded memory.
TRACE_LIMIT = 200

# The bank's identity. `mission` is a standing instruction Hindsight keeps in
# mind on every retain/reflect; `disposition` shapes how skeptical/literal/
# empathetic it is. Skepticism 4 + literalism 3 keeps it evidence-driven
# (an incident report is evidence, not a vibe). Empathy is deliberately low:
# this is an on-call analyst, not a therapist.
BANK_MISSION = (
    "I am an on-call incident analyst. I remember past incidents, their root "
    "causes and fixes, and use them to diagnose new ones. I focus on systemic "
    "causes, never on blaming people."
)
BANK_DISPOSITION = {"skepticism": 4, "literalism": 3, "empathy": 2}


class HindsightError(Exception):
    """Raised when Hindsight is unreachable/times out/returns an error.

    main.py catches this and turns it into a friendly HTTP 503 so the UI can
    show "memory is down" instead of a stack trace.
    """


# --------------------------------------------------------------------------
# Memory Trace
# --------------------------------------------------------------------------
# Every retain/recall/reflect call is appended here. The console print gives you
# a live log in the terminal; GET /trace serves the same data to the browser so
# the "Memory Trace" panel animates during the demo. Entry shape:
#   {"seq", "op", "bank_id", "query", "results", "latency_ms", "ok", "error",
#    "ts", "extra"}
_TRACE: deque[dict[str, Any]] = deque(maxlen=TRACE_LIMIT)
_TRACE_SEQ = 0


def _log(entry: dict[str, Any]) -> dict[str, Any]:
    """Console-print a trace entry and push it onto the in-memory ring buffer."""
    global _TRACE_SEQ
    _TRACE_SEQ += 1
    entry = {"seq": _TRACE_SEQ, "ts": datetime.now(timezone.utc).isoformat(), **entry}
    _TRACE.append(entry)

    if entry["ok"]:
        extra = f" {entry.get('extra') or ''}".rstrip()
        print(
            f"[memory #{entry['seq']}] {entry['op'].upper():<7} "
            f"results={entry['results']:<3} {entry['latency_ms']:>7.0f}ms "
            f"bank={entry['bank_id']}{extra}\n"
            f"           query: {entry['query'][:140]}",
            flush=True,
        )
    else:
        print(
            f"[memory #{entry['seq']}] {entry['op'].upper():<7} FAILED after "
            f"{entry['latency_ms']:.0f}ms: {entry['error']}",
            flush=True,
        )
    return entry


def get_trace(limit: int = 50) -> list[dict[str, Any]]:
    """Recent Hindsight calls, newest first. Backs the UI Memory Trace panel."""
    items = list(_TRACE)
    return list(reversed(items))[:limit]


def clear_trace() -> None:
    """Wipe the trace (used when switching banks for the memory-off demo)."""
    _TRACE.clear()


class BlamelessMemory:
    """Thin async wrapper around one Hindsight bank.

    One instance per process; swap `bank_id` at runtime for the
    "Memory OFF" demo (an empty bank) via `use_bank()`.
    """

    def __init__(self) -> None:
        if not HINDSIGHT_BASE_URL or not HINDSIGHT_API_KEY:
            raise HindsightError(
                "HINDSIGHT_BASE_URL / HINDSIGHT_API_KEY missing - copy .env.example to .env"
            )
        # timeout= is per-request; our asyncio.wait_for adds a hard ceiling too.
        self._client = Hindsight(
            base_url=HINDSIGHT_BASE_URL,
            api_key=HINDSIGHT_API_KEY,
            timeout=HINDSIGHT_TIMEOUT,
            user_agent="blameless/0.1.0",
        )
        self.bank_id = HINDSIGHT_BANK_ID
        self.memory_enabled = True
        self._lock = asyncio.Lock()  # serialize create/seed so startup can't race

    # -- bank management ---------------------------------------------------
    async def create_bank(self) -> None:
        """PRIMITIVE 1 - create_bank().

        Creates the bank if it does not exist, with the Blameless mission and
        disposition. Called on startup. If the bank already exists Hindsight
        returns 409 and we swallow it - banks are persistent by design, that is
        the whole point: memory survives restarts.
        """
        async with self._lock:
            start = time.perf_counter()
            try:
                async with asyncio.timeout(HINDSIGHT_TIMEOUT):
                    await self._client.acreate_bank(
                        bank_id=self.bank_id,
                        name="Blameless",
                        mission=BANK_MISSION,
                        disposition=BANK_DISPOSITION,
                    )
                _log(
                    {
                        "op": "create_bank",
                        "bank_id": self.bank_id,
                        "query": f"mission='{BANK_MISSION[:60]}...'",
                        "results": 1,
                        "latency_ms": (time.perf_counter() - start) * 1000,
                        "ok": True,
                        "error": None,
                        "extra": "created",
                    }
                )
            except Exception as exc:  # noqa: BLE001 - 409 "already exists" is fine
                msg = str(exc)
                already = "409" in msg or "already exists" in msg.lower()
                _log(
                    {
                        "op": "create_bank",
                        "bank_id": self.bank_id,
                        "query": "ensure bank exists",
                        "results": 1 if already else 0,
                        "latency_ms": (time.perf_counter() - start) * 1000,
                        "ok": True,
                        "error": None,
                        "extra": "already exists" if already else f"ignored: {msg[:120]}",
                    }
                )

    def use_bank(self, bank_id: str) -> None:
        """Point the wrapper at a different bank.

        Used by POST /reset-demo: the primary bank holds the seeded memory, the
        demo bank is empty. Flipping `bank_id` is the whole "Memory OFF" switch -
        no code paths change, we just stop remembering anything.
        """
        self.bank_id = bank_id
        self.memory_enabled = bank_id == HINDSIGHT_BANK_ID
        print(f"[memory] bank switched to {bank_id!r} (memory_enabled={self.memory_enabled})", flush=True)

    # -- RETAIN ------------------------------------------------------------
    async def retain_incident(self, incident: dict[str, Any]) -> None:
        """PRIMITIVE 2 - retain().

        The learning loop. After an incident is resolved we write the full
        postmortem (symptoms + root cause + resolution + affected system) into
        the bank so the *next* similar incident can recall it. One retain per
        incident; `document_id` makes it idempotent-ish and lets us find the
        document later. `context` labels the memory ("incident report") and
        `metadata` gives recall/reflect structured fields to filter on.
        """
        content = incident_to_memory_text(incident)
        start = time.perf_counter()
        try:
            async with asyncio.timeout(HINDSIGHT_TIMEOUT):
                resp = await self._client.aretain(
                    bank_id=self.bank_id,
                    content=content,
                    context="incident report",
                    timestamp=_incident_time(incident),
                    document_id=incident["id"],
                    metadata={
                        "severity": str(incident.get("severity", "unknown")),
                        "system": str(incident.get("system", "unknown")),
                    },
                )
            _log(
                {
                    "op": "retain",
                    "bank_id": self.bank_id,
                    "query": f"incident {incident['id']}: {incident['title']}",
                    "results": 1,
                    "latency_ms": (time.perf_counter() - start) * 1000,
                    "ok": True,
                    "error": None,
                    "extra": f"doc={incident['id']} success={getattr(resp, 'success', None)}",
                }
            )
        except Exception as exc:  # noqa: BLE001
            _log(
                {
                    "op": "retain",
                    "bank_id": self.bank_id,
                    "query": f"incident {incident['id']}",
                    "results": 0,
                    "latency_ms": (time.perf_counter() - start) * 1000,
                    "ok": False,
                    "error": str(exc)[:300],
                }
            )
            raise HindsightError(f"retain failed: {exc}") from exc

    async def retain_batch(self, incidents: list[dict[str, Any]]) -> int:
        """PRIMITIVE 2b - retain_batch().

        Same as retain() but many documents in one call. Used by POST /seed to
        load the hand-written incident library; one round trip instead of 18.
        `items` is a list of {content, context, timestamp, document_id, metadata}.
        Returns the number of items sent.
        """
        items = [
            {
                "content": incident_to_memory_text(inc),
                "context": "incident report",
                "timestamp": _incident_time(inc).isoformat(),
                "document_id": inc["id"],
                "metadata": {
                    "severity": str(inc.get("severity", "unknown")),
                    "system": str(inc.get("system", "unknown")),
                },
            }
            for inc in incidents
        ]
        start = time.perf_counter()
        try:
            async with asyncio.timeout(HINDSIGHT_TIMEOUT * 3):  # batches are slower
                resp = await self._client.aretain_batch(
                    bank_id=self.bank_id,
                    items=items,
                    document_id=f"seed-{uuid.uuid4().hex[:8]}",
                )
            count = getattr(resp, "items_count", len(items)) or len(items)
            _log(
                {
                    "op": "retain_batch",
                    "bank_id": self.bank_id,
                    "query": f"seed {len(items)} incidents",
                    "results": count,
                    "latency_ms": (time.perf_counter() - start) * 1000,
                    "ok": True,
                    "error": None,
                    "extra": "seeded",
                }
            )
            return count
        except Exception as exc:  # noqa: BLE001
            _log(
                {
                    "op": "retain_batch",
                    "bank_id": self.bank_id,
                    "query": f"seed {len(items)} incidents",
                    "results": 0,
                    "latency_ms": (time.perf_counter() - start) * 1000,
                    "ok": False,
                    "error": str(exc)[:300],
                }
            )
            raise HindsightError(f"retain_batch failed: {exc}") from exc

    # -- RECALL ------------------------------------------------------------
    async def recall(self, symptoms: str, max_tokens: int = 2048) -> list[dict[str, Any]]:
        """PRIMITIVE 3 - recall().

        Semantic search over the bank: "give me the raw past memories that match
        these symptoms". `budget` is Hindsight's reasoning-effort knob
        (low/mid/high) - "mid" is our accuracy/latency sweet spot for the demo.
        The returned list feeds BOTH the reflect() call and the "Recalled from
        memory" panel in the UI. Returns [] when the bank is empty (the
        "no memory" baseline) rather than raising.
        """
        start = time.perf_counter()
        try:
            async with asyncio.timeout(HINDSIGHT_TIMEOUT):
                resp = await self._client.arecall(
                    bank_id=self.bank_id,
                    query=symptoms,
                    budget="mid",
                    max_tokens=max_tokens,
                )
            results = [_recall_result_to_dict(r) for r in (getattr(resp, "results", None) or [])]
            _log(
                {
                    "op": "recall",
                    "bank_id": self.bank_id,
                    "query": symptoms,
                    "results": len(results),
                    "latency_ms": (time.perf_counter() - start) * 1000,
                    "ok": True,
                    "error": None,
                    "extra": f"budget=mid max_tokens={max_tokens}",
                }
            )
            return results
        except Exception as exc:  # noqa: BLE001
            _log(
                {
                    "op": "recall",
                    "bank_id": self.bank_id,
                    "query": symptoms,
                    "results": 0,
                    "latency_ms": (time.perf_counter() - start) * 1000,
                    "ok": False,
                    "error": str(exc)[:300],
                }
            )
            raise HindsightError(f"recall failed: {exc}") from exc

    # -- REFLECT -----------------------------------------------------------
    async def reflect(self, symptoms: str) -> str:
        """PRIMITIVE 4 - reflect().

        The main diagnosis path. Hindsight searches the bank itself, reads the
        relevant incident memories, and synthesises an answer to a
        natural-language question - root cause + fix, grounded in what actually
        happened before. No LLM prompt engineering on our side; the LLM in
        llm.py only reformats this text for the UI.
        """
        question = (
            "Given these symptoms, what is the most likely root cause and fix "
            "based on past incidents?\n\n"
            f"{symptoms}"
        )
        start = time.perf_counter()
        try:
            async with asyncio.timeout(HINDSIGHT_TIMEOUT):
                resp = await self._client.areflect(
                    bank_id=self.bank_id,
                    query=question,
                    budget="mid",
                )
            text = (getattr(resp, "text", None) or "").strip()
            _log(
                {
                    "op": "reflect",
                    "bank_id": self.bank_id,
                    "query": question,
                    "results": 1 if text else 0,
                    "latency_ms": (time.perf_counter() - start) * 1000,
                    "ok": True,
                    "error": None,
                    "extra": f"budget=mid chars={len(text)}",
                }
            )
            return text
        except Exception as exc:  # noqa: BLE001
            _log(
                {
                    "op": "reflect",
                    "bank_id": self.bank_id,
                    "query": question,
                    "results": 0,
                    "latency_ms": (time.perf_counter() - start) * 1000,
                    "ok": False,
                    "error": str(exc)[:300],
                }
            )
            raise HindsightError(f"reflect failed: {exc}") from exc


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def incident_to_memory_text(incident: dict[str, Any]) -> str:
    """Render an incident dict as the plain-text postmortem we store in Hindsight.

    Structure matters for retrieval quality: Hindsight chunks and embeds this
    text, so we keep a consistent labelled layout (WHAT / WHY / HOW) and repeat
    the affected system name - the words a future incident's symptoms are most
    likely to share.
    """
    parts = [
        f"INCIDENT {incident['id']}: {incident['title']}",
        f"Severity: {incident.get('severity', 'unknown')}",
        f"Affected system: {incident.get('system', 'unknown')}",
        f"Occurred: {incident.get('timestamp', 'unknown')}",
        "",
        "SYMPTOMS:",
        str(incident.get("symptoms", "")).strip(),
    ]
    if incident.get("root_cause"):
        parts += ["", "ROOT CAUSE:", str(incident["root_cause"]).strip()]
    if incident.get("resolution"):
        parts += ["", "RESOLUTION:", str(incident["resolution"]).strip()]
    return "\n".join(parts).strip()


def _incident_time(incident: dict[str, Any]) -> datetime:
    """Parse an incident timestamp (ISO 8601) into an aware datetime.

    Hindsight uses `timestamp` to place the memory on a timeline and for
    temporal retrieval ("what broke last quarter?"). Naive datetimes are
    rejected by the API, so we assume UTC.
    """
    raw = incident.get("timestamp")
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            dt = datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _recall_result_to_dict(r: Any) -> dict[str, Any]:
    """Flatten a RecallResult into a small dict for the LLM prompt and the UI."""
    scores = getattr(r, "scores", None)
    return {
        "text": getattr(r, "text", "") or "",
        "context": getattr(r, "context", None),
        "document_id": getattr(r, "document_id", None),
        "metadata": getattr(r, "metadata", None) or {},
        "type": getattr(r, "type", None),
        "score": _first_score(scores),
    }


def _first_score(scores: Any) -> float | None:
    """Pull a single relevance number out of the nested RecallScores model."""
    if scores is None:
        return None
    if isinstance(scores, (int, float)):
        return float(scores)
    for attr in ("overall", "combined", "final", "rerank", "score"):
        val = getattr(scores, attr, None)
        if isinstance(val, (int, float)):
            return round(float(val), 4)
    if isinstance(scores, dict):
        for key in ("overall", "combined", "final", "rerank", "score"):
            if isinstance(scores.get(key), (int, float)):
                return round(float(scores[key]), 4)
    return None


# Module-level singleton: one Hindsight connection for the whole app.
memory: BlamelessMemory | None = None


def get_memory() -> BlamelessMemory:
    """FastAPI dependency: return the shared memory wrapper, or fail loudly."""
    if memory is None:
        raise HindsightError("memory not initialised - app startup failed")
    return memory
