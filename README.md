# POS Resource Control Tower

User-owned derivative of
[`charlesDGY/claude-codex-usage-dashboard`](https://github.com/charlesDGY/claude-codex-usage-dashboard),
retaining its MIT license and attribution. No source was copied from
`Danielw412/AI-usage-tracker`; similar metrics here are independently derived
from local Claude, Codex, and POS data.

The dashboard is local-only (`127.0.0.1`) and read-only for provider data. It
never launches, stops, or reconfigures Codex, Claude, or Autonomy8 workers.

## POS quick start

```bash
./bin/pos-tower
```

Open <http://127.0.0.1:36668/>. Management commands are
`pos-tower start|stop|restart|status|log`; they control only this dashboard.

The top-level order is intentionally simple: OpenAI/Codex first, live
POS/Autonomy8 status next, and Claude second/bottom. It includes:

- 5h/7d quota, reset, and burn context;
- API-equivalent cost estimates and locally indexed token totals;
- session, project, model, and source attribution;
- tokens per 1% quota when the active-window data makes that derivable;
- Autonomy8 worker/build/reviewer counts, four lanes, cycles, commits, branch
  heads, recent activity, and a bounded healthy-burn/runaway warning.

---

## Upstream documentation

**Language**: [中文](README.zh-CN.md) · English

A local web dashboard that unifies **Claude Code (Anthropic)** and **Codex
(OpenAI)** token usage in one browser page:

- Anthropic real-time quota (5h / 7d utilization + reset countdown)
- Codex real-time quota (same, with a manual refresh button)
- 5h billing-block cost estimation (via ccusage) + adaptive weekly budget bar
- Token breakdown by tool / subagent type / project / model
- 24-hour minute-grain trend (today vs yesterday)
- Top 20 most expensive single turns
- RTK savings overview (if you have [RTK proxy](https://github.com/yusukebe/rtk) installed)
- Codex daily/per-model USD estimation (using a locally editable price table)

stdlib-only Python (with the exception of `npx` to fetch ccusage for Claude pricing).

```
┌──────────────────────────────┬──────────────────────────────┐
│ ⚡ Anthropic real-time quota │ ⚡ Codex real-time [🔄 refresh]│
│ 5h: 39%  7d: 69%             │ 5h: 27%  7d: 4%              │
│ resets 3h2m / 2d0h           │ resets 4h32m / 7d            │
└──────────────────────────────┴──────────────────────────────┘
┌──────────────────────────────────────────────────────────────┐
│ Key insights                                                 │
│ ⚡ Anthropic 5h 39% · 7d 69% / Codex 5h 27% · 7d 4%          │
│ 🔥 Subagents are 50% of total tokens / 🛠️ Bash is 67% of     │
│    tool tokens / 💾 cache hit 97% excellent /                │
│ ✂️ RTK overall compression 90.1%                             │
└──────────────────────────────────────────────────────────────┘
... (other panels)
```

---

## Data sources (5 independent sources, complementary)

| Data | Source | Notes |
|---|---|---|
| **Anthropic 5h / 7d real-time quota** | `https://api.anthropic.com/api/oauth/usage` | Direct call to the OAuth endpoint, authoritative real-time data |
| **Codex 5h / 7d real-time quota** | `rate_limits` field in `~/.codex/sessions/.../rollout-*.jsonl` | Returned by OpenAI servers in every codex API response; no REST call, zero extra cost |
| **Claude Code detailed token breakdown** | `~/.claude/projects/**/*.jsonl` | Direct scan of all session jsonl files, bucketed by isSidechain / tool / project |
| **Cost estimation + 5h block** | `npx ccusage` (LiteLLM pricing library) | Calls ccusage CLI on every refresh (daily/blocks/session) |
| **RTK savings** | `~/.local/share/rtk/history.db` (SQLite) | Direct read of the RTK proxy's command history |
| **Codex per-session token + cost** | `~/.codex/state_5.sqlite` (SQLite) + `~/.codex/sessions/...rollout-*.jsonl` | sqlite gives per-session totals; jsonl gives input/cached/output split for cost calculation |

**Key design decisions**:

1. **No long-running daemon / hooks** — single Python process with a 60s refresh loop.
2. **No external state mutation** (settings.json / .env / shell rc are not touched), read-only.
3. **Codex pricing in a locally editable JSON** — OpenAI does not publish a stable price list for codex models, so `codex_pricing.json` is user-maintained.
4. **Cloudflare bypass** — `chatgpt.com` blocks raw curl, so Codex quota data is read from local jsonl rather than via REST.

---

## Dependencies

| Software | Purpose | Required? |
|---|---|---|
| Python 3.8+ | Runs the dashboard | ✅ |
| Node.js + npm | `npx ccusage@latest` for Claude cost estimation | ✅ (unless you skip Claude cost) |
| `~/.claude/projects/` has data | Claude Code has been used at least once | Recommended (otherwise that section is empty) |
| `~/.codex/` has data | `codex login` + at least one `codex exec` run | Recommended (otherwise Codex section is empty) |
| `~/.local/share/rtk/history.db` | [RTK proxy](https://github.com/yusukebe/rtk) installed and used | Optional (RTK panel will be empty if missing) |
| `codex` CLI in PATH | "🔄 Refresh now" button | Optional |
| `curl` (`/usr/bin/curl`) | Calls Anthropic quota endpoint | ✅ |

---

## Quick start

```bash
git clone https://github.com/charlesDGY/claude-codex-usage-dashboard.git ~/.cache/cc-dashboard
cd ~/.cache/cc-dashboard

# Upstream launcher (the POS launcher above is preferred)
./bin/ccdash start
```

Symlink `bin/ccdash` into your PATH for convenience:

```bash
mkdir -p ~/.local/bin
ln -sf ~/.cache/cc-dashboard/bin/ccdash ~/.local/bin/ccdash
# Make sure ~/.local/bin is in PATH (usually in ~/.bashrc / ~/.zshrc)
```

Open in browser:

- Local: `http://localhost:36668/`
- LAN: `http://<server-ip>:36668/`
- SSH tunnel (recommended for remote servers): `ssh -L 36668:localhost:36668 user@server`, then `http://localhost:36668/`

---

## Commands

```bash
ccdash start      # Start (skips if already running)
ccdash stop       # Stop
ccdash restart    # Restart
ccdash status     # Status + key metrics + URLs
ccdash log        # Full server log
ccdash tail       # Live tail the log
```

---

## Configuration (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `CC_DASHBOARD_PORT` | `36668` | Listen port |
| `CC_DASHBOARD_HOST` | `127.0.0.1` | Local-only bind address |
| `CC_DASHBOARD_REFRESH` | `60` | Background refresh interval (seconds) |
| `CC_DASHBOARD_CCUSAGE_TIMEOUT` | `60` | Per-call timeout for ccusage (seconds) |
| `CC_DASHBOARD_LOCAL_DAYS` | `14` | Days of local jsonl to scan |
| `CC_DASHBOARD_WEEKLY_BUDGET_USD` | `0` (adaptive) | Weekly budget reference line (0 = max of last 4 weeks × 1.1) |
| `CC_DASHBOARD_CODEX_PROBE_AGE_S` | `0` | Auto-run a minimal Codex probe when quota data is older than this many seconds (`0` = disabled) |
| `CC_DASHBOARD_CODEX_PROBE_MIN_INTERVAL_S` | `60` | Throttle for the "🔄 Refresh now" button |
| `CC_DASHBOARD_LOG` | `<repo>/dashboard.log` | Log file path |
| `CC_DASHBOARD_PID` | `<repo>/dashboard.pid` | PID file path |

Example:

```bash
CC_DASHBOARD_PORT=8088 CC_DASHBOARD_HOST=127.0.0.1 ccdash restart
```

---

## Adapting on a new server

### Step 1: install dependencies

```bash
# Python 3.8+ usually pre-installed; node packages vary by distro
sudo apt install -y python3 nodejs npm  # or brew / yum / pacman equivalents
node --version  # confirm ≥ v18
python3 --version
```

### Step 2: log in to Claude Code (for Anthropic quota)

The dashboard reads `~/.claude/.credentials.json` for the OAuth token. This file
appears automatically once you've used the `claude` CLI and logged in. Verify:

```bash
ls -la ~/.claude/.credentials.json
python3 -c "import json; d=json.load(open('$HOME/.claude/.credentials.json'))['claudeAiOauth']; print('subscription:', d.get('subscriptionType'), 'tier:', d.get('rateLimitTier'))"
# Expected: subscription: max / pro / team, tier: <some value>
```

If the file is missing or you get 401, run `claude` CLI once to log in.

### Step 3: log in to Codex (for OpenAI quota)

```bash
# codex CLI (npm package)
npm i -g @openai/codex
codex login
# Choose "Sign in with ChatGPT" and follow the device-code flow
codex login status  # confirms "Logged in using ChatGPT"
ls -la ~/.codex/auth.json  # should exist
```

**Important**: the dashboard reads the `rate_limits` field from
`~/.codex/sessions/*/rollout-*.jsonl`, **which only appears after codex has
actually called the API at least once**. So after login, run **at least one
codex exec**:

```bash
echo 'hi' | codex exec --skip-git-repo-check
# This call makes the OpenAI server return current rate_limits, written to jsonl
```

### Step 4: clone + start

```bash
git clone https://github.com/charlesDGY/claude-codex-usage-dashboard.git ~/.cache/cc-dashboard
cd ~/.cache/cc-dashboard
./bin/ccdash start
```

Open the browser, wait ~60 seconds for the first refresh.

### Step 5: tune pricing (optional)

`codex_pricing.json` contains my **estimated** OpenAI codex model pricing
(2026-04 guesses, not authoritative). For accurate numbers, check
[OpenAI pricing](https://openai.com/api/pricing/) and edit the file. The
dashboard will pick up changes on the next refresh.

```bash
vim ~/.cache/cc-dashboard/codex_pricing.json
```

Format:

```json
{
  "gpt-5.5":      {"input": 5.0, "cached_input": 0.5, "output": 20.0},
  "gpt-5.4-mini": {"input": 0.30, "cached_input": 0.03, "output": 1.20},
  "default":      {"input": 5.0, "cached_input": 0.5, "output": 20.0}
}
```

USD per 1M tokens. `cached_input` is the discounted prompt-cache rate (typically
1/10 of input). `reasoning_output_tokens` are billed at the `output` rate.

### Step 6 (optional): Claude Code statusline enhancement

`extras/statusline_v2.sh` is a script that adds cost / burn rate / context %
info to the Claude Code statusline (not part of the dashboard, but uses the
same data). Install:

```bash
cp extras/statusline_v2.sh ~/.claude/scripts/statusline_v2.sh
chmod +x ~/.claude/scripts/statusline_v2.sh

# Edit ~/.claude/settings.json, set statusLine.command to "bash ~/.claude/scripts/statusline_v2.sh"
```

See `extras/README.md`.

---

## Architecture / data flow

```
                 ┌────────────────────────────────────┐
                 │  cc_dashboard.py (single Python)   │
                 │                                    │
   every 60s     │   ┌────────────────────────┐       │
   background    │   │ refresh_loop (thread)  │       │
   refresh       │   └────┬───────────────────┘       │
                 │        │ 5 sources:                │
                 │        ├─→ run_ccusage daily/...   │
                 │        ├─→ scan_local()             │
                 │        ├─→ scan_rtk()               │
                 │        ├─→ scan_codex()             │
                 │        └─→ fetch_anthropic_usage()  │
                 │                 ↓                   │
                 │        _state (in-memory dict)      │
                 │                 ↓                   │
                 │   ┌────────────────────────┐       │
                 │   │ ThreadingHTTPServer    │       │
                 │   │  GET /     → HTML      │       │
                 │   │  GET /api/data → JSON  │       │
                 │   │  POST /api/codex_probe │       │
                 │   └────────────────────────┘       │
                 └────────────────────────────────────┘
                              ↑ browser polls every 30s
```

**Data-flow details**:

1. **Startup**: `cc_dashboard.py` forks a background thread for `refresh_loop`; the main thread runs the HTTP server.
2. **refresh_loop**: every 60s, re-runs all 5 data sources; failure of any one does not affect the others.
3. **HTTP handler**: when a browser GETs `/api/data`, return the cached `_state` directly (no new computation).
4. **Probe button**: POST to `/api/codex_probe` triggers a background `codex exec` call, writing a new jsonl that the next refresh picks up.
5. **HTML page**: a static string with embedded vanilla JS, fetches `/api/data` every 30s and re-renders.

Threading model:

- 1 main thread (HTTP server, ThreadingHTTPServer handles concurrent requests)
- 1 background refresh thread (continuous)
- Occasional probe threads (spawned on demand)
- Data access is protected by `_state_lock`

---

## Security / privacy

The dashboard is **read-only** and does not modify any external state. It reads:

| Path | Content | What we do |
|---|---|---|
| `~/.claude/.credentials.json` | Anthropic OAuth token | Read access_token, send Bearer to `api.anthropic.com/api/oauth/usage` |
| `~/.claude/projects/**/*.jsonl` | Full Claude Code session history | Stream-read + aggregate stats, nothing sent externally |
| `~/.codex/auth.json` | OpenAI OAuth token | **Read into memory only**; not used for any network call (Codex quota comes from jsonl, not REST) |
| `~/.codex/sessions/**/*.jsonl` | Codex session history + server-returned rate_limits | Stream-read |
| `~/.codex/state_5.sqlite` | Codex metadata DB | Opened in read-only mode |
| `~/.local/share/rtk/history.db` | RTK command history | Opened in read-only mode |

**Network requests**:

- `https://api.anthropic.com/api/oauth/usage` — uses Anthropic OAuth Bearer token (every 60s)
- `npx ccusage@latest ...` — Node.js subprocess, calls the LiteLLM pricing library (one-time download, then cached)
- `codex exec` triggered via POST `/api/codex_probe` — calls codex CLI, sends an OpenAI API request (only on button click, throttled to 60s)

**Binding in this derivative**: defaults to `127.0.0.1:36668` and is local-only.
For public/untrusted networks, strongly recommend `CC_DASHBOARD_HOST=127.0.0.1` + SSH tunnel.

The dashboard sends no telemetry and calls no external webhooks.

---

## Uninstall

```bash
ccdash stop
rm -rf ~/.cache/cc-dashboard            # entire repo
rm -f ~/.local/bin/ccdash               # symlink
# Restore statusline (if you installed v2):
# Edit ~/.claude/settings.json, restore the original statusLine.command value
```

---

## Troubleshooting

### `ccdash status` says running but the browser can't connect

```bash
ccdash log | tail -50  # check errors
ss -lntp | grep 36668  # is the port occupied?
```

### Anthropic card shows "data unavailable (token expired?)"

```bash
# Check if the token has expired
python3 -c "import json,time; d=json.load(open('$HOME/.claude/.credentials.json'))['claudeAiOauth']; print('expires_at:', d.get('expiresAt'), 'now:', int(time.time()*1000))"
# If expires_at < now, run claude CLI once to auto-refresh
claude --help > /dev/null
```

### Codex card shows "no codex session files found"

```bash
# Codex hasn't run anything yet
codex login status  # must be "Logged in"
echo 'hi' | codex exec --skip-git-repo-check  # run once to produce a session jsonl
ls -la ~/.codex/sessions/*/*/*/rollout-*.jsonl 2>&1 | tail
```

### Codex quota data is "hours old"

Normal — this data is only pushed by the server when `codex exec` is called.
Click the **🔄 Refresh now** button on the dashboard to trigger a minimal codex
call that pulls fresh data; the dashboard will reflect it after the probe
finishes or on the next backend refresh (about 60s by default).

If you don't want to spend any quota on a probe, just wait — every routine codex
call automatically refreshes the data.

### ccusage stuck / slow / failing

```bash
# First-ever ccusage run downloads the LiteLLM pricing library (~few MB), so it's slow
node --version  # must be ≥ v18
npx -y ccusage@latest --version  # test that ccusage runs
```

### "Local scan" picks up too many old files; today's data looks small

The dashboard scans 14 days of history by default (`CC_DASHBOARD_LOCAL_DAYS=14`).
If you only want today + yesterday: `CC_DASHBOARD_LOCAL_DAYS=2 ccdash restart`

### Port conflict

```bash
CC_DASHBOARD_PORT=8088 ccdash restart
```

---

## Known limitations

1. **Codex real-time freshness**: 5h/7d % data only refreshes after a codex call, so it can be a few minutes stale. Anthropic data is from a direct REST call and is truly real-time.
2. **Codex pricing estimates are not authoritative**: OpenAI's published pricing for GPT-5.x changes frequently; the bundled `codex_pricing.json` is an estimate as of 2026-04.
3. **No multi-account split**: if multiple ChatGPT accounts have logged in to codex on the same machine, the dashboard reads whichever is currently active in `~/.codex/auth.json`.
4. **No origin attribution for codex calls** — calls from "Claude orchestrating Codex" vs "standalone Codex flow" are all counted on the codex side together.

---

## License

MIT (see `LICENSE`)

---

## Acknowledgements

- [`ccusage`](https://github.com/ryoppippi/ccusage) — Claude Code usage analyzer; this dashboard relies on it for Claude cost estimation.
- [`rtk`](https://github.com/yusukebe/rtk) — local command proxy (optional data source).
- The approach of reading session jsonl files directly comes from reverse-engineering Claude Code / Codex CLI's persistence strategy.
---

## Star History

<a href="https://star-history.com/#charlesDGY/claude-codex-usage-dashboard&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=charlesDGY/claude-codex-usage-dashboard&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=charlesDGY/claude-codex-usage-dashboard&type=Date" />
   <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=charlesDGY/claude-codex-usage-dashboard&type=Date" />
 </picture>
</a>
