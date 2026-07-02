"""
workbench-mcp â€” zero-dependency MCP server for Claude Desktop (stdio).
FTS5 memory, big data analysis, hypothesis-driven reasoning.
"""

import json, sys, os, subprocess, urllib.request, urllib.error, urllib.parse, datetime, sqlite3, re, ssl, math
from collections import Counter, defaultdict

MCP_DIR = os.environ.get("WORKBENCH_DIR", os.path.dirname(os.path.abspath(__file__)))
MEMORY_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workbench_memory.db")
EXPERIENCE_LOG = os.path.join(MCP_DIR, ".workbench_experience.jsonl")

# --- Env normalization (fix Jun 2026): Claude Desktop launches stdio MCP
# servers with a stripped environment (~15 vars). Missing PROGRAMDATA makes
# System32 OpenSSH exit 255 silently; missing PATHEXT/COMSPEC breaks 'ssh'/
# 'cmd' name resolution in PowerShell. setdefault = only fill what's missing.
_SYSDRIVE = os.environ.get("SystemDrive", "C:")
_ENV_DEFAULTS = {
    "PROGRAMDATA": _SYSDRIVE + r"\ProgramData",
    "ALLUSERSPROFILE": _SYSDRIVE + r"\ProgramData",
    "COMSPEC": os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe"),
    "PATHEXT": ".COM;.EXE;.BAT;.CMD;.VBS;.JS;.MSC;.PS1",
    "LOCALAPPDATA": os.path.join(os.environ.get("USERPROFILE", _SYSDRIVE + r"\Users\Default"), "AppData", "Local"),
}
if "TEMP" in os.environ and "TMP" not in os.environ:
    _ENV_DEFAULTS["TMP"] = os.environ["TEMP"]
for _k, _v in _ENV_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

