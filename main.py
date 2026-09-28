"""
main.py - the Blameless HTTP API and static page server.

The one workflow this app exists to demonstrate:

    POST /incidents  ->  recall() past incidents  ->  reflect() for a diagnosis
                      ->  LLM formats it          ->  return diagnosis + what memory was used
    POST /incidents/{id}/resolve
                      ->  retain() the postmortem  ->  the next similar incident can recall it

That second call is the learning loop. Everything else here (seeding, the trace
panel, the memory on/off switch) exists to make the loop visible on screen.

Run with:  uv run uvicorn main:app --reload
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import llm
import memory as memory_module
import seed_data
from memory import HindsightError

STATIC_DIR = Path(__file__).parent / "static"
# The "Memory OFF" demo writes to this empty bank instead of the real one, so the
# no-memory baseline can never contaminate the seeded incident library.
DEMO_BANK_SUFFIX = "-demo"


# --------------------------------------------------------------------------
# In-memory session state
# --------------------------------------------------------------------------
# Incidents raised in THIS session (as opposed to the seeded library, which
# lives in Hindsight). Deliberately a plain list: the spec is one workflow, no
# database. Resets on restart; the seeded memory does not, which is the point.
SESSION_INCIDENTS: list[dict[str, Any]] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: connect to Hindsight, make sure the bank exists. Shutdown: close clients."""
    try:
        memory_module.memory = memory_module.BlamelessMemory()
        # PRIMITIVE 1: create_bank() at startup. Idempotent - a bank that
        # already exists (from a previous run, or a previous demo) is left alone,
        # because persistent memory is the entire premise of the project.
        await memory_module.memory.create_bank()
        print(f"[startup] Hindsight bank '{memory_module.HINDSIGHT_BANK_ID}' ready", flush=True)
    except HindsightError as exc:
        # Do not crash the app: the UI should still load and explain the problem,
        # rather than the demo showing a connection-refused stack trace.
        print(f"[startup] Hindsight unavailable: {exc}", flush=True)

    print(f"[startup] Blameless ready -> http://127.0.0.1:8000 (bank={memory_module.HINDSIGHT_BANK_ID})", flush=True)
    yield

    if memory_module.memory is not None:
        await memory_module.memory._client.aclose()  # close the aiohttp session
    await llm.close_client()
    print("[shutdown] clients closed", flush=True)


app = FastAPI(
    title="Blameless",
    description="The on-call agent that remembers every outage. Memory powered by Hindsight.",
    version="0.1.0",
    lifespan=lifespan,
)


