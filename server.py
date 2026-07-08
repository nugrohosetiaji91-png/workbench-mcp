"""
workbench-mcp — zero-dependency MCP server for Claude Desktop (stdio).
FTS5 memory, big data analysis, hypothesis-driven reasoning.
"""

import json, sys, os, subprocess, urllib.request, urllib.error, urllib.parse, datetime, sqlite3, re, ssl, math, time, base64, struct, socket
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

MAX_SUBPROCESS_TIMEOUT = 25  # hard ceiling — MCP clients typically time out long before minutes

def _ps(cmd, timeout=30):
    timeout = min(timeout, MAX_SUBPROCESS_TIMEOUT)
    proc = None
    try:
        proc = subprocess.Popen(
            ["powershell", "-Command", cmd],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=MCP_DIR
        )
        out, err = proc.communicate(timeout=timeout)
        parts = [s for s in [out.strip(), err.strip()] if s]
        parts.append("[EXIT %d]" % proc.returncode)
        return "\n".join(parts)
    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
            proc.wait(timeout=5)
        return "Timed out after %ds — process killed" % timeout
    except Exception as e:
        if proc and proc.poll() is None:
            proc.kill()
        return "Error: %s" % e

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

## MEMORY & SKILL SYSTEM (5-stage progression: fail -> investigate -> verify -> distill -> consult)
- memory(action="store", key="failure:TOPIC", value="what broke + repro") — stage 1, log it before you move on
- memory(action="store", key="verified:TOPIC", value="checked fact, not a guess") — stage 3, only after you actually confirmed it
- memory(action="store", key="rule:TOPIC", value="general rule beyond this one case") — stage 4, distilled from >=1 verified fact
- memory(action="store", key="skill:TOPIC" / "insight:TOPIC" / "pattern:TOPIC", value="...") — reusable procedural knowledge
- memory(action="search", query="keyword") — FTS5 full-text search across all memory — ALWAYS do this before re-deriving something you might already know (stage 5, consult)
- memory(action="read") — browse all stored knowledge
- self(action="log") — auto-record after every significant action (this is stage 1/2 raw material — self(action="review") and memory "rule:"/"verified:" entries are what turn it into stage 3/4)
- self(action="review") — periodic pattern analysis of your own performance
Read at session start (already automatic via _build_context), write before you stop: if a session ends without a memory(store) or self(log) call, the next session restarts from zero.

## VERIFIER DISCIPLINE — self-critique is measurably weaker than independent verification
A model grading its own output sees its own reasoning trail and prefers conclusions consistent with what it already wrote. This tool has no separate verifier process to call — YOU are both maker and the only checker available — so compensate deliberately:
- For anything non-trivial (a fix you're about to call done, code you're about to ship, a claim you're about to report as fact): before declaring success, re-read the artifact ALONE, as if you had never written it and only had the stated goal + the artifact. Does it actually satisfy the goal, or does it just look plausible because you already believe it?
- For genuinely high-stakes changes, prefer starting a **fresh Claude Desktop conversation** with zero prior context to review this session's diff/output against the stated goal — a clean context is the closest thing to an independent verifier this setup has.
- Do not mark self(action="log") success=true on the strength of your own explanation alone. Success = you observed the actual result (ran the code, read the file back, saw the screenshot), not "this should work."

## VISION SELF-CHECK (for anything visual: UI, screenshots, rendered output)
system(action="screenshot") and browser/stealth_browser(action="screenshot") capture pixels — they do nothing on their own. The check is YOU looking at the returned image and comparing it against the stated goal before declaring done:
1. Take the screenshot.
2. Look at it. Does it actually match the goal, or a design-token/Skill reference if one exists for this project?
3. Mismatch → describe the specific gap, fix, re-screenshot. Match → then and only then self(action="log") success=true.
Never declare a visual task done from code review alone — the render is the source of truth.

## ANTI-PATTERNS (instant fail)
- "Let me check..." → NO. Execute immediately
- "I would suggest..." → NO. Implement and verify
- "This might be..." → NO. Read the file, run the code, get evidence
- "In my next step..." → NO. Everything in THIS turn
- Guessing → NO. Use tools to verify. If you don't know, say so and investigate
- Fixing symptoms → NO. Find the root cause
- Accepting first solution → NO. Consider 2 alternatives minimum
- Declaring success on your own say-so → NO. See VERIFIER DISCIPLINE — observe the actual result

## EXCELLENCE STANDARD
Every interaction: think() first → analyze the system → form hypothesis → execute → verify against the actual observed result (not your own explanation) → document (self log, with a real stage: failure/verified/rule) → extract pattern (memory store).
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
    timeout = min(timeout, MAX_SUBPROCESS_TIMEOUT)
    target = VPS_HOSTS.get(host, host)
    if "@" not in target:
        return "Error: host '%s' is not a known alias (%s) or user@ip format" % (host, ", ".join(VPS_HOSTS))
    proc = None
    try:
        proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", target, command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace")
        out, err = proc.communicate(timeout=timeout)
        parts = [s for s in [out.strip(), err.strip()] if s]
        parts.append("[EXIT %d]" % proc.returncode)
        return "\n".join(parts)
    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
            proc.wait(timeout=5)
        return "Timed out after %ds — process killed" % timeout
    except Exception as e:
        if proc and proc.poll() is None:
            proc.kill()
        return "Error: %s" % e

