#!/usr/bin/env python3
"""POS Resource Control Tower (stdlib-only, local and read-only).

Two data sources:
  1. ccusage --json  → daily / 5h-block / session totals + costs (LiteLLM pricing)
  2. local jsonl scan → fine-grained per-tool / per-subagent / per-project
                        / hourly trend / cache efficiency / top-expensive-turns

Caches both in memory; background thread refreshes every REFRESH_SECONDS.
Binds 127.0.0.1:<port> for local-only access.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("CC_DASHBOARD_PORT", "36668"))
HOST = os.environ.get("CC_DASHBOARD_HOST", "127.0.0.1")
REFRESH_SECONDS = int(os.environ.get("CC_DASHBOARD_REFRESH", "60"))
CCUSAGE_TIMEOUT = int(os.environ.get("CC_DASHBOARD_CCUSAGE_TIMEOUT", "60"))
LOCAL_SCAN_DAYS = int(os.environ.get("CC_DASHBOARD_LOCAL_DAYS", "14"))
WEEKLY_BUDGET_USD = float(os.environ.get("CC_DASHBOARD_WEEKLY_BUDGET_USD", "0"))  # 0 = adaptive
# Codex active-probe: if data is older than this many seconds, run a minimal codex call to refresh.
# 0 = disabled (button-only, no auto probe).
CODEX_AUTO_PROBE_AGE_SECONDS = int(os.environ.get("CC_DASHBOARD_CODEX_PROBE_AGE_S", "0"))
CODEX_PROBE_MIN_INTERVAL = int(os.environ.get("CC_DASHBOARD_CODEX_PROBE_MIN_INTERVAL_S", "60"))  # min 60s between probes

PROJECTS_DIR = Path.home() / ".claude" / "projects"
RTK_DB = Path(os.environ.get("CC_DASHBOARD_RTK_DB", str(Path.home() / ".local" / "share" / "rtk" / "history.db"))).expanduser()
CREDS_FILE = Path.home() / ".claude" / ".credentials.json"
ANTHROPIC_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
CODEX_DB = Path.home() / ".codex" / "state_5.sqlite"
CODEX_PRICING_FILE = Path(__file__).with_name("codex_pricing.json")
AUTONOMY8_ROOT = Path.home() / "POS-builds" / "claude-autonomy8"
AUTONOMY8_STATUS = AUTONOMY8_ROOT / "STATUS.json"
AUTONOMY8_LOG = Path("/tmp/a8.log")
AUTONOMY8_REPO = Path.home() / "POS-builds" / "autonomous-dispatch-v0"
AUTONOMY8_BASES = {
    "dispatcher": "recovery/r2-boundary-bypass-20261002",
    "serial": "management/bounded-claude-serial-batch-v1-20261003",
    "v4f": "recovery/r2-k2-k3-census-v4f-exact-byte-admission-20261003",
    "r2pin": "recovery/r2-boundary-bypass-20261002",
}

_state = {
    "daily": None, "blocks": None, "session": None, "weekly": None, "anthropic": None, "codex": None,
    "autonomy8": None,
    "local": None,
    "rtk": None,
    "last_refresh_ts": 0, "last_refresh_status": "init", "errors": [],
}
_state_lock = threading.Lock()
_codex_probe_state = {"last_probe_ts": 0, "last_probe_status": None, "in_progress": False, "last_probe_reason": None}
_codex_probe_lock = threading.Lock()


def run_ccusage(args):
    try:
        cmd = ["npx", "-y", "ccusage@latest", *args, "--json", "--offline"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=CCUSAGE_TIMEOUT)
        if proc.returncode != 0:
            return None, f"rc={proc.returncode}: {proc.stderr[:200]}"
        return json.loads(proc.stdout), None
    except subprocess.TimeoutExpired:
        return None, f"timeout {CCUSAGE_TIMEOUT}s"
    except Exception as e:
        return None, str(e)


def _iter_jsonl_files(root):
    """Yield ``*.jsonl`` Paths under *root*, tolerant of dirs vanishing mid-walk.

    Claude session dirs under ``~/.claude/projects/`` are reclaimed at runtime.
    A plain ``Path.rglob()`` raises ``FileNotFoundError`` if a directory
    disappears between enumeration and descent, which previously propagated out
    of ``scan_local`` and killed the refresh thread (auto-update silently
    stopped). ``os.walk`` with an ``onerror`` callback skips such dirs instead.
    """
    for dirpath, _dirnames, filenames in os.walk(root, onerror=lambda _e: None):
        for name in filenames:
            if name.endswith(".jsonl"):
                yield Path(dirpath) / name


def scan_local():
    """Walk ~/.claude/projects/**/*.jsonl and aggregate fine-grained metrics."""
    if not PROJECTS_DIR.is_dir():
        return None, "projects dir missing"

    cutoff = time.time() - LOCAL_SCAN_DAYS * 86400
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    last7_cutoff = (today - timedelta(days=7)).isoformat()

    by_tool = defaultdict(lambda: defaultdict(float))           # tool -> metrics (recent)
    by_project = defaultdict(lambda: defaultdict(float))        # project -> metrics
    main_tot = defaultdict(float)
    sub_tot = defaultdict(float)
    sub_types_count = defaultdict(int)                          # subagent_type -> invocations
    sub_types_token_proxy = defaultdict(float)                  # subagent_type -> output_tokens of the spawn turn (proxy)
    by_hour_today = defaultdict(float)                          # hour 0-23 -> tokens
    by_hour_yesterday = defaultdict(float)
    cache_today = defaultdict(float)                            # 'cr','cc','in','out' counts
    cache_7d = defaultdict(float)
    cache_all = defaultdict(float)
    content_kind = defaultdict(int)                             # thinking|text|tool_use counts
    expensive_turns = []                                        # heaviest single turns
    files_scanned = 0
    files_skipped_old = 0
    msg_total = 0

    for jsonl in _iter_jsonl_files(PROJECTS_DIR):
        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            files_skipped_old += 1
            continue
        files_scanned += 1
        # Detect subagent files by path (under <project>/<uuid>/subagents/)
        is_subagent_file = "/subagents/" in str(jsonl)
        # Project name = top-level dir under PROJECTS_DIR
        rel = jsonl.relative_to(PROJECTS_DIR)
        proj_name = rel.parts[0] if rel.parts else jsonl.parent.name
        # Strip leading dash and decode path-style project name back to /-/
        proj_label = proj_name.lstrip("-").replace("-", "/").replace("//", "/")
        # Trim to last 2-3 path segments for display
        parts = proj_label.split("/")
        proj_short = "/".join(parts[-3:]) if len(parts) > 3 else proj_label

        try:
            with open(jsonl) as fp:
                for line in fp:
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("type") != "assistant":
                        continue
                    msg_total += 1
                    msg = obj.get("message") or {}
                    usage = msg.get("usage") or {}
                    in_t = int(usage.get("input_tokens") or 0)
                    out_t = int(usage.get("output_tokens") or 0)
                    cc_t = int(usage.get("cache_creation_input_tokens") or 0)
                    cr_t = int(usage.get("cache_read_input_tokens") or 0)
                    total = in_t + out_t + cc_t + cr_t
                    sidechain = bool(obj.get("isSidechain")) or is_subagent_file
                    ts = obj.get("timestamp", "") or ""
                    date_str = ts[:10] if ts else ""

                    # By-project
                    bp = by_project[proj_short]
                    bp["turns"] += 1
                    bp["input"] += in_t; bp["output"] += out_t
                    bp["cache_create"] += cc_t; bp["cache_read"] += cr_t
                    bp["total"] += total

                    # Main vs subagent
                    bucket = sub_tot if sidechain else main_tot
                    bucket["turns"] += 1
                    bucket["input"] += in_t; bucket["output"] += out_t
                    bucket["cache_create"] += cc_t; bucket["cache_read"] += cr_t
                    bucket["total"] += total

                    # Cache breakdown
                    cache_all["in"] += in_t; cache_all["out"] += out_t
                    cache_all["cc"] += cc_t; cache_all["cr"] += cr_t
                    if date_str >= last7_cutoff:
                        cache_7d["in"] += in_t; cache_7d["out"] += out_t
                        cache_7d["cc"] += cc_t; cache_7d["cr"] += cr_t
                    if date_str == today.isoformat():
                        cache_today["in"] += in_t; cache_today["out"] += out_t
                        cache_today["cc"] += cc_t; cache_today["cr"] += cr_t

                    # Hourly trend
                    if len(ts) >= 13:
                        try:
                            hr = int(ts[11:13])
                            if date_str == today.isoformat():
                                by_hour_today[hr] += total
                            elif date_str == yesterday.isoformat():
                                by_hour_yesterday[hr] += total
                        except ValueError:
                            pass

                    # Tool-level + content-kind + subagent_type
                    tools = set()
                    for c in msg.get("content") or []:
                        kind = c.get("type")
                        if kind:
                            content_kind[kind] += 1
                        if kind == "tool_use":
                            tn = c.get("name") or "?"
                            tools.add(tn)
                            if tn in ("Agent", "Task"):
                                inp = c.get("input") or {}
                                st = inp.get("subagent_type") or inp.get("agent_type") or ""
                                if st:
                                    sub_types_count[st] += 1
                                    sub_types_token_proxy[st] += out_t / max(1, len(tools))

                    if tools:
                        share_out = out_t / len(tools)
                        share_cr = cr_t / len(tools)
                        share_total = total / len(tools)
                        for tn in tools:
                            bt = by_tool[tn]
                            bt["turns"] += 1
                            bt["output"] += share_out
                            bt["cache_read"] += share_cr
                            bt["total"] += share_total

                    # Top expensive turns (output tokens)
                    if out_t >= 3000:
                        expensive_turns.append({
                            "ts": ts,
                            "project": proj_short,
                            "model": (msg.get("model") or "").replace("claude-", ""),
                            "output": out_t,
                            "cache_read": cr_t,
                            "tools": sorted(tools)[:6],
                            "sidechain": sidechain,
                        })
        except Exception:
            continue

    expensive_turns.sort(key=lambda x: -x["output"])

    def cache_pct(d):
        denom = d["in"] + d["cc"] + d["cr"]
        return (d["cr"] / denom * 100) if denom > 0 else 0.0

    return {
        "by_tool": {k: dict(v) for k, v in by_tool.items()},
        "by_project": {k: dict(v) for k, v in by_project.items()},
        "main_total": dict(main_tot),
        "sub_total": dict(sub_tot),
        "sub_types_count": dict(sub_types_count),
        "sub_types_token_proxy": dict(sub_types_token_proxy),
        "by_hour_today": dict(by_hour_today),
        "by_hour_yesterday": dict(by_hour_yesterday),
        "cache_today": dict(cache_today),
        "cache_7d": dict(cache_7d),
        "cache_all": dict(cache_all),
        "cache_pct_today": cache_pct(cache_today),
        "cache_pct_7d": cache_pct(cache_7d),
        "cache_pct_all": cache_pct(cache_all),
        "content_kind": dict(content_kind),
        "expensive_turns": expensive_turns[:20],
        "files_scanned": files_scanned,
        "files_skipped_old": files_skipped_old,
        "msg_total": msg_total,
        "scan_days": LOCAL_SCAN_DAYS,
    }, None



def scan_rtk():
    """Read RTK history.db and return aggregated savings metrics."""
    if not RTK_DB.is_file():
        return None, "rtk db missing"
    try:
        con = sqlite3.connect(f"file:{RTK_DB}?mode=ro", uri=True, timeout=2.0)
        con.row_factory = sqlite3.Row
        cur = con.cursor()

        cur.execute("""SELECT COUNT(*) c, SUM(input_tokens) raw_in, SUM(output_tokens) rtk_out,
                              SUM(saved_tokens) saved, AVG(savings_pct) avg_pct,
                              MIN(date(timestamp)) first_day, MAX(date(timestamp)) last_day
                       FROM commands""")
        s = dict(cur.fetchone())
        # Real overall compression: saved / raw_in (not the avg of per-row pcts,
        # which drags down because many tiny commands skew the average)
        s["overall_pct"] = (s["saved"] / s["raw_in"] * 100) if (s.get("raw_in") or 0) > 0 else 0.0

        cur.execute("""SELECT date(timestamp) day, COUNT(*) cnt,
                              SUM(saved_tokens) saved, ROUND(AVG(savings_pct),1) pct,
                              SUM(input_tokens) raw_in, SUM(output_tokens) rtk_out
                       FROM commands
                       WHERE date(timestamp) >= date('now','-13 days')
                       GROUP BY date(timestamp) ORDER BY day DESC""")
        daily = [dict(r) for r in cur.fetchall()]

        cur.execute("""SELECT substr(rtk_cmd, 1, 40) cmd, COUNT(*) cnt,
                              SUM(input_tokens) raw_in, SUM(output_tokens) rtk_out,
                              SUM(saved_tokens) saved, ROUND(AVG(savings_pct),1) pct
                       FROM commands
                       GROUP BY substr(rtk_cmd, 1, 40)
                       ORDER BY saved DESC LIMIT 15""")
        by_cmd = [dict(r) for r in cur.fetchall()]

        cur.execute("""SELECT project_path, COUNT(*) cnt, SUM(saved_tokens) saved
                       FROM commands WHERE project_path != ''
                       GROUP BY project_path ORDER BY saved DESC LIMIT 10""")
        by_proj = [dict(r) for r in cur.fetchall()]

        cur.execute("""SELECT COUNT(*) c FROM parse_failures
                       WHERE date(timestamp) >= date('now','-6 days')""")
        failures_7d = cur.fetchone()[0]

        # Also: which commands DON'T compress well (low savings_pct → wasted overhead)
        cur.execute("""SELECT substr(rtk_cmd, 1, 40) cmd, COUNT(*) cnt,
                              ROUND(AVG(savings_pct),1) avg_pct,
                              SUM(input_tokens) raw_in
                       FROM commands
                       WHERE rtk_cmd NOT LIKE 'rtk fallback:%'
                         AND input_tokens > 0
                       GROUP BY substr(rtk_cmd, 1, 40)
                       HAVING cnt >= 5
                       ORDER BY avg_pct ASC LIMIT 8""")
        worst = [dict(r) for r in cur.fetchall()]

        con.close()
        return {
            "summary": s,
            "daily": daily,
            "by_cmd": by_cmd,
            "by_project": by_proj,
            "failures_7d": failures_7d,
            "worst_commands": worst,
        }, None
    except sqlite3.Error as e:
        return None, f"sqlite: {e}"



import urllib.request
import urllib.error

def fetch_anthropic_usage():
    """Fetch real-time usage from Anthropic OAuth endpoint."""
    if not CREDS_FILE.is_file():
        return None, "creds file missing"
    try:
        creds = json.loads(CREDS_FILE.read_text())
        oauth = creds.get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
        sub = oauth.get("subscriptionType") or "?"
        tier = oauth.get("rateLimitTier") or "?"
        if not token:
            return None, "no access token"

        req = urllib.request.Request(
            ANTHROPIC_USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "claude-cli/2.1.122",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            data["_meta"] = {
                "subscription": sub,
                "tier": tier,
                "fetched_at": int(time.time()),
            }
            return data, None
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode()[:200]
        except: pass
        return None, f"HTTP {e.code}: {body}"
    except urllib.error.URLError as e:
        return None, f"URL error: {e.reason}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"




def probe_codex(reason="manual"):
    """Run a minimal codex exec to trigger a fresh rate_limits response in session jsonl.
    Cost: ~1 prompt token + reasoning_effort=minimal. Throttled to once per N seconds.
    """
    with _codex_probe_lock:
        if _codex_probe_state["in_progress"]:
            return False, "probe already in progress"
        now = int(time.time())
        last = _codex_probe_state["last_probe_ts"]
        if now - last < CODEX_PROBE_MIN_INTERVAL:
            return False, f"throttled (last probe {now - last}s ago, min interval {CODEX_PROBE_MIN_INTERVAL}s)"
        _codex_probe_state["in_progress"] = True
        _codex_probe_state["last_probe_reason"] = reason

    try:
        cmd = ["codex", "exec", "--model", "gpt-5.4-mini",
               "-c", 'model_reasoning_effort="low"',
               "--skip-git-repo-check"]
        proc = subprocess.run(cmd, input="ok\n", capture_output=True, text=True, timeout=60,
                              cwd=str(Path.home() / ".cache" / "cc-dashboard"))
        ok = proc.returncode == 0
        if ok:
            codex, _ = scan_codex()
            if codex is not None:
                with _state_lock:
                    _state["codex"] = codex
        with _codex_probe_lock:
            _codex_probe_state["last_probe_ts"] = int(time.time())
            _codex_probe_state["last_probe_status"] = "ok" if ok else f"rc={proc.returncode}: {(proc.stderr or '')[:120]}"
            _codex_probe_state["in_progress"] = False
        return ok, "probed" if ok else f"failed: {(proc.stderr or '')[:120]}"
    except Exception as e:
        with _codex_probe_lock:
            _codex_probe_state["in_progress"] = False
            _codex_probe_state["last_probe_status"] = f"exception: {e}"
        return False, str(e)


def scan_codex():
    """Read codex session jsonl rate_limits + state_5.sqlite per-session totals.
    
    Codex writes the OpenAI-server-returned rate_limits dict into every
    `event_msg` of type `token_count` in the session jsonl. We pick the LATEST
    such record (the most recent codex API call across all sessions today/yesterday)
    and that gives us authoritative real-time 5h + 7d quota %.
    """
    if not CODEX_SESSIONS_DIR.is_dir():
        return None, "codex sessions dir missing"
    
    out = {
        "rate_limits": None,
        "rate_limits_age_seconds": None,
        "rate_limits_source": None,
        "session_meta": None,
        "today": None,
        "by_day": [],
        "by_model": [],
        "recent_sessions": [],
        "by_source": [],
        "lifetime_tokens": 0,
        "lifetime_sessions": 0,
        "by_day_cost": [],   # day -> {usd, input_tokens, cached_input, output, reasoning}
        "by_model_cost": [], # model -> {usd, ...}
        "total_cost_14d": 0.0,
        "today_cost": 0.0,
    }
    
    # Load pricing
    pricing = {"default": {"input": 5.0, "cached_input": 0.5, "output": 20.0}}
    try:
        if CODEX_PRICING_FILE.is_file():
            pricing = json.loads(CODEX_PRICING_FILE.read_text())
    except Exception:
        pass
    
    def _price_for(model):
        if not model: return pricing.get("default") or pricing.get("_default") or {"input": 5.0, "cached_input": 0.5, "output": 20.0}
        # exact match first, then prefix match
        if model in pricing: return pricing[model]
        for k in pricing:
            if k.startswith("_"): continue
            if model.startswith(k) and k != "default":
                return pricing[k]
        return pricing.get("default") or {"input": 5.0, "cached_input": 0.5, "output": 20.0}
    
    def _cost(in_t, cached_t, out_t, reasoning_t, model):
        p = _price_for(model)
        # Reasoning tokens are billed as output
        return ((in_t - cached_t) * p.get("input", 5.0)
                + cached_t * p.get("cached_input", 0.5)
                + (out_t + reasoning_t) * p.get("output", 20.0)) / 1_000_000.0
    
    # Walk recent sessions newest-first by actual file mtime. Session directory names
    # are creation-time based, but long-running sessions can keep receiving fresh
    # token_count events days later.
    candidates = []
    try:
        touched = []
        for f in CODEX_SESSIONS_DIR.rglob("rollout-*.jsonl"):
            try:
                touched.append((f.stat().st_mtime, f))
            except OSError:
                continue
        candidates = [f for _, f in sorted(touched, key=lambda x: x[0], reverse=True)[:100]]
    except OSError as e:
        return None, f"sessions walk error: {e}"
    
    # Find latest rate_limits, tracked PER limit_id (see selection note below).
    latest_rl = None
    latest_rl_ts = 0
    latest_rl_file = None
    rl_by_limit = {}            # limit_id -> {"ts","rl","file"}
    _global_info_ts = 0         # for out["last_token_usage"] (global-latest token_count)
    
    for fpath in candidates:
        try:
            # Read last few KB only — token_count events are typically near the end
            with open(fpath) as fp:
                lines = fp.readlines()
            for line in reversed(lines):
                try:
                    obj = json.loads(line)
                except: continue
                if obj.get("type") == "event_msg":
                    payload = obj.get("payload") or {}
                    if payload.get("type") == "token_count" and payload.get("rate_limits"):
                        # Convert ISO timestamp to unix
                        try:
                            ts_iso = obj.get("timestamp", "")
                            if ts_iso.endswith("Z"):
                                ts_iso = ts_iso[:-1] + "+00:00"
                            from datetime import datetime as _dt
                            ts = int(_dt.fromisoformat(ts_iso).timestamp())
                        except Exception:
                            ts = 0
                        rl = payload["rate_limits"]
                        try:
                            src = str(fpath.relative_to(CODEX_SESSIONS_DIR))
                        except ValueError:
                            src = str(fpath)
                        # Keep the latest reading per limit_id (a session always
                        # reports one limit_id; aggregate the freshest per bucket).
                        lid = rl.get("limit_id") or "?"
                        prev = rl_by_limit.get(lid)
                        if prev is None or ts > prev["ts"]:
                            rl_by_limit[lid] = {"ts": ts, "rl": rl, "file": src}
                        # last_token_usage tracks the globally-latest token_count
                        if ts > _global_info_ts and payload.get("info"):
                            _global_info_ts = ts
                            out["last_token_usage"] = payload["info"]
                        break  # don't walk further back in this file
        except Exception:
            continue

    # Pick the BINDING limit bucket: the highest current usage (max of primary /
    # secondary used_percent) among the latest-per-limit_id readings, ties broken
    # by recency. A long-running session can keep emitting an always-zero
    # promotional bucket (e.g. limit_id 'codex_bengalfox' / GPT-5.x-Codex-Spark)
    # which, being continuously active, otherwise owns the globally-latest
    # token_count and masks the real plan limit ('codex'). This rule ignores
    # always-zero buckets without hardcoding any limit_id, and still reports 0
    # correctly when usage is genuinely 0.
    def _max_used_pct(rl):
        pr = (rl.get("primary") or {}).get("used_percent") or 0.0
        se = (rl.get("secondary") or {}).get("used_percent") or 0.0
        return max(pr, se)

    if rl_by_limit:
        chosen = max(rl_by_limit.values(), key=lambda e: (_max_used_pct(e["rl"]), e["ts"]))
        latest_rl = chosen["rl"]
        latest_rl_ts = chosen["ts"]
        latest_rl_file = chosen["file"]
    
    # Aggregate per-session token breakdown + cost from session jsonl
    # (Cheaper than rescanning all files: reuse `candidates` from the loop above + walk a few more)
    cost_cutoff = time.time() - 14 * 86400
    by_day_cost = defaultdict(lambda: {"input": 0, "cached_input": 0, "output": 0, "reasoning": 0, "usd": 0.0, "n_sessions": 0})
    by_model_cost = defaultdict(lambda: {"input": 0, "cached_input": 0, "output": 0, "reasoning": 0, "usd": 0.0, "n_sessions": 0})
    
    # Walk all sessions in last 14 days for cost breakdown
    cost_files = []
    try:
        for ydir in sorted(CODEX_SESSIONS_DIR.iterdir(), reverse=True):
            if not ydir.is_dir(): continue
            for mdir in sorted(ydir.iterdir(), reverse=True):
                if not mdir.is_dir(): continue
                for ddir in sorted(mdir.iterdir(), reverse=True):
                    if not ddir.is_dir(): continue
                    for f in ddir.glob("rollout-*.jsonl"):
                        try:
                            if f.stat().st_mtime >= cost_cutoff:
                                cost_files.append(f)
                        except OSError: continue
    except OSError: pass
    
    for fpath in cost_files:
        try:
            session_model = None
            session_date = None
            last_total = None  # take the LATEST total_token_usage as the session's cumulative
            with open(fpath) as fp:
                for line in fp:
                    try:
                        obj = json.loads(line)
                    except: continue
                    if obj.get("type") == "session_meta":
                        payload = obj.get("payload") or {}
                        session_date = (obj.get("timestamp") or payload.get("timestamp") or "")[:10]
                        # try to get model from rollout — actually session_meta doesn't include model directly
                    if obj.get("type") == "event_msg":
                        p = obj.get("payload") or {}
                        if p.get("type") == "token_count":
                            info = p.get("info") or {}
                            ttu = info.get("total_token_usage") or {}
                            if ttu:
                                last_total = ttu
                            if not session_date:
                                session_date = (obj.get("timestamp") or "")[:10]
                            if not session_model and p.get("model"):
                                session_model = p["model"]
                    if obj.get("type") == "turn_context" or obj.get("type") == "turn_completed":
                        p = obj.get("payload") or {}
                        if not session_model and p.get("model"):
                            session_model = p["model"]
            
            if not last_total or not session_date: continue
            in_t = last_total.get("input_tokens", 0) or 0
            cached_t = last_total.get("cached_input_tokens", 0) or 0
            out_t = last_total.get("output_tokens", 0) or 0
            reason_t = last_total.get("reasoning_output_tokens", 0) or 0
            
            # Try to read model from sqlite for this session id
            if not session_model:
                # Extract id from filename: rollout-YYYY-MM-DD...-{uuid}.jsonl
                fname = fpath.name
                # uuid is last hex segment before .jsonl
                import re
                m = re.search(r"-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", fname)
                if m and CODEX_DB.is_file():
                    try:
                        con2 = sqlite3.connect(f"file:{CODEX_DB}?mode=ro", uri=True, timeout=1.0)
                        cur2 = con2.cursor()
                        cur2.execute("SELECT model FROM threads WHERE id = ?", (m.group(1),))
                        row = cur2.fetchone()
                        if row and row[0]: session_model = row[0]
                        con2.close()
                    except Exception: pass
            session_model = session_model or "?"
            cost = _cost(in_t, cached_t, out_t, reason_t, session_model)
            
            d = by_day_cost[session_date]
            d["input"] += in_t; d["cached_input"] += cached_t
            d["output"] += out_t; d["reasoning"] += reason_t
            d["usd"] += cost; d["n_sessions"] += 1
            
            m_ = by_model_cost[session_model]
            m_["input"] += in_t; m_["cached_input"] += cached_t
            m_["output"] += out_t; m_["reasoning"] += reason_t
            m_["usd"] += cost; m_["n_sessions"] += 1
        except Exception:
            continue
    
    out["by_day_cost"] = sorted([{"day": k, **v} for k, v in by_day_cost.items()], key=lambda x: x["day"], reverse=True)
    out["by_model_cost"] = sorted([{"model": k, **v} for k, v in by_model_cost.items()], key=lambda x: -x["usd"])
    out["total_cost_14d"] = sum(v["usd"] for v in by_day_cost.values())
    today_str = datetime.now().strftime("%Y-%m-%d")
    out["today_cost"] = by_day_cost.get(today_str, {}).get("usd", 0.0)
    
    if latest_rl is not None:
        out["rate_limits"] = latest_rl
        out["rate_limits_age_seconds"] = int(time.time()) - latest_rl_ts if latest_rl_ts else None
        out["rate_limits_source"] = latest_rl_file
    
    # State_5.sqlite: per-thread tokens_used (proxy for per-session)
    if CODEX_DB.is_file():
        try:
            con = sqlite3.connect(f"file:{CODEX_DB}?mode=ro", uri=True, timeout=2.0)
            con.row_factory = sqlite3.Row
            cur = con.cursor()

            cur.execute("SELECT COUNT(*) AS n_threads, COALESCE(SUM(tokens_used), 0) AS total_tokens FROM threads")
            lifetime = dict(cur.fetchone())
            out["lifetime_sessions"] = lifetime["n_threads"]
            out["lifetime_tokens"] = lifetime["total_tokens"]
            
            # Today / 7d totals
            cur.execute("""SELECT date(updated_at, 'unixepoch', 'localtime') AS day,
                                  COUNT(*) AS n_threads,
                                  SUM(tokens_used) AS total_tokens
                           FROM threads
                           WHERE date(updated_at, 'unixepoch', 'localtime') >= date('now', '-13 days')
                           GROUP BY day ORDER BY day DESC""")
            out["by_day"] = [dict(r) for r in cur.fetchall()]
            
            # By model
            cur.execute("""SELECT COALESCE(model, '?') AS model,
                                  COUNT(*) AS n_threads,
                                  SUM(tokens_used) AS total_tokens
                           FROM threads
                           WHERE date(updated_at, 'unixepoch', 'localtime') >= date('now', '-13 days')
                           GROUP BY model ORDER BY total_tokens DESC""")
            out["by_model"] = [dict(r) for r in cur.fetchall()]

            cur.execute("""SELECT COALESCE(source, '?') AS source,
                                  COUNT(*) AS n_threads,
                                  SUM(tokens_used) AS total_tokens
                           FROM threads
                           GROUP BY source ORDER BY total_tokens DESC""")
            out["by_source"] = [dict(r) for r in cur.fetchall()]
            
            # Recent 10 sessions
            cur.execute("""SELECT id, datetime(updated_at, 'unixepoch', 'localtime') AS updated,
                                  tokens_used, source, model, reasoning_effort,
                                  substr(first_user_message, 1, 80) AS preview
                           FROM threads
                           ORDER BY updated_at DESC LIMIT 10""")
            out["recent_sessions"] = [dict(r) for r in cur.fetchall()]
            con.close()
        except sqlite3.Error as e:
            out["sqlite_error"] = str(e)
    
    return out, None


def scan_autonomy8():
    """Read controller state, process ancestry, git heads, and recent activity."""
    data = {
        "controller": None, "workers": [], "worker_count": 0,
        "builders": 0, "reviewers": 0, "lanes": {}, "activity": [],
        "health": "idle", "health_reason": "controller not running",
    }
    try:
        if AUTONOMY8_STATUS.is_file():
            snapshot = json.loads(AUTONOMY8_STATUS.read_text())
            data["snapshot_utc"] = snapshot.get("utc")
            data["snapshot_age_seconds"] = max(0, int(time.time() - AUTONOMY8_STATUS.stat().st_mtime))
            data["lanes"] = snapshot.get("lanes") or {}
    except Exception as e:
        data["status_error"] = str(e)

    try:
        rows = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,etime=,command="],
            capture_output=True, text=True, timeout=5,
        ).stdout.splitlines()
        processes = []
        for row in rows:
            m = re.match(r"\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*)", row)
            if m:
                pid, ppid, elapsed, command = m.groups()
                processes.append({"pid": int(pid), "ppid": int(ppid), "elapsed": elapsed, "command": command})
        controller = next((p for p in processes if "/tmp/a8.py" in p["command"]), None)
        if controller:
            data["controller"] = {k: controller[k] for k in ("pid", "elapsed")}
            descendants = {controller["pid"]}
            changed = True
            while changed:
                changed = False
                for p in processes:
                    if p["ppid"] in descendants and p["pid"] not in descendants:
                        descendants.add(p["pid"]); changed = True
            for p in processes:
                if p["pid"] not in descendants or p["pid"] == controller["pid"]:
                    continue
                cmd = p["command"]
                if not re.search(r"(?:^|/)claude(?:\s|$)|/claude/versions/", cmd):
                    continue
                lane = re.search(r"LANE:\s*(dispatcher|serial|v4f|r2pin)", cmd)
                reviewer = bool(re.search(r"Independent read-only (?:baseline|post-build) review", cmd, re.I))
                data["workers"].append({
                    "pid": p["pid"], "elapsed": p["elapsed"],
                    "lane": lane.group(1) if lane else "unknown",
                    "role": "reviewer" if reviewer else "builder",
                })
    except Exception as e:
        data["process_error"] = str(e)

    data["worker_count"] = len(data["workers"])
    data["builders"] = sum(w["role"] == "builder" for w in data["workers"])
    data["reviewers"] = sum(w["role"] == "reviewer" for w in data["workers"])

    for name, lane in data["lanes"].items():
        lane["short_head"] = (lane.get("head") or "")[:8]
        base = AUTONOMY8_BASES.get(name)
        if base and lane.get("head") and AUTONOMY8_REPO.is_dir():
            try:
                resolved_base = None
                for candidate in (base, "origin/" + base):
                    check = subprocess.run(
                        ["git", "rev-parse", "--verify", candidate + "^{commit}"],
                        cwd=AUTONOMY8_REPO, capture_output=True, text=True, timeout=5,
                    )
                    if check.returncode == 0:
                        resolved_base = check.stdout.strip()
                        break
                if not resolved_base:
                    continue
                proc = subprocess.run(
                    ["git", "rev-list", "--count", f"{resolved_base}..{lane['head']}"],
                    cwd=AUTONOMY8_REPO, capture_output=True, text=True, timeout=5,
                )
                if proc.returncode == 0:
                    lane["commits_ahead"] = int(proc.stdout.strip())
            except Exception:
                pass

    try:
        if AUTONOMY8_LOG.is_file():
            lines = [line.strip() for line in AUTONOMY8_LOG.read_text(errors="replace").splitlines() if line.strip()]
            data["activity"] = [line for line in lines if re.search(
                r"BUILDER_LAUNCHED|REVIEWER_LAUNCHED|LANE_|HOLD_|PUSH |ACCEPTED|HUMAN_REQUIRED|AUTONOMY8_TERMINAL",
                line,
            )][-10:]
            data["log_age_seconds"] = max(0, int(time.time() - AUTONOMY8_LOG.stat().st_mtime))
    except Exception as e:
        data["log_error"] = str(e)

    terminal = {"ACCEPTED", "HUMAN_REQUIRED", "HOLD_MAX_CYCLES"}
    statuses = [lane.get("status") for lane in data["lanes"].values()]
    stale = data.get("snapshot_age_seconds", 0) > 300
    if data["worker_count"] > 8:
        data["health"], data["health_reason"] = "suspected-runaway", "more than 8 Claude workers"
    elif data["controller"] and data["worker_count"] and stale:
        data["health"], data["health_reason"] = "suspected-runaway", "workers active but STATUS.json is over 5 minutes stale"
    elif data["controller"] and data["worker_count"] <= 8:
        data["health"], data["health_reason"] = "healthy-burn", "controller live, within 8-worker cap, status updating"
    elif statuses and all(s in terminal for s in statuses):
        data["health"], data["health_reason"] = "terminal", "all lanes reached terminal status"
    return data, None


def refresh_loop():
    while True:
        try:
            errs = []
            daily, err = run_ccusage(["daily"])
            if err: errs.append(f"daily: {err}")
            blocks, err = run_ccusage(["blocks", "--active"])
            if err: errs.append(f"blocks: {err}")
            session, err = run_ccusage(["session", "--order", "desc"])
            if err: errs.append(f"session: {err}")
            weekly, err = run_ccusage(["weekly"])
            if err: errs.append(f"weekly: {err}")
            local, err = scan_local()
            if err: errs.append(f"local: {err}")
            rtk, err = scan_rtk()
            if err: errs.append(f"rtk: {err}")
            anthro, err = fetch_anthropic_usage()
            if err: errs.append(f"anthropic: {err}")
            codex, err = scan_codex()
            if err: errs.append(f"codex: {err}")
            autonomy8, err = scan_autonomy8()
            if err: errs.append(f"autonomy8: {err}")
            with _state_lock:
                if daily is not None: _state["daily"] = daily
                if blocks is not None: _state["blocks"] = blocks
                if session is not None: _state["session"] = session
                if weekly is not None: _state["weekly"] = weekly
                if local is not None: _state["local"] = local
                if rtk is not None: _state["rtk"] = rtk
                if anthro is not None: _state["anthropic"] = anthro
                if codex is not None: _state["codex"] = codex
                if autonomy8 is not None: _state["autonomy8"] = autonomy8
                _state["last_refresh_ts"] = int(time.time())
                _state["last_refresh_status"] = "ok" if not errs else "partial"
                _state["errors"] = errs
        except Exception as e:
            # Never let an unexpected error kill the refresh thread — the whole
            # point of the dashboard is auto-updating. Log and retry next cycle.
            import traceback as _tb
            _tb.print_exc()
            try:
                with _state_lock:
                    _state["last_refresh_status"] = "error"
                    _state["errors"] = [f"refresh_loop: {e!r}"]
            except Exception:
                pass
        time.sleep(REFRESH_SECONDS)


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>POS Resource Control Tower</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }
h1 { font-size: 22px; margin-bottom: 4px; color: #f0f6fc; }
.subtle { color: #6e7681; font-size: 12px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; }
@media (max-width: 1100px) { .grid, .grid-3 { grid-template-columns: 1fr; } }
.card { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 18px; margin-bottom: 16px; }
.card h2 { font-size: 13px; font-weight: 600; color: #58a6ff; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
.metric { display: inline-block; margin-right: 24px; vertical-align: top; margin-bottom: 8px; }
.metric .label { font-size: 10px; color: #6e7681; text-transform: uppercase; }
.metric .value { font-size: 22px; font-weight: 600; color: #f0f6fc; margin-top: 2px; }
.metric .value.green { color: #3fb950; } .metric .value.yellow { color: #d29922; } .metric .value.red { color: #f85149; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { text-align: left; padding: 6px 10px; color: #8b949e; font-weight: 500; border-bottom: 1px solid #30363d; font-size: 10px; text-transform: uppercase; }
td { padding: 6px 10px; border-bottom: 1px solid #21262d; font-variant-numeric: tabular-nums; }
tr:hover { background: #1c2128; }
.right { text-align: right; }
.bar-wrap { width: 100%; height: 6px; background: #21262d; border-radius: 3px; overflow: hidden; margin-top: 4px; }
.bar { height: 100%; background: #3fb950; transition: width 0.3s; }
.bar.yellow { background: #d29922; } .bar.red { background: #f85149; } .bar.cyan { background: #39c5cf; } .bar.purple { background: #a371f7; }
.toolbar { display: flex; gap: 12px; align-items: center; margin-bottom: 16px; font-size: 12px; color: #8b949e; flex-wrap: wrap; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: #3fb950; display: inline-block; margin-right: 6px; }
.dot.stale { background: #d29922; }
.tag { display: inline-block; background: #1f6feb22; color: #58a6ff; border: 1px solid #1f6feb44; padding: 1px 6px; border-radius: 4px; font-size: 10px; margin-right: 4px; }
.tag.sub { background: #a371f722; color: #a371f7; border-color: #a371f744; }
.bartrack { display: inline-block; min-width: 110px; height: 10px; background: #21262d; border-radius: 2px; vertical-align: middle; margin-right: 6px; position: relative; }
.bartrack > span { display: block; height: 100%; background: #58a6ff; border-radius: 2px; }
.hourbar { display: inline-block; width: 14px; margin-right: 1px; vertical-align: bottom; background: #58a6ff; border-radius: 2px 2px 0 0; }
.hourbar.yest { background: #6e7681; opacity: 0.6; }
.hour-row { display: flex; align-items: flex-end; height: 60px; gap: 2px; margin-top: 8px; }
.hour-label { font-size: 9px; color: #6e7681; margin-top: 4px; display: flex; gap: 2px; }
.hour-label > span { width: 14px; text-align: center; }
</style></head><body>
<h1>POS • AI Resource Control Tower</h1>
<div class="subtle" id="hostinfo">…</div>

<div class="toolbar">
  <span><span class="dot" id="dot"></span><span id="status">初始化</span></span>
  <span>·</span><span>前端 30s / 后端 {{REFRESH_SECONDS}}s</span>
  <span>·</span><span id="errs"></span>
</div>


<!-- OpenAI / Codex first -->
<div class="card" style="background: #1f1322; border-color: #a371f744;">
  <h2 style="color: #a371f7;">OpenAI • Codex / Work</h2>
  <div id="codex_card">…</div>
  <div class="subtle" style="margin-top:8px;">Read-only local data: <code>~/.codex/state_5.sqlite</code> and session JSONL.</div>
</div>

<!-- POS / Autonomy8 operational status -->
<div class="card" style="background:#102131;border-color:#39c5cf55;">
  <h2 style="color:#39c5cf;">POS • Autonomy8</h2>
  <div id="a8_summary">…</div>
  <table id="a8_lanes"><thead><tr>
    <th>lane</th><th>status</th><th class="right">cycle</th><th class="right">commits</th><th>branch / head</th>
  </tr></thead><tbody></tbody></table>
  <div id="a8_activity" class="subtle" style="margin-top:10px;line-height:1.5;">…</div>
</div>

<!-- Claude second / bottom provider card -->
<div class="card" style="background: #11251a; border-color: #3fb95044;">
  <h2 style="color: #3fb950;">Claude</h2>
  <div id="anthropic_card">…</div>
  <div class="subtle" style="margin-top:8px;">Quota is fetched read-only; detailed usage comes from local Claude session data.</div>
</div>

<!-- Top: current 5h block -->
<!-- Key insights strip -->
<div class="card" id="insights_card" style="background: #1f2d3d; border-color: #1f6feb44;">
  <h2 style="color: #f0f6fc;">关键洞察 (auto-derived)</h2>
  <div id="insights" class="subtle" style="font-size: 13px; line-height: 1.7;">…</div>
</div>

<div class="card">
  <h2>当前 5 小时计费窗口</h2>
  <div id="block_metrics">…</div>
  <div class="bar-wrap"><div class="bar" id="block_bar" style="width:0%"></div></div>
</div>


<!-- Weekly budget bar -->
<div class="card">
  <h2>本周 (Sun–Sat) 用量 <span style="text-transform:none;color:#6e7681;font-weight:400;font-size:11px;">· budget 自适应 · 可设 env CC_DASHBOARD_WEEKLY_BUDGET_USD</span></h2>
  <div id="weekly_metrics">…</div>
  <div class="bar-wrap" style="margin-top:8px;"><div class="bar" id="weekly_bar" style="width:0%"></div></div>
  <div class="subtle" id="weekly_subtle" style="margin-top:6px;">…</div>
</div>

  <div class="grid">
  <div class="card"><h2>Codex 近 14 天 token (本地 sqlite)</h2>
    <table id="codex_day_table"><thead><tr>
      <th>日期</th><th class="right">sessions</th><th class="right">总 token</th>
      <th class="right">input</th><th class="right">cached_in</th><th class="right">output*</th>
      <th class="right">估算成本</th>
    </tr></thead><tbody></tbody></table>
  </div>
  <div class="card"><h2>Codex 按模型 / 来源 (近 14d)</h2>
    <table id="codex_model_table"><thead><tr>
      <th>模型</th><th class="right">sessions</th><th class="right">总 token</th>
      <th class="right">估算成本</th>
    </tr></thead><tbody></tbody></table>
  </div>
</div>

<div class="card">
  <h2>Codex 最近 10 个 session</h2>
  <table id="codex_sess_table"><thead><tr>
    <th>更新时间</th><th>来源</th><th>模型</th><th>effort</th>
    <th class="right">tokens</th><th>预览</th>
  </tr></thead><tbody></tbody></table>
</div>

<!-- Cache + main/sub split + content kind -->
<div class="grid-3">
  <div class="card"><h2>Cache 命中率</h2><div id="cache_card">…</div></div>
  <div class="card"><h2>Main vs Subagent</h2><div id="mainsub_card">…</div></div>
  <div class="card"><h2>输出内容类型</h2><div id="content_card">…</div></div>
</div>

<!-- Hourly trend -->
<div class="card">
  <h2>今日 vs 昨日 — 小时趋势 (token)</h2>
  <div class="hour-row" id="hour_chart"></div>
  <div class="hour-label" id="hour_labels"></div>
  <div class="subtle" style="margin-top: 8px;">
    <span style="display:inline-block;width:10px;height:10px;background:#58a6ff;border-radius:2px;"></span> 今日
    &nbsp; <span style="display:inline-block;width:10px;height:10px;background:#6e7681;opacity:0.6;border-radius:2px;"></span> 昨日
  </div>
</div>

<!-- By tool & subagent type -->
<div class="grid">
  <div class="card"><h2>按工具拆分 (recent {{DAYS}}d)</h2>
    <table id="tool_table"><thead><tr>
      <th>工具</th><th class="right">turns</th><th class="right">输出 (估)</th><th class="right">cache_read 归因</th><th>总占比</th>
    </tr></thead><tbody></tbody></table>
  </div>
  <div class="card"><h2>Subagent 类型 (recent {{DAYS}}d)</h2>
    <table id="subtype_table"><thead><tr>
      <th>类型</th><th class="right">调用次数</th><th class="right">触发 turn 输出 (近似)</th><th>占比</th>
    </tr></thead><tbody></tbody></table>
  </div>
</div>

<!-- By project -->
<div class="card">
  <h2>按项目拆分 (recent {{DAYS}}d)</h2>
  <table id="project_table"><thead><tr>
    <th>项目</th><th class="right">turns</th><th class="right">输出</th>
    <th class="right">cache 创建</th><th class="right">cache 读取</th><th class="right">合计 token</th><th>占比</th>
  </tr></thead><tbody></tbody></table>
</div>

<!-- Daily -->
<div class="card">
  <h2>近 14 天 (ccusage 成本)</h2>
  <table id="daily_table"><thead><tr>
    <th>日期</th><th>模型</th><th class="right">输入</th><th class="right">输出</th>
    <th class="right">缓存创建</th><th class="right">缓存读取</th><th class="right">合计</th><th class="right">成本 USD</th>
  </tr></thead><tbody></tbody></table>
</div>

<!-- Top expensive turns -->
<div class="card">
  <h2>Top 20 最贵 turns (单 turn 输出 token)</h2>
  <table id="exp_table"><thead><tr>
    <th>时间</th><th>项目</th><th>模型</th><th>工具</th><th class="right">输出</th><th class="right">cache_read</th>
  </tr></thead><tbody></tbody></table>
</div>


<!-- RTK savings -->
<div class="card">
  <h2>RTK 节省总览 (本地命令代理)</h2>
  <div id="rtk_summary">…</div>
</div>

<div class="grid">
  <div class="card"><h2>RTK 各命令节省 (top 15)</h2>
    <table id="rtk_cmd_table"><thead><tr>
      <th>命令</th><th class="right">次数</th><th class="right">原 token</th>
      <th class="right">RTK 后</th><th class="right">节省</th><th>占比</th>
    </tr></thead><tbody></tbody></table>
  </div>
  <div class="card"><h2>RTK 各项目节省</h2>
    <table id="rtk_proj_table"><thead><tr>
      <th>项目</th><th class="right">次数</th><th class="right">节省 token</th><th>占比</th>
    </tr></thead><tbody></tbody></table>
  </div>
</div>

<div class="grid">
  <div class="card"><h2>RTK 近 14 天每日节省</h2>
    <table id="rtk_daily_table"><thead><tr>
      <th>日期</th><th class="right">命令数</th><th class="right">原</th>
      <th class="right">RTK 后</th><th class="right">节省</th><th class="right">压缩率</th>
    </tr></thead><tbody></tbody></table>
  </div>
  <div class="card"><h2>RTK 低效命令 (压缩率低)</h2>
    <table id="rtk_worst_table"><thead><tr>
      <th>命令</th><th class="right">次数</th><th class="right">avg %</th><th class="right">原 input</th>
    </tr></thead><tbody></tbody></table>
    <div class="subtle" style="margin-top:8px;">压缩率 &lt; 20% 的命令意味着 RTK 几乎没帮你省 token，可以考虑直接裸跑（少跑 RTK 一层）</div>
  </div>
</div>

<!-- Sessions (ccusage) -->
<div class="card">
  <h2>Session 总览 (ccusage)</h2>
  <table id="session_table"><thead><tr>
    <th>Session / 项目</th><th>模型</th><th class="right">合计 token</th><th class="right">成本</th><th>最近活动</th>
  </tr></thead><tbody></tbody></table>
</div>

<script>
const fmt = (n) => {
  if (n == null) return '-';
  if (n >= 1e9) return (n/1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n/1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n/1e3).toFixed(1) + 'K';
  if (n < 1 && n > 0) return n.toFixed(2);
  return Math.round(n).toString();
};
const fmtUSD = (n) => n != null ? '$' + Number(n).toFixed(2) : '-';
const fmtPct = (n) => n != null ? Number(n).toFixed(1) + '%' : '-';
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// Defensive percent formatter (0dp). Returns '?' when value is missing/non-finite.
// Upstream may send null (e.g. Codex rate_limits.primary on the premium plan,
// or anthropic.seven_day when the API hasn't populated it yet).
const fmtPct0 = (n) => (n == null || !Number.isFinite(Number(n))) ? '?' : Number(n).toFixed(0) + '%';
const fmtPct1 = (n) => (n == null || !Number.isFinite(Number(n))) ? '?' : Number(n).toFixed(1) + '%';
// Tag color for utilization percentage; '⚪' when no data so the line still renders.
const tagPct = (n) => n == null ? '⚪' : (n < 50 ? '🟢' : n < 80 ? '🟡' : '🔴');

function bartrack(pct) {
  const w = Math.max(0, Math.min(100, pct));
  return `<span class="bartrack"><span style="width:${w}%"></span></span>${fmtPct(pct)}`;
}


function renderInsights(d) {
  const local = d.local || {}; const rtk = d.rtk || {}; const rs = rtk.summary || {};
  const mt = local.main_total || {}; const st = local.sub_total || {};
  const mtot = mt.total || 0; const stot = st.total || 0; const totalT = mtot + stot;
  const lines = [];

  if (totalT > 0) {
    const subPct = stot/totalT*100;
    if (subPct > 60) lines.push(`🔥 <b>Subagent 占 ${subPct.toFixed(0)}%</b> 总 token — 主要烧钱方。优化 subagent prompt / 减少 spawn 收益最大。`);
    else if (subPct > 40) lines.push(`📊 Subagent 占 ${subPct.toFixed(0)}% — 与 main session 大致对半。`);
    else lines.push(`✓ Subagent 占 ${subPct.toFixed(0)}% — main session 是主要消耗。`);
  }

  const tools = Object.entries(local.by_tool || {}).sort((a,b) => (b[1].total||0) - (a[1].total||0));
  if (tools.length > 0) {
    const [tn, tv] = tools[0];
    const totalTool = tools.reduce((s, [_,v]) => s + (v.total||0), 0);
    const tpct = totalTool > 0 ? (tv.total||0) / totalTool * 100 : 0;
    const cachePart = (tv.total||0) > 0 ? (tv.cache_read||0) / (tv.total||0) * 100 : 0;
    const cacheNote = cachePart > 80 ? `；其中 ${cachePart.toFixed(0)}% 是 cache_read 归因` : '';
    lines.push(`🛠️ 工具 #1: <b>${tn}</b> 占 ${tpct.toFixed(0)}% 工具 token (${fmt(tv.total||0)})${cacheNote}`);
  }

  const subTypes = Object.entries(local.sub_types_count || {}).sort((a,b) => b[1] - a[1]);
  if (subTypes.length > 0) {
    const [sn, sc] = subTypes[0];
    lines.push(`🤖 最常 spawn: <b>${sn}</b> ${sc} 次 (近 ${local.scan_days}d)`);
  }

  if (local.cache_pct_7d != null) {
    const c = local.cache_pct_7d;
    const tag = c >= 90 ? '✓ 极佳' : c >= 70 ? '👍 不错' : c >= 50 ? '⚠️ 可优化' : '❌ 偏低';
    lines.push(`💾 近 7 天 cache 命中 <b>${c.toFixed(1)}%</b> ${tag}`);
  }

  // Anthropic real-time quota (authoritative; surfaces above ccusage estimates)
  const anthro = d.anthropic || {};
  // Codex insight
  const codex = d.codex || {};
  if (codex.rate_limits) {
    // primary/secondary may be null on plans without enforced rate limits (e.g. premium).
    // Fall back to {} for safe field access; render '?' instead of crashing on toFixed.
    const p5h = codex.rate_limits.primary || {};
    const p7d = codex.rate_limits.secondary || {};
    const p5hPct = (p5h && p5h.used_percent != null) ? Number(p5h.used_percent) : null;
    const p7dPct = (p7d && p7d.used_percent != null) ? Number(p7d.used_percent) : null;
    // Only render this insight line if at least one window has data; otherwise it's noise.
    if (p5hPct != null || p7dPct != null || codex.rate_limits.primary || codex.rate_limits.secondary) {
      const fhTag = tagPct(p5hPct);
      const sdTag = tagPct(p7dPct);
      const remH = (r) => { if (!r) return '?'; const ms = r*1000 - Date.now(); const h = Math.floor(ms/3600000); const m = Math.floor((ms%3600000)/60000); return ms<0?'?':`${h}h${m}m`; };
      const remD = (r) => { if (!r) return '?'; const ms = r*1000 - Date.now(); const dd = Math.floor(ms/86400000); const h = Math.floor((ms%86400000)/3600000); return ms<0?'?':`${dd}d${h}h`; };
      const ageMin = codex.rate_limits_age_seconds != null ? Math.floor(codex.rate_limits_age_seconds/60) : null;
      const stale = ageMin != null && ageMin > 60 ? ` (数据 ${ageMin}m 前)` : '';
      lines.unshift(`⚡ <b>Codex</b>${stale}: 5h 配额 ${fhTag} <b>${fmtPct0(p5hPct)}</b> (重置 ${remH(p5h.resets_at)}) · 7d 配额 ${sdTag} <b>${fmtPct0(p7dPct)}</b> (重置 ${remD(p7d.resets_at)})`);
    }
  }

  if (anthro.five_hour) {
    const fh = anthro.five_hour, sd = anthro.seven_day || {};
    const fhU = (fh && fh.utilization != null) ? Number(fh.utilization) : null;
    const sdU = (sd && sd.utilization != null) ? Number(sd.utilization) : null;
    const fhTag = tagPct(fhU);
    const sdTag = tagPct(sdU);
    const fhRem = (() => { if (!fh.resets_at) return '?'; const ms = new Date(fh.resets_at) - new Date(); const h = Math.floor(ms/3600000); const m = Math.floor((ms%3600000)/60000); return ms < 0 ? '?' : `${h}h${m}m`; })();
    const sdRem = (() => { if (!sd.resets_at) return '?'; const ms = new Date(sd.resets_at) - new Date(); const d = Math.floor(ms/86400000); const h = Math.floor((ms%86400000)/3600000); return ms < 0 ? '?' : `${d}d${h}h`; })();
    lines.unshift(`⚡ <b>Anthropic 官方</b>: 5h 配额 ${fhTag} <b>${fmtPct0(fhU)}</b> (重置 ${fhRem}) · 7d 配额 ${sdTag} <b>${fmtPct0(sdU)}</b> (重置 ${sdRem})`);
  }

  if (rs.saved && rs.raw_in) {
    const pct = rs.saved / rs.raw_in * 100;
    const topCmd = (rtk.by_cmd || [])[0];
    if (topCmd) {
      const topShare = topCmd.saved / rs.saved * 100;
      lines.push(`✂️ RTK 整体压缩 <b>${pct.toFixed(1)}%</b> (${fmt(rs.saved)} saved); <b>${topCmd.cmd.trim()}</b> 一项贡献 ${topShare.toFixed(0)}%`);
    }
    if (rtk.failures_7d > 100) {
      lines.push(`⚠️ RTK 近 7 天 fallback ${rtk.failures_7d} 次 — 这些走 raw exec，没节省到`);
    }
  }

  const el = document.getElementById('insights');
  if (el) el.innerHTML = lines.length ? lines.map(l => '• ' + l).join('<br>') : '<span class="subtle">无数据</span>';
}


async function refresh() {
  try {
    const r = await fetch('/api/data');
    const d = await r.json();

    // Header
    const ts = d.last_refresh_ts ? new Date(d.last_refresh_ts*1000) : null;
    const ageS = ts ? Math.floor((Date.now() - ts.getTime())/1000) : 9999;
    document.getElementById('status').textContent = ts
      ? `更新于 ${ts.toLocaleTimeString()} (${ageS}s 前)` : '等待首次刷新...';
    document.getElementById('dot').className = 'dot' + (ageS > 180 ? ' stale' : '');
    document.getElementById('errs').textContent = (d.errors || []).join(' · ');
    const local = d.local || {};
    document.getElementById('hostinfo').textContent =
      `数据: ccusage + 直接扫 ~/.claude/projects/*.jsonl (近 ${local.scan_days || '?'} 天, ${local.files_scanned || 0} 文件 / ${fmt(local.msg_total || 0)} assistant turns) · 服务: ${d.host}:${d.port}`;

    // Block card
    const block = (d.blocks && d.blocks.blocks && d.blocks.blocks[0]) || null;
    const bm = document.getElementById('block_metrics');
    const bar = document.getElementById('block_bar');
    if (block) {
      const used = block.costUSD || 0;
      const proj = (block.projection?.totalCost) || used;
      const rate = block.burnRate?.costPerHour || 0;
      const remainMs = block.projection ? block.projection.remainingMinutes * 60000 : null;
      const remainStr = remainMs ? `${Math.floor(remainMs/3600000)}h ${Math.floor((remainMs%3600000)/60000)}m` : '-';
      const pct = proj > 0 ? Math.min(100, used/proj*100) : 0;
      const cls = pct < 50 ? 'green' : pct < 80 ? 'yellow' : 'red';
      bm.innerHTML = `
        <div class="metric"><div class="label">已用</div><div class="value ${cls}">${fmtUSD(used)}</div></div>
        <div class="metric"><div class="label">预测全窗口</div><div class="value">${fmtUSD(proj)}</div></div>
        <div class="metric"><div class="label">烧速</div><div class="value">${fmtUSD(rate)}/hr</div></div>
        <div class="metric"><div class="label">剩余时间</div><div class="value">${remainStr}</div></div>
        <div class="metric"><div class="label">合计 token</div><div class="value">${fmt(block.totalTokens)}</div></div>`;
      bar.style.width = pct + '%';
      bar.className = 'bar ' + (pct < 50 ? '' : pct < 80 ? 'yellow' : 'red');
    } else {
      bm.innerHTML = '<span class="subtle">无活跃 5h 块</span>';
    }

    // Anthropic real-time quota card
    const anthro = d.anthropic || {};
    const ac = document.getElementById('anthropic_card');
    if (anthro.five_hour) {
      const fh = anthro.five_hour;
      const sd = anthro.seven_day || {};
      const meta = anthro._meta || {};
      const sonnet = anthro.seven_day_sonnet;
      const opus = anthro.seven_day_opus;
      const extra = anthro.extra_usage || {};

      function timeRemaining(resetIso) {
        if (!resetIso) return '-';
        const ms = new Date(resetIso) - new Date();
        if (ms < 0) return '已过期';
        const h = Math.floor(ms / 3600000);
        const m = Math.floor((ms % 3600000) / 60000);
        const d = Math.floor(h / 24);
        if (d > 0) return `${d}d ${h % 24}h`;
        return `${h}h ${m}m`;
      }
      function utilCls(u) {
        return u < 50 ? 'green' : u < 80 ? 'yellow' : 'red';
      }
      function bar(u, cls) {
        return `<div class="bar-wrap" style="margin-top:6px;width:200px;display:inline-block;vertical-align:middle;"><div class="bar ${cls}" style="width:${u}%"></div></div>`;
      }

      const fhU = (fh && fh.utilization != null) ? Number(fh.utilization) : null;
      const sdU = (sd && sd.utilization != null) ? Number(sd.utilization) : null;
      const fhCls = fhU == null ? '' : utilCls(fhU);
      const sdCls = sdU == null ? '' : utilCls(sdU);

      let html = `
        <div style="margin-bottom:12px;">
          <div class="metric"><div class="label">5h 窗口已用</div>
            <div class="value ${fhCls}">${fmtPct1(fhU)}</div>
            ${fhU == null ? '' : bar(fhU, fhCls)}
          </div>
          <div class="metric"><div class="label">5h 重置倒计时</div><div class="value">${timeRemaining(fh.resets_at)}</div>
            <div class="subtle" style="font-size:9px;">${fh.resets_at ? new Date(fh.resets_at).toLocaleString() : ''}</div></div>
        </div>
        <div>
          <div class="metric"><div class="label">7 天窗口已用</div>
            <div class="value ${sdCls}">${fmtPct1(sdU)}</div>
            ${sdU == null ? '' : bar(sdU, sdCls)}
          </div>
          <div class="metric"><div class="label">7 天重置倒计时</div><div class="value">${timeRemaining(sd.resets_at)}</div>
            <div class="subtle" style="font-size:9px;">${sd.resets_at ? new Date(sd.resets_at).toLocaleString() : ''}</div></div>
        </div>`;

      // Per-model 7d if present
      const subRows = [];
      if (sonnet) subRows.push(`<span class="metric"><span class="label">Sonnet 7d</span> <b>${fmtPct1(sonnet.utilization)}</b></span>`);
      if (opus) subRows.push(`<span class="metric"><span class="label">Opus 7d</span> <b>${fmtPct1(opus.utilization)}</b></span>`);
      if (extra && extra.is_enabled) {
        subRows.push(`<span class="metric"><span class="label">Extra usage</span> <b>${(extra.utilization || 0).toFixed(1)}% of ${extra.currency || 'USD'} ${extra.monthly_limit || '?'}</b></span>`);
      }
      if (subRows.length) {
        html += `<div style="margin-top:12px;border-top:1px solid #21262d;padding-top:8px;font-size:12px;color:#8b949e;">${subRows.join(' · ')}</div>`;
      }

      const claudeDaily = (d.daily?.daily) || [];
      const claudeApiCost = claudeDaily.reduce((sum, row) => sum + (row.totalCost || 0), 0);
      const ca = local.cache_all || {};
      const claudeIndexed = (ca.in || 0) + (ca.out || 0) + (ca.cc || 0) + (ca.cr || 0);
      const activeBlock = (d.blocks?.blocks || [])[0] || {};
      const tokensPerPct = fhU > 0 && activeBlock.totalTokens ? activeBlock.totalTokens / fhU : null;
      html += `<div style="margin-top:12px;border-top:1px solid #21262d;padding-top:8px;">
        <span class="metric"><span class="label">API-equivalent cost</span> <b>${fmtUSD(claudeApiCost)}</b></span>
        <span class="metric"><span class="label">Indexed tokens (${local.scan_days || '?'}d)</span> <b>${fmt(claudeIndexed)}</b></span>
        <span class="metric"><span class="label">5h tokens / 1% quota</span> <b>${tokensPerPct == null ? 'not derivable' : fmt(tokensPerPct)}</b></span>
      </div>`;
      html += `<div class="subtle" style="margin-top:8px;">Subscription: <b>${meta.subscription || '?'}</b> · tier: <b>${meta.tier || '?'}</b></div>`;
      ac.innerHTML = html;
    } else {
      ac.innerHTML = '<span class="subtle">Anthropic 数据未读到 (token 失效?). 重新跑 claude 登录刷新</span>';
    }

    // Codex (OpenAI) real-time quota card
    const codex = d.codex || {};
    const cd = document.getElementById('codex_card');
    if (codex.rate_limits) {
      const rl = codex.rate_limits;
      const p5h = rl.primary || {};
      const p7d = rl.secondary || {};

      function _remain(reset) {
        if (!reset) return '-';
        const ms = reset * 1000 - Date.now();
        if (ms < 0) return '已过期';
        const h = Math.floor(ms / 3600000);
        const m = Math.floor((ms % 3600000) / 60000);
        const dd = Math.floor(h / 24);
        if (dd > 0) return `${dd}d ${h % 24}h`;
        return `${h}h ${m}m`;
      }
      function _cls(u) { return u < 50 ? 'green' : u < 80 ? 'yellow' : 'red'; }
      function _bar(u, cls) {
        return `<div class="bar-wrap" style="margin-top:6px;width:200px;display:inline-block;vertical-align:middle;"><div class="bar ${cls}" style="width:${u}%"></div></div>`;
      }

      const fhU = p5h.used_percent || 0;
      const sdU = p7d.used_percent || 0;
      const fhCls = _cls(fhU);
      const sdCls = _cls(sdU);
      const ageMin = codex.rate_limits_age_seconds != null ? Math.floor(codex.rate_limits_age_seconds / 60) : null;
      const ageStr = ageMin == null ? '?' : (ageMin < 60 ? `${ageMin}m` : `${Math.floor(ageMin/60)}h${ageMin%60}m`);
      const stale = ageMin != null && ageMin > 60;

      let html = `
        <div style="margin-bottom:12px;">
          <div class="metric"><div class="label">5h 窗口已用</div>
            <div class="value ${fhCls}">${fhU.toFixed(1)}%</div>
            ${_bar(fhU, fhCls)}
          </div>
          <div class="metric"><div class="label">5h 重置倒计时</div><div class="value">${_remain(p5h.resets_at)}</div>
            <div class="subtle" style="font-size:9px;">${p5h.resets_at ? new Date(p5h.resets_at*1000).toLocaleString() : ''}</div></div>
        </div>
        <div>
          <div class="metric"><div class="label">7 天窗口已用</div>
            <div class="value ${sdCls}">${sdU.toFixed(1)}%</div>
            ${_bar(sdU, sdCls)}
          </div>
          <div class="metric"><div class="label">7 天重置倒计时</div><div class="value">${_remain(p7d.resets_at)}</div>
            <div class="subtle" style="font-size:9px;">${p7d.resets_at ? new Date(p7d.resets_at*1000).toLocaleString() : ''}</div></div>
        </div>
        <div class="subtle" style="margin-top:8px;">
          套餐: <b>${rl.plan_type || '?'}</b> · ${rl.rate_limit_reached_type ? '<span style="color:#f85149">⚠️ '+rl.rate_limit_reached_type+'</span>' : ''}
          数据新鲜度: <span class="${stale ? 'value yellow' : ''}">${ageStr} 前的回包</span>
          ${stale ? ' (久未跑 codex, 可能不是最新)' : ''}
        </div>`;
      // Append cost section
      if (codex.total_cost_14d != null) {
        html += `<div style="margin-top:12px;border-top:1px solid #21262d;padding-top:8px;font-size:12px;color:#8b949e;">
          <span class="metric"><span class="label">API-equivalent today</span> <b style="color:#a371f7">${fmtUSD(codex.today_cost)}</b></span>
          <span class="metric"><span class="label">API-equivalent 14d</span> <b style="color:#a371f7">${fmtUSD(codex.total_cost_14d)}</b></span>
          <span class="metric"><span class="label">Indexed lifetime tokens</span> <b>${fmt(codex.lifetime_tokens || 0)}</b></span>
          <span class="metric"><span class="label">Indexed sessions</span> <b>${fmt(codex.lifetime_sessions || 0)}</b></span>
          <span class="subtle"> · local price table</span>
        </div>`;
      }
      cd.innerHTML = html;
    } else {
      cd.innerHTML = '<span class="subtle">没找到 codex session 文件 (跑过任一 codex 任务后即可)</span>';
    }

    // POS / Autonomy8
    const a8 = d.autonomy8 || {};
    const a8s = document.getElementById('a8_summary');
    const healthColor = a8.health === 'healthy-burn' ? '#3fb950' : (a8.health === 'suspected-runaway' ? '#f85149' : '#d29922');
    const controller = a8.controller || {};
    a8s.innerHTML = `
      <div class="metric"><div class="label">health</div><div class="value" style="color:${healthColor}">${esc(a8.health || 'unknown')}</div><div class="subtle">${esc(a8.health_reason || '')}</div></div>
      <div class="metric"><div class="label">Claude workers</div><div class="value">${fmt(a8.worker_count || 0)}/8</div></div>
      <div class="metric"><div class="label">builders / reviewers</div><div class="value">${fmt(a8.builders || 0)} / ${fmt(a8.reviewers || 0)}</div></div>
      <div class="metric"><div class="label">controller</div><div class="value">${controller.pid ? 'PID ' + controller.pid : 'offline'}</div><div class="subtle">${esc(controller.elapsed || '')}</div></div>`;
    const a8b = document.querySelector('#a8_lanes tbody');
    a8b.innerHTML = '';
    for (const name of ['dispatcher', 'serial', 'v4f', 'r2pin']) {
      const lane = (a8.lanes || {})[name] || {};
      const st = lane.status || 'UNKNOWN';
      const stColor = st === 'ACCEPTED' ? '#3fb950' : (st.includes('HUMAN_REQUIRED') ? '#f85149' : (st.startsWith('HOLD') ? '#d29922' : '#39c5cf'));
      a8b.innerHTML += `<tr><td><b>${name}</b></td><td style="color:${stColor}">${esc(st)}</td>
        <td class="right">${esc(lane.cycle ?? '-')}</td><td class="right">${esc(lane.commits_ahead ?? '-')}</td>
        <td><span class="subtle">${esc(lane.branch || '-')} @ ${esc(lane.short_head || '-')}</span></td></tr>`;
    }
    const a8a = document.getElementById('a8_activity');
    a8a.innerHTML = '<b>Recent activity</b><br>' + ((a8.activity || []).map(esc).join('<br>') || 'No recent controller activity.');

    // Codex by-day — merge state_5.sqlite token sums with jsonl-derived cost
    const cdb = document.querySelector('#codex_day_table tbody');
    cdb.innerHTML = '';
    const dayCosts = new Map((codex.by_day_cost || []).map(r => [r.day, r]));
    for (const r of (codex.by_day || []).slice(0, 14)) {
      const c = dayCosts.get(r.day) || {};
      cdb.innerHTML += `<tr>
        <td>${r.day}</td>
        <td class="right">${fmt(r.n_threads)}</td>
        <td class="right">${fmt(r.total_tokens)}</td>
        <td class="right">${fmt(c.input || 0)}</td>
        <td class="right">${fmt(c.cached_input || 0)}</td>
        <td class="right">${fmt((c.output||0) + (c.reasoning||0))}</td>
        <td class="right"><strong style="color:#a371f7">${c.usd ? fmtUSD(c.usd) : '-'}</strong></td>
      </tr>`;
    }

    // Codex by-model — merge token totals with cost
    const cmb = document.querySelector('#codex_model_table tbody');
    cmb.innerHTML = '';
    const modelCosts = new Map((codex.by_model_cost || []).map(r => [r.model, r]));
    for (const r of (codex.by_model || [])) {
      const c = modelCosts.get(r.model) || {};
      cmb.innerHTML += `<tr>
        <td>${r.model || '?'}</td>
        <td class="right">${fmt(r.n_threads)}</td>
        <td class="right">${fmt(r.total_tokens)}</td>
        <td class="right"><strong style="color:#a371f7">${c.usd ? fmtUSD(c.usd) : '-'}</strong></td>
      </tr>`;
    }

    // Codex recent sessions
    const csb = document.querySelector('#codex_sess_table tbody');
    csb.innerHTML = '';
    for (const r of (codex.recent_sessions || [])) {
      const preview = (r.preview || '').replace(/[<>]/g, '');
      csb.innerHTML += `<tr>
        <td><span class="subtle">${r.updated || '-'}</span></td>
        <td>${r.source || '-'}</td>
        <td><span class="subtle">${r.model || '-'}</span></td>
        <td><span class="subtle">${r.reasoning_effort || '-'}</span></td>
        <td class="right">${fmt(r.tokens_used || 0)}</td>
        <td><span class="subtle">${preview}</span></td>
      </tr>`;
    }



    // Weekly card
    const weeks = (d.weekly?.weekly) || [];
    const wm = document.getElementById('weekly_metrics');
    const wb = document.getElementById('weekly_bar');
    const ws = document.getElementById('weekly_subtle');
    if (weeks.length > 0) {
      const cur = weeks[weeks.length - 1];
      const prev4 = weeks.slice(-5, -1);  // up to 4 prior weeks
      // determine days elapsed in current week (ISO Sun-start ccusage style)
      // ccusage calls the start-date field "period".
      const startDate = new Date((cur.period || cur.week) + 'T00:00:00');
      const now = new Date();
      const elapsedDays = Math.max(1, Math.min(7, Math.floor((now - startDate) / 86400000) + 1));
      const fractionElapsed = elapsedDays / 7;
      const used = cur.totalCost || 0;
      const projected = used / fractionElapsed;  // linear extrapolation to full week
      const prevAvg = prev4.length > 0 ? prev4.reduce((s, w) => s + (w.totalCost||0), 0) / prev4.length : 0;
      const prevMax = prev4.length > 0 ? Math.max(...prev4.map(w => w.totalCost||0)) : 0;
      const lastWeek = prev4.length > 0 ? prev4[prev4.length - 1].totalCost : 0;

      // Adaptive budget
      let budget = d.weekly_budget_usd || 0;
      if (!budget || budget <= 0) {
        budget = Math.max(prevMax * 1.1, projected * 1.1, 50);
      }
      const pct = budget > 0 ? Math.min(100, used / budget * 100) : 0;
      const projPct = budget > 0 ? Math.min(100, projected / budget * 100) : 0;
      const cls = pct < 50 ? 'green' : pct < 80 ? 'yellow' : 'red';

      wm.innerHTML = `
        <div class="metric"><div class="label">本周已用</div><div class="value ${cls}">${fmtUSD(used)}</div></div>
        <div class="metric"><div class="label">按速度推全周</div><div class="value">${fmtUSD(projected)}</div></div>
        <div class="metric"><div class="label">上周</div><div class="value">${fmtUSD(lastWeek)}</div></div>
        <div class="metric"><div class="label">前 4 周平均</div><div class="value">${fmtUSD(prevAvg)}</div></div>
        <div class="metric"><div class="label">budget</div><div class="value">${fmtUSD(budget)}</div></div>
        <div class="metric"><div class="label">进度</div><div class="value ${cls}">${pct.toFixed(1)}%</div></div>`;
      wb.style.width = pct + '%';
      wb.className = 'bar ' + (pct < 50 ? '' : pct < 80 ? 'yellow' : 'red');
      const startStr = startDate.toLocaleDateString('zh-CN', {month: '2-digit', day: '2-digit'});
      const endDate = new Date(startDate.getTime() + 6 * 86400000);
      const endStr = endDate.toLocaleDateString('zh-CN', {month: '2-digit', day: '2-digit'});
      const budgetSrc = (d.weekly_budget_usd > 0) ? 'env 设定' : 'auto: max(前 4 周最高×1.1, 本周推算×1.1, $50)';
      ws.innerHTML = `Day ${elapsedDays}/7 of ${startStr}–${endStr} · cur token ${fmt(cur.totalTokens)} · budget 来源: ${budgetSrc}`;
    } else {
      wm.innerHTML = '<span class="subtle">无周数据</span>';
    }


    // Cache card
    const cc = document.getElementById('cache_card');
    cc.innerHTML = `
      <div class="metric"><div class="label">今日</div><div class="value green">${fmtPct(local.cache_pct_today)}</div></div>
      <div class="metric"><div class="label">近 7 天</div><div class="value">${fmtPct(local.cache_pct_7d)}</div></div>
      <div class="metric"><div class="label">全期</div><div class="value">${fmtPct(local.cache_pct_all)}</div></div>
      <div class="subtle" style="margin-top:8px;">cache_read / (input + cache_create + cache_read)。越高越省钱。</div>`;

    // Main vs subagent
    const ms = document.getElementById('mainsub_card');
    const mt = local.main_total || {};
    const st = local.sub_total || {};
    const totalTokens = (mt.total || 0) + (st.total || 0);
    const subPct = totalTokens > 0 ? (st.total || 0) / totalTokens * 100 : 0;
    ms.innerHTML = `
      <div class="metric"><div class="label">Main turns</div><div class="value">${fmt(mt.turns || 0)}</div></div>
      <div class="metric"><div class="label">Sub turns</div><div class="value">${fmt(st.turns || 0)}</div></div>
      <div class="metric"><div class="label">Sub 占比</div><div class="value ${subPct > 60 ? 'yellow' : ''}">${fmtPct(subPct)}</div></div>
      <div class="subtle" style="margin-top:8px;">Main 输出 ${fmt(mt.output||0)} · Sub 输出 ${fmt(st.output||0)}</div>`;

    // Content kind
    const ck = local.content_kind || {};
    const total_kind = (ck.thinking||0) + (ck.text||0) + (ck.tool_use||0);
    const cdiv = document.getElementById('content_card');
    cdiv.innerHTML = `
      <div class="metric"><div class="label">thinking</div><div class="value">${fmt(ck.thinking||0)}</div></div>
      <div class="metric"><div class="label">text</div><div class="value">${fmt(ck.text||0)}</div></div>
      <div class="metric"><div class="label">tool_use</div><div class="value">${fmt(ck.tool_use||0)}</div></div>
      <div class="subtle" style="margin-top:8px;">assistant message content blocks 数量 (近 ${local.scan_days}d)</div>`;

    // Hourly chart
    const hours = Array.from({length: 24}, (_, i) => i);
    const ht = local.by_hour_today || {};
    const hy = local.by_hour_yesterday || {};
    const allMax = Math.max(1, ...hours.map(h => (ht[h]||0)), ...hours.map(h => (hy[h]||0)));
    const chart = document.getElementById('hour_chart');
    chart.innerHTML = '';
    for (const h of hours) {
      const today_h = ht[h] || 0;
      const yest_h = hy[h] || 0;
      const div = document.createElement('div');
      div.style.position = 'relative'; div.style.width = '14px'; div.style.height = '60px';
      const tBar = document.createElement('div');
      tBar.className = 'hourbar';
      tBar.style.position = 'absolute'; tBar.style.bottom = '0'; tBar.style.left = '7px';
      tBar.style.width = '6px';
      tBar.style.height = (today_h / allMax * 100) + '%';
      tBar.title = `今日 ${h}时: ${fmt(today_h)}`;
      const yBar = document.createElement('div');
      yBar.className = 'hourbar yest';
      yBar.style.position = 'absolute'; yBar.style.bottom = '0'; yBar.style.left = '0';
      yBar.style.width = '6px';
      yBar.style.height = (yest_h / allMax * 100) + '%';
      yBar.title = `昨日 ${h}时: ${fmt(yest_h)}`;
      div.appendChild(yBar); div.appendChild(tBar);
      chart.appendChild(div);
    }
    const labels = document.getElementById('hour_labels');
    labels.innerHTML = hours.map(h => `<span>${h}</span>`).join('');

    // Tools table
    const tt = document.querySelector('#tool_table tbody');
    tt.innerHTML = '';
    const tools = Object.entries(local.by_tool || {})
      .sort((a,b) => (b[1].total || 0) - (a[1].total || 0)).slice(0, 12);
    const totalToolToken = tools.reduce((s, [_,v]) => s + (v.total||0), 0);
    for (const [name, v] of tools) {
      const pct = totalToolToken > 0 ? (v.total||0) / totalToolToken * 100 : 0;
      tt.innerHTML += `<tr>
        <td>${name}</td>
        <td class="right">${fmt(v.turns||0)}</td>
        <td class="right">${fmt(v.output||0)}</td>
        <td class="right">${fmt(v.cache_read||0)}</td>
        <td>${bartrack(pct)}</td>
      </tr>`;
    }

    // Subagent types
    const stb = document.querySelector('#subtype_table tbody');
    stb.innerHTML = '';
    const subTypes = Object.entries(local.sub_types_count || {})
      .sort((a,b) => b[1] - a[1]);
    const tokenProxy = local.sub_types_token_proxy || {};
    const totalSubInv = subTypes.reduce((s, [_,c]) => s + c, 0);
    if (subTypes.length === 0) {
      stb.innerHTML = '<tr><td colspan="4" class="subtle">最近未捕获到 subagent 调用</td></tr>';
    }
    for (const [name, count] of subTypes) {
      const pct = totalSubInv > 0 ? count/totalSubInv * 100 : 0;
      stb.innerHTML += `<tr>
        <td>${name}</td>
        <td class="right">${count}</td>
        <td class="right">${fmt(tokenProxy[name]||0)}</td>
        <td>${bartrack(pct)}</td>
      </tr>`;
    }

    // Projects
    const pt = document.querySelector('#project_table tbody');
    pt.innerHTML = '';
    const projects = Object.entries(local.by_project || {})
      .sort((a,b) => (b[1].total||0) - (a[1].total||0)).slice(0, 15);
    const totalProj = projects.reduce((s,[_,v]) => s + (v.total||0), 0);
    for (const [name, v] of projects) {
      const pct = totalProj > 0 ? (v.total||0)/totalProj * 100 : 0;
      pt.innerHTML += `<tr>
        <td><span class="subtle">${name}</span></td>
        <td class="right">${fmt(v.turns||0)}</td>
        <td class="right">${fmt(v.output||0)}</td>
        <td class="right">${fmt(v.cache_create||0)}</td>
        <td class="right">${fmt(v.cache_read||0)}</td>
        <td class="right">${fmt(v.total||0)}</td>
        <td>${bartrack(pct)}</td>
      </tr>`;
    }

    // Daily
    const db = document.querySelector('#daily_table tbody');
    db.innerHTML = '';
    const daily = (d.daily?.daily) || [];
    for (const row of daily.slice(-14).reverse()) {
      const models = (row.modelsUsed || []).map(m => m.replace('claude-','').replace(/-2025\d+/,'')).join(' + ');
      db.innerHTML += `<tr>
        <td>${row.period || row.date || '-'}</td>
        <td><span class="subtle">${models}</span></td>
        <td class="right">${fmt(row.inputTokens)}</td>
        <td class="right">${fmt(row.outputTokens)}</td>
        <td class="right">${fmt(row.cacheCreationTokens)}</td>
        <td class="right">${fmt(row.cacheReadTokens)}</td>
        <td class="right">${fmt(row.totalTokens)}</td>
        <td class="right"><strong>${fmtUSD(row.totalCost)}</strong></td>
      </tr>`;
    }

    // Expensive turns
    const eb = document.querySelector('#exp_table tbody');
    eb.innerHTML = '';
    for (const t of (local.expensive_turns || [])) {
      const ts = t.ts ? new Date(t.ts).toLocaleString() : '-';
      const tools = (t.tools || []).map(n => `<span class="tag${t.sidechain?' sub':''}">${n}</span>`).join('');
      eb.innerHTML += `<tr>
        <td><span class="subtle">${ts}</span></td>
        <td><span class="subtle">${t.project || '-'}</span></td>
        <td><span class="subtle">${t.model || '-'}</span></td>
        <td>${tools || (t.sidechain ? '<span class="tag sub">subagent</span>' : '<span class="subtle">(text only)</span>')}</td>
        <td class="right"><strong>${fmt(t.output)}</strong></td>
        <td class="right">${fmt(t.cache_read)}</td>
      </tr>`;
    }

    // Sessions (ccusage)
    const sb = document.querySelector('#session_table tbody');
    sb.innerHTML = '';
    const sessions = (d.session?.sessions) || [];
    for (const row of sessions.slice(0, 15)) {
      const models = (row.modelsUsed || []).map(m => m.replace('claude-','').replace(/-2025\d+/,'')).join(', ');
      const sid = (row.sessionId || '').slice(0, 50);
      sb.innerHTML += `<tr>
        <td><span class="subtle">${sid}</span></td>
        <td><span class="subtle">${models}</span></td>
        <td class="right">${fmt(row.totalTokens)}</td>
        <td class="right"><strong>${fmtUSD(row.totalCost)}</strong></td>
        <td><span class="subtle">${row.lastActivity || '-'}</span></td>
      </tr>`;
    }


    // RTK summary
    const rtk = d.rtk || {};
    const rs = rtk.summary || {};
    const rsum = document.getElementById('rtk_summary');
    if (rs.c) {
      rsum.innerHTML = `
        <div class="metric"><div class="label">总命令数</div><div class="value">${fmt(rs.c)}</div></div>
        <div class="metric"><div class="label">原 token (raw)</div><div class="value">${fmt(rs.raw_in)}</div></div>
        <div class="metric"><div class="label">RTK 后 (output)</div><div class="value">${fmt(rs.rtk_out)}</div></div>
        <div class="metric"><div class="label">节省</div><div class="value green">${fmt(rs.saved)}</div></div>
        <div class="metric"><div class="label">实际压缩率</div><div class="value green">${fmtPct(rs.overall_pct)}</div><div class="subtle" style="font-size:9px;margin-top:2px;">(saved/raw_in)</div></div>
        <div class="metric"><div class="label">parse 失败 (7d)</div><div class="value ${rtk.failures_7d > 100 ? 'yellow' : ''}">${fmt(rtk.failures_7d || 0)}</div></div>
        <div class="subtle" style="margin-top:8px;">数据范围: ${rs.first_day || '?'} ~ ${rs.last_day || '?'}</div>`;
    } else {
      rsum.innerHTML = '<span class="subtle">RTK 数据未读到</span>';
    }

    // RTK by command
    const rcb = document.querySelector('#rtk_cmd_table tbody');
    rcb.innerHTML = '';
    const cmds = rtk.by_cmd || [];
    const totalSaved = cmds.reduce((s, c) => s + (c.saved || 0), 0);
    for (const c of cmds) {
      const pct = totalSaved > 0 ? c.saved / totalSaved * 100 : 0;
      rcb.innerHTML += `<tr>
        <td><span class="subtle">${c.cmd}</span></td>
        <td class="right">${fmt(c.cnt)}</td>
        <td class="right">${fmt(c.raw_in)}</td>
        <td class="right">${fmt(c.rtk_out)}</td>
        <td class="right"><strong>${fmt(c.saved)}</strong></td>
        <td>${bartrack(pct)}</td>
      </tr>`;
    }

    // RTK by project
    const rpb = document.querySelector('#rtk_proj_table tbody');
    rpb.innerHTML = '';
    const projs = rtk.by_project || [];
    const totalProjSaved = projs.reduce((s,p) => s + (p.saved||0), 0);
    for (const p of projs) {
      const pct = totalProjSaved > 0 ? p.saved/totalProjSaved * 100 : 0;
      const pname = p.project_path.split('/').slice(-3).join('/');
      rpb.innerHTML += `<tr>
        <td><span class="subtle">${pname}</span></td>
        <td class="right">${fmt(p.cnt)}</td>
        <td class="right"><strong>${fmt(p.saved)}</strong></td>
        <td>${bartrack(pct)}</td>
      </tr>`;
    }

    // RTK daily
    const rdb = document.querySelector('#rtk_daily_table tbody');
    rdb.innerHTML = '';
    for (const r of (rtk.daily || [])) {
      rdb.innerHTML += `<tr>
        <td>${r.day}</td>
        <td class="right">${fmt(r.cnt)}</td>
        <td class="right">${fmt(r.raw_in)}</td>
        <td class="right">${fmt(r.rtk_out)}</td>
        <td class="right"><strong>${fmt(r.saved)}</strong></td>
        <td class="right">${fmtPct(r.pct)}</td>
      </tr>`;
    }

    // RTK worst
    const rwb = document.querySelector('#rtk_worst_table tbody');
    rwb.innerHTML = '';
    for (const w of (rtk.worst_commands || [])) {
      rwb.innerHTML += `<tr>
        <td><span class="subtle">${w.cmd}</span></td>
        <td class="right">${fmt(w.cnt)}</td>
        <td class="right ${w.avg_pct < 20 ? 'red' : ''}">${fmtPct(w.avg_pct)}</td>
        <td class="right">${fmt(w.raw_in)}</td>
      </tr>`;
    }

    renderInsights(d);
  } catch (e) {
    document.getElementById('status').textContent = '刷新失败: ' + e.message;
    document.getElementById('dot').className = 'dot stale';
  }
}
refresh();
setInterval(refresh, 30000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = HTML_PAGE.replace("{{DAYS}}", str(LOCAL_SCAN_DAYS)).replace("{{REFRESH_SECONDS}}", str(REFRESH_SECONDS))
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
        elif self.path == "/api/data":
            with _state_lock:
                payload = {
                    "daily": _state["daily"], "blocks": _state["blocks"],
                    "session": _state["session"], "weekly": _state["weekly"], "local": _state["local"], "rtk": _state["rtk"], "anthropic": _state["anthropic"], "codex": _state["codex"], "autonomy8": _state["autonomy8"],
                    "last_refresh_ts": _state["last_refresh_ts"],
                    "last_refresh_status": _state["last_refresh_status"],
                    "errors": _state["errors"],
                    "host": HOST, "port": PORT, "refresh_seconds": REFRESH_SECONDS, "weekly_budget_usd": WEEKLY_BUDGET_USD, "codex_probe": dict(_codex_probe_state), "codex_auto_probe_age_s": CODEX_AUTO_PROBE_AGE_SECONDS,
                }
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers(); self.wfile.write(body)
        elif self.path == "/healthz":
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        self.send_response(405)
        self.send_header("Allow", "GET")
        self.end_headers()


def main():
    if not shutil.which("npx"):
        sys.stderr.write("ERROR: npx not in PATH\n"); sys.exit(1)
    print(f"=> refresh every {REFRESH_SECONDS}s (ccusage timeout {CCUSAGE_TIMEOUT}s; local scan {LOCAL_SCAN_DAYS}d)", flush=True)
    threading.Thread(target=refresh_loop, daemon=True).start()
    print(f"=> binding {HOST}:{PORT}", flush=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"=> ready at http://{HOST}:{PORT}/", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: server.shutdown()


if __name__ == "__main__":
    main()
