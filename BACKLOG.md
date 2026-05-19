# MemPalace — Backlog

---

## BUG-002 — `spellcheck.py` reads wrong registry key, always returns empty set

**Severity:** High
**Reported:** 2026-05-19
**Resolved:** 2026-05-19 - commit `05750af`
**Affects:** `spellcheck.py` `_load_known_names()` - registered names never corrected

`_load_known_names` reads `reg._data.get('entities', {})` but `EntityRegistry` stores
people under `'people'`. Spellcheck has silently never seen any registered names.

**Fix:** `reg._data.get("people", {})` + update field access to match `people` dict structure.

---

## BUG-003 — `onboarding.py` infinite loop on entity code collision

**Severity:** High
**Reported:** 2026-05-19
**Resolved:** 2026-05-19 - commit `05750af`
**Affects:** `onboarding.py` entity code generation

Collision loop sets `code = name[:4].upper()` unconditionally. Two names sharing a
4-char prefix (e.g. "Alice"/"Alicia" both yield "ALIC") cause an infinite loop.

**Fix:** Use numeric suffix: `code = name[:3].upper() + str(suffix)` with incrementing `suffix`.

---

## BUG-004 — `dialect.py` crashes with `IndexError` when `--config` has no value

**Severity:** Medium
**Reported:** 2026-05-19
**Resolved:** 2026-05-19 - commit `05750af`
**Affects:** `dialect.py` CLI, `--config` flag

`args[idx + 1]` accessed without bounds check. `dialect --config` (no path argument)
raises `IndexError` with no useful error message.

**Fix:** Check `idx + 1 < len(args)` before access; print clear error and exit if missing.

---

## BUG-005 — `fact_checker.py` datetime string comparison misses same-day expiries

**Severity:** Medium
**Reported:** 2026-05-19
**Resolved:** 2026-05-19 - commit `05750af`
**Affects:** `fact_checker.py` stale fact detection

`str(valid_to) < now_iso` compares a datetime string (`2025-01-09T12:00:00Z`) against a
date string (`2025-01-09`). Python string ordering: `'2025-01-09'` < `'2025-01-09T...'`,
so the condition is False for any fact expiring today with a time component. Facts that
expired today are not flagged as stale.

**Fix:** `date.fromisoformat(str(valid_to)[:10]) < datetime.now(timezone.utc).date()`

---

## BUG-006 — `migrate.py` leaks SQLite connection on exception

**Severity:** Medium
**Reported:** 2026-05-19
**Resolved:** 2026-05-19 - commit `05750af`
**Affects:** `migrate.py` `_read_drawers_from_sqlite()`

`conn = sqlite3.connect(db_path)` at line 56, closed at line 109 with no `try/finally`.
Any exception in between leaks the connection. On Windows, a leaked SQLite connection
holds a file lock that blocks subsequent palace access.

**Fix:** Wrap body in `try/finally: conn.close()`.

---

## WARN-001 — `palace.py` IndexError on mismatched ChromaDB metadatas

**Severity:** Low
**Reported:** 2026-05-19
**Resolved:** 2026-05-19 - commit `05750af`
**Affects:** `palace.py` `get_drawer()`

`results.get("metadatas", [{}])[0]` raises `IndexError` if ChromaDB returns empty
`metadatas` with non-empty `ids`. Caught by outer `except Exception` so impact is
limited to silent re-mining, but root cause is hidden.

**Fix:** `(results.get("metadatas") or [{}])[0] or {}`

---

## WARN-002 — `split_mega_files.py` uses platform-default encoding

**Severity:** Low
**Reported:** 2026-05-19
**Resolved:** 2026-05-19 - commit `05750af`
**Affects:** `split_mega_files.py` lines 189, 278

File reads use no `encoding=` argument; defaults to system locale (CP1252 on Windows).
UTF-8 transcripts with non-ASCII characters silently corrupt on Windows.

**Fix:** `path.read_text(encoding="utf-8", errors="replace")`

---

## BUG-001 — `mcp_call_tool` serialises arguments as a string in long Devin sessions

**Severity:** High
**Reported:** 2026-05-18
**Resolved:** 2026-05-18 — commit `8de2d4f` (`fix(mcp): parse JSON-string arguments defensively`)
**Affects:** All parameterised MCP tool calls (`mempalace_diary_write`, `mempalace_add_drawer`, `mempalace_search`, etc.)

### Symptom

In Devin for Terminal sessions of ~50+ messages, every call to `mcp_call_tool` with
arguments returns:

```
invalid type: string "{"agent_name": "devin", "entry": "..."}", expected a map
```

The tool receives the `arguments` parameter as a JSON-encoded string instead of a parsed
JSON object (map). The call silently fails — no save occurs, no error is raised by the
palace itself.

`mcp_list_tools` continues to work normally. Only calls with parameters fail.

### Root cause

Unknown — suspected to be a Devin for Terminal serialisation bug that manifests after
a session reaches a certain message count or memory threshold. The exact trigger is not
known (confirmed working at <50 messages, confirmed broken at ~200+ messages in the same
session).

This is a client-side bug in Devin, not in MemPalace's MCP server. The MCP server receives
a string where it expects a map and correctly rejects it.

### Workaround

Use the Python path directly (bypasses the MCP client entirely):

```python
# Run from projects/mempalace: uv run python /path/to/script.py
import sys, uuid, datetime
sys.path.insert(0, 'C:/Users/George.Murga/projects/mempalace')
from mempalace.palace import get_collection
from mempalace.config import MempalaceConfig

cfg = MempalaceConfig()
col = get_collection(cfg.palace_path, cfg.collection_name)
now = datetime.datetime.now().isoformat()

col.upsert(
    ids=[str(uuid.uuid4())],
    documents=["content here"],
    metadatas=[{'wing': 'wing_devin', 'room': 'diary', 'type': 'diary',
                'agent_name': 'devin', 'topic': 'general',
                'source': 'devin-session', 'timestamp': now}]
)
```

**Critical:** run from `projects/mempalace` root (`cd projects/mempalace && uv run python
script.py`) so the correct chromadb version is used.

### Potential fix in MemPalace

The MCP server (`mcp_server.py`) could add a defensive argument normalisation layer:
if an argument is received as a string and is valid JSON, parse it before passing to
the handler. This would make the server tolerant of the Devin client bug.

```python
# In mcp_server.py handler dispatch — pseudocode
if isinstance(args, str):
    try:
        args = json.loads(args)
    except json.JSONDecodeError:
        raise ValueError(f"arguments must be a map, got string: {args[:100]}")
```

### References

- Confirmed broken: Devin for Terminal v2026.5.6-8, session ~200+ messages
- Workaround documented: `~/.agents/AGENTS.md` § MemPalace AUTO-SAVE, `~/.agents/skills/mempalace/SKILL.md` § LONG SESSION BUG
