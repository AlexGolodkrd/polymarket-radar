# Python Execution Sandbox

**Source**: trohitg/MachinaOS/server/skills/coding_agent/python-skill

## Overview

Execute Python code for calculations, data processing, automation. Sandboxed; no network, no FS, no subprocess. 30-second timeout.

## Available libraries (sandbox-safe)

- `math` — math operations
- `json` — encoding/decoding
- `datetime` / `timedelta` — time manipulation (relevant: prevents timezone bugs)
- `re` — regex
- `random` — random numbers
- `collections` — Counter, defaultdict

## Built-in variables

- `input_data` — workflow node data dict
- `output` — set this to return result

## Why useful for plan-kapkan

Reference for **timezone-aware datetime patterns** that we use in:
- `_next_utc_midnight()` (just fixed in Phase 9tt)
- `is_within_window()`
- analytics aggregation
- session state TTL

The skill enforces:
- Always `datetime.now(timezone.utc)` (never naked `datetime.now()`)
- Always `tz=timezone.utc` on `fromtimestamp()`
- `timedelta` for arithmetic (not `replace(day=...)`)

## Key patterns we should follow

```python
from datetime import datetime, timedelta, timezone

now = datetime.now(timezone.utc)
tomorrow = now + timedelta(days=1)            # ✅ never crashes
# vs
tomorrow = now.replace(day=now.day + 1)       # ❌ ValueError on month-end
```

```python
ts = float(unix_ms) / 1000 if float(unix_ms) > 1e12 else float(unix_ms)
iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
```

We followed this pattern for the SX/Limitless `end_date` parsing in near_summary.

## Repository

https://github.com/trohitg/MachinaOS
