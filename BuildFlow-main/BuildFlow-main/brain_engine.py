"""
brain_engine.py — Layer 2 AI Reasoning Module
----------------------------------------------
Self-contained module that takes Jira issues (+ optional signals)
and returns structured PM insights via Claude.

Zero Flask dependencies — pure Python + anthropic.
"""

import json
import os
import time
from datetime import datetime

# Graceful import — app works without anthropic installed
try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

# ── CONFIGURATION ──────────────────────────────────────────────────

MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 4000


# ── SYSTEM PROMPT ──────────────────────────────────────────────────
# This is the engineered prompt that makes Claude think like a
# senior PM strategist. This is the intellectual property of Layer 2.

SYSTEM_PROMPT = """
You are a senior product strategist with 10 years of experience at 
high-growth B2B SaaS companies. You think deeply, reason from evidence, 
and give direct, confident recommendations.

You will be given a list of Jira issues from a software product team,
and optionally additional signals from interviews, support tickets,
user feedback, and analytics observations.

Your job is to analyze everything and answer the question every PM dreads:
"What should we actually build next — and why?"

YOUR ANALYSIS MUST DO ALL OF THE FOLLOWING:

1. FIND HIDDEN THEMES
   - Group tickets by the REAL underlying problem, not surface labels
   - Look for tickets that seem different but share a root cause
   - If signals are provided, cross-reference them with ticket patterns
   - Count how many tickets belong to each theme
   - Note how long the oldest ticket in each theme has sat in backlog

2. FIND WHAT THE TEAM IS AVOIDING
   - Identify high-priority tickets that keep getting deprioritized
   - Look for patterns in which problems never get solved
   - This reveals organizational blind spots or engineering fear

3. FIND REVENUE-BLOCKING ISSUES
   - Look for comments mentioning: deals blocked, enterprise, churn,
     customer complaints, sales, QBR, procurement
   - Cross-reference with any interview or support signals provided
   - These are your highest-leverage items

4. MAKE A CLEAR RECOMMENDATION
   - Pick ONE thing to build next
   - Justify it with specific evidence from ticket comments and signals
   - Give a rough scope (what a v1 looks like)
   - Explain the risk of NOT building it

5. FLAG QUICK WINS
   - Identify 1-2 bugs or small fixes that unblock the most users
   - These can be done in parallel with the main recommendation

6. RISK ASSESSMENT
   - Estimate urgency as days_until_critical
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


# ── HELPERS ─────────────────────────────────────────────────────────

def is_brain_available():
    """Check if the brain can run (anthropic installed + key set)."""
    return ANTHROPIC_AVAILABLE and bool(os.environ.get("ANTHROPIC_API_KEY", ""))


def get_brain_status():
    """Return detailed status of brain readiness."""
    return {
        "available": is_brain_available(),
        "anthropic_installed": ANTHROPIC_AVAILABLE,
        "api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY", "")),
        "model": MODEL,
    }


def format_issues_for_brain(issues):
    """
    Transform BuildFlow Jira issue format into a clean format for the brain.
    BuildFlow issues have the Jira REST API structure with nested fields.
    """
    formatted = []
    now = datetime.utcnow()

    for issue in issues:
        fields = issue.get("fields", {})

        # Extract key info
        key = issue.get("key", "UNKNOWN")
        summary = fields.get("summary", "No title")
        priority = fields.get("priority", {}).get("name", "Medium")
        status = fields.get("status", {}).get("name", "Backlog")
        issue_type = fields.get("issuetype", {}).get("name", "Task")
        assignee = None
        if fields.get("assignee"):
            assignee = fields["assignee"].get("displayName", "Unassigned")

        # Calculate days in backlog
        days_in_backlog = 0
        created = fields.get("created")
        if created:
            try:
                created_dt = datetime.fromisoformat(
                    str(created).replace("Z", "+00:00").split("+")[0]
                )
                days_in_backlog = (now - created_dt).days
            except Exception:
                pass

        # Description
        description = fields.get("description", "")
        if isinstance(description, dict):
            # Jira Cloud ADF format — flatten to text
            description = str(description.get("content", ""))
        if description and len(description) > 300:
            description = description[:300] + "..."

        # Due date
        due_date = fields.get("duedate")

        formatted.append({
            "id": key,
            "title": summary,
            "type": issue_type,
            "priority": priority,
            "status": status,
            "assignee": assignee or "Unassigned",
            "days_in_backlog": days_in_backlog,
            "due_date": due_date,
            "description": description[:200] if description else "",
        })

    return formatted


def format_signals_for_brain(signals):
    """
    Format signal entries for inclusion in the brain prompt.
    Each signal has: source_type, content, created_at.
    """
    if not signals:
        return ""

    signal_text = "\n\nADDITIONAL SIGNALS FROM OTHER SOURCES:\n"
    for i, sig in enumerate(signals, 1):
        source = sig.get("source_type", "unknown")
        content = sig.get("content", "")
        if len(content) > 500:
            content = content[:500] + "..."
        signal_text += f"\n--- Signal {i} [{source.upper()}] ---\n{content}\n"

    return signal_text


# ── MAIN BRAIN FUNCTION ────────────────────────────────────────────

def run_brain(issues, project_key="UNKNOWN", signals=None, retries=2):
    """
    Run the AI brain on a set of Jira issues + optional signals.

    Args:
        issues: List of Jira issues in BuildFlow format (fields.summary, etc.)
        project_key: The project key (e.g., 'UNCIA')
        signals: Optional list of signal dicts with source_type, content
        retries: Number of retry attempts on failure

    Returns:
        dict: Structured insights JSON, or None on failure
        str: Error message if failed, None on success
    """
    if not ANTHROPIC_AVAILABLE:
        return None, "anthropic library not installed. Run: pip install anthropic"

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None, "ANTHROPIC_API_KEY not set. Get one at console.anthropic.com"

    # Format data for the brain
    formatted_issues = format_issues_for_brain(issues)
    signal_text = format_signals_for_brain(signals) if signals else ""

    user_message = f"""