def get_mem() -> memory_module.BlamelessMemory:
    """FastAPI dependency: the shared Hindsight wrapper.

    Raises 503 if Hindsight was unreachable at startup, so the UI can say
    "memory is down" instead of a 500.
    """
    try:
        return memory_module.get_memory()
    except memory_module.HindsightError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def group_memories(memories: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    """Collapse recalled fragments into one card per past incident.

    Hindsight stores *facts*, not documents, so a single seeded incident comes
    back as several fragments ("symptom...", "root cause...", "we moved it to
    pgbouncer...") - ~45 results for 20 seeded incidents. Rendering those raw
    would be an unreadable wall of text, so we group by document_id and keep
    the best-scoring fragment as the representative, with a count of how many
    fragments backed it. This is also what the "Recalled from memory" panel is
    actually about: which past incidents were used.

    Ranking: fragments that belong to a real incident outrank Hindsight's own
    synthesized observations (which have no document_id). Both can score near
    1.0, and for the demo the point is "this happened to us in March", not
    "the bank has a general note about TLS".
    """
    grouped: dict[str, dict[str, Any]] = {}
    for m in memories:
        key = m.get("document_id") or f"obs:{m.get('text', '')[:60]}"
        score = m.get("score") or 0.0
        card = grouped.get(key)
        if card is None:
            grouped[key] = {
                "document_id": m.get("document_id"),
                "system": (m.get("metadata") or {}).get("system", "unknown"),
                "severity": (m.get("metadata") or {}).get("severity", "unknown"),
                "score": score,
                "fragments": 1,
                "text": m.get("text", ""),
            }
        else:
            card["fragments"] += 1
            if score > card["score"]:  # keep the strongest fragment as the summary
                card["score"] = score
                card["text"] = m.get("text", "")

    ranked = sorted(
        grouped.values(),
        key=lambda c: (c["document_id"] is None, -c["score"]),  # incidents first
    )
    return ranked[:limit]


# --------------------------------------------------------------------------
# Request/response models
# --------------------------------------------------------------------------
class IncidentIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    symptoms: str = Field(..., min_length=1, description="Raw log paste or alert text")
    system: str = Field(..., min_length=1, max_length=100)
    severity: str = Field("SEV3", max_length=10)


class ResolveIn(BaseModel):
    root_cause: str = Field(..., min_length=1)
    resolution: str = Field(..., min_length=1)


class ResetIn(BaseModel):
    memory_enabled: bool = True
    clear_incidents: bool = False


# --------------------------------------------------------------------------
# Static page
# --------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """The single-page UI (vanilla JS, no build step)."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness plus what the demo is currently pointed at."""
    mem = memory_module.memory
    return {
        "ok": True,
        "memory_connected": mem is not None,
        "bank_id": mem.bank_id if mem else None,
        "memory_enabled": mem.memory_enabled if mem else False,
        "session_incidents": len(SESSION_INCIDENTS),
    }


# --------------------------------------------------------------------------
# Memory Trace
# --------------------------------------------------------------------------
@app.get("/trace")
async def trace(limit: int = 50) -> dict[str, Any]:
    """Recent Hindsight calls (retain/recall/reflect) for the Memory Trace panel.

    This is the same data memory.py prints to the console - the UI polls it so
    the demo can show the calls happening in real time.
    """
    mem = memory_module.memory
    return {
        "bank_id": mem.bank_id if mem else None,
        "memory_enabled": mem.memory_enabled if mem else False,
        "seeded": len(seed_data.SEED_INCIDENTS),
        "calls": memory_module.get_trace(limit=limit),
    }


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------
@app.post("/seed")
async def seed(mem: memory_module.BlamelessMemory = Depends(get_mem)) -> dict[str, Any]:
    """Load the 20 hand-written incidents from seed_data.py into the bank.

    Uses retain_batch: one round trip for the whole library instead of 20
    serial retains. In production you would seed once; here you re-seed before
    every demo so the run is reproducible.
    """
    incidents = seed_data.get_seed_incidents()
    try:
        count = await mem.retain_batch(incidents)
    except HindsightError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "seeded": count,
        "bank_id": mem.bank_id,
        "message": f"Retained {count} past incidents into bank '{mem.bank_id}'. Memory is live.",
    }


