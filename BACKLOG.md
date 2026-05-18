# MemPalace — Backlog

---

## BUG-001 — `mcp_call_tool` serialises arguments as a string in long Devin sessions

**Severity:** High
**Reported:** 2026-05-18
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
