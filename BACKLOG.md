# MemPalace — Backlog

---

## OPEN: TASK-001 — Sync fork to upstream v3.5.0 (and PR the backup feature upstream)

**Type:** Task  **Status:** READY-TO-CUTOVER  **Reported:** 2026-06-25  **Validated:** 2026-06-26
**Affects:** whole repo (george-cm fork)

**UPDATE 2026-06-26 — Phase 0/A/B done; validated branch ready, cutover deferred.**
A validated branch `sync/upstream-v3.5.0` (off upstream `v3.5.0` = `2c08bb2`) exists locally and on
the fork. It re-applies the backup feature, **re-adds the fork-only `devin` harness** (upstream
v3.5.0 only has `claude-code`/`codex` — a blind cutover would break Devin auto-save), and the
personal config (bash test fix, conftest `tmp_path`, `.gitignore .worktrees`, AGENTS TDD rule).
Validation against a *restored copy* of the live palace: v3.5.0 **reads it and search works** (no
hard blocker); backup tests 33 pass, hooks/backup 167 pass, full suite 2871 pass. Two NON-blocking
migrations to run on the LIVE palace **at cutover**: `mempalace palace set-embedder --model minilm`
and `mempalace repair` (rebuild index w/ cosine). **Cutover (Phase D) NOT yet done** — deferred to a
deliberate session (close Devin first; `reset --hard sync/upstream-v3.5.0` + `--force-with-lease`;
reinstall editable tool; smoke-test; rollback via backup). See plan + filled-in Decisions:
`C:\Users\George.Murga\.config\mempalace-sync\2026-06-25-upstream-sync-plan.md`.

**Remaining to do (Phase D cutover — do in a deliberate session, not mid-Devin-work):**
- [ ] Fresh `mempalace backup --out C:\Users\George.Murga\Backups\MemPalace --keep 14` + `backup-verify`.
- [ ] Close Devin / MCP server (release palace locks).
- [ ] LIVE palace migrations: `mempalace palace set-embedder --model minilm`; then `mempalace repair`.
- [ ] `git checkout main` → `git reset --hard sync/upstream-v3.5.0` → `git push fork main --force-with-lease`.
- [ ] `uv tool install -e . --force` (→ v3.5.0); confirm `mempalace --help` lists backup/backup-verify/restore.
- [ ] Smoke-test: `mempalace status`, `mempalace search "scp -O NAS"`; restart Devin, confirm hook fires.
- [ ] Rollback if needed: restore backup + repair; `git reset --hard 812eefe` + force-push; reinstall tool.

**Remaining follow-up (non-blocking):**
- [ ] PR the backup/verify/restore feature to upstream (it has no backup command).
- [ ] Evaluate upstream's new MCP tools (`mempalace_checkpoint`, `mempalace_mine`, `delete_by_source`).

The fork is ~525 commits behind upstream `milla-jovovich/mempalace` (diverged at v3.3.5,
merge-base `d0163a7`; upstream now at **v3.5.0** = `2c08bb2`; fork main = `812eefe`).
Upstream has shipped pluggable vector backends (pgvector/qdrant/sqlite_exact + RFC 001
embedder-identity), multilingual embeddings, IDE integrations (Cursor/Antigravity/Gemini/
Continue/Pi), MCP HTTP transport, an opt-in write daemon, Docker, hallway/tunnel dynamics,
and new MCP tools (`mempalace_checkpoint`, `mempalace_mine`, `delete_by_source`).

**Risk:** this repo is the LIVE memory tooling (Devin MCP server + Stop/SessionEnd hooks +
editable `mempalace` command all point here). v3.5.0's RFC 001 "embedder-identity / three-state
enforcement" may require a **palace migration** before it will read the existing 33k-drawer palace.
Do NOT blind-rebase/force-push; validate v3.5.0 against a restored COPY of the palace first.

**Plan (full, step-by-step):**
`C:\Users\George.Murga\.config\mempalace-sync\2026-06-25-upstream-sync-plan.md`
Phase 0 (backup+snapshot) → A (keep/drop the 32 fork commits) → B (isolated worktree validation,
incl. the palace-compat check) → C (decision gate) → D (cutover + rollback).

**Keep on sync:** the backup/verify/restore feature (new files, clean adds) + chosen personal
config. **Drop:** the MCP-arg / Windows-UTF-8 / KG-uuid4 / BUG-WARN fixes (upstream already covers them).

