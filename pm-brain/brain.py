"""
brain.py — Layer 2 PM Insight Engine (v2)
------------------------------------------
Reads Jira tickets (real or mock), sends to Claude,
gets back ranked product insights a PM can act on.

Then generates:
  1. A clean terminal report (with colors)
  2. A raw JSON output file
  3. A beautiful HTML dashboard

HOW TO RUN:
  1. pip install anthropic
  2. Set your API key: set ANTHROPIC_API_KEY=your_key_here
  3. python brain.py

"""

import json
import os
import sys
import time
from datetime import datetime

try:
    import anthropic
except ImportError:
    print("ERROR: 'anthropic' library not installed.")
    print("Run:  pip install anthropic")
    sys.exit(1)

import config


# ── COLORS FOR TERMINAL ─────────────────────────────────────────────

class C:
    """Terminal color codes."""
    if config.USE_COLORS:
        BOLD = "\033[1m"
        DIM = "\033[2m"
        RED = "\033[91m"
        GREEN = "\033[92m"
        YELLOW = "\033[93m"
        BLUE = "\033[94m"
        MAGENTA = "\033[95m"
        CYAN = "\033[96m"
        WHITE = "\033[97m"
        RESET = "\033[0m"
        BG_RED = "\033[41m"
        BG_GREEN = "\033[42m"
        BG_BLUE = "\033[44m"
    else:
        BOLD = DIM = RED = GREEN = YELLOW = BLUE = ""
        MAGENTA = CYAN = WHITE = RESET = ""
        BG_RED = BG_GREEN = BG_BLUE = ""


# ── 1. LOAD JIRA DATA ──────────────────────────────────────────────

def load_jira_data(filepath=None):
    """Load Jira issues from JSON file."""
    filepath = filepath or config.MOCK_DATA_PATH
    
    if not os.path.exists(filepath):
        print(f"{C.RED}ERROR: File not found: {filepath}{C.RESET}")
        sys.exit(1)
    
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    issue_count = data.get("total_issues", len(data.get("issues", [])))
    project = data.get("project", "Unknown")
    
    print(f"{C.CYAN}▸ Loaded {C.BOLD}{issue_count} issues{C.RESET}{C.CYAN} from project: {C.BOLD}{project}{C.RESET}")
    
    # Quick stats
    priorities = {}
    statuses = {}
    for issue in data.get("issues", []):
        p = issue.get("priority", "Unknown")
        s = issue.get("status", "Unknown")
        priorities[p] = priorities.get(p, 0) + 1
        statuses[s] = statuses.get(s, 0) + 1
    
    print(f"{C.DIM}  Priorities: {priorities}{C.RESET}")
    print(f"{C.DIM}  Statuses:   {statuses}{C.RESET}")
    print()
    
    return data


# ── 2. THE BRAIN — SYSTEM PROMPT ────────────────────────────────────
# This is the engineered prompt that makes Claude think like a senior PM.
# This is YOUR intellectual property — the secret sauce of Layer 2.

SYSTEM_PROMPT = """
You are a senior product strategist with 10 years of experience at 
high-growth B2B SaaS companies. You think deeply, reason from evidence, 
and give direct, confident recommendations.

You will be given a list of Jira issues from a software product team.
Your job is to analyze them and answer the question every PM dreads:
"What should we actually build next — and why?"

YOUR ANALYSIS MUST DO ALL OF THE FOLLOWING:

1. FIND HIDDEN THEMES
   - Group tickets by the REAL underlying problem, not surface labels
   - Look for tickets that seem different but share a root cause
   - Count how many tickets belong to each theme
   - Note how long the oldest ticket in each theme has sat in backlog

2. FIND WHAT THE TEAM IS AVOIDING
   - Identify high-priority tickets that keep getting deprioritized
   - Look for patterns in which problems never get solved
   - This reveals organizational blind spots or engineering fear

3. FIND REVENUE-BLOCKING ISSUES
   - Look for comments mentioning: deals blocked, enterprise, churn,
     customer complaints, sales, QBR, procurement
   - These are your highest-leverage items

4. MAKE A CLEAR RECOMMENDATION
   - Pick ONE thing to build next
   - Justify it with specific evidence from ticket comments
   - Give a rough scope (what a v1 looks like)
   - Explain the risk of NOT building it

5. FLAG QUICK WINS
   - Identify 1-2 bugs or small fixes that unblock the most users
   - These can be done in parallel with the main recommendation

6. RISK ASSESSMENT
   - Estimate urgency as days_until_critical (how many days before
     this becomes a crisis if ignored)
   - Flag any tickets that mention compliance, legal, or security

RETURN VALID JSON ONLY. No explanation before or after. No markdown.
Use exactly this structure:

{
  "summary": "2-3 sentence executive summary of what you found",
  "health_score": 72,
  "themes": [
    {
      "name": "Theme name",
      "ticket_count": 3,
      "oldest_days_in_backlog": 67,
      "signal_strength": "high|medium|low",
      "description": "What the real problem is",
      "evidence": ["quote from comment 1", "quote from comment 2"],
      "revenue_impact": "high|medium|low|none"
    }
  ],
  "avoided_problems": [
    {
      "issue_id": "TF-XXX",
      "title": "ticket title",
      "days_avoided": 91,
      "why_it_matters": "explanation",
      "urgency": "critical|high|medium"
    }
  ],
  "revenue_blockers": [
    {
      "issue_id": "TF-XXX",
      "title": "ticket title",
      "deals_blocked": 3,
      "evidence": "direct quote from comments"
    }
  ],
  "recommendation": {
    "what": "Feature name",
    "why": "Detailed reasoning tied to evidence",
    "v1_scope": "What a minimum first version looks like",
    "risk_of_not_building": "What happens if you skip this",
    "estimated_impact": "high|medium|low",
    "supporting_tickets": ["TF-001", "TF-002"]
  },
  "quick_wins": [
    {
      "issue_id": "TF-XXX",
      "title": "ticket title",
      "why_now": "why this is fast and high impact",
      "effort": "hours|days|week"
    }
  ],
  "compliance_flags": [
    {
      "issue_id": "TF-XXX",
      "title": "ticket title",
      "risk": "description of compliance/security risk",
      "days_until_critical": 30
    }
  ]
}
"""