Here are {len(formatted_issues)} Jira issues from project "{project_key}".
Today's date is {datetime.utcnow().strftime('%Y-%m-%d')}.
Analyze them and tell me what to build next.

ISSUES:
{json.dumps(formatted_issues, indent=2)}
{signal_text}

What should this team build next and why? Return your analysis as JSON.
"""

    client = anthropic.Anthropic(api_key=api_key)

    for attempt in range(retries + 1):
        try:
            start_time = time.time()

            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )

            elapsed = time.time() - start_time

            raw_output = response.content[0].text

            # Extract JSON if Claude wrapped it
            if "```json" in raw_output:
                raw_output = raw_output.split("```json")[1].split("```")[0]
            elif "```" in raw_output:
                raw_output = raw_output.split("```")[1].split("```")[0]

            insights = json.loads(raw_output.strip())

            # Attach metadata
            insights["_meta"] = {
                "model": MODEL,
                "elapsed_seconds": round(elapsed, 1),
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "issue_count": len(formatted_issues),
                "signal_count": len(signals) if signals else 0,
                "project_key": project_key,
                "analyzed_at": datetime.utcnow().isoformat(),
            }

            return insights, None

        except json.JSONDecodeError:
            if attempt < retries:
                continue
            return None, "Claude returned invalid JSON after multiple attempts"

        except Exception as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            return None, f"API error: {str(e)}"


# ── MOCK BRAIN (for demo without API key) ──────────────────────────

def run_mock_brain(issues, project_key="UNKNOWN"):
    """
    Return realistic mock brain output for demos.
    This lets the full UI work without an API key.
    """
    issue_count = len(issues)

    return {
        "summary": f"Analysis of {issue_count} issues in {project_key} reveals significant execution risk. The team has 3 high-priority items lingering in backlog for 45+ days, multiple enterprise deals are blocked by missing export functionality, and the notification system is causing measurable user churn. Immediate action on export capabilities would unblock the most revenue.",
        "health_score": 58,
        "themes": [
            {
                "name": "Export & Reporting Gap",
                "ticket_count": 4,
                "oldest_days_in_backlog": 67,
                "signal_strength": "high",
                "description": "Multiple tickets around CSV/PDF export, bulk export, scheduled reports, and workspace analytics all point to the same root cause: the product cannot get data OUT in formats enterprises need.",
                "evidence": [
                    "Sales: 3 enterprise deals on hold because of this",
                    "Customer Acme Corp threatened to cancel if not fixed in Q2"
                ],
                "revenue_impact": "high"
            },
            {
                "name": "Notification System Overload",
                "ticket_count": 3,
                "oldest_days_in_backlog": 38,
                "signal_strength": "high",
                "description": "The notification system is actively driving users away. Wrong unread counts, email flooding, and no granular controls are causing 23% of users to disable notifications entirely.",
                "evidence": [
                    "23% of users disabled all notifications last month",
                    "DAU dropped 8% correlated with notification changes"
                ],
                "revenue_impact": "medium"
            },
            {
                "name": "Core Task Management Gaps",
                "ticket_count": 3,
                "oldest_days_in_backlog": 91,
                "signal_strength": "medium",
                "description": "Task dependencies and recurring tasks are the #1 and #2 most-requested features. Without them, project managers cannot plan sprints effectively and operations teams lack workflow automation.",
                "evidence": [
                    "Most requested feature in NPS survey — 340 votes",
                    "Moved from last 3 sprints due to capacity"
                ],
                "revenue_impact": "medium"
            },
            {
                "name": "Enterprise Security & Compliance",
                "ticket_count": 2,
                "oldest_days_in_backlog": 72,
                "signal_strength": "high",
                "description": "Two-factor authentication and audit logs are blocking 3 financial services deals and are required for SOC2 compliance. This is a binary blocker — enterprises cannot buy without it.",
                "evidence": [
                    "Blocking 3 deals in financial services sector",
                    "SOC2 audit requires this"
                ],
                "revenue_impact": "high"
            },
            {
                "name": "Performance & Search Degradation",
                "ticket_count": 2,
                "oldest_days_in_backlog": 52,
                "signal_strength": "medium",
                "description": "Dashboard load times and search quality are degrading as workspaces scale. Enterprise customers with 500+ tasks experience 8-12 second load times.",
                "evidence": [
                    "Enterprise client GlobalCorp complained in QBR",
                    "Users reverting to browser Ctrl+F"
                ],
                "revenue_impact": "medium"
            }
        ],
        "avoided_problems": [
            {
                "issue_id": "TF-014",
                "title": "Recurring tasks — daily, weekly, monthly",
                "days_avoided": 91,
                "why_it_matters": "Second most requested feature (280 votes) that has been pushed from the last 3 sprints. The team is avoiding this likely due to complexity of the scheduling system, but operations teams need it for daily standups and weekly reviews.",
                "urgency": "high"
            },
            {
                "issue_id": "TF-021",
                "title": "Custom fields for tasks",
                "days_avoided": 85,
                "why_it_matters": "Enterprise customers need to add their own metadata. Blocking 2 large accounts from full adoption. Competitors Notion and Airtable both have this.",
                "urgency": "high"
            },
            {
                "issue_id": "TF-013",
                "title": "Task dependencies — block/blocked by relationship",
                "days_avoided": 78,
                "why_it_matters": "Most requested feature (340 votes). Project managers literally cannot plan sprints without dependency tracking. Jira and Linear both have this.",
                "urgency": "medium"
            }
        ],
        "revenue_blockers": [
            {
                "issue_id": "TF-001",
                "title": "Users cannot export reports to CSV or PDF",
                "deals_blocked": 3,
                "evidence": "Sales: 3 enterprise deals on hold because of this. Customer Acme Corp threatened to cancel if not fixed in Q2."
            },
            {
                "issue_id": "TF-024",
                "title": "Two-factor authentication for all users",
                "deals_blocked": 3,
                "evidence": "Blocking 3 deals in financial services sector. SOC2 audit requires this."
            },
            {
                "issue_id": "TF-010",
                "title": "Dashboard loads slowly for large workspaces",
                "deals_blocked": 1,
                "evidence": "Enterprise client GlobalCorp complained in QBR. Losing enterprise deals because of this."
            }
        ],
        "recommendation": {
            "what": "Report Export Engine (CSV + PDF)",
            "why": "This is the single highest-leverage feature to build. It directly unblocks 3 enterprise deals (TF-001), enables bulk admin exports (TF-002), and opens the door to scheduled reports (TF-003). The sales team has flagged this repeatedly, and a customer has threatened cancellation. No other feature has this density of revenue evidence.",
            "v1_scope": "Build CSV export for project reports with filters by status, assignee, and date range. Add a PDF summary view with charts. Ship to the 3 blocked enterprise accounts first as a beta.",
            "risk_of_not_building": "Lose 3 enterprise deals worth estimated $180K ARR. Acme Corp cancels. Sales team loses confidence in product roadmap. Competitors who already have this capture the deals.",
            "estimated_impact": "high",
            "supporting_tickets": ["TF-001", "TF-002", "TF-003", "TF-017"]
        },
        "quick_wins": [
            {
                "issue_id": "TF-004",
                "title": "Notification bell shows wrong unread count",
                "why_now": "Already in progress, affects 14+ users per week, and is a trust-eroding UX bug. Quick fix that improves perceived quality immediately.",
                "effort": "hours"
            },
            {
                "issue_id": "TF-015",
                "title": "Subtasks not showing in main task list view",
                "why_now": "UX regression from last release causing user confusion. Likely a rendering filter bug that can be fixed in a single PR.",
                "effort": "hours"
            }
        ],
        "compliance_flags": [
            {
                "issue_id": "TF-024",
                "title": "Two-factor authentication for all users",
                "risk": "Enterprise customers in financial services require 2FA for compliance. SOC2 audit flagged this as a gap. Without it, the product cannot pass enterprise procurement security reviews.",
                "days_until_critical": 30
            },
            {
                "issue_id": "TF-025",
                "title": "Audit log for enterprise compliance",
                "risk": "Required for SOC2 certification and enterprise procurement. Same 3 financial services deals are blocked by both this and 2FA.",
                "days_until_critical": 45
            }
        ],
        "_meta": {
            "model": "mock-brain",
            "elapsed_seconds": 0.1,
            "input_tokens": 0,
            "output_tokens": 0,
            "issue_count": issue_count,
            "signal_count": 0,
            "project_key": project_key,
            "analyzed_at": datetime.utcnow().isoformat(),
        }
    }, None


# ── PRD GENERATOR ───────────────────────────────────────────────

PRD_PROMPT = """
You are a senior product manager writing a crisp, actionable Product Requirements Document (PRD).
You will be given a recommendation from an AI analysis, along with the project context.
Write a concise PM-style PRD in Markdown format.

