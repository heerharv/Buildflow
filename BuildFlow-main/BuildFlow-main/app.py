"""
UNCIA v3 - Multi-User Project Risk Management Dashboard
Complete Flask backend with authentication, database, and Jira integration
"""

import os
import requests
import json
import secrets
from datetime import datetime, timedelta
from functools import wraps
from requests.exceptions import RequestException

# Layer 2 — AI Brain (graceful if anthropic not installed)
try:
    import brain_engine
    BRAIN_AVAILABLE = True
except ImportError:
    BRAIN_AVAILABLE = False

from flask import Flask, jsonify, request, session, send_from_directory, redirect
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app, supports_credentials=True)

# Configuration
db_url = os.getenv('DATABASE_URL', 'sqlite:///uncia.db')
if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-change-in-production')
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = os.getenv('FLASK_ENV') == 'production'
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['USE_MOCK_JIRA'] = os.getenv('USE_MOCK_JIRA', 'false').lower() in ('1', 'true', 'yes', 'on')

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'serve_index'

@login_manager.unauthorized_handler
def handle_unauthorized():
    # API callers must receive 401 JSON, not HTML redirects, otherwise
    # frontend auth checks can mis-detect session state and bounce pages.
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Unauthorized', 'code': 401}), 401
    return redirect('/')
# ===== DATABASE MODELS =====

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    jira_domain = db.Column(db.String(255), nullable=True)
    jira_email = db.Column(db.String(120), nullable=True)
    jira_api_token = db.Column(db.String(255), nullable=True)
    teams_webhook_url = db.Column(db.String(500), nullable=True)
    default_project = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    risk_items = db.relationship('RiskItem', backref='user', lazy='dynamic', cascade='all, delete-orphan')
    cached_projects = db.relationship('CachedProject', backref='user', lazy='dynamic', cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def has_jira_configured(self):
        return bool(self.jira_domain and self.jira_email and self.jira_api_token)

    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'email': self.email,
            'has_jira': self.has_jira_configured(),
            'default_project': self.default_project,
            'created_at': self.created_at.isoformat()
        }


class CachedProject(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    project_key = db.Column(db.String(50), nullable=False)
    project_name = db.Column(db.String(255), nullable=False)
    project_id = db.Column(db.String(50), nullable=True)
    cached_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('user_id', 'project_key'),)

    def to_dict(self):
        return {'key': self.project_key, 'name': self.project_name, 'id': self.project_id}


class RiskItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    issue_key = db.Column(db.String(50), nullable=False)
    project_key = db.Column(db.String(50), nullable=False)
    summary = db.Column(db.String(500), nullable=True)
    risk_category = db.Column(db.String(50), nullable=True)
    risk_score = db.Column(db.Float, default=0.0)
    impact_level = db.Column(db.String(20), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('user_id', 'issue_key'),)

    def to_dict(self):
        return {
            'issue_key': self.issue_key,
            'summary': self.summary,
            'category': self.risk_category,
            'score': self.risk_score,
            'impact': self.impact_level
        }


class DashboardCache(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    project_key = db.Column(db.String(50), nullable=False)
    total_issues = db.Column(db.Integer, default=0)
    completed_issues = db.Column(db.Integer, default=0)
    in_progress_issues = db.Column(db.Integer, default=0)
    todo_issues = db.Column(db.Integer, default=0)
    overdue_count = db.Column(db.Integer, default=0)
    total_risks = db.Column(db.Integer, default=0)
    critical_risks = db.Column(db.Integer, default=0)
    cached_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('user_id', 'project_key'),)


class ClientShare(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    project_key = db.Column(db.String(50), nullable=False)
    token = db.Column(db.String(128), unique=True, nullable=False)
    label = db.Column(db.String(200), nullable=True)
    show_risks = db.Column(db.Boolean, default=True)
    show_team = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)

    def is_valid(self):
        return not (self.expires_at and datetime.utcnow() > self.expires_at)

    def to_dict(self):
        return {
            'id': self.id,
            'project_key': self.project_key,
            'token': self.token,
            'label': self.label,
            'show_risks': self.show_risks,
            'show_team': self.show_team,
            'created_at': self.created_at.isoformat(),
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'share_url': f'/share/{self.token}'
        }


# ===== LAYER 2 — INSIGHT CACHE MODEL =====

class InsightCache(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    project_key = db.Column(db.String(50), nullable=False)
    insights_json = db.Column(db.Text, nullable=False)
    health_score = db.Column(db.Integer, default=50)
    issue_count = db.Column(db.Integer, default=0)
    signal_count = db.Column(db.Integer, default=0)
    model_used = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'project_key': self.project_key,
            'health_score': self.health_score,
            'issue_count': self.issue_count,
            'signal_count': self.signal_count,
            'model_used': self.model_used,
            'created_at': self.created_at.isoformat(),
        }


class Signal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    source_type = db.Column(db.String(50), nullable=False)  # interview, support, feedback, analytics, other
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'source_type': self.source_type,
            'content': self.content,
            'created_at': self.created_at.isoformat(),
        }

class SpecDocument(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    project_key = db.Column(db.String(50), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    content = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(50), default='Draft') # Draft, Approved
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'project_key': self.project_key,
            'title': self.title,
            'content': self.content,
            'status': self.status,
            'created_at': self.created_at.isoformat()
        }


class GeneratedTicket(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    spec_id = db.Column(db.Integer, db.ForeignKey('spec_document.id'), nullable=False)
    type = db.Column(db.String(50), nullable=False) # Epic, Story, Task
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=False)
    acceptance_criteria = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(50), default='Pending') # Pending, Pushed
    jira_key = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'spec_id': self.spec_id,
            'type': self.type,
            'title': self.title,
            'description': self.description,
            'acceptance_criteria': self.acceptance_criteria,
            'status': self.status,
            'jira_key': self.jira_key,
            'created_at': self.created_at.isoformat()
        }


# ===== LOGIN MANAGER =====

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ===== STATIC FILE SERVING =====

@app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

@app.route('/<path:filename>')
def serve_static(filename):
    return send_from_directory('.', filename)


# ===== ERROR HANDLERS =====

@app.errorhandler(401)
def unauthorized(e):
    return jsonify({'error': 'Unauthorized', 'code': 401}), 401

@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Not found', 'code': 404}), 404

@app.errorhandler(500)
def internal_error(e):
    db.session.rollback()
    return jsonify({'error': 'Internal server error', 'code': 500}), 500


# ===== AUTH ROUTES =====

@app.route('/api/auth/register', methods=['POST'])
def register():
    data = request.get_json()
    if not data or not all(k in data for k in ['username', 'password', 'email']):
        return jsonify({'error': 'Missing required fields'}), 400

    username = data['username'].strip()
    email = data['email'].strip().lower()
    password = data['password']

    if len(username) < 3:
        return jsonify({'error': 'Username must be at least 3 characters'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({'error': 'Username already exists'}), 409
    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'Email already exists'}), 409

    user = User(username=username, email=email)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    login_user(user)
    return jsonify({'message': 'Account created', 'user': user.to_dict()}), 201


@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.get_json()
    if not data or not data.get('username') or not data.get('password'):
        return jsonify({'error': 'Missing username or password'}), 400

    user = User.query.filter_by(username=data['username']).first()
    if user and user.check_password(data['password']):
        login_user(user, remember=data.get('remember', False))
        return jsonify({'message': 'Login successful', 'user': user.to_dict()}), 200

    return jsonify({'error': 'Invalid username or password'}), 401