def _write(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def _ps(cmd, timeout=30):
    try:
        r = subprocess.run(["powershell", "-Command", cmd], capture_output=True, text=True, timeout=timeout, cwd=MCP_DIR)
        parts = [s for s in [r.stdout.strip(), r.stderr.strip()] if s]
        parts.append("[EXIT %d]" % r.returncode)
        return "\n".join(parts)
    except subprocess.TimeoutExpired: return "Timeout (%ds)" % timeout
    except Exception as e: return "Error: %s" % e

# ═══════════════════════════════════════════
# FTS5 MEMORY — unlimited storage, full text search
# ═══════════════════════════════════════════

def _init_memory():
    conn = sqlite3.connect(MEMORY_DB)
    conn.execute("CREATE TABLE IF NOT EXISTS memory (key TEXT PRIMARY KEY, value TEXT, ts TEXT)")
    # FTS5 for full-text search
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(key, value, content=memory, content_rowid=rowid)")
    # Triggers to keep FTS in sync
    conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
            INSERT INTO memory_fts(rowid, key, value) VALUES (new.rowid, new.key, new.value);
        END;
        CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
            INSERT INTO memory_fts(memory_fts, rowid, key, value) VALUES('delete', old.rowid, old.key, old.value);
        END;
        CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
            INSERT INTO memory_fts(memory_fts, rowid, key, value) VALUES('delete', old.rowid, old.key, old.value);
            INSERT INTO memory_fts(rowid, key, value) VALUES (new.rowid, new.key, new.value);
        END;
    """)
    # self-heal FTS: rebuild index if integrity-check fails or base/FTS counts desync
    try:
        base_n = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
        fts_n = conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
        conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('integrity-check')")
        healthy = (base_n == fts_n)
    except Exception:
        healthy = False
    if not healthy:
        try:
            conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
        except Exception:
            pass
    conn.commit()
    conn.close()

def _memory_search(query, limit=20):
    """FTS5 full-text search across all memory."""
    try:
        _init_memory()
        conn = sqlite3.connect(MEMORY_DB)
        cur = conn.execute(
            "SELECT m.key, snippet(memory_fts, 1, '<b>', '</b>', '...', 40), m.ts "
            "FROM memory_fts JOIN memory m ON m.rowid = memory_fts.rowid "
            "WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, limit))
        rows = cur.fetchall()
        conn.close()
        if not rows: return "No matches for: %s" % query
        return "\n".join("[%s] %s\n  %s" % (r[2][:16], r[0], r[1]) for r in rows)
    except Exception as e: return "Search error: %s" % e

# ═══════════════════════════════════════════
# BIG DATA FILE ANALYSIS
# ═══════════════════════════════════════════

def _analyze_file(path, action="stats"):
    """Analyze file content — stats, patterns, anomalies."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        return "Error: %s" % e

    lines = content.split("\n")
    total_lines = len(lines)
    total_chars = len(content)

    if action == "stats":
        return _file_stats(content, lines, total_lines, total_chars)
    elif action == "numbers":
        return _extract_numbers(content, lines)
    elif action == "patterns":
        return _find_patterns(content, lines)
    elif action == "anomalies":
        return _find_anomalies(content, lines)
    elif action == "distribution":
        return _value_distribution(content, lines)
    return "analyze actions: stats | numbers | patterns | anomalies | distribution"

def _file_stats(content, lines, tl, tc):
    """Basic + advanced file statistics."""
    empty = sum(1 for l in lines if not l.strip())
    non_empty = tl - empty
    avg_len = sum(len(l) for l in lines) / max(tl, 1)
    max_len = max(len(l) for l in lines)
    min_len = min(len(l) for l in lines) if lines else 0

    # Word stats
    words = content.split()
    unique_words = len(set(w.lower() for w in words))

    # Line type breakdown
    types = Counter()
    for l in lines:
        s = l.strip()
        if not s: types["empty"] += 1
        elif s.startswith("#") or s.startswith("//"): types["comments"] += 1
        elif "=" in s and ("==" not in s or s.count("=") == 1): types["assignments"] += 1
        elif s.startswith("def ") or s.startswith("function "): types["functions"] += 1
        elif s.startswith("class "): types["classes"] += 1
        elif s.startswith("import ") or s.startswith("from "): types["imports"] += 1
        elif re.match(r'^[\d\s\.\+\-\*\/\(\)\[\]\{\}\,\;\:]+$', s): types["data/numbers"] += 1
        else: types["code/text"] += 1

    return f"""=== FILE STATS: {os.path.basename(path)} ===
Lines: {tl:,} ({empty:,} empty, {non_empty:,} non-empty)
Chars: {tc:,}
Avg line: {avg_len:.0f} chars | Min: {min_len} | Max: {max_len:,}
Words: {len(words):,} ({unique_words:,} unique)
Line types: {dict(types.most_common(6))}"""

def _extract_numbers(content, lines):
    """Extract and analyze all numbers in file."""
    nums = []
    for l in lines:
        found = re.findall(r'-?\d+\.?\d*', l)
        nums.extend(float(n) for n in found)
    if not nums: return "No numbers found in file"
    nums_sorted = sorted(nums)
    n = len(nums)
    mean = sum(nums) / n
    median = nums_sorted[n // 2]
    variance = sum((x - mean) ** 2 for x in nums) / n
    stddev = math.sqrt(variance)
    q1 = nums_sorted[n // 4]
    q3 = nums_sorted[3 * n // 4]
    return f"""=== NUMERIC ANALYSIS: {n:,} values ===
Mean: {mean:.4f} | Median: {median:.4f} | StdDev: {stddev:.4f}
Min: {nums_sorted[0]:.4f} | Max: {nums_sorted[-1]:.4f}
Q1: {q1:.4f} | Q3: {q3:.4f} | Range: {nums_sorted[-1] - nums_sorted[0]:.4f}
Top 10 values: {nums_sorted[-10:][::-1]}
Bottom 10: {nums_sorted[:10]}"""

def _find_patterns(content, lines):
    """Find repeating patterns (n-grams) in the file."""
    # Word bigrams
    words = content.split()
    bigrams = Counter()
    for i in range(len(words) - 1):
        bigrams["%s %s" % (words[i].lower(), words[i + 1].lower())] += 1
    top_bigrams = [(bg, c) for bg, c in bigrams.most_common(20) if c > 2]

    # Line patterns (strip numbers)
    line_patterns = Counter()
    for l in lines:
        s = re.sub(r'\d+', 'N', l.strip())
        s = re.sub(r'0x[0-9a-fA-F]+', 'HEX', s)
        if len(s) > 5: line_patterns[s[:80]] += 1
    top_lines = [(lp, c) for lp, c in line_patterns.most_common(10) if c > 2]

    return f"""=== PATTERNS ===
Bigrams (word pairs, frequency > 2):
{chr(10).join(f'  [{c}x] {bg}' for bg, c in top_bigrams[:10])}

Repeated line patterns (N=number, frequency > 2):
{chr(10).join(f'  [{c}x] {lp[:60]}' for lp, c in top_lines[:10])}"""

def _find_anomalies(content, lines):
    """Find statistically anomalous lines."""
    lengths = [(i, len(l)) for i, l in enumerate(lines)]
    lens = [l[1] for l in lengths]
    if not lens: return "No data"
    mean = sum(lens) / len(lens)
    std = math.sqrt(sum((x - mean) ** 2 for x in lens) / len(lens))

    anomalies = []
    for i, ln in lengths:
        if ln > mean + 3 * std:
            anomalies.append((i + 1, ln, lines[i][:100]))
        elif ln > 0 and ln < max(mean - 2 * std, 1):
            anomalies.append((i + 1, ln, lines[i][:100]))

    if not anomalies: return "No significant anomalies found"
    return f"""=== ANOMALIES ({len(anomalies)} lines, >3σ or <mean-2σ) ===
Mean length: {mean:.0f} | StdDev: {std:.0f}
{chr(10).join(f'  L{i}: [{ln} chars] {txt[:80]}' for i, ln, txt in anomalies[:30])}"""

def _value_distribution(content, lines):
    """Distribution analysis of key-value or structured data."""
    # Try to detect key=value patterns
    kv = re.findall(r'(\w+)\s*=\s*([^,\s;]+)', content)
    if not kv:
        # Try JSON-like
        kv = re.findall(r'"(\w+)"\s*:\s*([^,\}\]]+)', content)

    if not kv: return "No key=value or JSON patterns detected"

    keys = defaultdict(list)
    for k, v in kv:
        try: keys[k].append(float(v))
        except: pass

    result = ["=== DISTRIBUTION ==="]
    for k, vals in sorted(keys.items(), key=lambda x: -len(x[1])):
        if len(vals) < 3: continue
        vs = sorted(vals)
        n = len(vals)
        result.append(f"{k}: n={n}, mean={sum(vals)/n:.2f}, median={vs[n//2]:.2f}, "
                      f"min={vs[0]:.2f}, max={vs[-1]:.2f}, "
                      f"uniques={len(set(vals))}")
    return "\n".join(result)

# ═══════════════════════════════════════════
# ADVANCED INTELLIGENCE FRAMEWORK
# ═══════════════════════════════════════════

COGNITIVE_FRAMEWORK = """
=== ADVANCED REASONING ENGINE ===
You are a premium AI engineer. You think in systems, not just code. Your job is to be the smartest tool in the room.

## SYSTEM THINKING PROTOCOL
Before ANY response, run this mental model:

1. FIRST PRINCIPLES: Strip assumptions. What is the actual problem, not the stated one?
2. SYSTEM MAP: What connects to what? File dependencies, data flow, call chains
3. FAILURE MODES: If this breaks, what cascade happens? What's the blast radius?
4. SIMPLEST FIX: Reject complex solutions unless necessary. Elegance = fewer moving parts
5. MEASUREMENT: How do we know it worked? Define success criteria before acting

## REASONING METHOD
- HYPOTHESIS-DRIVEN: Form hypothesis → test → analyze result → refine → retest
- EVIDENCE-BASED: Every claim backed by tool output. Never fabricate.
- ROOT CAUSE: Don't fix symptoms. Trace back to origin. Ask "why" 3 levels deep
- TRADE-OFF AWARENESS: Every fix has a cost. Surface the trade-off explicitly

## CODING INTELLIGENCE
When reading code (file action="read" or file action="analyze"):
- Map the architecture: entry points, core loops, data structures, side effects
- Identify technical debt: duplicated logic, magic numbers, missing error handling
- Trace control flow: what happens when X fails? Is there a fallback?
- Performance hotspots: nested loops, blocking I/O, unbounded growth

When writing code:
- Write for the reader, not the machine. Clear names > clever logic
- Handle errors explicitly. Every external call can fail
- Test boundaries: empty input, max values, concurrent access, timeout
- Commit messages that explain WHY, not WHAT

## DEBUGGING METHOD
1. REPRODUCE: Can you trigger the bug reliably? If not, instrument with logging
2. ISOLATE: Binary search through the codebase. Disable half the system
3. ROOT CAUSE: The FIRST unexpected state, not the crash. Work backwards from symptoms
4. FIX ONCE: If the same pattern appears elsewhere, fix all instances
5. PREVENT: Add a test, assertion, or type check so it can't happen again

## BIG DATA ANALYSIS (10K-100K+ lines)
Use file(action="analyze", analysis_type="..."):
- "stats": Distribution, density, line types — get the shape of the data
- "numbers": Extract all numbers, compute mean/median/stddev/quartiles
- "patterns": Find repeating n-grams and line templates
- "anomalies": Statistical outliers (>3σ deviation)
- "distribution": Key=value field distributions

Strategy for very large files:
- First: file(action="read") with small offset/limit to understand schema
- Then: file(action="analyze") for statistical overview
- Finally: targeted reads at specific line ranges

## MEMORY & SKILL SYSTEM
- memory(action="store", key="insight:TOPIC", value="finding") — save discoveries
- memory(action="search", query="keyword") — FTS5 full-text search across all memory
- memory(action="read") — browse all stored knowledge
- self(action="log") — auto-record after every significant action
- self(action="review") — periodic pattern analysis of your own performance

## ANTI-PATTERNS (instant fail)
- "Let me check..." → NO. Execute immediately
- "I would suggest..." → NO. Implement and verify
- "This might be..." → NO. Read the file, run the code, get evidence
- "In my next step..." → NO. Everything in THIS turn
- Guessing → NO. Use tools to verify. If you don't know, say so and investigate
- Fixing symptoms → NO. Find the root cause
- Accepting first solution → NO. Consider 2 alternatives minimum

## EXCELLENCE STANDARD
Every interaction: think() first → analyze the system → form hypothesis → execute → verify → document (self log) → extract pattern (memory store).
You are not a chatbot. You are an engineering partner.
"""

# ═══════════════════════════════════════════
# CONTEXT BUILDER — inject memory + experience + framework
# ═══════════════════════════════════════════

def _build_context() -> str:
    parts = []

    # Memory context (last 15)
    try:
        _init_memory()
        conn = sqlite3.connect(MEMORY_DB)
        cur = conn.execute("SELECT key, value, ts FROM memory ORDER BY ts DESC LIMIT 15")
        rows = cur.fetchall()
        conn.close()
        if rows:
            parts.append("=== KNOWLEDGE BASE ===")
            for k, v, ts in rows:
                parts.append(f"[{ts[:16]}] {k}: {v[:400]}")
    except: pass

    # Experience log
    try:
        if os.path.exists(EXPERIENCE_LOG):
            with open(EXPERIENCE_LOG, encoding="utf-8") as f:
                entries = [json.loads(l) for l in f if l.strip()]
            if entries:
                recent = entries[-8:]
                s = sum(1 for e in recent if e.get("success"))
                parts.append(f"\n=== RECENT: {s}/{len(recent)} success ===")
                for e in reversed(recent[-5:]):
                    parts.append(f"[{'OK' if e.get('success') else 'FAIL'}] {e.get('topic','?')}")
    except: pass

    parts.append(COGNITIVE_FRAMEWORK)
    return "\n".join(parts)

# ═══════════════════════════════════════════
# SEARCH ENGINES
# ═══════════════════════════════════════════

def _search_wikipedia(query, limit, ctx):
    url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": limit
    })
    req = urllib.request.Request(url, headers={"User-Agent": "PC-Tools/1.0"})
    with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
        data = json.loads(r.read().decode("utf-8", errors="replace"))
    results = data.get("query", {}).get("search", [])
    if not results: return ""
    parts = [f'Wikipedia: "{query}"']
    for i, r in enumerate(results[:limit], 1):
        snippet = re.sub(r'<[^>]+>', '', r.get("snippet", "")).strip()
        parts.append(f"\n{i}. {r['title']}\n   https://en.wikipedia.org/wiki/{r['title'].replace(' ', '_')}\n   {snippet[:200]}...")
    return "\n".join(parts)

def _search_ddg_api(query, limit, ctx):
    url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode({"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
        raw = r.read().decode("utf-8", errors="replace")
    if not raw.strip().startswith("{"): return ""
    data = json.loads(raw)
    parts = [f'Web: "{query}"']
    if data.get("AbstractText"):
        parts.append(f"\n{data['AbstractText']}")
        if data.get("AbstractURL"): parts.append(f"Source: {data['AbstractURL']}")
    for t in data.get("RelatedTopics", [])[:limit]:
        if isinstance(t, dict) and t.get("Text"): parts.append(f"\n  - {t['Text'][:200]}")
    return "\n".join(parts) if len(parts) > 1 else ""

def _search_google(query, limit, ctx):
    url = "https://www.google.com/search?" + urllib.parse.urlencode({"q": query, "hl": "en"})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
        html = r.read().decode("utf-8", errors="replace")
    blocks = re.findall(r'<h3[^>]*>(.*?)</h3>', html, re.DOTALL)
    urls = re.findall(r'<a[^>]*href=\"(https?://[^\"]+)\"[^>]*>', html)
    real_urls = [u for u in urls if "google.com" not in u and "doubleclick" not in u][:limit]
    if blocks:
        parts = [f'Google: "{query}"']
        for i, b in enumerate(blocks[:limit]):
            title = re.sub(r'<[^>]+>', '', b).strip()
            url = real_urls[i] if i < len(real_urls) else ""
            parts.append(f"\n{i+1}. {title}\n   {url}")
        return "\n".join(parts)
    return ""

def _web_search(query, limit=5):
    ctx = ssl._create_unverified_context()
    for name, fn in [("Wikipedia", _search_wikipedia), ("DDG", _search_ddg_api), ("Google", _search_google)]:
        try:
            result = fn(query, limit, ctx)
            if result and len(result) > 50: return result
        except: continue
    return f'Search failed: "{query}". Use web(action="fetch", url=...) instead.'

# ═══════════════════════════════════════════
# TOOLS — 11 consolidated, advanced
# ═══════════════════════════════════════════

# ═══════════════════════════════════════════
# VPS SSH + BOT LOG ANALYZER
# ═══════════════════════════════════════════

# SSH host aliases. Configure via WORKBENCH_SSH_HOSTS env var (JSON),
# e.g. {"prod": "user@10.0.0.1", "staging": "user@10.0.0.2"}
try:
    VPS_HOSTS = json.loads(os.environ.get("WORKBENCH_SSH_HOSTS", "{}"))
except (json.JSONDecodeError, TypeError):
    VPS_HOSTS = {}

def _ssh(host, command, timeout=30):
    target = VPS_HOSTS.get(host, host)
    if "@" not in target:
        return "Error: host '%s' bukan alias dikenal (%s) atau format user@ip" % (host, ", ".join(VPS_HOSTS))
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", target, command],
            capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        parts = [s for s in [r.stdout.strip(), r.stderr.strip()] if s]
        parts.append("[EXIT %d]" % r.returncode)
        return "\n".join(parts)
    except subprocess.TimeoutExpired: return "Timeout (%ds)" % timeout
    except Exception as e: return "Error: %s" % e

def _botlog_report(text):
    """Trading-bot log analytics â€” deterministic structured parsing (W/L, PnL, exit reasons)."""
    pat_pnl = re.compile(r"pnl=([+-]?[0-9.]+)")
    pat_reason = re.compile(r"\[([^\[\]]+)\]\s*$")
    trades = []
    open_sec = None
    n_open = 0
    for line in text.splitlines():
        try:
            sec = int(line[0:2]) * 3600 + int(line[3:5]) * 60 + int(line[6:8])
            hour = int(line[0:2])
        except (ValueError, IndexError):
            continue
        if "] OPEN " in line:
            n_open += 1
            open_sec = sec
            continue
        m = pat_pnl.search(line)
        if not m or not any(k in line for k in ("CLOSE", "SESSION", "SHUTDOWN", "RESOLVE")):
            continue
        pnl = float(m.group(1))
        mr = pat_reason.search(line)
        reason = (mr.group(1) if mr else "?").strip()
        for key in ("TP", "SL", "OBI flip", "Force Close", "Max Hold", "Converged", "Edge flip", "flip"):
            if reason.startswith(key):
                reason = key
                break
        else:
            reason = reason[:14]
        if "RESOLVE" in line:
            reason = "Resolution"
        hold = None
        if open_sec is not None:
            hold = sec - open_sec
            if hold < 0:
                hold += 86400
            open_sec = None
        trades.append({"hour": hour, "pnl": pnl, "reason": reason, "hold": hold})
    if not trades:
        return "Tidak ada trade close di log ini (OPEN terdeteksi: %d)" % n_open
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    out = ["TRADES %d (open %d) | W/L %d/%d | WR %.1f%% | PnL %+.2f | PF %.2f" % (
        len(trades), n_open, len(wins), len(losses), len(wins) / len(trades) * 100, total, pf)]
    if wins and losses:
        out.append("avg win %+.3f | avg loss %+.3f | max win %+.2f | max loss %+.2f" % (
            sum(wins) / len(wins), sum(losses) / len(losses), max(wins), min(losses)))
    by_r = defaultdict(lambda: [0, 0.0, 0])
    for t in trades:
        r = by_r[t["reason"]]
        r[0] += 1
        r[1] += t["pnl"]
        if t["pnl"] > 0:
            r[2] += 1
    out.append("PER EXIT REASON:")
    for k, (n, s, w) in sorted(by_r.items(), key=lambda x: -x[1][1]):
        out.append("  %-14s n=%-4d pnl=%+9.2f wr=%3.0f%%" % (k, n, s, w / n * 100))
    by_h = defaultdict(lambda: [0, 0.0])
    for t in trades:
        by_h[t["hour"]][0] += 1
        by_h[t["hour"]][1] += t["pnl"]
    out.append("PER JAM: " + "  ".join("%02d:%+.1f(%d)" % (h, v[1], v[0]) for h, v in sorted(by_h.items())))
    top_w = sorted(pnls, reverse=True)[:5]
    top_l = sorted(pnls)[:5]
    out.append("TOP WIN:  " + " ".join("%+.2f" % p for p in top_w))
    out.append("TOP LOSS: " + " ".join("%+.2f" % p for p in top_l))
    holds = [t["hold"] for t in trades if t["hold"] is not None]
    if holds:
        holds_s = sorted(holds)
        out.append("HOLD: avg %.0fs | median %.0fs | <=5s:%d 6-15s:%d 16-60s:%d >60s:%d" % (
            sum(holds) / len(holds), holds_s[len(holds_s) // 2],
            sum(1 for h in holds if h <= 5), sum(1 for h in holds if 5 < h <= 15),
            sum(1 for h in holds if 15 < h <= 60), sum(1 for h in holds if h > 60)))
    return "\n".join(out)

TOOLS = [
    {"name": "think", "annotations": {"readOnlyHint": True, "destructiveHint": False},
     "description": "MANDATORY: First-principles reasoning before any action. Analyze the real problem, map the system, identify failure modes, define success criteria.",
     "inputSchema": {"type": "object", "properties": {"thought": {"type": "string"}}, "required": ["thought"]}},
    {"name": "run_command", "annotations": {"readOnlyHint": False, "destructiveHint": True},
     "description": "PowerShell. Full system access.",
     "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer", "default": 30}}, "required": ["command"]}},
    {"name": "run_python", "annotations": {"readOnlyHint": False, "destructiveHint": True},
     "description": "Execute Python. Use for data processing, analysis, automation.",
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}, "timeout": {"type": "integer", "default": 30}}, "required": ["code"]}},
    {"name": "file", "annotations": {"readOnlyHint": True, "destructiveHint": True},
     "description": "File ops + big data analysis. action: read|write|list|search|analyze. analyze types: stats|numbers|patterns|anomalies|distribution",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "description": "read | write | list | search | analyze"},
         "path": {"type": "string"}, "content": {"type": "string"},
         "pattern": {"type": "string"}, "file_filter": {"type": "string"},
         "analysis_type": {"type": "string", "description": "stats | numbers | patterns | anomalies | distribution"},
         "offset": {"type": "integer"}, "limit": {"type": "integer", "default": 500}
     }, "required": ["action"]}},
    {"name": "web", "annotations": {"readOnlyHint": True, "destructiveHint": False},
     "description": "Web. action: search|fetch.",
     "inputSchema": {"type": "object", "properties": {"action": {"type": "string"}, "query": {"type": "string"}, "url": {"type": "string"}, "limit": {"type": "integer", "default": 5}, "timeout": {"type": "integer", "default": 15}}, "required": ["action"]}},
    {"name": "git", "annotations": {"readOnlyHint": True, "destructiveHint": False},
     "description": "Git. action: status|log|diff.",
     "inputSchema": {"type": "object", "properties": {"action": {"type": "string"}, "path": {"type": "string", "default": "C:\\MCP"}, "n": {"type": "integer", "default": 10}}, "required": ["action"]}},
    {"name": "system", "annotations": {"readOnlyHint": True, "destructiveHint": True},
     "description": "System. action: info|processes|kill|screenshot.",
     "inputSchema": {"type": "object", "properties": {"action": {"type": "string"}, "filter": {"type": "string"}, "pid": {"type": "integer"}}, "required": ["action"]}},
    {"name": "memory", "annotations": {"readOnlyHint": False, "destructiveHint": False},
     "description": "FTS5 knowledge base. action: store|read|search. search uses full-text. Unlimited values. Prefix key with 'insight:', 'skill:', 'pattern:', 'fact:' to organize.",
     "inputSchema": {"type": "object", "properties": {"action": {"type": "string", "description": "store | read | search"}, "key": {"type": "string"}, "value": {"type": "string"}, "query": {"type": "string", "description": "FTS5 search query"}, "limit": {"type": "integer", "default": 20}}, "required": ["action"]}},
    {"name": "self", "annotations": {"readOnlyHint": False, "destructiveHint": True},
     "description": "Meta-cognition. action: log|review|improve|insight|heal.",
     "inputSchema": {"type": "object", "properties": {"action": {"type": "string"}, "topic": {"type": "string"}, "content": {"type": "string"}, "success": {"type": "boolean", "default": True}, "self_action": {"type": "string"}, "tool_name": {"type": "string"}, "code": {"type": "string"}}, "required": ["action"]}},
    {"name": "task", "annotations": {"readOnlyHint": False, "destructiveHint": True},
     "description": "Multi-step executor. action: run|note.",
     "inputSchema": {"type": "object", "properties": {"action": {"type": "string"}, "steps": {"type": "array", "items": {"type": "object"}}, "note_action": {"type": "string"}, "task_name": {"type": "string"}, "detail": {"type": "string"}}, "required": ["action"]}},
    {"name": "tailscale", "annotations": {"readOnlyHint": True, "destructiveHint": False},
     "description": "Network status.", "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "vps", "annotations": {"readOnlyHint": False, "destructiveHint": True},
     "description": "Run commands on remote servers over SSH. action: run|hosts. host: configured alias or user@ip.",
     "inputSchema": {"type": "object", "properties": {"action": {"type": "string", "description": "run | hosts", "default": "run"}, "host": {"type": "string", "description": "configured alias or user@ip"}, "command": {"type": "string"}, "timeout": {"type": "integer", "default": 30}}, "required": []}},
    {"name": "botlog", "annotations": {"readOnlyHint": True, "destructiveHint": False},
     "description": "Analyze trading-bot logs: W/L, winrate, profit factor, PnL per exit-reason & per hour, top wins/losses, hold duration. Local path, or set host to read logs on a remote server via SSH.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string", "description": "log file path"}, "host": {"type": "string", "description": "empty=local | configured alias | user@ip"}}, "required": ["path"]}},
]