def _botlog_report(text):
    """Trading-bot log analytics — deterministic structured parsing (W/L, PnL, exit reasons)."""
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

# ═══════════════════════════════════════════
# CDP BROWSER — Chrome DevTools Protocol
# ═══════════════════════════════════════════

CDP_PORT = 9222
_CDP_PROC = None
_CDP_TAB_WS = {}

def _cdp_find_chrome():
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    ]
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidates.extend([
            os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(local, r"Microsoft\Edge\Application\msedge.exe"),
        ])
    for c in candidates:
        if os.path.exists(c): return c
    try:
        r = subprocess.run(["where", "chrome"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip(): return r.stdout.strip().split("\n")[0]
    except: pass
    return None

def _cdp_launch(headless=True, stealth=False):
    global _CDP_PROC
    chrome = _cdp_find_chrome()
    if not chrome: raise Exception("Chrome/Edge/Brave not found")
    try:
        urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json/version", timeout=3)
        return CDP_PORT
    except: pass
    if _CDP_PROC and _CDP_PROC.poll() is None:
        _CDP_PROC.kill(); _CDP_PROC.wait(timeout=5)
    user_data = os.path.join(MCP_DIR, ".cdp_profile")
    os.makedirs(user_data, exist_ok=True)
    args = [chrome, f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={user_data}",
            "--no-first-run", "--no-default-browser-check",
            "--disable-background-networking", "--disable-sync",
            "--disable-extensions", "--disable-default-apps",
            "--disable-popup-blocking", "--disable-prompt-on-repost",
            "--disable-notifications"]
    if headless: args.append("--headless=new")
    if stealth:
        args.extend(["--disable-blink-features=AutomationControlled",
                     "--disable-features=IsolateOrigins,site-per-process"])
    _CDP_PROC = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json/version", timeout=2)
            return CDP_PORT
        except: pass
    raise Exception("Chrome CDP failed to start after 10s")

def _cdp_ws_send(ws_url, method, params=None, timeout=15):
    from urllib.parse import urlparse
    u = urlparse(ws_url); host, port = u.hostname, u.port or CDP_PORT
    path = u.path + ("?" + u.query if u.query else "")
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        if u.scheme == "wss":
            ctx = ssl._create_unverified_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        crlf = chr(13) + chr(10)
        req = "GET " + path + " HTTP/1.1" + crlf + "Host: " + host + ":" + str(port) + crlf + "Upgrade: websocket" + crlf + "Connection: Upgrade" + crlf + "Sec-WebSocket-Key: " + key + crlf + "Sec-WebSocket-Version: 13" + crlf + crlf
        sock.send(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk: raise Exception("No handshake")
            resp += chunk
        if b"101" not in resp: raise Exception("WS handshake failed")
        msg_id = int(time.time() * 1000) % 100000
        payload = json.dumps({"id": msg_id, "method": method, "params": params or {}}).encode()
        mask = os.urandom(4)
        frame = bytearray([0x81]); plen = len(payload)
        if plen < 126: frame.append(0x80 | plen)
        elif plen < 65536: frame.append(0x80 | 126); frame.extend(struct.pack("!H", plen))
        else: frame.append(0x80 | 127); frame.extend(struct.pack("!Q", plen))
        frame.extend(mask); frame.extend(bytes(b ^ mask[i%4] for i,b in enumerate(payload)))
        sock.send(bytes(frame))
        data = b""; deadline = time.time() + timeout
        while time.time() < deadline:
            sock.settimeout(max(deadline - time.time(), 0.5))
            try:
                chunk = sock.recv(65536)
                if not chunk: break
                data += chunk
                if len(data) >= 2:
                    plen3 = data[1] & 0x7F; hdr_check = 2
                    if plen3 == 126: hdr_check = 4
                    elif plen3 == 127: hdr_check = 10
                    if len(data) >= hdr_check + plen3:
                        if plen3 == 126:
                            plen3 = struct.unpack("!H", data[2:4])[0]
                        elif plen3 == 127:
                            plen3 = struct.unpack("!Q", data[2:10])[0]
                        if len(data) >= hdr_check + plen3: break
            except socket.timeout: break
        results = []; offset = 0
        while offset < len(data) - 1:
            if offset+1 >= len(data): break
            opcode = data[offset] & 0x0F
            if opcode == 0x08: break
            plen2 = data[offset+1] & 0x7F; hdr = 2
            if plen2 == 126:
                if offset+4 > len(data): break
                plen2 = struct.unpack("!H", data[offset+2:offset+4])[0]; hdr = 4
            elif plen2 == 127:
                if offset+10 > len(data): break
                plen2 = struct.unpack("!Q", data[offset+2:offset+10])[0]; hdr = 10
            if offset+hdr+plen2 > len(data): break
            raw = data[offset+hdr:offset+hdr+plen2]; offset += hdr+plen2
            try: results.append(json.loads(raw.decode(errors="replace")))
            except: pass
        for r in results:
            if r.get("id") == msg_id:
                if "error" in r: return None, r["error"].get("message", str(r["error"]))
                return r.get("result", {}), None
        return None, "No response (got %d frames)" % len(results)
    finally:
        try: sock.close()
        except: pass

def _cdp_call(method, params=None, tab_idx=0, timeout=15):
    try: _cdp_launch(headless=True)
    except Exception as e: return None, f"Launch failed: {e}"
    try:
        r = urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json", timeout=5)
        tabs = json.loads(r.read())
    except Exception as e: return None, f"CDP not responding: {e}"
    if not tabs: return None, "No tabs open"
    if tab_idx >= len(tabs): tab_idx = 0
    tab = tabs[tab_idx]
    ws_url = tab.get("webSocketDebuggerUrl", "")
    if not ws_url: return None, "No debug URL"
    _CDP_TAB_WS[tab["id"]] = ws_url
    return _cdp_ws_send(ws_url, method, params, timeout)

def _cdp_eval(js, tab_idx=0, timeout=10):
    result, err = _cdp_call("Runtime.evaluate", {"expression": js, "returnByValue": True, "awaitPromise": True}, tab_idx, timeout)
    if err: return None, err
    r = result.get("result", {})
    if r.get("subtype") == "error": return None, r.get("description", "JS error")
    return r.get("value"), None

def _cdp_new_tab_navigate(url, inject_script=None, timeout=20):
    """Create a new tab and navigate via proper CDP Page.navigate (not a JS
    location.href hack). If inject_script is given, it's registered via
    Page.addScriptToEvaluateOnNewDocument so it re-applies automatically on
    every future navigation in that tab, instead of being eval'd once on the
    current document — the latter is what caused a real bug here: running
    the same fingerprint-spoofing script twice via Runtime.evaluate throws
    "Cannot redefine property: webdriver" on the second call, because
    Object.defineProperty without configurable:true only succeeds once per
    document. Page.addScriptToEvaluateOnNewDocument avoids that entirely —
    it fires exactly once per new document, before any page script runs."""
    req = urllib.request.Request(f"http://localhost:{CDP_PORT}/json/new", method="PUT")
    r = urllib.request.urlopen(req, timeout=15)
    data = json.loads(r.read())
    tabs = json.loads(urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json", timeout=5).read())
    new_idx = next((i for i, t in enumerate(tabs) if t.get("id") == data.get("id")), len(tabs) - 1)
    _cdp_call("Page.enable", {}, new_idx, timeout)
    if inject_script:
        _, err = _cdp_call("Page.addScriptToEvaluateOnNewDocument", {"source": inject_script}, new_idx, timeout)
        if err: return new_idx, data, f"Script injection failed: {err}"
    _, err = _cdp_call("Page.navigate", {"url": url}, new_idx, timeout)
    return new_idx, data, err

def _cdp_screenshot(tab_idx=0, timeout=15):
    # Use headless Chrome CLI for screenshots — more reliable than CDP Page.captureScreenshot
    try:
        tabs = json.loads(urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json", timeout=5).read())
        if tab_idx < len(tabs):
            url = tabs[tab_idx].get("url", "about:blank")
        else:
            url = "about:blank"
    except:
        url = "about:blank"
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(MCP_DIR, f"cdp_screen_{ts}.png")
    chrome = _cdp_find_chrome()
    if not chrome: return None, "Chrome not found"
    proc = None
    try:
        proc = subprocess.Popen(
            [chrome, f"--headless=new", "--disable-gpu", f"--screenshot={path}",
             "--window-size=1920,1080", "--hide-scrollbars", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, creationflags=0x08000000 if sys.platform == "win32" else 0
        )
        _, err_out = proc.communicate(timeout=min(timeout, 25))
        if proc.returncode != 0:
            return None, f"Screenshot failed: {err_out[:300]}"
        if os.path.exists(path):
            return path, None
        return None, f"Screenshot not written: {err_out[:200]}"
    except subprocess.TimeoutExpired:
        if proc: proc.kill()
        return None, "Screenshot timed out"
    except Exception as e:
        return None, str(e)

def _cdp_close():
    global _CDP_PROC, _CDP_TAB_WS
    _CDP_TAB_WS = {}
    if _CDP_PROC and _CDP_PROC.poll() is None:
        _CDP_PROC.terminate()
        try: _CDP_PROC.wait(timeout=5)
        except: _CDP_PROC.kill()
        _CDP_PROC = None
        return "Browser closed"
    return "No browser running"

# ═══════════════════════════════════════════
# STEALTH BROWSER — CDP + anti-fingerprinting
#
# The fingerprint-spoofing script below (webdriver/plugins/languages
# override, cdc_ variable removal) is not a novel technique — it's the
# same community-standard pattern popularized by projects like
# puppeteer-extra-plugin-stealth and undetected-chromedriver, adapted here
# for raw CDP without a browser-automation library dependency. Credit to
# that open-source lineage for the technique; this file just re-implements
# it against the stdlib-only constraint of the rest of this server.
#
# Built out (including the double-injection bug fix above) with Claude
# (Anthropic) — see README Acknowledgments.
# ═══════════════════════════════════════════

STEALTH_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
]

_STEALTH_FP_JS = """
(function(){
    Object.defineProperty(navigator,'webdriver',{get:()=>false});
    Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
    Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
    window.chrome={runtime:{}};
    const oq=window.navigator.permissions.query;
    window.navigator.permissions.query=(p)=>p.name==='notifications'?Promise.resolve({state:Notification.permission}):oq(p);
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
})()
"""

def _stealth_launch():
    global _CDP_PROC
    _cdp_close()
    chrome = _cdp_find_chrome()
    if not chrome: raise Exception("Chrome not found")
    ua = STEALTH_UAS[int(time.time()) % len(STEALTH_UAS)]
    user_data = os.path.join(MCP_DIR, ".cdp_profile")
    args = [chrome, f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={user_data}",
            f"--user-agent={ua}", "--no-first-run", "--no-default-browser-check",
            "--disable-background-networking", "--disable-sync",
            "--disable-extensions", "--disable-default-apps",
            "--disable-popup-blocking", "--disable-prompt-on-repost",
            "--disable-notifications", "--disable-infobars",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-client-side-phishing-detection",
            "--disable-component-update", "--disable-domain-reliability",
            "--no-pings", "--window-size=1920,1080"]
    _CDP_PROC = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json/version", timeout=2)
            return CDP_PORT
        except: pass
    raise Exception("Stealth Chrome failed to start")

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
     "inputSchema": {"type": "object", "properties": {"action": {"type": "string"}, "path": {"type": "string", "default": "."}, "n": {"type": "integer", "default": 10}}, "required": ["action"]}},
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
     "description": "Multi-step / Dynamic-Workflow-style executor. action: run (sequential, independent steps) | parallel (fan-out, steps run concurrently in threads, synthesize the results yourself after) | pipeline (sequential, each stage's result is substituted for the literal string '{prev}' in the next stage's params) | loop_until (repeat one step until `check` — a python expression with `result` bound to the step's return value — is true, or max_iterations hits) | note (task list: add|list|done). steps/step items are {tool, params}. NOTE: this has no independent LLM judgment per branch (no separate API key configured) — parallel/pipeline/loop_until give you real concurrency and data-flow for tool calls, but YOU are still the one judging/synthesizing results, not N separate agents. For a true independent check, open a fresh Claude Desktop conversation (see VERIFIER DISCIPLINE).",
     "inputSchema": {"type": "object", "properties": {"action": {"type": "string", "description": "run | parallel | pipeline | loop_until | note"}, "steps": {"type": "array", "items": {"type": "object"}}, "step": {"type": "object", "description": "single {tool, params} for loop_until"}, "check": {"type": "string", "description": "python expression for loop_until, e.g. \"'DONE' in result\""}, "max_iterations": {"type": "integer", "default": 10}, "note_action": {"type": "string"}, "task_name": {"type": "string"}, "detail": {"type": "string"}}, "required": ["action"]}},
    {"name": "tailscale", "annotations": {"readOnlyHint": True, "destructiveHint": False},
     "description": "Network status.", "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "vps", "annotations": {"readOnlyHint": False, "destructiveHint": True},
     "description": "Run commands on remote servers over SSH. action: run|hosts. host: configured alias or user@ip.",
     "inputSchema": {"type": "object", "properties": {"action": {"type": "string", "description": "run | hosts", "default": "run"}, "host": {"type": "string", "description": "configured alias or user@ip"}, "command": {"type": "string"}, "timeout": {"type": "integer", "default": 30}}, "required": []}},
    {"name": "botlog", "annotations": {"readOnlyHint": True, "destructiveHint": False},
     "description": "Analyze trading-bot logs: W/L, winrate, profit factor, PnL per exit-reason & per hour, top wins/losses, hold duration. Local path, or set host to read logs on a remote server via SSH.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string", "description": "log file path"}, "host": {"type": "string", "description": "empty=local | configured alias | user@ip"}}, "required": ["path"]}},
    {"name": "browser", "annotations": {"readOnlyHint": False, "destructiveHint": True},
     "description": "CDP browser (Chrome DevTools Protocol). action: launch(headless?) | navigate(url,new_tab?) | click(selector) | scroll(direction,amount) | type(selector,text) | snapshot | screenshot | execute(js) | close | tabs. Auto-launches Chrome if needed.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "description": "launch | navigate | click | scroll | type | snapshot | screenshot | execute | close | tabs"},
         "url": {"type": "string"}, "selector": {"type": "string"}, "text": {"type": "string"},
         "js": {"type": "string"}, "direction": {"type": "string", "default": "down"},
         "amount": {"type": "integer", "default": 500}, "headless": {"type": "boolean", "default": True},
         "new_tab": {"type": "boolean", "default": True}, "tab_idx": {"type": "integer", "default": 0},
         "timeout": {"type": "integer", "default": 15}
     }, "required": ["action"]}},
    {"name": "stealth_browser", "annotations": {"readOnlyHint": False, "destructiveHint": True},
     "description": "Fallback CDP browser with anti-fingerprinting (rotating user-agent, webdriver-flag removal, fingerprint masking). Use when the plain 'browser' tool gets detected/blocked by a site's bot defenses. Intended for personal-scale research/automation on your own sessions — respect target sites' terms of service. action: launch | navigate | click | scroll | type | snapshot | screenshot | execute | close | tabs.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "description": "launch | navigate | click | scroll | type | snapshot | screenshot | execute | close | tabs"},
         "url": {"type": "string"}, "selector": {"type": "string"}, "text": {"type": "string"},
         "js": {"type": "string"}, "direction": {"type": "string", "default": "down"},
         "amount": {"type": "integer", "default": 500}, "new_tab": {"type": "boolean", "default": True},
         "tab_idx": {"type": "integer", "default": 0}, "timeout": {"type": "integer", "default": 15}
     }, "required": ["action"]}},
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
        return _ps(args.get("command", ""), min(args.get("timeout", 30), MAX_SUBPROCESS_TIMEOUT))

    elif name == "run_python":
        sc = os.path.join(MCP_DIR, ".mcp_tmp.py")
        timeout = min(args.get("timeout", 30), MAX_SUBPROCESS_TIMEOUT)
        proc = None
        try:
            with open(sc, "w", encoding="utf-8") as f: f.write(args["code"])
            proc = subprocess.Popen(
                ["python", sc],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, cwd=MCP_DIR
            )
            out, err = proc.communicate(timeout=timeout)
            parts = [s for s in [out.strip(), "[STDERR] " + err.strip() if err.strip() else ""] if s]
            parts.append("[EXIT %d]" % proc.returncode)
            return "\n".join(parts)
        except subprocess.TimeoutExpired:
            if proc:
                proc.kill()
                proc.wait(timeout=5)
            return "Timed out after %ds — process killed" % timeout
        except Exception as e:
            if proc and proc.poll() is None:
                proc.kill()
            return "Error: %s" % e
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
            sp = os.path.abspath(__file__)
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
                sp = os.path.abspath(__file__)
                with open(sp, encoding="utf-8", errors="replace") as f: compile(f.read(), sp, "exec")
                lines.append("%s: OK" % os.path.basename(sp))
            except SyntaxError as se: lines.append("%s: FAIL line %d" % (os.path.basename(sp), se.lineno))
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
        elif a == "parallel":
            steps = args.get("steps", [])
            if not steps: return "No steps"
            import concurrent.futures

            def _run_branch(s):
                t = s.get("tool", s.get("name", ""))
                p = s.get("params", s.get("arguments", {}))
                try:
                    return t, handle(t, p), None
                except Exception as ex:
                    return t, None, str(ex)

            results = [None] * len(steps)
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(steps), 8)) as ex:
                futs = {ex.submit(_run_branch, s): i for i, s in enumerate(steps)}
                for fut in concurrent.futures.as_completed(futs):
                    results[futs[fut]] = fut.result()
            lines = ["[PARALLEL] %d branches, %d workers" % (len(steps), min(len(steps), 8))]
            for i, (t, res, err) in enumerate(results, 1):
                if err: lines.append("Branch %d [%s]: ERROR %s" % (i, t, err))
                else: lines.append("Branch %d [%s]: %s" % (i, t, str(res)[:400]))
            lines.append("\n(No independent LLM judgment happened above — that's N tool calls run concurrently. Synthesize/compare the branches yourself.)")
            return "\n".join(lines)
        elif a == "pipeline":
            steps = args.get("steps", [])
            if not steps: return "No steps"
            prev = None
            lines = []
            for i, s in enumerate(steps, 1):
                t = s.get("tool", s.get("name", ""))
                p = dict(s.get("params", s.get("arguments", {})))
                if prev is not None:
                    p = {k: (v.replace("{prev}", str(prev)) if isinstance(v, str) else v) for k, v in p.items()}
                try:
                    res = handle(t, p)
                    prev = res
                    lines.append("Stage %d [%s]: %s" % (i, t, str(res)[:400]))
                except Exception as ex:
                    lines.append("Stage %d [%s]: ERROR %s — pipeline stopped" % (i, t, ex))
                    break
            return "\n".join(lines)
        elif a == "loop_until":
            step = args.get("step", {})
            t = step.get("tool", step.get("name", ""))
            p = step.get("params", step.get("arguments", {}))
            cond = args.get("check", "")
            max_iter = args.get("max_iterations", 10)
            if not t: return "step.tool required"
            if not cond: return "check (python expression, e.g. \"'DONE' in result\") required"
            lines = []
            for i in range(1, max_iter + 1):
                try:
                    result = handle(t, p)
                except Exception as ex:
                    result = "ERROR: %s" % ex
                lines.append("Iteration %d [%s]: %s" % (i, t, str(result)[:200]))
                try:
                    done = bool(eval(cond, {"__builtins__": {}}, {"result": result}))
                except Exception as ex:
                    lines.append("Check expression error: %s (stopping)" % ex)
                    break
                if done:
                    lines.append("Stop condition met after %d iteration(s)." % i)
                    break
            else:
                lines.append("Max iterations (%d) reached without meeting the stop condition." % max_iter)
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
        return "task actions: run | parallel | pipeline | loop_until | note"

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

    elif name == "browser":
        a = args.get("action", "")
        if a == "launch":
            hl = args.get("headless", True)
            try:
                _cdp_launch(headless=hl)
                return f"Browser launched (headless={hl}, port={CDP_PORT})"
            except Exception as e: return f"Launch failed: {e}"
        elif a == "navigate":
            url = args.get("url", "")
            if not url: return "url required"
            if args.get("new_tab", True):
                try:
                    _cdp_launch(headless=True)
                    new_idx, data, err = _cdp_new_tab_navigate(url, timeout=args.get("timeout", 20))
                    if err: return f"Tab created but nav failed: {err}"
                    return f"New tab [{new_idx}]: {data.get('title', '...')}\n{url}"
                except Exception as e: return f"Navigate failed: {e}"
            else:
                ti = args.get("tab_idx", 0)
                _, err = _cdp_call("Page.navigate", {"url": url}, ti, args.get("timeout", 15))
                if err: return f"Navigate failed: {err}"
                return "Navigating..."
        elif a == "click":
            sel = args.get("selector", "")
            if not sel: return "selector required"
            js = "(function(){var el=document.querySelector(" + json.dumps(sel) + ");if(!el)return'NOT FOUND';el.scrollIntoView({block:'center'});el.click();return'CLICKED: '+" + json.dumps(sel) + ";})()"
            val, err = _cdp_eval(js, args.get("tab_idx", 0), args.get("timeout", 10))
            return str(val) if not err else f"Click failed: {err}"
        elif a == "scroll":
            d = args.get("direction", "down")
            amt = args.get("amount", 500)
            sign = "-" if d == "up" else ""
            val, err = _cdp_eval(f"window.scrollBy({{left:0,top:{sign}{amt},behavior:'smooth'}});'SCROLLED {d} {amt}px'", args.get("tab_idx", 0), args.get("timeout", 10))
            return str(val) if not err else f"Scroll failed: {err}"
        elif a == "type":
            sel = args.get("selector", "")
            txt = args.get("text", "")
            if not sel: return "selector required"
            js = "(function(){var el=document.querySelector(" + json.dumps(sel) + ");if(!el)return'NOT FOUND';el.focus();el.value=" + json.dumps(txt) + ";el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return'TYPED';})()"
            val, err = _cdp_eval(js, args.get("tab_idx", 0), args.get("timeout", 10))
            return str(val) if not err else f"Type failed: {err}"
        elif a == "snapshot":
            js = "(function(){var t=document.title,u=location.href,b=document.body?document.body.innerText.substring(0,6000):'';var els=document.querySelectorAll('input,textarea,select,button,a[href]');var ia=[];for(var i=0;i<Math.min(els.length,80);i++){var e=els[i];var tag=e.tagName.toLowerCase();var txt=(e.value||e.textContent||e.placeholder||e.name||e.id||'').trim().substring(0,60);var h=e.href||'';ia.push(tag+(txt?': '+txt:'')+(h?' -> '+h:''));}return JSON.stringify({title:t,url:u,body:b,inputs:ia});})()"
            val, err = _cdp_eval(js, args.get("tab_idx", 0), args.get("timeout", 10))
            if err: return f"Snapshot failed: {err}"
            try:
                data = json.loads(str(val))
                parts = [f"TITLE: {data.get('title','')}", f"URL: {data.get('url','')}", "", "INTERACTIVE:", "\n".join(f"  {x}" for x in data.get("inputs", [])), "", data.get("body", "")]
                return "\n".join(parts)[:8000]
            except: return str(val)[:8000]
        elif a == "screenshot":
            path, err = _cdp_screenshot(args.get("tab_idx", 0), args.get("timeout", 15))
            if err: return f"Screenshot failed: {err}"
            return f"Screenshot: {path}"
        elif a == "execute":
            js = args.get("js", "")
            if not js: return "js required"
            val, err = _cdp_eval(js, args.get("tab_idx", 0), args.get("timeout", 10))
            return str(val) if not err else f"JS error: {err}"
        elif a == "close":
            return _cdp_close()
        elif a == "tabs":
            try:
                _cdp_launch(headless=True)
                r = urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json", timeout=5)
                tabs = json.loads(r.read())
                lines = [f"Tabs ({len(tabs)}):"]
                for i, t in enumerate(tabs):
                    lines.append(f"  [{i}] {t.get('title','?')[:80]}\n     {t.get('url','')[:120]}")
                return "\n".join(lines)
            except Exception as e: return f"Tabs error: {e}"
        return "browser actions: launch | navigate | click | scroll | type | snapshot | screenshot | execute | close | tabs"

    elif name == "stealth_browser":
        a = args.get("action", "")
        if a == "launch":
            try:
                _stealth_launch()
                return f"Stealth browser launched (port={CDP_PORT}, rotating UA)"
            except Exception as e: return f"Stealth launch failed: {e}"
        elif a == "navigate":
            url = args.get("url", "")
            if not url: return "url required"
            if args.get("new_tab", True):
                try:
                    _stealth_launch()
                    new_idx, data, err = _cdp_new_tab_navigate(url, inject_script=_STEALTH_FP_JS, timeout=args.get("timeout", 20))
                    if err: return f"Tab created but nav failed: {err}"
                    return f"[STEALTH] New tab [{new_idx}]: {data.get('title', '...')}\n{url}"
                except Exception as e: return f"Stealth navigate failed: {e}"
            else:
                ti = args.get("tab_idx", 0)
                _cdp_call("Page.addScriptToEvaluateOnNewDocument", {"source": _STEALTH_FP_JS}, ti, args.get("timeout", 15))
                _, err = _cdp_call("Page.navigate", {"url": url}, ti, args.get("timeout", 15))
                if err: return f"Stealth navigate failed: {err}"
                return "Navigating (stealth)..."
        elif a == "click":
            sel = args.get("selector", "")
            if not sel: return "selector required"
            js = "(function(){var el=document.querySelector(" + json.dumps(sel) + ");if(!el)return'NOT FOUND';el.scrollIntoView({block:'center'});el.click();return'CLICKED: '+" + json.dumps(sel) + ";})()"
            val, err = _cdp_eval(js, args.get("tab_idx", 0), args.get("timeout", 10))
            return str(val) if not err else f"Click failed: {err}"
        elif a == "scroll":
            d = args.get("direction", "down")
            amt = args.get("amount", 500)
            sign = "-" if d == "up" else ""
            val, err = _cdp_eval(f"window.scrollBy({{left:0,top:{sign}{amt},behavior:'smooth'}});'SCROLLED {d} {amt}px'", args.get("tab_idx", 0), args.get("timeout", 10))
            return str(val) if not err else f"Scroll failed: {err}"
        elif a == "type":
            sel = args.get("selector", "")
            txt = args.get("text", "")
            if not sel: return "selector required"
            js = "(function(){var el=document.querySelector(" + json.dumps(sel) + ");if(!el)return'NOT FOUND';el.focus();el.value=" + json.dumps(txt) + ";el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return'TYPED';})()"
            val, err = _cdp_eval(js, args.get("tab_idx", 0), args.get("timeout", 10))
            return str(val) if not err else f"Type failed: {err}"
        elif a == "snapshot":
            js = "(function(){var t=document.title,u=location.href,b=document.body?document.body.innerText.substring(0,6000):'';var els=document.querySelectorAll('input,textarea,select,button,a[href]');var ia=[];for(var i=0;i<Math.min(els.length,80);i++){var e=els[i];var tag=e.tagName.toLowerCase();var txt=(e.value||e.textContent||e.placeholder||e.name||e.id||'').trim().substring(0,60);var h=e.href||'';ia.push(tag+(txt?': '+txt:'')+(h?' -> '+h:''));}return JSON.stringify({title:t,url:u,body:b,inputs:ia});})()"
            val, err = _cdp_eval(js, args.get("tab_idx", 0), args.get("timeout", 10))
            if err: return f"Snapshot failed: {err}"
            try:
                data = json.loads(str(val))
                parts = [f"[STEALTH] TITLE: {data.get('title','')}", f"URL: {data.get('url','')}", "", "INTERACTIVE:", "\n".join(f"  {x}" for x in data.get("inputs", [])), "", data.get("body", "")]
                return "\n".join(parts)[:8000]
            except: return str(val)[:8000]
        elif a == "screenshot":
            path, err = _cdp_screenshot(args.get("tab_idx", 0), args.get("timeout", 15))
            if err: return f"Screenshot failed: {err}"
            return f"[STEALTH] Screenshot: {path}"
        elif a == "execute":
            js = args.get("js", "")
            if not js: return "js required"
            val, err = _cdp_eval(js, args.get("tab_idx", 0), args.get("timeout", 10))
            return str(val) if not err else f"JS error: {err}"
        elif a == "close":
            return _cdp_close()
        elif a == "tabs":
            try:
                _cdp_launch(headless=True)
                r = urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json", timeout=5)
                tabs = json.loads(r.read())
                lines = [f"[STEALTH] Tabs ({len(tabs)}):"]
                for i, t in enumerate(tabs):
                    lines.append(f"  [{i}] {t.get('title','?')[:80]}\n     {t.get('url','')[:120]}")
                return "\n".join(lines)
            except Exception as e: return f"Tabs error: {e}"
        return "stealth_browser actions: launch | navigate | click | scroll | type | snapshot | screenshot | execute | close | tabs"

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