# ── 3. CALL CLAUDE — THE BRAIN RUNS ─────────────────────────────────

def run_brain(jira_data, retries=2):
    """Send Jira data to Claude and get structured insights back."""
    
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    
    # Format the data for Claude
    user_message = f"""
Here are {jira_data['total_issues']} Jira issues from project "{jira_data['project']}".
Today's date is {datetime.now().strftime('%Y-%m-%d')}.
Analyze them and tell me what to build next.

ISSUES:
{json.dumps(jira_data['issues'], indent=2)}

What should this team build next and why? Return your analysis as JSON.
"""
    
    for attempt in range(retries + 1):
        try:
            print(f"{C.MAGENTA}▸ Sending {len(jira_data['issues'])} tickets to Claude brain...{C.RESET}")
            
            start_time = time.time()
            
            response = client.messages.create(
                model=config.MODEL,
                max_tokens=config.MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": user_message}
                ]
            )
            
            elapsed = time.time() - start_time
            print(f"{C.GREEN}▸ Brain responded in {elapsed:.1f}s{C.RESET}")
            print(f"{C.DIM}  Tokens used: {response.usage.input_tokens} in / {response.usage.output_tokens} out{C.RESET}\n")
            
            # Extract and parse
            raw_output = response.content[0].text
            
            # Try to extract JSON if Claude wrapped it in markdown
            if "```json" in raw_output:
                raw_output = raw_output.split("```json")[1].split("```")[0]
            elif "```" in raw_output:
                raw_output = raw_output.split("```")[1].split("```")[0]
            
            insights = json.loads(raw_output.strip())
            return insights
            
        except json.JSONDecodeError as e:
            if attempt < retries:
                print(f"{C.YELLOW}▸ Retry {attempt + 1}/{retries}: Claude returned invalid JSON, retrying...{C.RESET}")
                continue
            else:
                print(f"{C.RED}ERROR: Claude returned invalid JSON after {retries + 1} attempts.{C.RESET}")
                print(f"{C.DIM}Raw output:{C.RESET}\n{raw_output[:500]}")
                sys.exit(1)
                
        except anthropic.APIError as e:
            print(f"{C.RED}ERROR: API call failed: {e}{C.RESET}")
            if attempt < retries:
                wait = 2 ** attempt
                print(f"{C.YELLOW}▸ Retrying in {wait}s...{C.RESET}")
                time.sleep(wait)
            else:
                sys.exit(1)


# ── 4. TERMINAL REPORT ──────────────────────────────────────────────

