"""
====================================================================
  BuildFlow — Three New Features (append these to your app.py)
====================================================================
  1. Rework / Churn Index   → /api/churn/<project_key>
  2. Dependency Map         → /api/dependency/<project_key>
  3. Client Share Link      → /api/client/share  +  /share/<token>
====================================================================
"""

import hashlib
import re

# ─────────────────────────────────────────────────────────────────
# DB MODEL  –  ClientShare  (add this near your other models)
# ─────────────────────────────────────────────────────────────────
class ClientShare(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    project_key  = db.Column(db.String(50),  nullable=False)
    token        = db.Column(db.String(64),  unique=True, nullable=False)
    label        = db.Column(db.String(200), nullable=True)   # e.g. "Shared with Accenture"
    show_risks   = db.Column(db.Boolean, default=True)
    show_team    = db.Column(db.Boolean, default=False)        # hide internal names by default
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at   = db.Column(db.DateTime, nullable=True)

    def is_valid(self):
        if self.expires_at and datetime.utcnow() > self.expires_at:
            return False
        return True

    def to_dict(self):
        return {
            'id':          self.id,
            'project_key': self.project_key,
            'token':       self.token,
            'label':       self.label,
            'show_risks':  self.show_risks,
            'show_team':   self.show_team,
            'created_at':  self.created_at.isoformat(),
            'expires_at':  self.expires_at.isoformat() if self.expires_at else None,
            'share_url':   f'/share/{self.token}'
        }


# ─────────────────────────────────────────────────────────────────
# FEATURE 1 — REWORK / CHURN INDEX
# ─────────────────────────────────────────────────────────────────
def _churn_mock(project_key):
    """Return realistic mock churn data for demo / USE_MOCK_JIRA mode."""
    issues = mock_issues(project_key)
    import random
    random.seed(42)

    churned, clean = [], []
    for issue in issues:
        f = issue.get('fields', {})
        status = f.get('status', {}).get('name', '').lower()
        is_done = any(s in status for s in ['done', 'closed', 'resolved'])
        # ~30 % of done issues get flagged as churned in mock
        if is_done and random.random() < 0.30:
            reopen_count = random.randint(1, 3)
            churned.append({
                'key':          issue.get('key'),
                'summary':      f.get('summary', ''),
                'assignee':     f.get('assignee', {}).get('displayName', 'Unassigned') if f.get('assignee') else 'Unassigned',
                'reopen_count': reopen_count,
                'issue_type':   f.get('issuetype', {}).get('name', 'Task'),
                'priority':     f.get('priority', {}).get('name', 'Medium'),
            })
        elif not is_done:
            clean.append(issue)

    total_done   = len([i for i in issues if any(
        s in i.get('fields', {}).get('status', {}).get('name', '').lower()
        for s in ['done', 'closed', 'resolved'])])
    churn_rate   = round(len(churned) / total_done * 100, 1) if total_done else 0

    # Per-assignee breakdown
    by_assignee = {}
    for c in churned:
        a = c['assignee']
        by_assignee[a] = by_assignee.get(a, 0) + c['reopen_count']

    # Per-type breakdown
    by_type = {}
    for c in churned:
        t = c['issue_type']
        by_type[t] = by_type.get(t, 0) + 1

    churn_index = min(100, round(churn_rate * 1.5 + len(churned) * 0.8, 1))

    return {
        'churn_index':       churn_index,
        'churn_rate_pct':    churn_rate,
        'total_issues':      len(issues),
        'total_done':        total_done,
        'churned_count':     len(churned),
        'churned_issues':    sorted(churned, key=lambda x: x['reopen_count'], reverse=True),
        'by_assignee':       [{'assignee': k, 'reopens': v} for k, v in sorted(by_assignee.items(), key=lambda x: -x[1])],
        'by_type':           [{'type': k, 'count': v} for k, v in sorted(by_type.items(), key=lambda x: -x[1])],
        'verdict':           'Critical' if churn_index >= 70 else ('High' if churn_index >= 45 else ('Medium' if churn_index >= 20 else 'Low')),
    }


def _churn_live(project_key):
    """
    Compute churn from real Jira data.
    Strategy: fetch issues updated in last 30 days whose changelog shows
    a transition *back* to In Progress / To Do after being Done/Resolved.
    We approximate by fetching recently-updated resolved issues and checking
    if updatedDate > resolutiondate (meaning activity happened after resolve).
    """
    r = jira_search({
        'jql': f'{project_jql(project_key)} AND statusCategory = Done ORDER BY updated DESC',
        'maxResults': 200,
        'fields': 'summary,status,assignee,issuetype,priority,resolutiondate,updated,created'
    })
    if r.status_code != 200:
        return None, r.status_code

    issues      = r.json().get('issues', [])
    churned     = []
    by_assignee = {}
    by_type     = {}

    for issue in issues:
        f          = issue.get('fields', {})
        resolved   = f.get('resolutiondate')
        updated    = f.get('updated')
        if not resolved or not updated:
            continue
        try:
            res_dt = datetime.strptime(resolved[:19], '%Y-%m-%dT%H:%M:%S')
            upd_dt = datetime.strptime(updated[:19],  '%Y-%m-%dT%H:%M:%S')
        except Exception:
            continue
        # If ticket was touched more than 1 hour after being resolved → likely reopened
        delta_hours = (upd_dt - res_dt).total_seconds() / 3600
        if delta_hours > 1:
            assignee = (f.get('assignee') or {}).get('displayName', 'Unassigned')
            itype    = f.get('issuetype', {}).get('name', 'Task')
            churned.append({
                'key':          issue.get('key'),
                'summary':      f.get('summary', ''),
                'assignee':     assignee,
                'reopen_count': 1,          # Jira API v3 needs changelog for exact count; 1 is conservative
                'issue_type':   itype,
                'priority':     f.get('priority', {}).get('name', 'Medium'),
                'hours_after_resolve': round(delta_hours, 1),
            })
            by_assignee[assignee] = by_assignee.get(assignee, 0) + 1
            by_type[itype]        = by_type.get(itype, 0) + 1

    total_done  = len(issues)
    churn_rate  = round(len(churned) / total_done * 100, 1) if total_done else 0
    churn_index = min(100, round(churn_rate * 1.5 + len(churned) * 0.8, 1))

    return {
        'churn_index':    churn_index,
        'churn_rate_pct': churn_rate,
        'total_issues':   total_done,
        'total_done':     total_done,
        'churned_count':  len(churned),
        'churned_issues': sorted(churned, key=lambda x: x.get('hours_after_resolve', 0), reverse=True),
        'by_assignee':    [{'assignee': k, 'reopens': v} for k, v in sorted(by_assignee.items(), key=lambda x: -x[1])],
        'by_type':        [{'type': k, 'count': v} for k, v in sorted(by_type.items(), key=lambda x: -x[1])],
        'verdict':        'Critical' if churn_index >= 70 else ('High' if churn_index >= 45 else ('Medium' if churn_index >= 20 else 'Low')),
    }, None


@app.route('/api/churn/<project_key>', methods=['GET'])
@login_required
def get_churn_index(project_key):
    if use_mock_jira():
        return jsonify({**_churn_mock(project_key), 'mock_mode': True}), 200

    if not current_user.has_jira_configured() and not jira_oauth_configured():
        return jsonify({'error': 'Jira not configured'}), 400

    try:
        data, err = _churn_live(project_key)
        if err:
            return jsonify({'error': 'Failed to fetch churn data', 'jira_status': err}), err
        return jsonify({**data, 'mock_mode': False}), 200
    except RequestException as e:
        return jsonify({'error': str(e)}), 502


# ─────────────────────────────────────────────────────────────────
# FEATURE 2 — CROSS-PROJECT DEPENDENCY MAP
# ─────────────────────────────────────────────────────────────────
def _dep_mock(project_keys):
    """Build a mock dependency graph across multiple projects."""
    import random
    random.seed(7)

    nodes, edges = [], []
    all_keys_pool = []

    for pk in project_keys:
        issues = mock_issues(pk)[:8]   # keep it readable
        for issue in issues:
            f   = issue.get('fields', {})
            key = issue.get('key')
            all_keys_pool.append(key)
            nodes.append({
                'id':       key,
                'label':    f.get('summary', key)[:55],
                'project':  pk,
                'status':   f.get('status', {}).get('name', 'To Do'),
                'priority': f.get('priority', {}).get('name', 'Medium'),
                'assignee': (f.get('assignee') or {}).get('displayName', 'Unassigned'),
            })

    # Sprinkle cross-project links
    if len(all_keys_pool) > 4:
        for _ in range(min(12, len(all_keys_pool) // 2)):
            src = random.choice(all_keys_pool)
            tgt = random.choice(all_keys_pool)
            if src != tgt:
                src_proj = src.split('-')[0]
                tgt_proj = tgt.split('-')[0]
                link_type = random.choice(['blocks', 'is blocked by', 'relates to', 'duplicates'])
                edges.append({
                    'source':    src,
                    'target':    tgt,
                    'type':      link_type,
                    'cross_proj': src_proj != tgt_proj
                })

    blockers = len([e for e in edges if 'block' in e['type']])
    return {
        'nodes':           nodes,
        'edges':           edges,
        'total_nodes':     len(nodes),
        'total_edges':     len(edges),
        'cross_proj_edges': len([e for e in edges if e['cross_proj']]),
        'blocking_count':  blockers,
        'projects':        project_keys,
    }


def _dep_live(project_keys):
    """Fetch real issue links from Jira across multiple projects."""
    nodes, edges = {}, []

    for pk in project_keys:
        r = jira_search({
            'jql': f'{project_jql(pk)} ORDER BY priority DESC',
            'maxResults': 50,
            'fields': 'summary,status,priority,assignee,issuetype,issuelinks'
        })
        if r.status_code != 200:
            continue
        for issue in r.json().get('issues', []):
            f   = issue.get('fields', {})
            key = issue.get('key')
            nodes[key] = {
                'id':       key,
                'label':    f.get('summary', key)[:55],
                'project':  pk,
                'status':   f.get('status', {}).get('name', 'To Do'),
                'priority': f.get('priority', {}).get('name', 'Medium'),
                'assignee': (f.get('assignee') or {}).get('displayName', 'Unassigned'),
            }
            for link in f.get('issuelinks', []):
                if link.get('outwardIssue'):
                    tgt_key = link['outwardIssue']['key']
                    edges.append({
                        'source':    key,
                        'target':    tgt_key,
                        'type':      link.get('type', {}).get('outward', 'relates to'),
                        'cross_proj': key.split('-')[0] != tgt_key.split('-')[0]
                    })
                if link.get('inwardIssue'):
                    src_key = link['inwardIssue']['key']
                    edges.append({
                        'source':    src_key,
                        'target':    key,
                        'type':      link.get('type', {}).get('inward', 'relates to'),
                        'cross_proj': src_key.split('-')[0] != key.split('-')[0]
                    })

    node_list = list(nodes.values())
    return {
        'nodes':            node_list,
        'edges':            edges,
        'total_nodes':      len(node_list),
        'total_edges':      len(edges),
        'cross_proj_edges': len([e for e in edges if e['cross_proj']]),
        'blocking_count':   len([e for e in edges if 'block' in e['type']]),
        'projects':         project_keys,
    }


@app.route('/api/dependency', methods=['GET'])
@login_required
def get_dependency_map():
    """
    ?projects=PROJ1,PROJ2,PROJ3
    Returns nodes + edges for a dependency graph.
    """
    raw     = request.args.get('projects', '')
    keys    = [k.strip().upper() for k in raw.split(',') if k.strip()]
    if not keys:
        # Fall back to all cached projects for this user
        cached = CachedProject.query.filter_by(user_id=current_user.id).all()
        keys   = [c.project_key for c in cached][:6]
    if not keys:
        return jsonify({'error': 'No projects specified or cached'}), 400

    if use_mock_jira():
        return jsonify({**_dep_mock(keys), 'mock_mode': True}), 200

    if not current_user.has_jira_configured() and not jira_oauth_configured():
        return jsonify({'error': 'Jira not configured'}), 400

    try:
        return jsonify({**_dep_live(keys), 'mock_mode': False}), 200
    except RequestException as e:
        return jsonify({'error': str(e)}), 502


# ─────────────────────────────────────────────────────────────────
# FEATURE 3 — CLIENT-FACING READ-ONLY SHARE LINK
# ─────────────────────────────────────────────────────────────────

@app.route('/api/client/shares', methods=['GET'])
@login_required
def list_shares():
    shares = ClientShare.query.filter_by(user_id=current_user.id).order_by(ClientShare.created_at.desc()).all()
    return jsonify({'shares': [s.to_dict() for s in shares]}), 200


@app.route('/api/client/share', methods=['POST'])
@login_required
def create_share():
    data        = request.get_json() or {}
    project_key = data.get('project_key', '').strip().upper()
    if not project_key:
        return jsonify({'error': 'project_key is required'}), 400

    label      = data.get('label', f'Shared — {project_key}')
    show_risks = data.get('show_risks', True)
    show_team  = data.get('show_team', False)
    expires_in = data.get('expires_days')        # None = no expiry

    token = secrets.token_urlsafe(32)
    share = ClientShare(
        user_id     = current_user.id,
        project_key = project_key,
        token       = token,
        label       = label,
        show_risks  = show_risks,
        show_team   = show_team,
        expires_at  = datetime.utcnow() + timedelta(days=int(expires_in)) if expires_in else None
    )
    db.session.add(share)
    db.session.commit()
    return jsonify({'share': share.to_dict()}), 201


@app.route('/api/client/share/<token>', methods=['DELETE'])
@login_required
def delete_share(token):
    share = ClientShare.query.filter_by(token=token, user_id=current_user.id).first()
    if not share:
        return jsonify({'error': 'Share not found'}), 404
    db.session.delete(share)
    db.session.commit()
    return jsonify({'message': 'Share deleted'}), 200


@app.route('/share/<token>', methods=['GET'])
def client_share_view(token):
    """Public route — serves the client dashboard HTML (no login needed)."""
    share = ClientShare.query.filter_by(token=token).first()
    if not share or not share.is_valid():
        return '<h2 style="font-family:sans-serif;padding:2rem">This link has expired or does not exist.</h2>', 404
    return send_from_directory('.', 'client_share.html')


@app.route('/api/share/<token>/data', methods=['GET'])
def client_share_data(token):
    """
    Public API that serves sanitised project data for a share token.
    No authentication — scoped entirely by the token.
    """
    share = ClientShare.query.filter_by(token=token).first()
    if not share or not share.is_valid():
        return jsonify({'error': 'Invalid or expired share link'}), 403

    owner = User.query.get(share.user_id)
    if not owner:
        return jsonify({'error': 'Owner not found'}), 404

    pk = share.project_key

    # Temporarily impersonate owner for Jira calls
    # We call Jira using the owner's stored token (Basic auth path)
    if owner.has_jira_configured():
        auth    = HTTPBasicAuth(owner.jira_email, owner.jira_api_token)
        headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        base    = f"https://{owner.jira_domain}/rest/api/3"

        def owner_get(path, params=None):
            req_s = requests.Session()
            req_s.trust_env = False
            return req_s.get(f"{base}{path}", headers=headers, auth=auth, params=params, timeout=15)

        def owner_search(params):
            return owner_get('/search', params)

        try:
            r = owner_search({
                'jql': f'project = "{pk}" ORDER BY priority DESC',
                'maxResults': 100,
                'fields': 'summary,status,assignee,priority,duedate,issuetype'
            })
            if r.status_code != 200:
                issues = []
            else:
                issues = r.json().get('issues', [])
        except Exception:
            issues = []
    else:
        # Fall back to mock
        issues = mock_issues(pk)

    # Build sanitised payload
    total = len(issues)
    done  = in_prog = todo = overdue_count = 0
    priority_dist = {'Critical': 0, 'High': 0, 'Medium': 0, 'Low': 0}

    for issue in issues:
        f          = issue.get('fields', {})
        status_raw = f.get('status', {}).get('name', '').lower()
        if any(s in status_raw for s in ['done', 'closed', 'resolved']):
            done += 1
        elif any(s in status_raw for s in ['progress', 'review']):
            in_prog += 1
        else:
            todo += 1
        pri = f.get('priority', {}).get('name', 'Medium')
        if pri in priority_dist:
            priority_dist[pri] += 1
        due = f.get('duedate')
        if due and 'done' not in status_raw:
            try:
                if datetime.strptime(due, '%Y-%m-%d') < datetime.now():
                    overdue_count += 1
            except Exception:
                pass

    completion_pct = round(done / total * 100, 1) if total > 0 else 0
    # Simple delivery confidence: penalise for overdues and high-priority open items
    confidence = max(0, min(100, completion_pct - overdue_count * 5 - priority_dist.get('Critical', 0) * 8))

    payload = {
        'project_key':      pk,
        'project_name':     pk,
        'total_issues':     total,
        'completed':        done,
        'in_progress':      in_prog,
        'todo':             todo,
        'overdue_count':    overdue_count,
        'completion_pct':   completion_pct,
        'confidence':       round(confidence, 1),
        'priority_dist':    priority_dist,
        'last_updated':     datetime.utcnow().strftime('%d %b %Y, %H:%M UTC'),
        'label':            share.label,
        'expires_at':       share.expires_at.isoformat() if share.expires_at else None,
    }

    # Optionally include risk summary
    if share.show_risks:
        risks = []
        for issue in issues:
            f     = issue.get('fields', {})
            score = RiskAnalyzer.calculate_risk_score(issue)
            if score >= 55:
                risks.append({
                    'key':      issue.get('key'),
                    'summary':  f.get('summary', ''),
                    'priority': f.get('priority', {}).get('name', 'Medium'),
                    'score':    score,
                    'impact':   RiskAnalyzer.get_impact(score),
                })
        payload['top_risks'] = sorted(risks, key=lambda x: -x['score'])[:5]

    # Optionally include team workload (owner may hide this)
    if share.show_team:
        team = {}
        for issue in issues:
            f = issue.get('fields', {})
            a = (f.get('assignee') or {}).get('displayName', 'Unassigned')
            team[a] = team.get(a, 0) + 1
        payload['team_workload'] = [{'name': k, 'count': v} for k, v in sorted(team.items(), key=lambda x: -x[1])]

    return jsonify(payload), 200
