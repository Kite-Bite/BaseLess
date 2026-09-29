# Only resolved incidents survive a restart in my Hindsight bank

I built an incident-diagnosis agent that remembers every outage we've resolved. The interesting part isn't the diagnosis — it's the persistence boundary I ended up drawing, and how much of the system's value lives on one side of it.

The rule ended up being one line: incidents live in a process-local list and vanish on restart; only *resolved* incidents get written into a [Hindsight](https://hindsight.vectorize.io/) bank and outlive everything. That constraint shaped every other decision in the codebase, including the ones I'd defend hardest in a design review.

## What the system does

Blameless has one workflow, and it runs in a loop:

1. **Recall** — a new incident arrives as raw log paste. Semantic search over the bank finds past incidents with similar symptoms.
2. **Reflect** — the same bank gets a natural-language question: given these symptoms, what's the most likely root cause and fix, based on past incidents? This is the diagnosis.
3. **Retain** — when a human marks the incident resolved, the postmortem is written back into the bank so the next similar incident can use it.

The division of labour matters, and it's opinionated: **the LLM does not diagnose.** Hindsight's `reflect()` does the reasoning over memory; the LLM call in `llm.py` exists only to reshape that text into UI sections. If I take the memory away, quality visibly collapses — which is the point, and which I can demonstrate with a button.

Every Hindsight call lives in a single file, `memory.py`. Nothing else in the project imports `hindsight_client`. That gives me one place to log, time, error-handle, and explain the memory layer, and it means the [Hindsight Python SDK](https://github.com/vectorize-io/hindsight) can be swapped without touching the app.

## The persistence boundary

Here is the whole asymmetry, in code:

```python
# main.py - session state. Resets on restart, deliberately.
SESSION_INCIDENTS: list[dict[str, Any]] = []
```

```python
# main.py - POST /incidents/{incident_id}/resolve
incident["root_cause"] = payload.root_cause
incident["resolution"] = payload.resolution
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
```

That `try/except` returning HTTP 200 with a `retained: false` flag is the design decision I care about most. An incident that has been diagnosed, root-caused, and fixed by a human at 3am is the single most expensive piece of information in the system, and it is written in a form nobody has seen before. Failing that write with a 500 would invite a retry that could double-retain, and worse, would make the operator think their work was lost. So: record it, report the failure honestly, let the operator decide.

The corollary is that the *un*resolved incident is disposable. You can always re-paste logs. You cannot re-derive a root cause. That asymmetry is why the bank is append-only from the app's perspective and why there is no delete endpoint in `main.py`.

## What actually goes into a memory

Before memory can be good it has to be written consistently. `incident_to_memory_text()` renders every postmortem into the same labelled layout:

```python
parts = [
    f"INCIDENT {incident['id']}: {incident['title']}",
    f"Severity: {incident.get('severity', 'unknown')}",
    f"Affected system: {incident.get('system', 'unknown')}",
    "",
    "SYMPTOMS:",
    str(incident.get("symptoms", "")).strip(),
]
if incident.get("root_cause"):
    parts += ["", "ROOT CAUSE:", str(incident["root_cause"]).strip()]
if incident.get("resolution"):
    parts += ["", "RESOLUTION:", str(incident["resolution"]).strip()]
```

The structure is not cosmetic. Hindsight chunks and embeds this text, so a predictable layout retrieves better than free-form prose — the symptom block of incident A lands near the symptom block of incident B, not interleaved with someone's rambling narrative about the same outage.

The retain call also passes `document_id=incident["id"]`, `timestamp`, and `metadata={"severity", "system"}`. `document_id` is what makes a retained memory traceable back to the card that produced it; `timestamp` is what makes "what broke last quarter?" answerable. This is the difference between agent memory as a vector store and [agent memory](https://vectorize.io/what-is-agent-memory) as an operational record — the retrieval layer has to support the questions your on-call rotation actually asks.

The bank's identity is also declared, not inferred. `create_bank()` takes a mission and a disposition:

```python
BANK_MISSION = (
    "I am an on-call incident analyst. I remember past incidents, their root "
    "causes and fixes, and use them to diagnose new ones. I focus on systemic "
    "causes, never on blaming people."
)
BANK_DISPOSITION = {"skepticism": 4, "literalism": 3, "empathy": 2}
```

High skepticism because an incident report is evidence, not a vibe. Low empathy because this is an on-call analyst, not a support agent. These are standing instructions Hindsight holds on every retain and every reflect — which means the blameless constraint is enforced at the memory layer, not by hoping the prompt in `llm.py` behaves.

## Two things that bit me

**The sync client doesn't work in FastAPI.** Hindsight's sync helpers are implemented with `loop.run_until_complete()`. Calling them from inside an `async def` route raises `RuntimeError: event loop is already running`. This took me a while to find because the error surfaces far from the cause. The fix is the `a*` variants, which also let recall and reflect run concurrently, since they're independent calls against the same bank:

```python
memories, reflect_text = await asyncio.gather(
    mem.recall(payload.symptoms),
    mem.reflect(payload.symptoms),
)
```

That gather is most of the reason a diagnosis lands in about ten seconds instead of twenty.

**`create_bank` fails on the second run.** A bank that already exists returns 409, and memory is supposed to outlive the process — so re-creating on every boot is wrong. I swallow the 409 and treat startup as "ensure the bank exists", not "make the bank".

## Recall returns facts, not documents

The other thing worth knowing: `arecall()` on a 20-incident bank returns roughly 45 results. Hindsight returns *facts*, so one seeded incident comes back as several fragments — the symptom, the root cause, the fix. Rendering those raw is an unreadable wall of text, so I collapse them back to one card per incident:

```python
def group_memories(memories: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for m in memories:
        key = m.get("document_id") or f"obs:{m.get('text', '')[:60]}"
        ...
    ranked = sorted(
        grouped.values(),
        key=lambda c: (c["document_id"] is None, -c["score"]),  # incidents first
    )
```

Real incidents rank above Hindsight's own synthesised observations, which have no `document_id` and can score near 1.0. For on-call use, "this happened to us in March" beats "the bank has a general note about TLS".

## What it looks like in practice

Seed the bank, then submit an incident that isn't in it — say a cert-expiry failure on a system nobody's seen before:

```
[memory #3] RETAIN_BATCH results=20   15062ms bank=blameless seeded
[memory #4] RECALL      results=45     1183ms bank=blameless budget=mid max_tokens=2048
[memory #5] REFLECT     results=1      9204ms bank=blameless budget=mid chars=1840
```

The recalled panel names the seeded TLS incidents with relevance scores. The diagnosis cites those specific incident IDs, names the systemic cause — a renewal job with no owner and no failure alerting — and proposes the fix already proven in production: two independent alerts, one on `notAfter < 14 days`, one on the renewal job's own success/failure. Then a human resolves it, `retain` fires, and the next similar incident recalls *this* one.

Now the comparison, which is the only part of the demo I'd defend as evidence: flip the header to **Memory: OFF**. The app points at an empty bank — no code path changes, just a different `bank_id`. Submit the exact same incident. Recall returns nothing, reflect has no precedent, the confidence note says so explicitly, and the diagnosis degrades to generic first-principles checklist advice. Same incident, same model, same code. The only variable is whether the bank exists.

Without that toggle, "the agent uses memory" is an assertion. With it, it's a diff.

## The trace panel

Every memory call is logged to console and to a bounded ring buffer served at `GET /trace`, polled by the UI every 700ms while a diagnosis runs. It records op, query, result count, latency, and ok/failed:

```python
_TRACE: deque[dict[str, Any]] = deque(maxlen=TRACE_LIMIT)
```

This exists because invisible infrastructure is unfalsifiable infrastructure. When someone asks "did it actually recall anything, or did the model just hallucinate a confident answer?", the answer should be a URL. In practice it also caught real bugs — the trace showed `results=0` on a call I expected to return 45, which turned out to be a bank that had never been seeded.

## Lessons

**Put the expensive, irreplaceable write behind a non-throwing API.** A failed retain must not fail the resolve. Anything that destroys a human's work should be idempotent and separately retryable.

**If your memory is a record, treat it like a record.** Stable `document_id`s, real timestamps, consistent text layout. Vector-store ergonomics and operational-record ergonomics conflict, and the record wins.

**Make the counterfactual a button.** The memory on/off switch is worth more than any amount of documentation. If you can't show the same input degrading without the system, you don't yet know what your system contributes.

**Enforce the organisational constraint at the layer that can't drift.** "Blameless" as a bank mission survives a model swap, a prompt edit, and a new engineer. "Blameless" as a line in a system prompt survives none of them.

**Don't hand the diagnosis to the LLM.** Passing `reflect()`'s grounded answer through a formatter is weaker than it sounds — the formatter can still over-claim. Keeping the split sharp, and degrading to raw `reflect` output when the formatter is down, means the product survives provider outages.