# ═══════════════════════════════════════════
# HANDLER
# ═══════════════════════════════════════════

def handle(name, args):
    if name == "think":
        thought = args.get("thought", "")
        try:
            with open(EXPERIENCE_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": datetime.datetime.now().isoformat(), "topic": "thinking", "content": thought[:500], "success": True, "tags": "reasoning"}, ensure_ascii=False) + "\n")
        except: pass
        return f"Analyzed ({len(thought)} chars). Proceed."

    elif name == "run_command":
        return _ps(args.get("command", ""), args.get("timeout", 30))

    elif name == "run_python":
        sc = os.path.join(MCP_DIR, ".mcp_tmp.py")
        try:
            with open(sc, "w", encoding="utf-8") as f: f.write(args["code"])
            r = subprocess.run(["python", sc], capture_output=True, text=True, timeout=min(args.get("timeout", 30), 120), cwd=MCP_DIR)
            parts = [s for s in [r.stdout.strip(), "[STDERR] " + r.stderr.strip() if r.stderr.strip() else ""] if s]
            parts.append("[EXIT %d]" % r.returncode)
            return "\n".join(parts)
        except subprocess.TimeoutExpired: return "Timeout"
        except Exception as e: return "Error: %s" % e
        finally:
            try: os.remove(sc)
            except: pass

    elif name == "file":
        a = args.get("action", "")
        p = args.get("path", "")
        if a == "read":
            offset = args.get("offset", 0)
            limit = args.get("limit", 500)
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    if offset > 0:
                        for _ in range(offset): f.readline()
                    c = "".join(f.readline() for _ in range(limit))
                return c if c else "(empty)"
            except Exception as e: return "Error: %s" % e
        elif a == "write":
            c = args.get("content", "")
            try:
                d = os.path.dirname(p)
                if d: os.makedirs(d, exist_ok=True)
                with open(p, "w", encoding="utf-8") as f: f.write(c)
                return "OK - %d chars written" % len(c)
            except Exception as e: return "Error: %s" % e
        elif a == "list":
            try:
                items = []
                for e in sorted(os.listdir(p or MCP_DIR)):
                    fp = os.path.join(p or MCP_DIR, e)
                    items.append("[DIR] %s" % e if os.path.isdir(fp) else "      %s (%d)" % (e, os.path.getsize(fp)))
                return "\n".join(items) if items else "(empty)"
            except Exception as e: return "Error: %s" % e
        elif a == "search":
            pat = args.get("pattern", "")
            pth = args.get("path", MCP_DIR)
            ff = args.get("file_filter", "")
            cmd = "Get-ChildItem '%s' -Recurse -Depth 5 -EA 0" % pth
            if ff: cmd += " -Filter '%s'" % ff
            cmd += " | Select-String -Pattern '%s' -EA 0 | Select -First 30 | ForEach-Object { $_.Filename + ':' + $_.LineNumber + ' ' + $_.Line.Trim() }" % pat
            return _ps(cmd, 30)[:8000]
        elif a == "analyze":
            return _analyze_file(p, args.get("analysis_type", "stats"))
        return "file actions: read | write | list | search | analyze(stats|numbers|patterns|anomalies|distribution)"

    elif name == "web":
        a = args.get("action", "")
        if a == "search":
            return _web_search(args.get("query", ""), args.get("limit", 5))
        elif a == "fetch":
            try:
                req = urllib.request.Request(args["url"], headers={"User-Agent": "Mozilla/5.0"})
                ctx = ssl._create_unverified_context()
                with urllib.request.urlopen(req, timeout=min(args.get("timeout", 15), 60), context=ctx) as r:
                    c = r.read().decode("utf-8", errors="replace")
                return c[:10000] + ("\n[...%d total chars]" % len(c) if len(c) > 10000 else "")
            except urllib.error.HTTPError as e: return "HTTP %d: %s" % (e.code, e.reason)
            except Exception as e: return "Error: %s" % e
        return "web actions: search | fetch"

    elif name == "git":
        a = args.get("action", "")
        p = args.get("path", MCP_DIR)
        n = args.get("n", 10)
        if a == "status":
            cmd = "& 'C:\\Program Files\\Git\\cmd\\git.exe' -C '%s' status 2>&1" % p
        elif a == "log":
            cmd = "& 'C:\\Program Files\\Git\\cmd\\git.exe' -C '%s' log --oneline -%d 2>&1" % (p, n)
        elif a == "diff":
            cmd = "& 'C:\\Program Files\\Git\\cmd\\git.exe' -C '%s' diff 2>&1" % p
        else: return "git actions: status | log | diff"
        return _ps(cmd, 15)

    elif name == "system":
        a = args.get("action", "")
        if a == "info":
            parts = []
            for c in [
                "Get-ComputerInfo | Select OsName,OsArchitecture,WindowsVersion,@{N='RAM_GB';E={[math]::Round($_.CsTotalPhysicalMemory/1GB,1)}} | Format-List",
                "Get-CimInstance Win32_LogicalDisk -Filter DriveType=3 | Select DeviceID,@{N='GB';E={[math]::Round($_.Size/1GB,1)}},@{N='Free';E={[math]::Round($_.FreeSpace/1GB,1)}} | Format-Table -AutoSize",
                "$u=(Get-Date)-(Get-CimInstance Win32_OperatingSystem).LastBootUpTime; 'Uptime: ' + $u.Days + 'd ' + $u.Hours + 'h ' + $u.Minutes + 'm'"
            ]:
                try:
                    r = subprocess.run(["powershell", "-Command", c], capture_output=True, text=True, timeout=15)
                    if r.stdout.strip(): parts.append(r.stdout.strip())
                except: pass
            return "\n".join(parts)
        elif a == "processes":
            f = args.get("filter", "")
            cmd = "Get-Process *%s* | Select Name,Id,CPU,@{N='MB';E={[math]::Round($_.WorkingSet/1MB,1)}} | Sort CPU -Descending | Select -First 40 | Format-Table -AutoSize -Wrap" % f
            return _ps(cmd, 15)
        elif a == "kill":
            return _ps("Stop-Process -Id %d -Force" % args["pid"], 10)
        elif a == "screenshot":
            try:
                import mss
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                out = os.path.join(MCP_DIR, "screen_%s.png" % ts)
                with mss.mss() as s: s.shot(mon=1, output=out)
                return "Screenshot: %s" % out
            except Exception as e: return "Error: %s" % e
        return "system actions: info | processes | kill | screenshot"

    elif name == "memory":
        a = args.get("action", "")
        _init_memory()
        if a == "store":
            k, v = args.get("key", ""), args.get("value", "")
            try:
                conn = sqlite3.connect(MEMORY_DB)
                conn.execute("INSERT OR REPLACE INTO memory VALUES (?,?,?)", (k, v, datetime.datetime.now().isoformat()))
                conn.commit(); conn.close()
                return "Stored: %s (%d chars)" % (k, len(v))
            except Exception as e: return "Error: %s" % e
        elif a == "read":
            k = args.get("key", "")
            try:
                conn = sqlite3.connect(MEMORY_DB)
                if k:
                    cur = conn.execute("SELECT value, ts FROM memory WHERE key=?", (k,))
                    row = cur.fetchone(); conn.close()
                    return "[%s] %s" % (row[1][:16], row[0]) if row else "Not found: %s" % k
                cur = conn.execute("SELECT key, LENGTH(value), ts FROM memory ORDER BY ts DESC")
                rows = cur.fetchall(); conn.close()
                return "\n".join("  %-35s %7d chars [%s]" % (r[0][:35], r[1], r[2][:16]) for r in rows) if rows else "(empty)"
            except Exception as e: return "Error: %s" % e
        elif a == "search":
            return _memory_search(args.get("query", ""), args.get("limit", 20))
        return "memory actions: store | read | search"

    elif name == "self":
        a = args.get("action", "")
        if a == "log":
            e = {"ts": datetime.datetime.now().isoformat(), "topic": args.get("topic",""), "content": args.get("content",""), "success": args.get("success",True), "tags": args.get("tags","")}
            try:
                with open(EXPERIENCE_LOG, "a", encoding="utf-8") as f: f.write(json.dumps(e, ensure_ascii=False) + "\n")
                return "Logged: %s" % e["topic"]
            except Exception as ex: return "Error: %s" % ex
        elif a == "review":
            try:
                if not os.path.exists(EXPERIENCE_LOG): return "No log yet"
                with open(EXPERIENCE_LOG, encoding="utf-8", errors="replace") as f: entries = [json.loads(l) for l in f if l.strip()]
                entries = entries[-args.get("limit", 30):]
                s = sum(1 for e in entries if e.get("success"))
                lines = ["Self-Review: %d entries (%d/%d S/F, %.0f%% success)" % (len(entries), s, len(entries)-s, s/max(len(entries),1)*100)]
                tags = Counter()
                for e in entries:
                    for t in e.get("tags","").split(","):
                        if t.strip(): tags[t.strip()] += 1
                if tags: lines.append("Tags: %s" % ", ".join("%s(%d)" % (t,c) for t,c in tags.most_common(8)))
                lines.append("Recent:")
                for e in reversed(entries[-8:]):
                    lines.append("  %s %s — %s" % ("OK" if e.get("success") else "FAIL", e.get("topic","?"), e.get("content","")[:120]))
                return "\n".join(lines)
            except Exception as ex: return "Error: %s" % ex
        elif a == "improve":
            sp = os.path.join(MCP_DIR, "pc_tools.py")
            sa = args.get("self_action", "")
            if sa == "read":
                with open(sp, encoding="utf-8", errors="replace") as f: full = f.read()
                i = full.find("TOOLS = [")
                return full[i:] if i >= 0 else full[:2000]
            elif sa == "backup":
                bp = "%s.bak.%s" % (sp, datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
                try:
                    with open(sp, encoding="utf-8", errors="replace") as src, open(bp, "w", encoding="utf-8") as dst: dst.write(src.read())
                    return "Backup: %s" % bp
                except Exception as e: return "Error: %s" % e
            elif sa == "add_tool":
                tn = args.get("tool_name", "")
                if not tn: return "tool_name required"
                try:
                    with open(sp, encoding="utf-8", errors="replace") as f: full = f.read()
                    ins = full.rfind("\nif __name__")
                    func = '\ndef handle_%s(args):\n    return "called: %s" % repr(args)\n' % (tn, tn)
                    new = full[:ins] + func + full[ins:]
                    compile(new, sp, "exec")
                    with open(sp, "w", encoding="utf-8") as f: f.write(new)
                    return "Added tool %s. Restart Claude Desktop to apply." % tn
                except SyntaxError as se: return "SYNTAX ERROR: %s" % se
                except Exception as e: return "Error: %s" % e
            elif sa == "restart":
                return "Cannot restart stdio. Restart Claude Desktop (Settings > Developer)."
            return "self improve actions: read | backup | add_tool | restart"
        elif a == "insight":
            try:
                _init_memory()
                conn = sqlite3.connect(MEMORY_DB)
                mc = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
                skills = conn.execute("SELECT COUNT(*) FROM memory WHERE key LIKE 'skill:%' OR key LIKE 'insight:%' OR key LIKE 'pattern:%'").fetchone()[0]
                conn.close()
                lines = ["Knowledge: %d entries (%d skills/insights/patterns)" % (mc, skills)]
            except: lines = ["Knowledge: ?"]
            try:
                if os.path.exists(EXPERIENCE_LOG):
                    with open(EXPERIENCE_LOG, encoding="utf-8", errors="replace") as f: entries = [json.loads(l) for l in f if l.strip()]
                    s = sum(1 for e in entries if e.get("success"))
                    lines.append("Experience: %d entries (%d/%d S/F)" % (len(entries), s, len(entries)-s))
            except: pass
            return "\n".join(lines)
        elif a == "heal":
            lines = ["=== HEALTH ==="]
            lines.append("Tools: %d" % len(TOOLS))
            try:
                _init_memory()
                conn = sqlite3.connect(MEMORY_DB)
                mc = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
                conn.close()
                lines.append("Memory: %d entries | FTS5: active" % mc)
            except Exception as e: lines.append("Memory: ERROR %s" % e)
            try:
                if os.path.exists(EXPERIENCE_LOG):
                    lines.append("Experience: %d bytes" % os.path.getsize(EXPERIENCE_LOG))
            except: pass
            try:
                sp = os.path.join(MCP_DIR, "pc_tools.py")
                with open(sp, encoding="utf-8", errors="replace") as f: compile(f.read(), sp, "exec")
                lines.append("pc_tools.py: OK")
            except SyntaxError as se: lines.append("pc_tools.py: FAIL line %d" % se.lineno)
            return "\n".join(lines)
        return "self actions: log | review | improve | insight | heal"

    elif name == "task":
        a = args.get("action", "")
        if a == "run":
            steps = args.get("steps", [])
            if not steps: return "No steps"
            lines = []
            for i, s in enumerate(steps, 1):
                t = s.get("tool", s.get("name", ""))
                p = s.get("params", s.get("arguments", {}))
                try:
                    res = handle(t, p)
                    lines.append("Step %d [%s]: %s" % (i, t, str(res)[:200]))
                except Exception as ex:
                    lines.append("Step %d [%s]: ERROR %s" % (i, t, ex))
            return "\n".join(lines)
        elif a == "note":
            na = args.get("note_action", "")
            tn = args.get("task_name", "")
            d = args.get("detail", "")
            _init_memory()
            conn = sqlite3.connect(MEMORY_DB)
            cur = conn.execute("SELECT value FROM memory WHERE key='tasks'")
            row = cur.fetchone()
            tasks = json.loads(row[0]) if row else []
            if na == "add":
                tasks.append({"task": tn, "detail": d, "ts": datetime.datetime.now().isoformat()})
                conn.execute("INSERT OR REPLACE INTO memory VALUES ('tasks',?,?)", (json.dumps(tasks), datetime.datetime.now().isoformat()))
                conn.commit(); conn.close()
                return "Task added: %s" % tn
            elif na == "list":
                conn.close()
                return "\n".join("  %d. %s" % (i+1, x["task"]) for i, x in enumerate(tasks)) if tasks else "(no tasks)"
            elif na == "done":
                tasks = [x for x in tasks if x.get("task") != tn]
                conn.execute("INSERT OR REPLACE INTO memory VALUES ('tasks',?,?)", (json.dumps(tasks), datetime.datetime.now().isoformat()))
                conn.commit(); conn.close()
                return "Task done: %s" % tn
            conn.close()
            return "note actions: add | list | done"
        return "task actions: run | note"

    elif name == "vps":
        a = args.get("action", "run")
        if a == "hosts":
            return "\n".join("%-8s = %s" % kv for kv in VPS_HOSTS.items())
        if a == "run":
            cmd = args.get("command", "")
            if not cmd: return "command required"
            return _ssh(args.get("host", next(iter(VPS_HOSTS), "")), cmd, args.get("timeout", 30))[:12000]
        return "vps actions: run | hosts"

    elif name == "botlog":
        path = args.get("path", "")
        host = args.get("host", "")
        if not path: return "path required (e.g. ~/logs/bot.log or C:\\logs\\bot.log)"
        try:
            if host:
                target = VPS_HOSTS.get(host, host)
                r = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", target, "cat %s" % path],
                    capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
                if r.returncode != 0:
                    return "SSH/cat gagal: %s" % (r.stderr.strip()[:300] or "exit %d" % r.returncode)
                text = r.stdout
            else:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
        except Exception as e:
            return "Error: %s" % e
        return _botlog_report(text)

    elif name == "tailscale":
        try:
            r = subprocess.run(["tailscale", "status"], capture_output=True, text=True, timeout=10)
            out = r.stdout.strip()
            if out:
                lines = [l for l in out.split("\n") if l.strip() and not l.startswith("#")]
                return "Tailscale:\n" + "\n".join(lines) if lines else out
            return out or "Tailscale not available"
        except Exception as e: return "Error: %s" % e

    return "Unknown tool: %s" % name

def main():
    while True:
        raw = sys.stdin.readline()
        if not raw: break
        raw = raw.strip()
        if not raw: continue
        try: req = json.loads(raw)
        except: continue
        m = req.get("method", "")
        rid = req.get("id")
        if m == "initialize":
            ctx = _build_context()
            _write({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "workbench", "version": "10.1"},
                "instructions": ctx
            }})
        elif m == "notifications/initialized": pass
        elif m == "tools/list":
            _write({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif m == "tools/call":
            p = req.get("params", {})
            r = handle(p.get("name", ""), p.get("arguments", {}))
            _write({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": str(r)}]}})
        elif rid is not None:
            _write({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Not found"}})

if __name__ == "__main__":
    _init_memory()
    main()