Include these sections:
1. # Title (From the recommendation)
2. ## The Problem & Context (Why are we doing this?)
3. ## Scope (What is in V1?)
4. ## Out of Scope (What are we explicitly NOT doing?)
5. ## Success Metrics (How will we know this worked?)

Do not include any pleasantries or conversational text. Output pure markdown.
"""

def generate_prd(project_key, recommendation, retries=2):
    if not ANTHROPIC_AVAILABLE:
        return run_mock_generate_prd(project_key, recommendation)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return run_mock_generate_prd(project_key, recommendation)

    user_message = f"""
Project: {project_key}
Recommendation details:
{json.dumps(recommendation, indent=2)}

Write the PRD now.
"""
    client = anthropic.Anthropic(api_key=api_key)

    for attempt in range(retries + 1):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=2000,
                system=PRD_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            return response.content[0].text.strip(), None
        except Exception as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            return None, f"API error: {str(e)}"

def run_mock_generate_prd(project_key, recommendation):
    title = recommendation.get('what', 'New Feature')
    why = recommendation.get('why', 'Customer requests.')
    scope = recommendation.get('v1_scope', 'Basic MVP functionality.')
    
    md = f"""# {title}

## The Problem & Context
{why}

This initiative directly addresses the blockers raised by enterprise accounts in the `{project_key}` project and ensures alignment with our Q3 revenue goals.