def display_report(insights):
    """Print a beautiful, color-coded insight report in the terminal."""
    
    w = 64  # width
    
    print()
    print(f"{C.BOLD}{C.CYAN}{'━' * w}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}  ⚡ PRODUCT INSIGHT REPORT{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}{'━' * w}{C.RESET}")
    print(f"{C.DIM}  Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}{C.RESET}")
    
    # Health score
    score = insights.get("health_score", "N/A")
    if isinstance(score, (int, float)):
        if score >= 70:
            score_color = C.GREEN
            score_label = "HEALTHY"
        elif score >= 50:
            score_color = C.YELLOW
            score_label = "NEEDS ATTENTION"
        else:
            score_color = C.RED
            score_label = "AT RISK"
        print(f"\n  {C.BOLD}Backlog Health: {score_color}{'█' * (score // 5)}{'░' * (20 - score // 5)} {score}/100 {score_label}{C.RESET}")
    
    # Executive summary
    print(f"\n{C.BOLD}{C.WHITE}  SUMMARY{C.RESET}")
    print(f"  {insights.get('summary', 'No summary available.')}\n")
    
    # ── Themes ──
    print(f"{C.BOLD}{C.BLUE}{'─' * w}{C.RESET}")
    print(f"{C.BOLD}{C.BLUE}  📊 THEMES FOUND IN YOUR BACKLOG{C.RESET}\n")
    
    for i, theme in enumerate(insights.get("themes", []), 1):
        signal = theme.get("signal_strength", "low")
        signal_colors = {"high": C.RED, "medium": C.YELLOW, "low": C.DIM}
        signal_icons = {"high": "🔴", "medium": "🟡", "low": "⚪"}
        sc = signal_colors.get(signal, C.DIM)
        si = signal_icons.get(signal, "⚪")
        
        revenue = theme.get("revenue_impact", "none")
        revenue_tag = f" {C.RED}💰 REVENUE{C.RESET}" if revenue == "high" else ""
        
        print(f"  {C.BOLD}{i}. {theme['name']}{C.RESET}  {si} {sc}{signal.upper()}{C.RESET}{revenue_tag}")
        print(f"     {C.DIM}Tickets: {theme.get('ticket_count', '?')}  │  Oldest: {theme.get('oldest_days_in_backlog', '?')} days{C.RESET}")
        print(f"     {theme.get('description', '')}")
        
        evidence = theme.get("evidence", [])
        if evidence:
            print(f"     {C.DIM}Evidence: \"{evidence[0]}\"{C.RESET}")
        print()
    
    # ── Avoided Problems ──
    avoided = insights.get("avoided_problems", [])
    if avoided:
        print(f"{C.BOLD}{C.YELLOW}{'─' * w}{C.RESET}")
        print(f"{C.BOLD}{C.YELLOW}  ⚠️  WHAT YOUR TEAM IS SILENTLY AVOIDING{C.RESET}\n")
        
        for item in avoided:
            urgency = item.get("urgency", "medium")
            urgency_colors = {"critical": C.RED, "high": C.YELLOW, "medium": C.DIM}
            uc = urgency_colors.get(urgency, C.DIM)
            
            print(f"  {C.BOLD}{item.get('issue_id', '?')}{C.RESET}: {item.get('title', '')}")
            print(f"     {uc}Avoided for {item.get('days_avoided', '?')} days — {urgency.upper()}{C.RESET}")
            print(f"     {item.get('why_it_matters', '')}\n")
    
    # ── Revenue Blockers ──
    blockers = insights.get("revenue_blockers", [])
    if blockers:
        print(f"{C.BOLD}{C.RED}{'─' * w}{C.RESET}")
        print(f"{C.BOLD}{C.RED}  💰 REVENUE-BLOCKING ISSUES{C.RESET}\n")
        
        for item in blockers:
            deals = item.get("deals_blocked", "?")
            print(f"  {C.BOLD}{C.RED}{item.get('issue_id', '?')}{C.RESET}: {item.get('title', '')}")
            print(f"     {C.RED}Deals blocked: {deals}{C.RESET}")
            print(f"     \"{item.get('evidence', '')}\"\n")
    
    # ── Main Recommendation ──
    rec = insights.get("recommendation", {})
    if rec:
        print(f"{C.BOLD}{C.GREEN}{'─' * w}{C.RESET}")
        print(f"{C.BOLD}{C.GREEN}  🎯 RECOMMENDATION: BUILD \"{rec.get('what', '').upper()}\" NEXT{C.RESET}\n")
        
        print(f"  {C.BOLD}Why:{C.RESET} {rec.get('why', '')}\n")
        print(f"  {C.BOLD}V1 Scope:{C.RESET} {rec.get('v1_scope', '')}\n")
        print(f"  {C.BOLD}Risk if skipped:{C.RESET} {C.YELLOW}{rec.get('risk_of_not_building', '')}{C.RESET}\n")
        
        tickets = rec.get("supporting_tickets", [])
        if tickets:
            print(f"  {C.DIM}Supporting tickets: {', '.join(tickets)}{C.RESET}")
    
    # ── Quick Wins ──
    wins = insights.get("quick_wins", [])
    if wins:
        print(f"\n{C.BOLD}{C.CYAN}{'─' * w}{C.RESET}")
        print(f"{C.BOLD}{C.CYAN}  ⚡ QUICK WINS — DO THESE IN PARALLEL{C.RESET}\n")
        
        for win in wins:
            effort = win.get("effort", "?")
            effort_colors = {"hours": C.GREEN, "days": C.YELLOW, "week": C.RED}
            ec = effort_colors.get(effort, C.DIM)
            
            print(f"  {C.BOLD}{win.get('issue_id', '?')}{C.RESET}: {win.get('title', '')}")
            print(f"     {ec}Effort: {effort}{C.RESET} — {win.get('why_now', '')}\n")
    
    # ── Compliance Flags ──
    flags = insights.get("compliance_flags", [])
    if flags:
        print(f"{C.BOLD}{C.RED}{'─' * w}{C.RESET}")
        print(f"{C.BOLD}{C.RED}  🛡️  COMPLIANCE & SECURITY FLAGS{C.RESET}\n")
        
        for flag in flags:
            days = flag.get("days_until_critical", "?")
            print(f"  {C.BOLD}{C.RED}{flag.get('issue_id', '?')}{C.RESET}: {flag.get('title', '')}")
            print(f"     Risk: {flag.get('risk', '')}")
            print(f"     {C.RED}⏰ Days until critical: {days}{C.RESET}\n")
    
    print(f"{C.BOLD}{C.CYAN}{'━' * w}{C.RESET}")
    print(f"{C.BOLD}  Report complete.{C.RESET}")
    print()