**Follow-up:** upstream has NO backup command — the feature is a clean upstream PR candidate.

---

## WARN-006 — `palace.py` lock file I/O missing `encoding="utf-8"`

**Severity:** Medium  **Reported:** 2026-05-19  **Resolved:** 2026-05-19 - commit `d0b76b6`
**Affects:** `palace.py` lines 297, 473 - lock holder identity on Windows

Lock file written/read without encoding. Argv may contain non-ASCII paths; cp1252 default corrupts content.

**Fix:** `open(lock_path, "w", encoding="utf-8")` and `open(lock_path, "r+", encoding="utf-8")`

---

## WARN-007 — `cli.py` and `layers.py` file reads missing `encoding="utf-8"`

**Severity:** Low  **Reported:** 2026-05-19  **Resolved:** 2026-05-19 - commit `d0b76b6`
**Affects:** `cli.py:215` `.gitignore` read, `layers.py:58` `identity.txt` read

**Fix:** Add `encoding="utf-8", errors="replace"` to both call sites.

---

## WARN-008 — `repair.py` O(n²) deduplication in pagination fallback

**Severity:** Medium  **Reported:** 2026-05-19  **Resolved:** 2026-05-19 - commit `d0b76b6`
**Affects:** `repair.py:113` - large palaces (>10k drawers) see severe slowdown

`set(ids)` reconstructed from the full list on every iteration. Fix: build the set once, extend incrementally.

---

## WARN-009 — `hooks_cli.py` PID file written empty before subprocess starts

**Severity:** Medium  **Reported:** 2026-05-19  **Resolved:** 2026-05-19 - commit `d0b76b6`
**Affects:** `hooks_cli.py:362` - duplicate mine processes can spawn concurrently

File created empty, Popen spawned, PID written. Gap allows concurrent hook to read empty file and start a second mine.

**Fix:** Write sentinel value immediately on creation; treat non-numeric content as "running".

---

## WARN-010 — Silent `except Exception` swallowing 4 failure paths

**Severity:** Low  **Reported:** 2026-05-19  **Resolved:** 2026-05-19 - commit `d0b76b6`
**Affects:** `miner.py:502`, `hooks_cli.py:608,743`, `diary_ingest.py:104`

Errors silently swallowed with no log entry. Users see degraded behavior (empty entity detection, skipped hooks, duplicate drawers) with no diagnostic message.

**Fix:** Add `logger.warning("...: %s", exc)` before each fallback.

---

## BUG-007 — `llm_client.py` IPv6 locality check matches any `fc`/`fd` hostname

**Severity:** High
**Reported:** 2026-05-19
**Resolved:** 2026-05-19 - commit `69aa37f`
**Affects:** `llm_client.py` `_is_local_endpoint()` - external API privacy gate

`host.startswith("fc") or host.startswith("fd")` matches FQDNs like `fchart.com`, not just
IPv6 unique-local addresses. The external-API warning is silently suppressed for public hosts.

**Fix:** Add colon guard: `if ":" in host and (host.startswith("fc") or host.startswith("fd")):`

---

## WARN-003 — Missing `encoding="utf-8"` on 7 file I/O call sites

**Severity:** Medium
**Reported:** 2026-05-19
**Resolved:** 2026-05-19 - commit `69aa37f`
**Affects:** Windows users with non-ASCII content (names, paths, YAML values)

File opens without explicit encoding default to system locale (CP1252 on Windows), causing
silent corruption or `UnicodeDecodeError` on Romanian diacritics, CJK, emoji, etc.

Sites: `miner.py:328`, `room_detector_local.py:295`, `config.py:265,291,422,438`, `hooks_cli.py:460`

**Fix:** Add `encoding="utf-8"` to all call sites.

---

## WARN-004 — `convo_miner.py` sha256 uses system-locale encoding for path hashing

**Severity:** Low
**Reported:** 2026-05-19
**Resolved:** 2026-05-19 - commit `69aa37f`
**Affects:** Sentinel ID stability on non-ASCII file paths

`source_file.encode()` uses platform default. Different hash on different systems for same path.

**Fix:** `source_file.encode("utf-8")`

---

## WARN-005 — `llm_refine.py` progress bar shows previous batch number

**Severity:** Low
**Reported:** 2026-05-19
**Resolved:** 2026-05-19 - commit `69aa37f`
**Affects:** UX only - shows "batch 0/5" while processing batch 1

**Fix:** `_print_progress(idx, len(batches), ...)` not `idx - 1`

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