## Scope (V1)
{scope}
- Telemetry/analytics hooks to track usage.
- Feature toggle for secure phased rollout.

## Out of Scope
- Advanced customization of the UI.
- Legacy system migrations.

## Success Metrics
1. **Adoption**: 30% of active enterprise accounts use this within 30 days.
2. **Revenue Unblocked**: Conversion of at least 2 of the 3 currently blocked pipeline deals.
3. **Support Load**: Zero critical bugs/incidents related to this feature in the first 2 weeks.
"""
    return md, None


# ── TICKET PARSER ───────────────────────────────────────────────

TICKETS_PROMPT = """
You are an experienced Engineering Manager. You are given a Product Requirements Document (PRD).
You need to break this PRD down into a set of Epics, Stories, and Tasks.

RETURN VALID JSON ONLY. No explanation before or after.
Use exactly this structure (an array of objects):
[
  {
    "type": "Epic",
    "title": "Short title",
    "description": "Brief description of the epic.",
    "acceptance_criteria": "Overall criteria."
  },
  {
    "type": "Story",
    "title": "Short title",
    "description": "As a X I want Y...",
    "acceptance_criteria": "- Must do A\n- Must do B"
  }
]
"""

def generate_tickets(prd_content, retries=2):
    if not ANTHROPIC_AVAILABLE:
        return run_mock_generate_tickets(prd_content)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return run_mock_generate_tickets(prd_content)

    user_message = f"Here is the PRD:\n\n{prd_content}\n\nGenerate the engineering tickets."
    client = anthropic.Anthropic(api_key=api_key)

    for attempt in range(retries + 1):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=2500,
                system=TICKETS_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            raw_output = response.content[0].text
            if "```json" in raw_output:
                raw_output = raw_output.split("```json")[1].split("```")[0]
            elif "```" in raw_output:
                raw_output = raw_output.split("```")[1].split("```")[0]

            tickets = json.loads(raw_output.strip())
            return tickets, None
        except json.JSONDecodeError:
            if attempt < retries:
                continue
            return None, "Claude returned invalid JSON for tickets."
        except Exception as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            return None, f"API error: {str(e)}"

def run_mock_generate_tickets(prd_content):
    return [
        {
            "type": "Epic",
            "title": "Implement V1 Functionality",
            "description": "Orchestrates all backend and frontend changes required for the V1 rollout.",
            "acceptance_criteria": "Completed when all stories below are resolved and pass QA."
        },
        {
            "type": "Story",
            "title": "Backend API Endpoints",
            "description": "As a developer, I need the REST API routes created securely.",
            "acceptance_criteria": "- Ensure GET and POST routes exist\n- Role-based access control verified"
        },
        {
            "type": "Story",
            "title": "Frontend UI Components",
            "description": "As a user, I need to interact with the new feature in the dashboard.",
            "acceptance_criteria": "- Match Figma designs\n- Responsive map/charts"
        },
        {
            "type": "Task",
            "title": "Analytics Tracking",
            "description": "Fire events for funnel tracking.",
            "acceptance_criteria": "- Trigger Mixpanel event on success"
        }
    ], None