@app.route('/api/auth/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return jsonify({'message': 'Logged out'}), 200


@app.route('/api/auth/me', methods=['GET'])
@login_required
def get_current_user():
    user_data = current_user.to_dict()
    user_data['has_jira'] = current_user.has_jira_configured() or jira_oauth_configured()
    user_data['jira_auth_mode'] = 'oauth' if jira_oauth_configured() else ('token' if current_user.has_jira_configured() else 'none')
    return jsonify({'user': user_data}), 200


# ===== SETTINGS ROUTES =====

@app.route('/api/settings/jira', methods=['GET'])
@login_required
def get_jira_settings():
    oauth_site = session.get('jira_oauth_site', '')
    return jsonify({
        'jira_domain': current_user.jira_domain or '',
        'jira_email': current_user.jira_email or '',
        'has_token': bool(current_user.jira_api_token),
        'teams_webhook': bool(current_user.teams_webhook_url),
        'oauth_connected': jira_oauth_configured(),
        'oauth_site': oauth_site
    }), 200


@app.route('/api/settings/jira', methods=['POST'])
@login_required
def save_jira_settings():
    data = request.get_json()

    domain = data.get('jira_domain', '').strip()
    email = data.get('jira_email', '').strip()
    token = data.get('jira_api_token', '').strip()

    # Handle partial updates (e.g. teams only)
    if domain or email or token:
        if use_mock_jira():
            if domain:
                current_user.jira_domain = domain.replace('https://', '').replace('http://', '')
            if email:
                current_user.jira_email = email
            if token:
                current_user.jira_api_token = token
            db.session.commit()
            return jsonify({
                'message': 'Settings saved (mock Jira mode)',
                'has_jira': True,
                'mock_mode': True
            }), 200

        if domain:
            domain = domain.replace('https://', '').replace('http://', '')
            current_user.jira_domain = domain
        if email:
            current_user.jira_email = email
        if token:
            current_user.jira_api_token = token

        # Validate if we have full credentials
        if current_user.has_jira_configured():
            try:
                response = jira_get('/myself')
                if response.status_code != 200:
                    db.session.commit()
                    return jsonify({
                        'error': 'Jira credentials saved, but validation failed — check domain, email, and API token',
                        'has_jira': current_user.has_jira_configured(),
                        'jira_status': response.status_code
                    }), 401
            except RequestException as e:
                db.session.commit()
                return jsonify({'error': f'Cannot reach Jira: {str(e)}'}), 502

    if 'teams_webhook_url' in data and data['teams_webhook_url']:
        current_user.teams_webhook_url = data['teams_webhook_url'].strip()

    db.session.commit()
    return jsonify({'message': 'Settings saved', 'has_jira': current_user.has_jira_configured()}), 200


@app.route('/api/jira/oauth/start', methods=['GET'])
@login_required
def jira_oauth_start():
    client_id = os.getenv('JIRA_OAUTH_CLIENT_ID', '').strip()
    redirect_uri = os.getenv('JIRA_OAUTH_REDIRECT_URI', '').strip() or f"{request.host_url.rstrip('/')}/oauth/callback"
    if not client_id:
        return jsonify({'error': 'JIRA_OAUTH_CLIENT_ID is not configured'}), 500

    state = secrets.token_urlsafe(24)
    session['jira_oauth_state'] = state
    session['jira_oauth_next'] = request.args.get('next', '/settings.html')
    scope = 'read:jira-work read:jira-user'
    params = {
        'audience': 'api.atlassian.com',
        'client_id': client_id,
        'scope': scope,
        'redirect_uri': redirect_uri,
        'state': state,
        'response_type': 'code',
        'prompt': 'consent'
    }
    auth_url = requests.Request('GET', 'https://auth.atlassian.com/authorize', params=params).prepare().url
    return jsonify({'auth_url': auth_url}), 200


@app.route('/oauth/callback', methods=['GET'])
@login_required
def jira_oauth_callback():
    code = request.args.get('code')
    state = request.args.get('state')
    next_path = session.get('jira_oauth_next', '/settings.html')
    expected_state = session.get('jira_oauth_state')
    session.pop('jira_oauth_state', None)
    session.pop('jira_oauth_next', None)

    if not code or not state or state != expected_state:
        return redirect('/settings.html?jira_oauth=error')

    client_id = os.getenv('JIRA_OAUTH_CLIENT_ID', '').strip()
    client_secret = os.getenv('JIRA_OAUTH_CLIENT_SECRET', '').strip()
    redirect_uri = os.getenv('JIRA_OAUTH_REDIRECT_URI', '').strip() or f"{request.host_url.rstrip('/')}/oauth/callback"
    if not client_id or not client_secret:
        return redirect('/settings.html?jira_oauth=error')

    try:
        token_res = requests.post(
            'https://auth.atlassian.com/oauth/token',
            json={
                'grant_type': 'authorization_code',
                'client_id': client_id,
                'client_secret': client_secret,
                'code': code,
                'redirect_uri': redirect_uri
            },
            headers={'Content-Type': 'application/json'},
            timeout=20
        )
        if token_res.status_code != 200:
            return redirect('/settings.html?jira_oauth=error')
        token_data = token_res.json()
        access_token = token_data.get('access_token')
        if not access_token:
            return redirect('/settings.html?jira_oauth=error')

        resources_res = requests.get(
            'https://api.atlassian.com/oauth/token/accessible-resources',
            headers={'Authorization': f'Bearer {access_token}', 'Accept': 'application/json'},
            timeout=20
        )
        if resources_res.status_code != 200:
            return redirect('/settings.html?jira_oauth=error')
        resources = resources_res.json()
        if not resources:
            return redirect('/settings.html?jira_oauth=error')

        selected = None
        configured_domain = (current_user.jira_domain or '').lower()
        for resource in resources:
            resource_url = (resource.get('url') or '').lower()
            if configured_domain and configured_domain in resource_url:
                selected = resource
                break
        if not selected:
            selected = resources[0]

        session['jira_oauth_access_token'] = access_token
        session['jira_oauth_cloud_id'] = selected.get('id')
        session['jira_oauth_site'] = selected.get('url', '')
        session['jira_oauth_expires_at'] = int(datetime.utcnow().timestamp()) + int(token_data.get('expires_in', 3600))

        selected_url = selected.get('url', '').replace('https://', '').replace('http://', '').strip('/')
        if selected_url and not current_user.jira_domain:
            current_user.jira_domain = selected_url
            db.session.commit()
    except RequestException:
        return redirect('/settings.html?jira_oauth=error')

    if not next_path.startswith('/'):
        next_path = '/settings.html'
    return redirect(f'{next_path}?jira_oauth=success')


@app.route('/api/settings/preferences', methods=['POST'])
@login_required
def save_preferences():
    data = request.get_json()
    if 'default_project' in data:
        current_user.default_project = data['default_project']
    db.session.commit()
    return jsonify({'message': 'Preferences saved'}), 200


# ===== JIRA HELPERS =====

def get_user_jira_auth():
    return HTTPBasicAuth(current_user.jira_email, current_user.jira_api_token)

def jira_oauth_configured():
    token = session.get('jira_oauth_access_token')
    cloud_id = session.get('jira_oauth_cloud_id')
    expires_at = int(session.get('jira_oauth_expires_at', 0))
    if not token or not cloud_id:
        return False
    if expires_at and int(datetime.utcnow().timestamp()) >= expires_at:
        session.pop('jira_oauth_access_token', None)
        session.pop('jira_oauth_cloud_id', None)
        session.pop('jira_oauth_site', None)
        session.pop('jira_oauth_expires_at', None)
        return False
    return True

def get_jira_headers():
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if jira_oauth_configured():
        headers["Authorization"] = f"Bearer {session.get('jira_oauth_access_token')}"
    return headers

def use_mock_jira():
    return app.config.get('USE_MOCK_JIRA', False)

def mock_projects():
    return [
        {'key': 'UNCIA', 'name': 'UNCIA Platform', 'id': '10001'},
        {'key': 'WEB', 'name': 'Web Experience', 'id': '10002'},
        {'key': 'MOB', 'name': 'Mobile App', 'id': '10003'},
        {'key': 'OPS', 'name': 'Operations Reliability', 'id': '10004'},
        {'key': 'SEC', 'name': 'Security Program', 'id': '10005'},
        {'key': 'DATA', 'name': 'Data Platform', 'id': '10006'}
    ]

def mock_project_blueprints():
    return {
        'UNCIA': {'focus': 'platform reliability and auth hardening', 'team': ['Heerha', 'Sindhu', 'Alex', 'Priya', 'Ravi'], 'status_shift': 0, 'priority_shift': 0, 'due_offset': 0},
        'WEB': {'focus': 'checkout and frontend performance', 'team': ['Alex', 'Priya', 'Ravi', 'Heerha'], 'status_shift': 1, 'priority_shift': 1, 'due_offset': -1},
        'MOB': {'focus': 'mobile release quality and crash reduction', 'team': ['Sindhu', 'Heerha', 'Alex'], 'status_shift': 2, 'priority_shift': -1, 'due_offset': 2},
        'OPS': {'focus': 'incident response and service uptime', 'team': ['Ravi', 'Sindhu', 'Heerha'], 'status_shift': 1, 'priority_shift': 2, 'due_offset': -3},
        'SEC': {'focus': 'security controls and token lifecycle', 'team': ['Heerha', 'Priya', 'Sindhu'], 'status_shift': 0, 'priority_shift': 2, 'due_offset': -2},
        'DATA': {'focus': 'data quality, lineage, and governance', 'team': ['Ravi', 'Priya', 'Alex'], 'status_shift': -1, 'priority_shift': 0, 'due_offset': 3}
    }

def mock_issue_templates():
    # Structured templates keep status/priority mix realistic while remaining deterministic.
    return [
        {'id': 101, 'summary': 'Fix login timeout under load', 'status': 'In Progress', 'assignee': 'Heerha', 'priority': 'High', 'type': 'Bug', 'due_days': 2, 'description': 'Users intermittently hit timeout during peak traffic.', 'subtasks': 5},
        {'id': 102, 'summary': 'Implement audit export endpoint', 'status': 'To Do', 'assignee': None, 'priority': 'Medium', 'type': 'Task', 'due_days': 5, 'description': 'Compliance team requires weekly audit export.', 'subtasks': 4},
        {'id': 103, 'summary': 'Resolve payment webhook signature vulnerability', 'status': 'Done', 'assignee': 'Sindhu', 'priority': 'Critical', 'type': 'Bug', 'due_days': -3, 'description': 'Security review flagged weak signature verification.', 'subtasks': 6},
        {'id': 104, 'summary': 'Improve dashboard render performance', 'status': 'Review', 'assignee': 'Alex', 'priority': 'High', 'type': 'Story', 'due_days': -1, 'description': 'Reduce initial render cost and API waterfall.', 'subtasks': 5},
        {'id': 105, 'summary': 'Migrate legacy API endpoints to v2', 'status': 'In Progress', 'assignee': 'Priya', 'priority': 'High', 'type': 'Story', 'due_days': 6, 'description': 'Deprecate v1 endpoints and migrate all clients.', 'subtasks': 7},
        {'id': 106, 'summary': 'Backfill analytics events for billing funnel', 'status': 'To Do', 'assignee': 'Ravi', 'priority': 'Medium', 'type': 'Task', 'due_days': 12, 'description': 'Missing events prevent conversion analysis in BI dashboards.', 'subtasks': 4},
        {'id': 107, 'summary': 'Investigate intermittent 502 errors in gateway', 'status': 'Review', 'assignee': 'Heerha', 'priority': 'Critical', 'type': 'Bug', 'due_days': -2, 'description': 'Production incidents correlated with high traffic bursts.', 'subtasks': 6},
        {'id': 108, 'summary': 'Harden webhook retry logic for third-party outages', 'status': 'Testing', 'assignee': 'Alex', 'priority': 'High', 'type': 'Story', 'due_days': 1, 'description': 'Current retry policy causes data drift during vendor downtime.', 'subtasks': 5},
        {'id': 109, 'summary': 'Document incident response runbook', 'status': 'Done', 'assignee': 'Sindhu', 'priority': 'Low', 'type': 'Task', 'due_days': -5, 'description': 'Operational readiness documentation for on-call engineers.', 'subtasks': 3},
        {'id': 110, 'summary': 'Add role-based access control for admin tools', 'status': 'To Do', 'assignee': None, 'priority': 'Highest', 'type': 'Epic', 'due_days': 14, 'description': 'Security and compliance requirement for privileged operations.', 'subtasks': 8},
        {'id': 111, 'summary': 'Optimize dashboard SQL query latency', 'status': 'In Progress', 'assignee': 'Ravi', 'priority': 'High', 'type': 'Bug', 'due_days': 3, 'description': 'Slow database response impacting core dashboard rendering.', 'subtasks': 5},
        {'id': 112, 'summary': 'Validate GDPR deletion workflow end-to-end', 'status': 'To Do', 'assignee': 'Priya', 'priority': 'Medium', 'type': 'Task', 'due_days': 9, 'description': 'Compliance validation for deletion requests across integrated systems.', 'subtasks': 4},
        {'id': 113, 'summary': 'Fix token rotation bug in auth middleware', 'status': 'Done', 'assignee': 'Heerha', 'priority': 'Critical', 'type': 'Bug', 'due_days': -4, 'description': 'Token refresh occasionally invalidates active sessions.', 'subtasks': 6},
        {'id': 114, 'summary': 'Create synthetic monitoring for checkout flow', 'status': 'In Progress', 'assignee': 'Alex', 'priority': 'Medium', 'type': 'Task', 'due_days': 4, 'description': 'Add proactive alerts for checkout degradation and failures.', 'subtasks': 5}
    ]

def build_mock_subtasks(project_key, template, now, assignee, team):
    count = int(template.get('subtasks', 3))
    statuses = ['To Do', 'In Progress', 'Review', 'Done']
    subtasks = []
    for i in range(count):
        status = statuses[(template['id'] + i) % len(statuses)]
        owner = assignee or (team[(template['id'] + i) % len(team)] if team else None)
        subtasks.append({
            'key': f"{project_key}-{template['id']}-ST{i+1}",
            'fields': {
                'summary': f"Subtask {i+1}: {template['summary']}",
                'status': {'name': status},
                'assignee': {'displayName': owner} if owner else None,
                'duedate': (now + timedelta(days=template['due_days'] + i + 1)).strftime('%Y-%m-%d')
            }
        })
    return subtasks

def build_mock_issue(project_key, template, now, blueprint):
    status_cycle = ['To Do', 'In Progress', 'Review', 'Testing', 'Done']
    priority_cycle = ['Low', 'Medium', 'High', 'Highest', 'Critical']
    base_status = template['status']
    base_priority = template['priority']
    status_idx = status_cycle.index(base_status) if base_status in status_cycle else 0
    priority_idx = priority_cycle.index(base_priority) if base_priority in priority_cycle else 1
    status_shift = int(blueprint.get('status_shift', 0))
    priority_shift = int(blueprint.get('priority_shift', 0))
    due_offset = int(blueprint.get('due_offset', 0))
    derived_status = status_cycle[(status_idx + status_shift) % len(status_cycle)]
    derived_priority = priority_cycle[(priority_idx + priority_shift) % len(priority_cycle)]
    derived_due_days = int(template['due_days']) + due_offset

    assignee = template['assignee']
    team = blueprint.get('team', [])
    if assignee is None and team:
        assignee = team[template['id'] % len(team)] if template['id'] % 2 == 0 else None
    subtasks = build_mock_subtasks(project_key, template, now, assignee, team)
    return {
        'key': f"{project_key}-{template['id']}",
        'fields': {
            'summary': f"{template['summary']} ({project_key})",
            'status': {'name': derived_status},
            'assignee': {'displayName': assignee} if assignee else None,
            'priority': {'name': derived_priority},
            'issuetype': {'name': template['type']},
            'duedate': (now + timedelta(days=derived_due_days)).strftime('%Y-%m-%d'),
            'description': f"{template['description']} Project focus: {blueprint.get('focus', 'general delivery')}.",
            'created': now.isoformat(),
            'updated': now.isoformat(),
            'subtasks': subtasks
        },
        'subtasks': subtasks
    }

def mock_workflow_definition():
    return {
        'name': 'UNCIA Delivery Workflow',
        'stages': [
            {'id': 'todo', 'label': 'To Do', 'category': 'backlog', 'wip_limit': 999},
            {'id': 'in_progress', 'label': 'In Progress', 'category': 'active', 'wip_limit': 8},
            {'id': 'review', 'label': 'Review', 'category': 'active', 'wip_limit': 6},
            {'id': 'testing', 'label': 'Testing', 'category': 'active', 'wip_limit': 5},
            {'id': 'done', 'label': 'Done', 'category': 'complete', 'wip_limit': 999}
        ],
        'transitions': [
            {'from': 'To Do', 'to': 'In Progress'},
            {'from': 'In Progress', 'to': 'Review'},
            {'from': 'Review', 'to': 'Testing'},
            {'from': 'Testing', 'to': 'Done'},
            {'from': 'Review', 'to': 'In Progress', 'type': 'rework'},
            {'from': 'Testing', 'to': 'In Progress', 'type': 'defect'}
        ]
    }

def build_mock_issue_history(issue, idx):
    fields = issue.get('fields', {})
    created = datetime.fromisoformat(fields.get('created'))
    status = fields.get('status', {}).get('name', 'To Do')
    key = issue.get('key')
    path = ['To Do']
    if status in ['In Progress', 'Review', 'Testing', 'Done']:
        path.append('In Progress')
    if status in ['Review', 'Testing', 'Done']:
        path.append('Review')
    if status in ['Testing', 'Done']:
        path.append('Testing')
    if status == 'Done':
        path.append('Done')
    events = []
    for i, step in enumerate(path):
        events.append({
            'issue_key': key,
            'event': 'status_change',
            'to_status': step,
            'at': (created + timedelta(days=i * 2 + (idx % 2))).isoformat()
        })
    events.append({
        'issue_key': key,
        'event': 'assignee_update',
        'assignee': fields.get('assignee', {}).get('displayName', 'Unassigned') if fields.get('assignee') else 'Unassigned',
        'at': (created + timedelta(days=1)).isoformat()
    })
    return events

def build_mock_deep_dive(project_key, issues):
    status_counts = {'To Do': 0, 'In Progress': 0, 'Review': 0, 'Testing': 0, 'Done': 0}
    priority_counts = {'Critical': 0, 'Highest': 0, 'High': 0, 'Medium': 0, 'Low': 0, 'Lowest': 0}
    owner_workload = {}
    overdue_by_owner = {}
    phase_map = {
        'Foundation': ['To Do'],
        'Execution': ['In Progress', 'Review', 'Testing'],
        'Release': ['Done']
    }
    phase_breakdown = {'Foundation': 0, 'Execution': 0, 'Release': 0}
    milestones = []
    trend_14d = []
    risk_hotspots = []
    subtask_status_counts = {'To Do': 0, 'In Progress': 0, 'Review': 0, 'Done': 0}
    subtask_total = 0
    workflow_cycle_time = {'To Do': 0, 'In Progress': 0, 'Review': 0, 'Testing': 0, 'Done': 0}
    issue_timeline = []

    for idx, issue in enumerate(issues):
        fields = issue.get('fields', {})
        status = fields.get('status', {}).get('name', 'To Do')
        priority = fields.get('priority', {}).get('name', 'Medium')
        assignee = fields.get('assignee', {}).get('displayName', 'Unassigned') if fields.get('assignee') else 'Unassigned'
        due_date = fields.get('duedate')
        issue_subtasks = fields.get('subtasks', issue.get('subtasks', []))
        subtask_total += len(issue_subtasks)
        issue_timeline.extend(build_mock_issue_history(issue, idx))
        workflow_cycle_time['To Do'] += 1 + (idx % 2)
        workflow_cycle_time['In Progress'] += 2 + (idx % 3)
        workflow_cycle_time['Review'] += 1 + (idx % 2)
        workflow_cycle_time['Testing'] += 1 if status in ['Testing', 'Done'] else 0
        workflow_cycle_time['Done'] += 0 if status != 'Done' else 1
        for st in issue_subtasks:
            st_status = st.get('fields', {}).get('status', {}).get('name', 'To Do')
            subtask_status_counts[st_status] = subtask_status_counts.get(st_status, 0) + 1

        status_counts[status] = status_counts.get(status, 0) + 1
        priority_counts[priority] = priority_counts.get(priority, 0) + 1
        owner_workload[assignee] = owner_workload.get(assignee, 0) + 1

        for phase, statuses in phase_map.items():
            if status in statuses:
                phase_breakdown[phase] += 1
                break

        if due_date and status != 'Done':
            try:
                due = datetime.strptime(due_date, '%Y-%m-%d')
                if due < datetime.now():
                    overdue_by_owner[assignee] = overdue_by_owner.get(assignee, 0) + 1
            except Exception:
                pass

    # Deterministic trend signal for last 14 days
    base = max(1, len(issues) // 5)
    for i in range(14):
        day = (datetime.utcnow() - timedelta(days=(13 - i))).strftime('%Y-%m-%d')
        completed = max(0, base + (i // 3) - 1)
        created = base + (i % 4)
        trend_14d.append({'date': day, 'created': created, 'completed': completed})

    milestones = [
        {'name': 'Architecture Baseline', 'status': 'done', 'target_date': (datetime.utcnow() - timedelta(days=12)).strftime('%Y-%m-%d')},
        {'name': 'Core Delivery Sprint', 'status': 'active', 'target_date': (datetime.utcnow() + timedelta(days=7)).strftime('%Y-%m-%d')},
        {'name': 'QA Signoff', 'status': 'upcoming', 'target_date': (datetime.utcnow() + timedelta(days=14)).strftime('%Y-%m-%d')},
        {'name': 'Production Rollout', 'status': 'upcoming', 'target_date': (datetime.utcnow() + timedelta(days=21)).strftime('%Y-%m-%d')}
    ]

    top_owners = sorted(owner_workload.items(), key=lambda x: x[1], reverse=True)[:5]
    issue_timeline.sort(key=lambda x: x.get('at'))
    workflow = mock_workflow_definition()
    workflow_metrics = {
        'avg_cycle_time_days': {
            'In Progress': round(workflow_cycle_time['In Progress'] / max(1, len(issues)), 1),
            'Review': round(workflow_cycle_time['Review'] / max(1, len(issues)), 1),
            'Testing': round(workflow_cycle_time['Testing'] / max(1, len(issues)), 1)
        },
        'throughput_last_14d': sum([d['completed'] for d in trend_14d]),
        'wip_by_stage': {
            'In Progress': status_counts.get('In Progress', 0),
            'Review': status_counts.get('Review', 0),
            'Testing': status_counts.get('Testing', 0)
        },
        'blocked_items': len([i for i in issues if i.get('fields', {}).get('status', {}).get('name') in ['Review', 'Testing'] and i.get('fields', {}).get('priority', {}).get('name') in ['Critical', 'High']])
    }
    sprint_timeline = [
        {'sprint': 'Sprint 31', 'start': (datetime.utcnow() - timedelta(days=21)).strftime('%Y-%m-%d'), 'end': (datetime.utcnow() - timedelta(days=8)).strftime('%Y-%m-%d'), 'goal': 'Stabilize core auth and dashboard APIs', 'status': 'completed'},
        {'sprint': 'Sprint 32', 'start': (datetime.utcnow() - timedelta(days=7)).strftime('%Y-%m-%d'), 'end': (datetime.utcnow() + timedelta(days=6)).strftime('%Y-%m-%d'), 'goal': 'Close performance bottlenecks and harden integrations', 'status': 'active'},
        {'sprint': 'Sprint 33', 'start': (datetime.utcnow() + timedelta(days=7)).strftime('%Y-%m-%d'), 'end': (datetime.utcnow() + timedelta(days=20)).strftime('%Y-%m-%d'), 'goal': 'QA signoff and production rollout readiness', 'status': 'planned'}
    ]
    risk_hotspots = [
        {'area': 'Authentication', 'severity': 'high', 'open_items': priority_counts.get('Critical', 0) + priority_counts.get('Highest', 0)},
        {'area': 'Integration', 'severity': 'medium', 'open_items': status_counts.get('Review', 0) + status_counts.get('Testing', 0)},
        {'area': 'Performance', 'severity': 'medium', 'open_items': status_counts.get('In Progress', 0)}
    ]

    return {
        'project_key': project_key,
        'status_breakdown': status_counts,
        'priority_breakdown': priority_counts,
        'phase_breakdown': phase_breakdown,
        'owner_workload': [{'owner': k, 'issues': v, 'overdue': overdue_by_owner.get(k, 0)} for k, v in top_owners],
        'subtask_total': subtask_total,
        'subtask_status_breakdown': subtask_status_counts,
        'subtask_completion_rate': round((subtask_status_counts.get('Done', 0) / subtask_total * 100), 1) if subtask_total else 0.0,
        'workflow': workflow,
        'workflow_metrics': workflow_metrics,
        'issue_timeline': issue_timeline[-40:],
        'sprint_timeline': sprint_timeline,
        'milestones': milestones,
        'trend_14d': trend_14d,
        'risk_hotspots': risk_hotspots
    }

def parse_jira_datetime(value):
    if not value:
        return None
    try:
        normalized = str(value).replace('Z', '+00:00')
        return datetime.fromisoformat(normalized)
    except Exception:
        return None

def build_live_deep_dive(project_key, issues):
    status_counts = {'To Do': 0, 'In Progress': 0, 'Review': 0, 'Testing': 0, 'Done': 0}
    priority_counts = {'Critical': 0, 'Highest': 0, 'High': 0, 'Medium': 0, 'Low': 0, 'Lowest': 0}
    owner_workload = {}
    overdue_by_owner = {}
    phase_map = {
        'Foundation': ['To Do'],
        'Execution': ['In Progress', 'Review', 'Testing'],
        'Release': ['Done']
    }
    phase_breakdown = {'Foundation': 0, 'Execution': 0, 'Release': 0}
    subtask_status_counts = {'To Do': 0, 'In Progress': 0, 'Review': 0, 'Done': 0}
    subtask_total = 0
    issue_timeline = []
    created_trend = {}
    completed_trend = {}
    done_cycle_days = []
    upcoming_due = []

    for issue in issues:
        fields = issue.get('fields', {})
        key = issue.get('key')
        status = fields.get('status', {}).get('name', 'To Do')
        priority = fields.get('priority', {}).get('name', 'Medium')
        assignee = fields.get('assignee', {}).get('displayName', 'Unassigned') if fields.get('assignee') else 'Unassigned'
        due_date = fields.get('duedate')
        created_at = parse_jira_datetime(fields.get('created'))
        updated_at = parse_jira_datetime(fields.get('updated'))
        resolved_at = parse_jira_datetime(fields.get('resolutiondate'))
        issue_subtasks = fields.get('subtasks', issue.get('subtasks', []))

        status_counts[status] = status_counts.get(status, 0) + 1
        priority_counts[priority] = priority_counts.get(priority, 0) + 1
        owner_workload[assignee] = owner_workload.get(assignee, 0) + 1
        subtask_total += len(issue_subtasks)

        for phase, statuses in phase_map.items():
            if status in statuses:
                phase_breakdown[phase] += 1
                break

        for st in issue_subtasks:
            st_status = st.get('fields', {}).get('status', {}).get('name', 'To Do')
            subtask_status_counts[st_status] = subtask_status_counts.get(st_status, 0) + 1

        if created_at:
            created_day = created_at.strftime('%Y-%m-%d')
            created_trend[created_day] = created_trend.get(created_day, 0) + 1
            issue_timeline.append({'issue_key': key, 'event': 'created', 'at': created_at.isoformat()})
        if updated_at and updated_at != created_at:
            issue_timeline.append({'issue_key': key, 'event': 'updated', 'at': updated_at.isoformat()})
        if resolved_at:
            resolved_day = resolved_at.strftime('%Y-%m-%d')
            completed_trend[resolved_day] = completed_trend.get(resolved_day, 0) + 1
            issue_timeline.append({'issue_key': key, 'event': 'resolved', 'at': resolved_at.isoformat()})
            if created_at:
                done_cycle_days.append(max(0, (resolved_at - created_at).days))

        if due_date:
            try:
                due = datetime.strptime(due_date, '%Y-%m-%d')
                if due >= datetime.now():
                    upcoming_due.append((due, issue))
                elif status != 'Done':
                    overdue_by_owner[assignee] = overdue_by_owner.get(assignee, 0) + 1
            except Exception:
                pass

    trend_14d = []
    for i in range(14):
        day = (datetime.utcnow() - timedelta(days=(13 - i))).strftime('%Y-%m-%d')
        trend_14d.append({
            'date': day,
            'created': created_trend.get(day, 0),
            'completed': completed_trend.get(day, 0)
        })

    upcoming_due.sort(key=lambda x: x[0])
    milestones = []
    for due, issue in upcoming_due[:4]:
        fields = issue.get('fields', {})
        status = fields.get('status', {}).get('name', 'To Do')
        status_lower = status.lower()
        state = 'upcoming'
        if any(s in status_lower for s in ['done', 'closed', 'resolved']):
            state = 'done'
        elif any(s in status_lower for s in ['progress', 'review', 'testing']):
            state = 'active'
        milestones.append({
            'name': issue.get('key'),
            'status': state,
            'target_date': due.strftime('%Y-%m-%d')
        })

    top_owners = sorted(owner_workload.items(), key=lambda x: x[1], reverse=True)[:5]
    issue_timeline.sort(key=lambda x: x.get('at'))
    avg_cycle = round(sum(done_cycle_days) / len(done_cycle_days), 1) if done_cycle_days else None
    blocked_items = len([i for i in issues if i.get('fields', {}).get('status', {}).get('name') in ['Review', 'Testing'] and i.get('fields', {}).get('priority', {}).get('name') in ['Critical', 'High']])
    risk_hotspots = [
        {'area': 'Authentication', 'severity': 'high', 'open_items': priority_counts.get('Critical', 0) + priority_counts.get('Highest', 0)},
        {'area': 'Integration', 'severity': 'medium', 'open_items': status_counts.get('Review', 0) + status_counts.get('Testing', 0)},
        {'area': 'Performance', 'severity': 'medium', 'open_items': status_counts.get('In Progress', 0)}
    ]

    return {
        'project_key': project_key,
        'status_breakdown': status_counts,
        'priority_breakdown': priority_counts,
        'phase_breakdown': phase_breakdown,
        'owner_workload': [{'owner': k, 'issues': v, 'overdue': overdue_by_owner.get(k, 0)} for k, v in top_owners],
        'subtask_total': subtask_total,
        'subtask_status_breakdown': subtask_status_counts,
        'subtask_completion_rate': round((subtask_status_counts.get('Done', 0) / subtask_total * 100), 1) if subtask_total else 0.0,
        'workflow': mock_workflow_definition(),
        'workflow_metrics': {
            'avg_cycle_time_days': {
                'In Progress': avg_cycle,
                'Review': None,
                'Testing': None
            },
            'throughput_last_14d': sum([d['completed'] for d in trend_14d]),
            'wip_by_stage': {
                'In Progress': status_counts.get('In Progress', 0),
                'Review': status_counts.get('Review', 0),
                'Testing': status_counts.get('Testing', 0)
            },
            'blocked_items': blocked_items
        },
        'issue_timeline': issue_timeline[-80:],
        'sprint_timeline': [],
        'milestones': milestones,
        'trend_14d': trend_14d,
        'risk_hotspots': risk_hotspots
    }

def mock_issues(project_key):
    now = datetime.utcnow()
    blueprints = mock_project_blueprints()
    blueprint = blueprints.get(project_key, {'focus': 'general product delivery', 'team': ['Heerha', 'Sindhu', 'Alex', 'Priya', 'Ravi']})
    return [build_mock_issue(project_key, t, now, blueprint) for t in mock_issue_templates()]

def jira_request(method, path, params=None, json=None, timeout=15):
    """
    Use a dedicated session that ignores host proxy environment variables.
    """
    if jira_oauth_configured():
        cloud_id = session.get('jira_oauth_cloud_id')
        url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3{path}"
        auth = None
    else:
        url = f"https://{current_user.jira_domain}/rest/api/3{path}"
        auth = get_user_jira_auth()
    req_session = requests.Session()
    req_session.trust_env = False
    return req_session.request(
        method,
        url,
        headers=get_jira_headers(),
        auth=auth,
        params=params,
        json=json,
        timeout=timeout
    )

def jira_get(path, params=None):
    return jira_request('GET', path, params=params)

def jira_post(path, json=None):
    return jira_request('POST', path, json=json)

def jira_search(params=None):
    """
    Jira Cloud is deprecating some legacy search routes in stages.
    Try the classic endpoint first, then fall back to the newer JQL route.
    """
    response = jira_get('/search', params=params)
    if response.status_code == 410:
        response = jira_get('/search/jql', params=params)
    return response

def project_jql(project_key):
    # Quote project key/name so reserved words (e.g. NEW) parse correctly.
    safe_key = str(project_key).replace('"', '\\"')
    return f'project = "{safe_key}"'


# ===== JIRA ROUTES =====

@app.route('/api/jira/test', methods=['GET'])
@login_required
def test_jira_connection():
    if use_mock_jira():
        return jsonify({
            'status': 'success',
            'authenticated_user': current_user.username,
            'email': current_user.email,
            'mock_mode': True
        }), 200

    if not current_user.has_jira_configured() and not jira_oauth_configured():
        return jsonify({'error': 'Jira not configured'}), 400
    try:
        r = jira_get('/myself')
        if r.status_code == 200:
            d = r.json()
            return jsonify({'status': 'success', 'authenticated_user': d.get('displayName'), 'email': d.get('emailAddress')}), 200
        return jsonify({'error': 'Authentication failed', 'jira_status': r.status_code}), 401
    except RequestException as e:
        return jsonify({'error': f'Jira connection error: {str(e)}'}), 502


@app.route('/api/jira/projects', methods=['GET'])
@login_required
def get_jira_projects():
    if use_mock_jira():
        projects = mock_projects()
        CachedProject.query.filter_by(user_id=current_user.id).delete()
        for p in projects:
            db.session.add(CachedProject(
                user_id=current_user.id,
                project_key=p['key'],
                project_name=p['name'],
                project_id=p['id']
            ))
        db.session.commit()
        return jsonify({'projects': projects, 'mock_mode': True}), 200

    if not current_user.has_jira_configured() and not jira_oauth_configured():
        return jsonify({'error': 'Jira not configured'}), 400
    try:
        r = jira_get('/project/search', {'maxResults': 50, 'orderBy': 'name'})
        if r.status_code == 200:
            projects = r.json().get('values', [])
            CachedProject.query.filter_by(user_id=current_user.id).delete()
            for p in projects:
                cached = CachedProject(
                    user_id=current_user.id,
                    project_key=p.get('key'),
                    project_name=p.get('name'),
                    project_id=p.get('id')
                )
                db.session.add(cached)
            db.session.commit()
            return jsonify({'projects': [{'key': p.get('key'), 'name': p.get('name'), 'id': p.get('id')} for p in projects]}), 200
        return jsonify({'error': 'Failed to fetch projects', 'jira_status': r.status_code}), r.status_code
    except RequestException as e:
        return jsonify({'error': f'Jira connection error: {str(e)}'}), 502


@app.route('/api/jira/dashboard/<project_key>', methods=['GET'])
@login_required
def get_dashboard(project_key):
    if use_mock_jira():
        issues = mock_issues(project_key)
        deep_dive = build_mock_deep_dive(project_key, issues)
        total = len(issues)
        completed = in_progress = todo = 0
        overdue = []
        assignee_counts = {}
        priority_counts = {'Critical': 0, 'High': 0, 'Medium': 0, 'Low': 0, 'Lowest': 0, 'Highest': 0}
        for issue in issues:
            fields = issue.get('fields', {})
            status_name = fields.get('status', {}).get('name', '').lower()
            priority_name = fields.get('priority', {}).get('name', 'Medium')
            if any(s in status_name for s in ['done', 'closed', 'resolved', "won't do"]):
                completed += 1
            elif any(s in status_name for s in ['progress', 'review', 'testing']):
                in_progress += 1
            else:
                todo += 1
            assignee = fields.get('assignee', {}).get('displayName', 'Unassigned') if fields.get('assignee') else 'Unassigned'
            assignee_counts[assignee] = assignee_counts.get(assignee, 0) + 1
            if priority_name in priority_counts:
                priority_counts[priority_name] += 1
            due_date = fields.get('duedate')
            if due_date and 'done' not in status_name and 'closed' not in status_name:
                try:
                    due = datetime.strptime(due_date, '%Y-%m-%d')
                    if due < datetime.now():
                        overdue.append({
                            'key': issue.get('key'),
                            'summary': fields.get('summary'),
                            'days_overdue': (datetime.now() - due).days,
                            'assignee': assignee,
                            'priority': priority_name
                        })
                except Exception:
                    pass
        overdue.sort(key=lambda x: x['days_overdue'], reverse=True)
        return jsonify({
            'total_issues': total,
            'completed_issues': completed,
            'in_progress_issues': in_progress,
            'todo_issues': todo,
            'overdue_tasks': overdue,
            'completion_rate': round((completed / total * 100) if total > 0 else 0, 1),
            'assignee_distribution': assignee_counts,
            'priority_distribution': priority_counts,
            'deep_dive': deep_dive,
            'mock_mode': True
        }), 200

    if not current_user.has_jira_configured() and not jira_oauth_configured():
        return jsonify({'error': 'Jira not configured'}), 400
    try:
        r = jira_search({
            'jql': f'{project_jql(project_key)} ORDER BY created DESC',
            'maxResults': 200,
            'fields': 'summary,status,assignee,duedate,priority,issuetype,description,created,updated'
        })
        if r.status_code != 200:
            return jsonify({'error': 'Failed to fetch data', 'jira_status': r.status_code}), r.status_code

        issues = r.json().get('issues', [])
        total = len(issues)
        completed = in_progress = todo = 0
        overdue = []
        assignee_counts = {}
        priority_counts = {'Critical': 0, 'High': 0, 'Medium': 0, 'Low': 0, 'Lowest': 0}

        for issue in issues:
            fields = issue.get('fields', {})
            status_name = fields.get('status', {}).get('name', '').lower()
            priority_name = fields.get('priority', {}).get('name', 'Medium')

            if any(s in status_name for s in ['done', 'closed', 'resolved', "won't do"]):
                completed += 1
            elif any(s in status_name for s in ['progress', 'review', 'testing']):
                in_progress += 1
            else:
                todo += 1

            # Assignee tracking
            assignee = fields.get('assignee', {}).get('displayName', 'Unassigned') if fields.get('assignee') else 'Unassigned'
            assignee_counts[assignee] = assignee_counts.get(assignee, 0) + 1

            # Priority tracking
            if priority_name in priority_counts:
                priority_counts[priority_name] += 1

            # Overdue check
            due_date = fields.get('duedate')
            if due_date and 'done' not in status_name and 'closed' not in status_name:
                try:
                    due = datetime.strptime(due_date, '%Y-%m-%d')
                    if due < datetime.now():
                        days_overdue = (datetime.now() - due).days
                        overdue.append({
                            'key': issue.get('key'),
                            'summary': fields.get('summary'),
                            'days_overdue': days_overdue,
                            'assignee': assignee,
                            'priority': priority_name
                        })
                except:
                    pass

        overdue.sort(key=lambda x: x['days_overdue'], reverse=True)

        # Cache
        cache = DashboardCache.query.filter_by(user_id=current_user.id, project_key=project_key).first()
        if not cache:
            cache = DashboardCache(user_id=current_user.id, project_key=project_key)
            db.session.add(cache)
        cache.total_issues = total
        cache.completed_issues = completed
        cache.in_progress_issues = in_progress
        cache.todo_issues = todo
        cache.overdue_count = len(overdue)
        cache.cached_at = datetime.utcnow()
        db.session.commit()

        return jsonify({
            'total_issues': total,
            'completed_issues': completed,
            'in_progress_issues': in_progress,
            'todo_issues': todo,
            'overdue_tasks': overdue,
            'completion_rate': round((completed / total * 100) if total > 0 else 0, 1),
            'assignee_distribution': assignee_counts,
            'priority_distribution': priority_counts
        }), 200
    except RequestException as e:
        return jsonify({'error': f'Jira connection error: {str(e)}'}), 502


@app.route('/api/jira/board/<project_key>', methods=['GET'])
@login_required
def get_kanban_board(project_key):
    if use_mock_jira():
        board = {'To Do': [], 'In Progress': [], 'Done': []}
        for issue in mock_issues(project_key):
            fields = issue.get('fields', {})
            status = fields.get('status', {}).get('name', '').lower()
            due_date = fields.get('duedate')
            is_overdue = False
            if due_date:
                try:
                    is_overdue = datetime.strptime(due_date, '%Y-%m-%d') < datetime.now()
                except Exception:
                    pass
            card = {
                'key': issue.get('key'),
                'summary': fields.get('summary'),
                'assignee': fields.get('assignee', {}).get('displayName', 'Unassigned') if fields.get('assignee') else 'Unassigned',
                'priority': fields.get('priority', {}).get('name', 'Medium'),
                'type': fields.get('issuetype', {}).get('name', 'Task'),
                'due_date': due_date,
                'is_overdue': is_overdue
            }
            if 'progress' in status or 'review' in status or 'testing' in status:
                board['In Progress'].append(card)
            elif 'done' in status or 'closed' in status or 'resolved' in status:
                board['Done'].append(card)
            else:
                board['To Do'].append(card)
        return jsonify({'board': board, 'mock_mode': True}), 200

    if not current_user.has_jira_configured() and not jira_oauth_configured():
        return jsonify({'error': 'Jira not configured'}), 400
    try:
        r = jira_search({
            'jql': f'{project_jql(project_key)} ORDER BY priority DESC',
            'maxResults': 200,
            'fields': 'summary,status,assignee,priority,issuetype,duedate,key'
        })
        if r.status_code != 200:
            return jsonify({'error': 'Failed to fetch board'}), r.status_code

        issues = r.json().get('issues', [])
        board = {'To Do': [], 'In Progress': [], 'Done': []}

        for issue in issues:
            fields = issue.get('fields', {})
            status = fields.get('status', {}).get('name', 'To Do').lower()
            due_date = fields.get('duedate')
            is_overdue = False
            if due_date:
                try:
                    is_overdue = datetime.strptime(due_date, '%Y-%m-%d') < datetime.now()
                except:
                    pass

            card = {
                'key': issue.get('key'),
                'summary': fields.get('summary'),
                'assignee': fields.get('assignee', {}).get('displayName', 'Unassigned') if fields.get('assignee') else 'Unassigned',
                'priority': fields.get('priority', {}).get('name', 'Medium'),
                'type': fields.get('issuetype', {}).get('name', 'Task'),
                'due_date': due_date,
                'is_overdue': is_overdue
            }

            if 'progress' in status or 'review' in status or 'testing' in status:
                board['In Progress'].append(card)
            elif 'done' in status or 'closed' in status or 'resolved' in status:
                board['Done'].append(card)
            else:
                board['To Do'].append(card)

        return jsonify({'board': board}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/jira/issues/<project_key>', methods=['GET'])
@login_required
def get_project_issues(project_key):
    if use_mock_jira():
        return jsonify({'issues': mock_issues(project_key), 'mock_mode': True}), 200

    if not current_user.has_jira_configured() and not jira_oauth_configured():
        return jsonify({'error': 'Jira not configured'}), 400
    try:
        r = jira_search({
            'jql': f'{project_jql(project_key)} ORDER BY created DESC',
            'maxResults': 100,
            'fields': 'summary,status,assignee,duedate,priority,issuetype,description,created'
        })
        if r.status_code == 200:
            return jsonify({'issues': r.json().get('issues', [])}), 200
        return jsonify({'error': 'Failed to fetch issues'}), r.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 400


# ===== RISK ANALYSIS =====

class RiskAnalyzer:
    risk_keywords = {
        'Critical': ['critical', 'urgent', 'emergency', 'blocker', 'showstopper', 'asap', 'p0'],
        'Security': ['security', 'breach', 'vulnerability', 'hack', 'attack', 'auth', 'password', 'token', 'exploit', 'injection', 'xss', 'csrf'],
        'Performance': ['performance', 'slow', 'timeout', 'scalability', 'capacity', 'memory', 'latency', 'bottleneck', 'load', 'cpu'],
        'Integration': ['api', 'integration', 'third-party', 'external', 'vendor', 'service', 'sync', 'webhook', 'connector'],
        'Compliance': ['compliance', 'regulatory', 'legal', 'gdpr', 'audit', 'policy', 'hipaa', 'sox', 'pci', 'iso'],
        'Financial': ['budget', 'cost', 'revenue', 'financial', 'billing', 'payment', 'invoice', 'chargeback'],
        'Data': ['data loss', 'corruption', 'migration', 'backup', 'database', 'inconsistency', 'integrity'],
        'Operational': []
    }

    priority_weights = {'Critical': 4, 'Highest': 4, 'High': 3, 'Medium': 2, 'Low': 1, 'Lowest': 1}
    impact_map = {(0, 30): 'Low', (30, 55): 'Medium', (55, 75): 'High', (75, 101): 'Critical'}

    @classmethod
    def detect_risk_category(cls, summary, description=''):
        text = (summary + ' ' + description).lower()
        for category, keywords in cls.risk_keywords.items():
            if keywords and any(kw in text for kw in keywords):
                return category
        return 'Operational'

    @classmethod
    def calculate_risk_score(cls, issue):
        fields = issue.get('fields', {})
        score = 0

        priority = fields.get('priority', {}).get('name', 'Medium')
        score += cls.priority_weights.get(priority, 2) * 15

        issue_type = fields.get('issuetype', {}).get('name', '').lower()
        if 'bug' in issue_type:
            score += 15
        if 'security' in issue_type or 'vulnerability' in issue_type:
            score += 25

        description = fields.get('description', '')
        if isinstance(description, dict):
            description = str(description.get('content', ''))
        if description and len(str(description)) > 200:
            score += 10

        due_date = fields.get('duedate')
        if due_date:
            try:
                due = datetime.strptime(due_date, '%Y-%m-%d')
                days_overdue = (datetime.now() - due).days
                if days_overdue > 0:
                    score += min(25, 10 + days_overdue)
            except:
                pass

        # No assignee = higher risk
        if not fields.get('assignee'):
            score += 10

        return min(100, score)

    @classmethod
    def get_impact(cls, score):
        for (low, high), label in cls.impact_map.items():
            if low <= score < high:
                return label
        return 'Critical'


@app.route('/api/risk/analyze/<project_key>', methods=['GET'])
@login_required
def analyze_risks(project_key):
    if use_mock_jira():
        issues = mock_issues(project_key)
        deep_dive = build_mock_deep_dive(project_key, issues)
        risks = []
        category_counts = {}
        for issue in issues:
            fields = issue.get('fields', {})
            summary = fields.get('summary', '')
            description = fields.get('description', '')
            category = RiskAnalyzer.detect_risk_category(summary, str(description))
            score = RiskAnalyzer.calculate_risk_score(issue)
            impact = RiskAnalyzer.get_impact(score)
            category_counts[category] = category_counts.get(category, 0) + 1
            if score > 25:
                risks.append({
                    'key': issue.get('key'),
                    'summary': summary,
                    'category': category,
                    'score': score,
                    'impact': impact,
                    'priority': fields.get('priority', {}).get('name', 'Medium'),
                    'assignee': fields.get('assignee', {}).get('displayName', 'Unassigned') if fields.get('assignee') else 'Unassigned',
                    'due_date': fields.get('duedate')
                })
        risks.sort(key=lambda x: x['score'], reverse=True)
        highest_risks = risks[:5]
        return jsonify({
            'risks': risks,
            'total_analyzed': len(issues),
            'total_high_risk': len(risks),
            'critical_count': len([r for r in risks if r['score'] >= 75]),
            'high_count': len([r for r in risks if 55 <= r['score'] < 75]),
            'category_breakdown': category_counts,
            'deep_dive': {
                'top_risks': highest_risks,
                'hotspots': deep_dive.get('risk_hotspots', []),
                'owner_workload': deep_dive.get('owner_workload', []),
                'trend_14d': deep_dive.get('trend_14d', [])
            },
            'mock_mode': True
        }), 200

    if not current_user.has_jira_configured() and not jira_oauth_configured():
        return jsonify({'error': 'Jira not configured'}), 400
    try:
        r = jira_search({
            'jql': f'{project_jql(project_key)} AND statusCategory != Done ORDER BY priority DESC',
            'maxResults': 100,
            'fields': 'summary,priority,issuetype,description,duedate,status,assignee'
        })
        if r.status_code != 200:
            return jsonify({'error': 'Failed to analyze risks', 'jira_status': r.status_code}), r.status_code

        issues = r.json().get('issues', [])
        risks = []
        category_counts = {}

        for issue in issues:
            fields = issue.get('fields', {})
            summary = fields.get('summary', '')
            description = fields.get('description', '')
            category = RiskAnalyzer.detect_risk_category(summary, str(description))
            score = RiskAnalyzer.calculate_risk_score(issue)
            impact = RiskAnalyzer.get_impact(score)

            category_counts[category] = category_counts.get(category, 0) + 1

            if score > 25:
                risks.append({
                    'key': issue.get('key'),
                    'summary': summary,
                    'category': category,
                    'score': score,
                    'impact': impact,
                    'priority': fields.get('priority', {}).get('name', 'Medium'),
                    'assignee': fields.get('assignee', {}).get('displayName', 'Unassigned') if fields.get('assignee') else 'Unassigned',
                    'due_date': fields.get('duedate')
                })

        risks.sort(key=lambda x: x['score'], reverse=True)

        # Save to DB
        for risk in risks[:20]:
            existing = RiskItem.query.filter_by(user_id=current_user.id, issue_key=risk['key']).first()
            if not existing:
                ri = RiskItem(
                    user_id=current_user.id,
                    issue_key=risk['key'],
                    project_key=project_key,
                    summary=risk['summary'],
                    risk_category=risk['category'],
                    risk_score=risk['score'],
                    impact_level=risk['impact']
                )
                db.session.add(ri)
            else:
                existing.risk_score = risk['score']
                existing.risk_category = risk['category']
                existing.impact_level = risk['impact']
        db.session.commit()

        return jsonify({
            'risks': risks,
            'total_analyzed': len(issues),
            'total_high_risk': len(risks),
            'critical_count': len([r for r in risks if r['score'] >= 75]),
            'high_count': len([r for r in risks if 55 <= r['score'] < 75]),
            'category_breakdown': category_counts
        }), 200
    except RequestException as e:
        return jsonify({'error': f'Jira connection error: {str(e)}'}), 502

@app.route('/api/jira/deep-dive/<project_key>', methods=['GET'])
@login_required
def jira_deep_dive(project_key):
    if use_mock_jira():
        issues = mock_issues(project_key)
        return jsonify({
            'project_key': project_key,
            'issue_count': len(issues),
            'deep_dive': build_mock_deep_dive(project_key, issues),
            'mock_mode': True
        }), 200

    if not current_user.has_jira_configured() and not jira_oauth_configured():
        return jsonify({'error': 'Jira not configured'}), 400

    try:
        r = jira_search({
            'jql': f'{project_jql(project_key)} ORDER BY created DESC',
            'maxResults': 200,
            'fields': 'summary,status,assignee,duedate,priority,issuetype,created,updated,resolutiondate,subtasks'
        })
        if r.status_code != 200:
            return jsonify({'error': 'Failed to fetch deep dive data', 'jira_status': r.status_code}), r.status_code

        issues = r.json().get('issues', [])
        # Reuse the same shape for consistency in frontend.
        return jsonify({
            'project_key': project_key,
            'issue_count': len(issues),
            'deep_dive': build_live_deep_dive(project_key, issues),
            'mock_mode': False
        }), 200
    except RequestException as e:
        return jsonify({'error': f'Jira connection error: {str(e)}'}), 502


# ===== DELIVERY INTELLIGENCE FEATURES =====

def _churn_mock(project_key):
    issues = mock_issues(project_key)
    churned = []
    for issue in issues:
        fields = issue.get('fields', {})
        status = fields.get('status', {}).get('name', '').lower()
        is_done = any(s in status for s in ['done', 'closed', 'resolved'])
        if not is_done:
            continue
        score_seed = int(str(issue.get('key', '0')).split('-')[-1])
        reopen_count = 1 + (score_seed % 3)
        if score_seed % 4 == 0:
            churned.append({
                'key': issue.get('key'),
                'summary': fields.get('summary', ''),
                'assignee': fields.get('assignee', {}).get('displayName', 'Unassigned') if fields.get('assignee') else 'Unassigned',
                'reopen_count': reopen_count,
                'issue_type': fields.get('issuetype', {}).get('name', 'Task'),
                'priority': fields.get('priority', {}).get('name', 'Medium')
            })

    total_done = len([i for i in issues if any(
        s in i.get('fields', {}).get('status', {}).get('name', '').lower()
        for s in ['done', 'closed', 'resolved']
    )])
    churn_rate = round((len(churned) / total_done * 100), 1) if total_done else 0
    by_assignee = {}
    by_type = {}
    for item in churned:
        by_assignee[item['assignee']] = by_assignee.get(item['assignee'], 0) + item['reopen_count']
        by_type[item['issue_type']] = by_type.get(item['issue_type'], 0) + 1
    churn_index = min(100, round(churn_rate * 1.5 + len(churned) * 0.8, 1))
    return {
        'churn_index': churn_index,
        'churn_rate_pct': churn_rate,
        'total_issues': len(issues),
        'total_done': total_done,
        'churned_count': len(churned),
        'churned_issues': sorted(churned, key=lambda x: x['reopen_count'], reverse=True),
        'by_assignee': [{'assignee': k, 'reopens': v} for k, v in sorted(by_assignee.items(), key=lambda x: -x[1])],
        'by_type': [{'type': k, 'count': v} for k, v in sorted(by_type.items(), key=lambda x: -x[1])],
        'verdict': 'Critical' if churn_index >= 70 else ('High' if churn_index >= 45 else ('Medium' if churn_index >= 20 else 'Low'))
    }


def _churn_live(project_key):
    response = jira_search({
        'jql': f'{project_jql(project_key)} AND statusCategory = Done ORDER BY updated DESC',
        'maxResults': 200,
        'fields': 'summary,status,assignee,issuetype,priority,resolutiondate,updated'
    })
    if response.status_code != 200:
        return None, response.status_code

    issues = response.json().get('issues', [])
    churned = []
    by_assignee = {}
    by_type = {}
    for issue in issues:
        fields = issue.get('fields', {})
        resolved = fields.get('resolutiondate')
        updated = fields.get('updated')
        if not resolved or not updated:
            continue
        try:
            res_dt = datetime.strptime(resolved[:19], '%Y-%m-%dT%H:%M:%S')
            upd_dt = datetime.strptime(updated[:19], '%Y-%m-%dT%H:%M:%S')
        except Exception:
            continue
        delta_hours = (upd_dt - res_dt).total_seconds() / 3600
        if delta_hours <= 1:
            continue
        assignee = (fields.get('assignee') or {}).get('displayName', 'Unassigned')
        issue_type = fields.get('issuetype', {}).get('name', 'Task')
        churned.append({
            'key': issue.get('key'),
            'summary': fields.get('summary', ''),
            'assignee': assignee,
            'reopen_count': 1,
            'issue_type': issue_type,
            'priority': fields.get('priority', {}).get('name', 'Medium'),
            'hours_after_resolve': round(delta_hours, 1)
        })
        by_assignee[assignee] = by_assignee.get(assignee, 0) + 1
        by_type[issue_type] = by_type.get(issue_type, 0) + 1

    total_done = len(issues)
    churn_rate = round((len(churned) / total_done * 100), 1) if total_done else 0
    churn_index = min(100, round(churn_rate * 1.5 + len(churned) * 0.8, 1))
    return {
        'churn_index': churn_index,
        'churn_rate_pct': churn_rate,
        'total_issues': total_done,
        'total_done': total_done,
        'churned_count': len(churned),
        'churned_issues': sorted(churned, key=lambda x: x.get('hours_after_resolve', 0), reverse=True),
        'by_assignee': [{'assignee': k, 'reopens': v} for k, v in sorted(by_assignee.items(), key=lambda x: -x[1])],
        'by_type': [{'type': k, 'count': v} for k, v in sorted(by_type.items(), key=lambda x: -x[1])],
        'verdict': 'Critical' if churn_index >= 70 else ('High' if churn_index >= 45 else ('Medium' if churn_index >= 20 else 'Low'))
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


@app.route('/api/client/shares', methods=['GET'])
@login_required
def list_shares():
    shares = ClientShare.query.filter_by(user_id=current_user.id).order_by(ClientShare.created_at.desc()).all()
    return jsonify({'shares': [s.to_dict() for s in shares]}), 200


@app.route('/api/client/share', methods=['POST'])
@login_required
def create_share():
    data = request.get_json() or {}
    project_key = data.get('project_key', '').strip().upper()
    if not project_key:
        return jsonify({'error': 'project_key is required'}), 400
    label = data.get('label', f'Shared - {project_key}')
    show_risks = bool(data.get('show_risks', True))
    show_team = bool(data.get('show_team', False))
    expires_in = data.get('expires_days')
    token = secrets.token_urlsafe(32)
    share = ClientShare(
        user_id=current_user.id,
        project_key=project_key,
        token=token,
        label=label,
        show_risks=show_risks,
        show_team=show_team,
        expires_at=datetime.utcnow() + timedelta(days=int(expires_in)) if expires_in else None
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
    share = ClientShare.query.filter_by(token=token).first()
    if not share or not share.is_valid():
        return '<h2 style="font-family:sans-serif;padding:2rem">This link has expired or does not exist.</h2>', 404
    return send_from_directory('files', 'client_share.html')


@app.route('/api/share/<token>/data', methods=['GET'])
def client_share_data(token):
    share = ClientShare.query.filter_by(token=token).first()
    if not share or not share.is_valid():
        return jsonify({'error': 'Invalid or expired share link'}), 403

    owner = User.query.get(share.user_id)
    if not owner:
        return jsonify({'error': 'Owner not found'}), 404

    project_key = share.project_key
    issues = []
    if owner.has_jira_configured():
        auth = HTTPBasicAuth(owner.jira_email, owner.jira_api_token)
        headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        base = f"https://{owner.jira_domain}/rest/api/3"

        def owner_get(path, params=None):
            req_session = requests.Session()
            req_session.trust_env = False
            return req_session.get(f"{base}{path}", headers=headers, auth=auth, params=params, timeout=15)

        try:
            response = owner_get('/search', {
                'jql': f'project = "{project_key}" ORDER BY priority DESC',
                'maxResults': 100,
                'fields': 'summary,status,assignee,priority,duedate,issuetype'
            })
            if response.status_code == 200:
                issues = response.json().get('issues', [])
        except Exception:
            issues = []
    if not issues:
        issues = mock_issues(project_key)

    total = len(issues)
    done = 0
    in_prog = 0
    todo = 0
    overdue_count = 0
    priority_dist = {'Critical': 0, 'High': 0, 'Medium': 0, 'Low': 0}
    for issue in issues:
        fields = issue.get('fields', {})
        status_raw = fields.get('status', {}).get('name', '').lower()
        if any(s in status_raw for s in ['done', 'closed', 'resolved']):
            done += 1
        elif any(s in status_raw for s in ['progress', 'review']):
            in_prog += 1
        else:
            todo += 1
        priority = fields.get('priority', {}).get('name', 'Medium')
        if priority in priority_dist:
            priority_dist[priority] += 1
        due = fields.get('duedate')
        if due and 'done' not in status_raw:
            try:
                if datetime.strptime(due, '%Y-%m-%d') < datetime.now():
                    overdue_count += 1
            except Exception:
                pass

    completion_pct = round(done / total * 100, 1) if total else 0
    confidence = max(0, min(100, completion_pct - overdue_count * 5 - priority_dist.get('Critical', 0) * 8))
    payload = {
        'project_key': project_key,
        'project_name': project_key,
        'total_issues': total,
        'completed': done,
        'in_progress': in_prog,
        'todo': todo,
        'overdue_count': overdue_count,
        'completion_pct': completion_pct,
        'confidence': round(confidence, 1),
        'priority_dist': priority_dist,
        'last_updated': datetime.utcnow().strftime('%d %b %Y, %H:%M UTC'),
        'label': share.label,
        'expires_at': share.expires_at.isoformat() if share.expires_at else None
    }

    if share.show_risks:
        risks = []
        for issue in issues:
            fields = issue.get('fields', {})
            score = RiskAnalyzer.calculate_risk_score(issue)
            if score >= 55:
                risks.append({
                    'key': issue.get('key'),
                    'summary': fields.get('summary', ''),
                    'priority': fields.get('priority', {}).get('name', 'Medium'),
                    'score': score,
                    'impact': RiskAnalyzer.get_impact(score)
                })
        payload['top_risks'] = sorted(risks, key=lambda x: -x['score'])[:5]

    if share.show_team:
        team = {}
        for issue in issues:
            fields = issue.get('fields', {})
            assignee = (fields.get('assignee') or {}).get('displayName', 'Unassigned')
            team[assignee] = team.get(assignee, 0) + 1
        payload['team_workload'] = [{'name': k, 'count': v} for k, v in sorted(team.items(), key=lambda x: -x[1])]

    return jsonify(payload), 200


# ===== LAYER 2 — AI BRAIN API =====

@app.route('/api/brain/status', methods=['GET'])
@login_required
def brain_status():
    """Check if the AI brain is ready to run."""
    if BRAIN_AVAILABLE:
        status = brain_engine.get_brain_status()
    else:
        status = {
            'available': False,
            'anthropic_installed': False,
            'api_key_set': False,
            'model': 'N/A',
        }
    return jsonify(status), 200


@app.route('/api/brain/analyze/<project_key>', methods=['POST'])
@login_required
def brain_analyze(project_key):
    """Run the AI brain on a project's Jira issues + any saved signals."""
    # 1. Get Jira issues (same pattern as existing dashboard/risk routes)
    if use_mock_jira():
        issues = mock_issues(project_key)
    elif current_user.has_jira_configured() or jira_oauth_configured():
        try:
            r = jira_search({
                'jql': f'{project_jql(project_key)} ORDER BY created DESC',
                'maxResults': 200,
                'fields': 'summary,status,assignee,duedate,priority,issuetype,description,created,updated'
            })
            if r.status_code != 200:
                return jsonify({'error': 'Failed to fetch Jira issues', 'jira_status': r.status_code}), r.status_code
            issues = r.json().get('issues', [])
        except RequestException as e:
            return jsonify({'error': f'Jira connection error: {str(e)}'}), 502
    else:
        return jsonify({'error': 'Jira not configured'}), 400

    if not issues:
        return jsonify({'error': 'No issues found for this project'}), 404

    # 2. Get saved signals for this user
    user_signals = Signal.query.filter_by(user_id=current_user.id).order_by(Signal.created_at.desc()).limit(50).all()
    signals_data = [s.to_dict() for s in user_signals] if user_signals else None

    # 3. Run the brain
    if not BRAIN_AVAILABLE:
        return jsonify({'error': 'brain_engine module not available'}), 500

    if brain_engine.is_brain_available():
        insights, error = brain_engine.run_brain(issues, project_key, signals=signals_data)
    else:
        # Fall back to mock brain for demo
        insights, error = brain_engine.run_mock_brain(issues, project_key)

    if error:
        return jsonify({'error': error}), 500

    # 4. Cache the result
    try:
        cache = InsightCache(
            user_id=current_user.id,
            project_key=project_key,
            insights_json=json.dumps(insights),
            health_score=insights.get('health_score', 50),
            issue_count=len(issues),
            signal_count=len(signals_data) if signals_data else 0,
            model_used=insights.get('_meta', {}).get('model', 'unknown'),
        )
        db.session.add(cache)

        # Keep only last 20 analyses per user/project
        old = InsightCache.query.filter_by(
            user_id=current_user.id, project_key=project_key
        ).order_by(InsightCache.created_at.desc()).offset(20).all()
        for o in old:
            db.session.delete(o)

        db.session.commit()
    except Exception:
        db.session.rollback()

    return jsonify({
        'insights': insights,
        'project_key': project_key,
        'issue_count': len(issues),
        'mock_mode': use_mock_jira() or not brain_engine.is_brain_available(),
    }), 200


@app.route('/api/brain/history/<project_key>', methods=['GET'])
@login_required
def brain_history(project_key):
    """Get past brain analyses for trend tracking."""
    entries = InsightCache.query.filter_by(
        user_id=current_user.id, project_key=project_key
    ).order_by(InsightCache.created_at.asc()).limit(20).all()

    return jsonify({
        'history': [e.to_dict() for e in entries],
        'project_key': project_key,
    }), 200


@app.route('/api/brain/generate-prd', methods=['POST'])
@login_required
def brain_generate_prd():
    """Takes a recommendation + tickets + signals, passes to Brain, returns Draft PRD content."""
    data = request.get_json() or {}
    project_key = data.get('project_key')
    recommendation = data.get('recommendation')
    
    if not project_key or not recommendation:
        return jsonify({'error': 'Missing project_key or recommendation data'}), 400

    if not BRAIN_AVAILABLE or not brain_engine.is_brain_available():
        prd_md, error = brain_engine.run_mock_generate_prd(project_key, recommendation)
    else:
        prd_md, error = brain_engine.generate_prd(project_key, recommendation)

    if error:
        return jsonify({'error': error}), 500

    # Save it as a Draft SpecDocument
    spec = SpecDocument(
        user_id=current_user.id,
        project_key=project_key,
        title=recommendation.get('what', 'New Feature PRD'),
        content=prd_md
    )
    db.session.add(spec)
    db.session.commit()

    return jsonify({'spec': spec.to_dict()}), 201


@app.route('/api/brain/generate-tickets', methods=['POST'])
@login_required
def brain_generate_tickets():
    """Analyzes a PRD text and extracts a breakdown of Epics/Stories."""
    data = request.get_json() or {}
    spec_id = data.get('spec_id')

    if not spec_id:
        return jsonify({'error': 'Missing spec_id'}), 400

    spec = SpecDocument.query.filter_by(id=spec_id, user_id=current_user.id).first()
    if not spec:
        return jsonify({'error': 'Spec not found'}), 404

    if not BRAIN_AVAILABLE or not brain_engine.is_brain_available():
        tickets_data, error = brain_engine.run_mock_generate_tickets(spec.content)
    else:
        tickets_data, error = brain_engine.generate_tickets(spec.content)

    if error:
        return jsonify({'error': error}), 500

    # Save to GeneratedTicket table
    created_tickets = []
    # Clear existing unpushed tickets for this spec
    GeneratedTicket.query.filter_by(spec_id=spec_id, status='Pending').delete()
    
    for t in tickets_data:
        ticket = GeneratedTicket(
            spec_id=spec_id,
            type=t.get('type', 'Story'),
            title=t.get('title', 'Untitled'),
            description=t.get('description', ''),
            acceptance_criteria=t.get('acceptance_criteria', '')
        )
        db.session.add(ticket)
        created_tickets.append(ticket)
        
    db.session.commit()

    return jsonify({'tickets': [t.to_dict() for t in created_tickets]}), 201


# ===== SPECS API =====

@app.route('/api/specs/<project_key>', methods=['GET'])
@login_required
def list_specs(project_key):
    """List specs for a project."""
    specs = SpecDocument.query.filter_by(user_id=current_user.id, project_key=project_key).order_by(SpecDocument.created_at.desc()).all()
    return jsonify({'specs': [s.to_dict() for s in specs]}), 200

@app.route('/api/specs/<int:spec_id>', methods=['GET'])
@login_required
def get_spec(spec_id):
    """Get a specific spec and its tickets."""
    spec = SpecDocument.query.filter_by(id=spec_id, user_id=current_user.id).first()
    if not spec:
        return jsonify({'error': 'Spec not found'}), 404
        
    tickets = GeneratedTicket.query.filter_by(spec_id=spec_id).order_by(GeneratedTicket.id).all()
    return jsonify({
        'spec': spec.to_dict(),
        'tickets': [t.to_dict() for t in tickets]
    }), 200

@app.route('/api/specs/<int:spec_id>', methods=['PUT'])
@login_required
def update_spec(spec_id):
    data = request.get_json() or {}
    spec = SpecDocument.query.filter_by(id=spec_id, user_id=current_user.id).first()
    if not spec:
        return jsonify({'error': 'Spec not found'}), 404
        
    if 'title' in data:
        spec.title = data['title']
    if 'content' in data:
        spec.content = data['content']
    if 'status' in data:
        spec.status = data['status']
        
    db.session.commit()
    return jsonify({'spec': spec.to_dict()}), 200

@app.route('/api/specs/<int:spec_id>', methods=['DELETE'])
@login_required
def delete_spec(spec_id):
    spec = SpecDocument.query.filter_by(id=spec_id, user_id=current_user.id).first()
    if not spec:
        return jsonify({'error': 'Spec not found'}), 404
    GeneratedTicket.query.filter_by(spec_id=spec_id).delete()
    db.session.delete(spec)
    db.session.commit()
    return jsonify({'message': 'Deleted'}), 200


@app.route('/api/specs/<int:spec_id>/push', methods=['POST'])
@login_required
def push_spec_tickets(spec_id):
    """Push generated tickets to the Jira Cloud project."""
    spec = SpecDocument.query.filter_by(id=spec_id, user_id=current_user.id).first()
    if not spec:
        return jsonify({'error': 'Spec not found'}), 404

    tickets = GeneratedTicket.query.filter_by(spec_id=spec_id, status='Pending').all()
    if not tickets:
        return jsonify({'message': 'No pending tickets to push'}), 200

    if use_mock_jira() or (not current_user.has_jira_configured() and not jira_oauth_configured()):
        # Mock successful push
        for i, t in enumerate(tickets):
            t.status = 'Pushed'
            t.jira_key = f"{spec.project_key}-{1500+i}"
        db.session.commit()
        return jsonify({'message': 'Pushed', 'tickets': [t.to_dict() for t in tickets]}), 200

    # Build bulk issue creation payload
    issue_updates = []
    # Simplified mapping for common issue types. Real-world might need precise IDs fetched from API.
    type_map = {'Epic': 'Epic', 'Story': 'Story', 'Task': 'Task'}

    for t in tickets:
        desc_text = t.description or ''
        if t.acceptance_criteria:
            desc_text += f"\n\n*Acceptance Criteria:*\n{t.acceptance_criteria}"

        # Jira Cloud uses ADF natively, but accepts markdown-style text representation if content is string
        # For /issue/bulk we construct a v3 ADF object for description.
        description_adf = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": desc_text
                        }
                    ]
                }
            ]
        }
        
        issue_updates.append({
            "update": {},
            "fields": {
                "project": {"key": spec.project_key},
                "summary": t.title,
                "description": description_adf,
                "issuetype": {"name": type_map.get(t.type, 'Task')}
            }
        })

    try:
        r = jira_post('/issue/bulk', json={'issueUpdates': issue_updates})
        if r.status_code == 201:
            resp_data = r.json().get('issues', [])
            for i, issue_data in enumerate(resp_data):
                if i < len(tickets):
                    tickets[i].status = "Pushed"
                    tickets[i].jira_key = issue_data.get('key')
            db.session.commit()
            return jsonify({'message': 'Pushed', 'tickets': [t.to_dict() for t in tickets]}), 200
        else:
            return jsonify({'error': 'Jira API error', 'details': r.text}), 400
    except RequestException as e:
        return jsonify({'error': str(e)}), 502




# ===== SIGNAL INBOX API =====

@app.route('/api/signals', methods=['GET'])
@login_required
def list_signals():
    """Get all signals for the current user."""
    signals = Signal.query.filter_by(user_id=current_user.id).order_by(Signal.created_at.desc()).limit(100).all()
    return jsonify({'signals': [s.to_dict() for s in signals]}), 200


@app.route('/api/signals', methods=['POST'])
@login_required
def create_signal():
    """Add a new signal (interview, support, feedback, analytics, other)."""
    data = request.get_json() or {}
    content = (data.get('content') or '').strip()
    source_type = (data.get('source_type') or 'other').strip().lower()

    if not content:
        return jsonify({'error': 'Content is required'}), 400

    valid_types = ['interview', 'support', 'feedback', 'analytics', 'other']
    if source_type not in valid_types:
        source_type = 'other'

    signal = Signal(
        user_id=current_user.id,
        source_type=source_type,
        content=content[:5000],  # Cap at 5000 chars
    )
    db.session.add(signal)
    db.session.commit()

    return jsonify({'signal': signal.to_dict()}), 201


@app.route('/api/signals/<int:signal_id>', methods=['DELETE'])
@login_required
def delete_signal(signal_id):
    """Delete a signal."""
    signal = Signal.query.filter_by(id=signal_id, user_id=current_user.id).first()
    if not signal:
        return jsonify({'error': 'Signal not found'}), 404
    db.session.delete(signal)
    db.session.commit()
    return jsonify({'message': 'Signal deleted'}), 200


# ===== HEALTH + API INFO =====

@app.route('/api')
def api_info():
    return jsonify({"message": "UNCIA v3 API — Layer 2 Brain Active" if BRAIN_AVAILABLE else "UNCIA v3 API", "status": "running"}), 200

@app.route('/health')
def health():
    return jsonify({'status': 'healthy', 'brain_available': BRAIN_AVAILABLE}), 200


# ===== DB INIT =====

@app.before_request
def create_tables():
    db.create_all()


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    port = int(os.getenv('PORT', 8000))
    debug = os.getenv('FLASK_ENV', 'development') == 'development'
    print(f"[*] UNCIA v3 + Layer 2 Brain starting on http://localhost:{port}")
    app.run(host='0.0.0.0', port=port, debug=debug)
