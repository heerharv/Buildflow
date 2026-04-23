# ⚡ PM Brain — Layer 2 Intelligence Engine

> Reads Jira tickets → Sends to Claude → Returns actionable product insights
> with a beautiful HTML dashboard.

## What It Does

PM Brain analyzes your backlog and answers the 3 hardest PM questions:

1. **What hidden themes** are clustering across dozens of tickets?
2. **What is the team silently avoiding** despite marking it high priority?
3. **What should we actually build next** — backed by evidence?

## Architecture

```
┌─────────────┐     ┌───────────────┐     ┌──────────────────┐
│  Jira Data  │ ──→ │  Claude Brain  │ ──→ │  Insight Report  │
│  (JSON)     │     │  (System      │     │  • Terminal      │
│             │     │   Prompt +    │     │  • JSON file     │
│             │     │   Reasoning)  │     │  • HTML Dashboard│
└─────────────┘     └───────────────┘     └──────────────────┘
```

## Setup (3 steps)

### Step 1 — Install Python library
```
pip install anthropic
```

### Step 2 — Get your Anthropic API key
1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Sign up (free tier available)
3. Click **API Keys** → **Create Key**
4. Copy the key

### Step 3 — Set your API key

**Windows (CMD):**
```
set ANTHROPIC_API_KEY=sk-ant-your-key-here
```

**Windows (PowerShell):**
```
$env:ANTHROPIC_API_KEY="sk-ant-your-key-here"
```

**Mac/Linux:**
```
export ANTHROPIC_API_KEY=sk-ant-your-key-here
```

## Run It

```
python brain.py
```

## What You Get

| Output | Description |
|--------|-------------|
| **Terminal Report** | Color-coded insight report with themes, blockers, and recommendations |
| **insights_output.json** | Raw JSON — use this to feed other tools or build a UI |
| **dashboard.html** | Beautiful dark-mode HTML dashboard — open in any browser |

## Files

| File | Purpose |
|------|---------|
| `brain.py` | Main engine — loads data, calls Claude, generates outputs |
| `config.py` | Configuration — API key, model, file paths |
| `mock_jira.json` | 25 realistic fake Jira tickets for testing |

## The Brain's System Prompt

The system prompt in `brain.py` is engineered to make Claude think like a senior PM:
- Groups tickets by **real underlying problems**, not surface labels
- Identifies **revenue-blocking issues** from comment evidence
- Spots **organizational avoidance patterns** (the stuff that keeps getting pushed)
- Delivers a **single clear recommendation** backed by ticket data
- Flags **compliance and security risks** with urgency timelines

## Next Steps

- [ ] Connect to real Jira API (replace `mock_jira.json` with live data)
- [ ] Add memory layer (remember past analyses for trend tracking)
- [ ] Build a web UI wrapper around the dashboard
- [ ] Add Slack/email delivery of daily insight reports
