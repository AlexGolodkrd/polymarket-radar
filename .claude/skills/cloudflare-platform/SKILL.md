# Cloudflare Platform Skills

**Source**: anomalyco/opencode/.../cloudflare (151,254 ⭐)

## Categories

1. **Compute** — Workers (serverless), Pages, Durable Objects
2. **Storage** — KV, D1 (SQLite at edge), R2 (S3-compat object), Hyperdrive
3. **AI/ML** — Workers AI inference, Vectorize (vector DB), Agents SDK
4. **Networking** — Tunnel, Spectrum (TCP/UDP proxy), WebRTC
5. **Security** — WAF, DDoS, bot management

## Decision trees

### "I need to run code"
- API endpoint → Workers
- Static site + dynamic functions → Pages
- Stateful per-user → Durable Objects
- Cron job → Workers + scheduled trigger

### "I need to store data"
- Key-value, eventually consistent → KV
- Relational, transactional, low-latency → D1
- Files / blobs → R2
- Existing SQL DB connection accelerator → Hyperdrive

### "I need security"
- Bot management → Cloudflare Bot Fight Mode
- WAF rules → Cloudflare Rulesets
- DDoS → automatic on all plans

## Application to plan-kapkan

We use Cloudflare ONLY for DNS (`kapkan.4frdm.live`). Possible additions:

### Useful: WAF + Bot Management
Currently nginx basic-auth protects us. Adding Cloudflare WAF in front blocks bot scanners before they hit our origin.

### Probably useful: R2 for analytics archive
Current `Executions/_archive_*/price_history.jsonl` grows on local disk. R2 is cheaper than VPS disk for cold storage.

### NOT applicable
- Workers (we run a Python Flask server, not JS)
- Durable Objects (no need)
- Vectorize (no AI/embeddings)
- D1 (we use simple JSONL files, no relational data)

## Recommendation

Don't migrate — just add Cloudflare WAF rules:
- Block requests without `User-Agent`
- Rate limit `/api/*` to 100 req/min per IP
- Block known bot networks