# ── 5. SAVE OUTPUTS ─────────────────────────────────────────────────

def save_json(insights):
    """Save raw JSON output."""
    output = {
        "generated_at": datetime.now().isoformat(),
        "model": config.MODEL,
        "insights": insights
    }
    with open(config.OUTPUT_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"{C.DIM}  📄 JSON saved to: {config.OUTPUT_JSON_PATH}{C.RESET}")


def save_dashboard(insights, jira_data):
    """Generate a beautiful HTML dashboard from the insights."""
    
    # Build theme cards
    theme_cards = ""
    for i, theme in enumerate(insights.get("themes", []), 1):
        signal = theme.get("signal_strength", "low")
        revenue = theme.get("revenue_impact", "none")
        signal_class = f"signal-{signal}"
        revenue_badge = '<span class="badge badge-revenue">💰 Revenue Impact</span>' if revenue == "high" else ""
        
        evidence_html = ""
        for ev in theme.get("evidence", []):
            evidence_html += f'<div class="evidence-quote">"{ev}"</div>'
        
        theme_cards += f"""
        <div class="card theme-card {signal_class}">
            <div class="card-header">
                <span class="theme-number">{i}</span>
                <h3>{theme.get('name', '')}</h3>
                <span class="badge badge-{signal}">{signal.upper()}</span>
                {revenue_badge}
            </div>
            <div class="card-stats">
                <div class="stat">
                    <span class="stat-value">{theme.get('ticket_count', '?')}</span>
                    <span class="stat-label">Tickets</span>
                </div>
                <div class="stat">
                    <span class="stat-value">{theme.get('oldest_days_in_backlog', '?')}</span>
                    <span class="stat-label">Days Oldest</span>
                </div>
            </div>
            <p class="card-desc">{theme.get('description', '')}</p>
            {evidence_html}
        </div>
        """
    
    # Build avoided problems
    avoided_html = ""
    for item in insights.get("avoided_problems", []):
        urgency = item.get("urgency", "medium")
        avoided_html += f"""
        <div class="card avoided-card">
            <div class="card-header">
                <span class="ticket-id">{item.get('issue_id', '')}</span>
                <h4>{item.get('title', '')}</h4>
                <span class="badge badge-{urgency}">{urgency.upper()}</span>
            </div>
            <div class="avoided-days">{item.get('days_avoided', '?')} days avoided</div>
            <p>{item.get('why_it_matters', '')}</p>
        </div>
        """
    
    # Revenue blockers
    blocker_html = ""
    for item in insights.get("revenue_blockers", []):
        blocker_html += f"""
        <div class="card blocker-card">
            <div class="card-header">
                <span class="ticket-id">{item.get('issue_id', '')}</span>
                <h4>{item.get('title', '')}</h4>
            </div>
            <div class="deals-blocked">
                <span class="deals-number">{item.get('deals_blocked', '?')}</span>
                <span>deals blocked</span>
            </div>
            <div class="evidence-quote">"{item.get('evidence', '')}"</div>
        </div>
        """
    
    # Quick wins
    wins_html = ""
    for win in insights.get("quick_wins", []):
        effort = win.get("effort", "?")
        wins_html += f"""
        <div class="card win-card">
            <div class="card-header">
                <span class="ticket-id">{win.get('issue_id', '')}</span>
                <h4>{win.get('title', '')}</h4>
                <span class="badge badge-effort-{effort}">{effort}</span>
            </div>
            <p>{win.get('why_now', '')}</p>
        </div>
        """
    
    # Compliance
    compliance_html = ""
    for flag in insights.get("compliance_flags", []):
        compliance_html += f"""
        <div class="card compliance-card">
            <div class="card-header">
                <span class="ticket-id">{flag.get('issue_id', '')}</span>
                <h4>{flag.get('title', '')}</h4>
            </div>
            <p>{flag.get('risk', '')}</p>
            <div class="days-critical">⏰ {flag.get('days_until_critical', '?')} days until critical</div>
        </div>
        """
    
    # Recommendation
    rec = insights.get("recommendation", {})
    rec_tickets = ", ".join(rec.get("supporting_tickets", []))
    
    # Health score
    score = insights.get("health_score", 50)
    if isinstance(score, (int, float)):
        score_color = "#10b981" if score >= 70 else ("#f59e0b" if score >= 50 else "#ef4444")
    else:
        score = 50
        score_color = "#f59e0b"
    
    # Issue stats for the header
    total = jira_data.get("total_issues", 0)
    project = jira_data.get("project", "Unknown")
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PM Brain — Insight Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-primary: #0a0a0f;
            --bg-secondary: #12121a;
            --bg-card: #1a1a2e;
            --bg-card-hover: #1f1f35;
            --border: #2a2a3e;
            --text-primary: #e8e8f0;
            --text-secondary: #8888a0;
            --text-dim: #5a5a72;
            --accent-blue: #4f8ef7;
            --accent-purple: #8b5cf6;
            --accent-green: #10b981;
            --accent-yellow: #f59e0b;
            --accent-red: #ef4444;
            --accent-cyan: #06b6d4;
            --gradient-main: linear-gradient(135deg, #4f8ef7 0%, #8b5cf6 50%, #06b6d4 100%);
            --gradient-danger: linear-gradient(135deg, #ef4444 0%, #f97316 100%);
            --gradient-success: linear-gradient(135deg, #10b981 0%, #06b6d4 100%);
            --shadow: 0 4px 24px rgba(0, 0, 0, 0.4);
            --shadow-glow: 0 0 30px rgba(79, 142, 247, 0.1);
        }}
        
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            overflow-x: hidden;
        }}
        
        /* ── Animated background ── */
        body::before {{
            content: '';
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: 
                radial-gradient(ellipse 80% 50% at 20% 20%, rgba(79, 142, 247, 0.06), transparent),
                radial-gradient(ellipse 60% 40% at 80% 80%, rgba(139, 92, 246, 0.05), transparent),
                radial-gradient(ellipse 50% 30% at 50% 50%, rgba(6, 182, 212, 0.04), transparent);
            pointer-events: none;
            z-index: 0;
        }}
        
        .app {{
            position: relative;
            z-index: 1;
            max-width: 1200px;
            margin: 0 auto;
            padding: 2rem;
        }}
        
        /* ── Header ── */
        .header {{
            text-align: center;
            margin-bottom: 3rem;
            padding: 2rem 0;
        }}
        
        .header-logo {{
            display: inline-flex;
            align-items: center;
            gap: 0.75rem;
            margin-bottom: 1rem;
        }}
        
        .header-logo .icon {{
            font-size: 2rem;
        }}
        
        .header h1 {{
            font-size: 2.5rem;
            font-weight: 800;
            background: var(--gradient-main);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            letter-spacing: -0.02em;
        }}
        
        .header-meta {{
            color: var(--text-secondary);
            font-size: 0.9rem;
            margin-top: 0.5rem;
        }}
        
        .header-meta span {{
            display: inline-block;
            margin: 0 0.75rem;
        }}
        
        /* ── Health Score Ring ── */
        .health-section {{
            display: flex;
            justify-content: center;
            margin-bottom: 2.5rem;
        }}
        
        .health-ring {{
            position: relative;
            width: 160px;
            height: 160px;
        }}
        
        .health-ring svg {{
            width: 160px;
            height: 160px;
            transform: rotate(-90deg);
        }}
        
        .health-ring .ring-bg {{
            fill: none;
            stroke: var(--border);
            stroke-width: 8;
        }}
        
        .health-ring .ring-fill {{
            fill: none;
            stroke: {score_color};
            stroke-width: 8;
            stroke-linecap: round;
            stroke-dasharray: {score * 4.08} 408;
            transition: stroke-dasharray 1.5s ease-out;
        }}
        
        .health-score-text {{
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            text-align: center;
        }}
        
        .health-score-text .score-num {{
            font-size: 2.5rem;
            font-weight: 800;
            color: {score_color};
            line-height: 1;
        }}
        
        .health-score-text .score-label {{
            font-size: 0.7rem;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            color: var(--text-secondary);
            margin-top: 4px;
        }}
        
        /* ── Summary ── */
        .summary {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 2rem;
            margin-bottom: 2.5rem;
            box-shadow: var(--shadow);
        }}
        
        .summary p {{
            font-size: 1.1rem;
            line-height: 1.8;
            color: var(--text-secondary);
        }}
        
        /* ── Section ── */
        .section {{
            margin-bottom: 3rem;
        }}
        
        .section-header {{
            display: flex;
            align-items: center;
            gap: 0.75rem;
            margin-bottom: 1.5rem;
        }}
        
        .section-header .section-icon {{
            font-size: 1.5rem;
        }}
        
        .section-header h2 {{
            font-size: 1.3rem;
            font-weight: 700;
            letter-spacing: -0.01em;
        }}
        
        .section-header .count {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            padding: 2px 10px;
            border-radius: 20px;
            font-size: 0.8rem;
            color: var(--text-secondary);
        }}
        
        /* ── Cards ── */
        .cards-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
            gap: 1rem;
        }}
        
        .card {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 1.5rem;
            transition: all 0.3s cubic-bezier(0.19, 1, 0.22, 1);
            box-shadow: var(--shadow);
        }}
        
        .card:hover {{
            background: var(--bg-card-hover);
            border-color: rgba(79, 142, 247, 0.3);
            transform: translateY(-2px);
            box-shadow: var(--shadow-glow);
        }}
        
        .card-header {{
            display: flex;
            align-items: center;
            gap: 0.75rem;
            margin-bottom: 1rem;
            flex-wrap: wrap;
        }}
        
        .theme-number {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 28px;
            height: 28px;
            border-radius: 8px;
            background: var(--gradient-main);
            color: white;
            font-weight: 700;
            font-size: 0.85rem;
            flex-shrink: 0;
        }}
        
        .card-header h3 {{
            font-size: 1.05rem;
            font-weight: 600;
            flex: 1;
        }}
        
        .card-header h4 {{
            font-size: 0.95rem;
            font-weight: 600;
            flex: 1;
        }}
        
        .ticket-id {{
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            padding: 2px 8px;
            border-radius: 6px;
            font-size: 0.8rem;
            font-weight: 600;
            color: var(--accent-cyan);
            font-family: 'JetBrains Mono', monospace;
            flex-shrink: 0;
        }}
        
        /* ── Badges ── */
        .badge {{
            padding: 3px 10px;
            border-radius: 20px;
            font-size: 0.7rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        
        .badge-high {{ background: rgba(239, 68, 68, 0.15); color: var(--accent-red); border: 1px solid rgba(239, 68, 68, 0.3); }}
        .badge-medium {{ background: rgba(245, 158, 11, 0.15); color: var(--accent-yellow); border: 1px solid rgba(245, 158, 11, 0.3); }}
        .badge-low {{ background: rgba(136, 136, 160, 0.15); color: var(--text-secondary); border: 1px solid rgba(136, 136, 160, 0.3); }}
        .badge-critical {{ background: rgba(239, 68, 68, 0.2); color: #ff6b6b; border: 1px solid rgba(239, 68, 68, 0.4); }}
        .badge-revenue {{ background: rgba(245, 158, 11, 0.15); color: var(--accent-yellow); border: 1px solid rgba(245, 158, 11, 0.3); }}
        .badge-effort-hours {{ background: rgba(16, 185, 129, 0.15); color: var(--accent-green); border: 1px solid rgba(16, 185, 129, 0.3); }}
        .badge-effort-days {{ background: rgba(245, 158, 11, 0.15); color: var(--accent-yellow); border: 1px solid rgba(245, 158, 11, 0.3); }}
        .badge-effort-week {{ background: rgba(239, 68, 68, 0.15); color: var(--accent-red); border: 1px solid rgba(239, 68, 68, 0.3); }}
        
        /* ── Card internals ── */
        .card-stats {{
            display: flex;
            gap: 1.5rem;
            margin-bottom: 1rem;
        }}
        
        .stat {{
            display: flex;
            flex-direction: column;
        }}
        
        .stat-value {{
            font-size: 1.4rem;
            font-weight: 700;
            color: var(--accent-blue);
        }}
        
        .stat-label {{
            font-size: 0.72rem;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--text-dim);
        }}
        
        .card-desc {{
            color: var(--text-secondary);
            font-size: 0.9rem;
            line-height: 1.6;
            margin-bottom: 0.75rem;
        }}
        
        .evidence-quote {{
            border-left: 3px solid var(--accent-purple);
            padding: 0.5rem 1rem;
            margin-top: 0.5rem;
            font-size: 0.85rem;
            color: var(--text-dim);
            font-style: italic;
            background: rgba(139, 92, 246, 0.05);
            border-radius: 0 8px 8px 0;
        }}
        
        .avoided-days {{
            font-size: 1.2rem;
            font-weight: 700;
            color: var(--accent-yellow);
            margin-bottom: 0.5rem;
        }}
        
        .deals-blocked {{
            display: flex;
            align-items: baseline;
            gap: 0.5rem;
            margin-bottom: 0.75rem;
        }}
        
        .deals-number {{
            font-size: 2rem;
            font-weight: 800;
            color: var(--accent-red);
        }}
        
        .deals-blocked span {{
            color: var(--text-secondary);
            font-size: 0.9rem;
        }}
        
        .days-critical {{
            margin-top: 0.75rem;
            font-weight: 600;
            color: var(--accent-red);
        }}
        
        /* ── Signal borders ── */
        .signal-high {{ border-left: 3px solid var(--accent-red); }}
        .signal-medium {{ border-left: 3px solid var(--accent-yellow); }}
        .signal-low {{ border-left: 3px solid var(--border); }}
        
        /* ── Recommendation ── */
        .recommendation {{
            background: linear-gradient(135deg, rgba(16, 185, 129, 0.08), rgba(6, 182, 212, 0.06));
            border: 1px solid rgba(16, 185, 129, 0.25);
            border-radius: 16px;
            padding: 2rem;
            margin-bottom: 2.5rem;
            box-shadow: 0 0 40px rgba(16, 185, 129, 0.05);
        }}
        
        .recommendation h2 {{
            font-size: 1.3rem;
            font-weight: 700;
            margin-bottom: 1.5rem;
        }}
        
        .recommendation .rec-title {{
            font-size: 1.6rem;
            font-weight: 800;
            background: var(--gradient-success);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            margin-bottom: 1.5rem;
        }}
        
        .rec-section {{
            margin-bottom: 1.25rem;
        }}
        
        .rec-section .label {{
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--text-dim);
            margin-bottom: 4px;
        }}
        
        .rec-section p {{
            color: var(--text-secondary);
            line-height: 1.7;
        }}
        
        .rec-tickets {{
            display: flex;
            gap: 0.5rem;
            flex-wrap: wrap;
            margin-top: 1rem;
        }}
        
        .rec-tickets span {{
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 0.8rem;
            color: var(--accent-cyan);
            font-family: 'JetBrains Mono', monospace;
        }}
        
        /* ── Blocker/Compliance cards ── */
        .blocker-card {{
            border-left: 3px solid var(--accent-red);
        }}
        
        .compliance-card {{
            border-left: 3px solid var(--accent-yellow);
        }}
        
        .win-card {{
            border-left: 3px solid var(--accent-green);
        }}
        
        /* ── Footer ── */
        .footer {{
            text-align: center;
            padding: 2rem 0;
            color: var(--text-dim);
            font-size: 0.8rem;
        }}
        
        .footer a {{
            color: var(--accent-blue);
            text-decoration: none;
        }}
        
        /* ── Animations ── */
        @keyframes fadeInUp {{
            from {{ opacity: 0; transform: translateY(20px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        
        .card, .summary, .recommendation {{
            animation: fadeInUp 0.5s ease-out backwards;
        }}
        
        .cards-grid .card:nth-child(1) {{ animation-delay: 0.1s; }}
        .cards-grid .card:nth-child(2) {{ animation-delay: 0.15s; }}
        .cards-grid .card:nth-child(3) {{ animation-delay: 0.2s; }}
        .cards-grid .card:nth-child(4) {{ animation-delay: 0.25s; }}
        .cards-grid .card:nth-child(5) {{ animation-delay: 0.3s; }}
        
        /* ── Responsive ── */
        @media (max-width: 768px) {{
            .app {{ padding: 1rem; }}
            .header h1 {{ font-size: 1.8rem; }}
            .cards-grid {{ grid-template-columns: 1fr; }}
            .recommendation {{ padding: 1.5rem; }}
        }}
    </style>
</head>
<body>
    <div class="app">
        <!-- Header -->
        <div class="header">
            <div class="header-logo">
                <span class="icon">⚡</span>
                <h1>PM Brain</h1>
            </div>
            <div class="header-meta">
                <span>Project: <strong>{project}</strong></span>
                <span>•</span>
                <span>{total} tickets analyzed</span>
                <span>•</span>
                <span>{datetime.now().strftime('%B %d, %Y')}</span>
            </div>
        </div>
        
        <!-- Health Score -->
        <div class="health-section">
            <div class="health-ring">
                <svg viewBox="0 0 140 140">
                    <circle class="ring-bg" cx="70" cy="70" r="65"/>
                    <circle class="ring-fill" cx="70" cy="70" r="65"/>
                </svg>
                <div class="health-score-text">
                    <div class="score-num">{score}</div>
                    <div class="score-label">Health Score</div>
                </div>
            </div>
        </div>
        
        <!-- Summary -->
        <div class="summary">
            <p>{insights.get('summary', '')}</p>
        </div>
        
        <!-- Recommendation -->
        <div class="recommendation section">
            <div class="section-header">
                <span class="section-icon">🎯</span>
                <h2>Top Recommendation</h2>
            </div>
            <div class="rec-title">Build "{rec.get('what', '')}" Next</div>
            <div class="rec-section">
                <div class="label">Why</div>
                <p>{rec.get('why', '')}</p>
            </div>
            <div class="rec-section">
                <div class="label">V1 Scope</div>
                <p>{rec.get('v1_scope', '')}</p>
            </div>
            <div class="rec-section">
                <div class="label">Risk if Skipped</div>
                <p>{rec.get('risk_of_not_building', '')}</p>
            </div>
            <div class="rec-tickets">
                {''.join(f'<span>{t}</span>' for t in rec.get('supporting_tickets', []))}
            </div>
        </div>
        
        <!-- Themes -->
        <div class="section">
            <div class="section-header">
                <span class="section-icon">📊</span>
                <h2>Themes in Your Backlog</h2>
                <span class="count">{len(insights.get('themes', []))}</span>
            </div>
            <div class="cards-grid">
                {theme_cards}
            </div>
        </div>
        
        <!-- Revenue Blockers -->
        {"" if not blockers else f'''
        <div class="section">
            <div class="section-header">
                <span class="section-icon">💰</span>
                <h2>Revenue Blockers</h2>
                <span class="count">{len(insights.get('revenue_blockers', []))}</span>
            </div>
            <div class="cards-grid">
                {blocker_html}
            </div>
        </div>
        '''}
        
        <!-- Avoided Problems -->
        {"" if not avoided else f'''
        <div class="section">
            <div class="section-header">
                <span class="section-icon">⚠️</span>
                <h2>What Your Team is Avoiding</h2>
                <span class="count">{len(insights.get('avoided_problems', []))}</span>
            </div>
            <div class="cards-grid">
                {avoided_html}
            </div>
        </div>
        '''}
        
        <!-- Quick Wins -->
        {"" if not wins else f'''
        <div class="section">
            <div class="section-header">
                <span class="section-icon">⚡</span>
                <h2>Quick Wins</h2>
                <span class="count">{len(insights.get('quick_wins', []))}</span>
            </div>
            <div class="cards-grid">
                {wins_html}
            </div>
        </div>
        '''}
        
        <!-- Compliance Flags -->
        {"" if not flags else f'''
        <div class="section">
            <div class="section-header">
                <span class="section-icon">🛡️</span>
                <h2>Compliance & Security Flags</h2>
                <span class="count">{len(insights.get('compliance_flags', []))}</span>
            </div>
            <div class="cards-grid">
                {compliance_html}
            </div>
        </div>
        '''}
        
        <!-- Footer -->
        <div class="footer">
            <p>Generated by <strong>PM Brain</strong> · Layer 2 Intelligence Engine · Model: {config.MODEL}</p>
        </div>
    </div>
</body>
</html>"""
    
    with open(config.OUTPUT_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"{C.DIM}  🌐 Dashboard saved to: {config.OUTPUT_HTML_PATH}{C.RESET}")


# ── 6. MAIN ─────────────────────────────────────────────────────────

def main():
    print(f"\n{C.BOLD}{C.CYAN}⚡ PM Brain — Layer 2 Intelligence Engine{C.RESET}\n")
    
    # Validate config
    errors = config.validate()
    if errors:
        for err in errors:
            print(f"{C.RED}✗ {err}{C.RESET}\n")
        sys.exit(1)
    
    # Load data
    jira_data = load_jira_data()
    
    # Run the brain
    insights = run_brain(jira_data)
    
    # Display terminal report
    display_report(insights)
    
    # Save outputs
    save_json(insights)
    save_dashboard(insights, jira_data)
    
    print(f"\n{C.GREEN}{C.BOLD}✓ Done!{C.RESET} Open {C.CYAN}dashboard.html{C.RESET} in your browser for the visual report.\n")


if __name__ == "__main__":
    main()
