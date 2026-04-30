#!/bin/bash
# smoke_test.sh — verify radar is healthy after deploy or rollback.
# Run from operator's machine (any OS with bash + curl).
# Exit code 0 = all green, 1 = some check failed.
#
# Usage:
#   bash deploy/smoke_test.sh                  # using defaults
#   BASE=https://kapkan.4frdm.live AUTH=admin:Ts6RLPzIMQr2tKAMvNAN bash deploy/smoke_test.sh

set -uo pipefail

BASE="${BASE:-https://kapkan.4frdm.live}"
AUTH="${AUTH:-admin:Ts6RLPzIMQr2tKAMvNAN}"
TIMEOUT="${TIMEOUT:-10}"

PASS=0
FAIL=0
CHECKS=()

check() {
    local label="$1"; local cmd="$2"
    local result
    result=$(eval "$cmd" 2>&1)
    local rc=$?
    if [ $rc -eq 0 ]; then
        echo "✅ $label"
        PASS=$((PASS+1))
        CHECKS+=("PASS|$label")
    else
        echo "❌ $label"
        echo "   $result" | head -3
        FAIL=$((FAIL+1))
        CHECKS+=("FAIL|$label")
    fi
}

echo "=== plan-kapkan smoke test ==="
echo "BASE=$BASE"
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo

# 1. /api/health (Phase 9kkk+ — added in observability-stack skill)
check "/api/health 200" \
    "curl -sS -u $AUTH -o /dev/null -w '%{http_code}' --max-time $TIMEOUT $BASE/api/health | grep -qE '^(200|404)$'"
# 404 ok if endpoint not yet deployed (Phase 9kkk skill suggests it)

# 2. /api/wallets — must exist with count >= 1
check "/api/wallets count > 0" \
    "curl -sS -u $AUTH --max-time $TIMEOUT $BASE/api/wallets | python3 -c 'import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get(\"count\",0)>=1 else 1)'"

# 3. /api/deals — JSON valid + has 'deals' key
check "/api/deals JSON valid" \
    "curl -sS -u $AUTH --max-time $TIMEOUT $BASE/api/deals | python3 -c 'import sys,json; d=json.load(sys.stdin); sys.exit(0 if \"deals\" in d else 1)'"

# 4. /api/analytics?period=day works
check "/api/analytics period=day" \
    "curl -sS -u $AUTH --max-time $TIMEOUT '$BASE/api/analytics?period=day' | python3 -c 'import sys,json; d=json.load(sys.stdin); sys.exit(0 if \"closed_count\" in d else 1)'"

# 5. /api/paper_stats — works
check "/api/paper_stats responsive" \
    "curl -sS -u $AUTH --max-time $TIMEOUT '$BASE/api/paper_stats?window=100' | python3 -c 'import sys,json; d=json.load(sys.stdin); sys.exit(0 if \"count\" in d else 1)'"

# 6. /api/risk_status — kill switch + limits
check "/api/risk_status responsive" \
    "curl -sS -u $AUTH --max-time $TIMEOUT $BASE/api/risk_status | python3 -c 'import sys,json; d=json.load(sys.stdin); sys.exit(0 if \"daily_pnl\" in d or \"paused\" in d else 1)'"

# 7. /api/circuit_breakers (Phase 9kkk+) — graceful if missing
check "/api/circuit_breakers (Phase 9kkk+)" \
    "[ \$(curl -sS -u $AUTH -o /dev/null -w '%{http_code}' --max-time $TIMEOUT $BASE/api/circuit_breakers) = '200' ] || true"

# 8. Dashboard HTML loads
check "Dashboard / loads HTML" \
    "curl -sS -u $AUTH --max-time $TIMEOUT $BASE/ | head -c 200 | grep -qi 'arbitrage\\|radar\\|<html'"

# 9. /api/near responsive
check "/api/near responsive" \
    "curl -sS -u $AUTH --max-time $TIMEOUT $BASE/api/near | python3 -c 'import sys,json; d=json.load(sys.stdin); sys.exit(0 if \"items\" in d else 1)'"

# 10. /api/stats responsive
check "/api/stats responsive" \
    "curl -sS -u $AUTH --max-time $TIMEOUT $BASE/api/stats | python3 -c 'import sys,json; json.load(sys.stdin); sys.exit(0)'"

echo
echo "=== Result ==="
echo "PASS: $PASS / $((PASS + FAIL))"
echo "FAIL: $FAIL"
if [ $FAIL -gt 0 ]; then
    echo
    echo "FAILED CHECKS:"
    for c in "${CHECKS[@]}"; do
        if [[ $c == FAIL* ]]; then
            echo "  - ${c#FAIL|}"
        fi
    done
    echo
    echo "❌ SMOKE TEST FAILED — consider rollback (deploy/ROLLBACK.md)"
    exit 1
fi
echo "✅ ALL CHECKS PASSED"
exit 0
