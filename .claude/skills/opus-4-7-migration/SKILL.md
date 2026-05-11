# Opus 4.7 Migration Scanner

**Source**: Mathews-Tom/armory/skills/opus-4-7-migration

## What it does

Static analysis tool that scans a repo for Claude Opus 4.6 → 4.7 migration issues.

## What it detects

### Deterministic (always actionable)

**Category A**: `budget_tokens=N` with Extended Thinking — 4.7 doesn't support fixed budgets.

**Category B**: Outdated model aliases:
- `claude-opus-4-5` → must be `claude-opus-4-7`
- `claude-3-5-sonnet-latest` → check current routing
- `claude-3-haiku-20240307` → retired

**Category C**: Hardcoded `model="..."` strings outside config files.

### Heuristic (manual review needed)

**Category D**: Prompts assuming 4.6's verbose default. 4.7 is terser by default — explicit length hints needed.

**Category E**: Parallel sub-agent dispatch without explicit independence markers.

## Run

```bash
python3 scripts/scan.py /path/to/repo
python3 scripts/scan.py /path/to/repo --categories A,B,C  # deterministic only
python3 scripts/scan.py /path/to/repo --format json       # for CI
```

## Application to plan-kapkan

This Python project has NO Anthropic API client embedded — we don't call Claude directly. So:

- Categories A-E **not applicable** to our scan code
- BUT: any future skill/agent we build that uses `anthropic` SDK should be scanned with this tool

**Action**: bookmark for future use. Not relevant to current bugs.
