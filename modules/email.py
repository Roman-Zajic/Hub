"""
Email module — placeholder shell for your email composer tool.

Drop this file in modules/ to enable the Email tab in the sidebar;
remove it (or rename it with a leading underscore) to take it out of the
app entirely — no changes to app.py or layout.html are needed either way.
Add routes and business logic here as the module grows.
"""
from flask import Blueprint, render_template

# ── Module metadata (read by app.py's auto-discovery) ───────────────
NAV_LABEL = 'Email'
NAV_PATH = '/email'
ORDER = 40

bp = Blueprint('email', __name__)


@bp.route('/email')
def email_module():
    return render_template('email.html')