# --------------------------------------------------------------------------
# The main workflow
# --------------------------------------------------------------------------
@app.post("/incidents")
async def create_incident(
    payload: IncidentIn,
    mem: memory_module.BlamelessMemory = Depends(get_mem),
) -> dict[str, Any]:
    """Diagnose a new incident from memory.

    1. RECALL: pull the raw past memories that match these symptoms.
    2. REFLECT: let Hindsight reason over the bank and propose a root cause + fix.
       These two run concurrently - they are independent calls to the same bank.
    3. FORMAT: the LLM reshapes reflect()'s answer into UI sections. The LLM
       does not do the diagnosing; Hindsight does.

    With memory OFF, steps 1-2 are skipped entirely: the same incident goes to
    the LLM with no precedent, which is the baseline for the demo comparison.
    """
    incident = {
        "id": f"inc-{uuid.uuid4().hex[:8]}",
        "title": payload.title,
        "symptoms": payload.symptoms,
        "system": payload.system,
        "severity": payload.severity,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    SESSION_INCIDENTS.append(incident)

    # -- 1 & 2: recall + reflect, concurrently --------------------------------
    memories: list[dict[str, Any]] = []
    reflect_text = ""
    memory_error: str | None = None

    if mem.memory_enabled:
        try:
            memories, reflect_text = await asyncio.gather(
                mem.recall(payload.symptoms),
                mem.reflect(payload.symptoms),
            )
        except HindsightError as exc:
            # Hindsight down/timed out. Say so plainly and still try to help
            # with the LLM alone, rather than failing the whole request.
            memory_error = str(exc)
    else:
        memory_error = None  # not an error: this is the deliberate no-memory run

    # -- 3: format with the LLM ---------------------------------------------
    try:
        formatted = await llm.format_diagnosis(
            title=payload.title,
            symptoms=payload.symptoms,
            system_name=payload.system,
            reflect_text=reflect_text,
            memories=memories,
            memory_enabled=mem.memory_enabled,
        )
        diagnosis = formatted["diagnosis"]
        confidence_note = formatted["confidence_note"]
        suspected = formatted["suspected_causes"]
    except llm.LLMError as exc:
        # Graceful degradation: show Hindsight's own answer even if the
        # formatter is unavailable. The diagnosis is the product; the JSON
        # sections are presentation.
        diagnosis = reflect_text or (
            "No automated diagnosis available: memory and the LLM formatter are both "
            "unreachable. Escalate to a human on-call with the logs above."
        )
        confidence_note = f"LLM formatter unavailable ({exc}). Showing Hindsight's raw analysis."
        suspected = []

    if not mem.memory_enabled:
        confidence_note = (
            "MEMORY OFF - the bank was empty for this run, so there is no precedent to "
            "reason from. Compare this answer with the same incident under Memory ON."
        )
    if memory_error:
        confidence_note = f"Hindsight call failed: {memory_error}. {confidence_note}"

    incident["diagnosis"] = diagnosis
    incident["confidence_note"] = confidence_note
    incident["memory_enabled"] = mem.memory_enabled
    incident["recalled_count"] = len(memories)

    return {
        "incident_id": incident["id"],
        "diagnosis": diagnosis,
        "recalled_memories": group_memories(memories),
        "confidence_note": confidence_note,
        "suspected_causes": suspected,
        "reflect_analysis": reflect_text,
        "memory_enabled": mem.memory_enabled,
        "bank_id": mem.bank_id,
        "recalled_count": len(memories),
        "recalled_incidents": len({m.get("document_id") for m in memories if m.get("document_id")}),
        "warning": memory_error,
    }


@app.get("/incidents")
async def list_incidents() -> dict[str, Any]:
    """Incidents raised in this session, newest first (with diagnosis if any)."""
    return {
        "incidents": list(reversed(SESSION_INCIDENTS)),
        "count": len(SESSION_INCIDENTS),
    }


@app.post("/incidents/{incident_id}/resolve")
async def resolve_incident(
    incident_id: str,
    payload: ResolveIn,
    mem: memory_module.BlamelessMemory = Depends(get_mem),
) -> dict[str, Any]:
    """Close the learning loop: retain the resolved incident so future ones benefit.

    This is the whole product in one endpoint. After this, a similar incident
    arriving next week will recall THIS one during its recall() call - the
    system gets better at diagnosing the longer it runs.
    """
    incident = next((i for i in SESSION_INCIDENTS if i["id"] == incident_id), None)
    if incident is None:
        raise HTTPException(status_code=404, detail=f"incident {incident_id} not found in this session")

    incident["root_cause"] = payload.root_cause
    incident["resolution"] = payload.resolution
    incident["resolved_at"] = datetime.now(timezone.utc).isoformat()
    incident["status"] = "resolved"

    retained = False
    warning: str | None = None
    try:
        await mem.retain_incident(incident)
        retained = True
    except HindsightError as exc:
        # Resolving an incident must never lose the incident, so we return 200
        # with a warning rather than erroring out.
        warning = str(exc)

    return {
        "incident_id": incident_id,
        "retained": retained,
        "bank_id": mem.bank_id,
        "warning": warning,
        "message": (
            f"Retained into bank '{mem.bank_id}'. The next similar incident will recall this one."
            if retained
            else "Resolution recorded for this session but NOT retained - Hindsight unavailable."
        ),
    }


# --------------------------------------------------------------------------
# Demo control
# --------------------------------------------------------------------------
@app.post("/reset-demo")
async def reset_demo(
    payload: ResetIn,
    mem: memory_module.BlamelessMemory = Depends(get_mem),
) -> dict[str, Any]:
    """Switch between the seeded bank (Memory ON) and an empty bank (Memory OFF).

    No code path changes - the app simply points at a different bank, so the
    only difference between the two runs is whether memory exists. The empty
    bank is created on demand and stays empty.
    """
    target = (
        memory_module.HINDSIGHT_BANK_ID
        if payload.memory_enabled
        else f"{memory_module.HINDSIGHT_BANK_ID}{DEMO_BANK_SUFFIX}"
    )
    mem.use_bank(target)
    await mem.create_bank()  # idempotent: creates the empty bank if it is new
    memory_module.clear_trace()

    if payload.clear_incidents:
        SESSION_INCIDENTS.clear()

    return {
        "bank_id": target,
        "memory_enabled": mem.memory_enabled,
        "message": (
            f"Memory ON - pointing at the seeded bank '{target}'."
            if mem.memory_enabled
            else f"Memory OFF - pointing at the empty bank '{target}'. Run /seed to fill it."
        ),
    }
