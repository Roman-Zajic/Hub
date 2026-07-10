"""
Compare module — placeholder shell for your comparison tool.

Drop this file in modules/ to enable the Compare tab in the sidebar;
remove it (or rename it with a leading underscore) to take it out of the
app entirely — no changes to app.py or layout.html are needed either way.
Add routes and business logic here as the module grows.
"""
from flask import Blueprint, render_template

# ── Module metadata (read by app.py's auto-discovery) ───────────────
NAV_LABEL = 'Compare'
NAV_PATH = '/compare'
ORDER = 20

bp = Blueprint('compare', __name__)


@bp.route('/compare')
def compare_module():
    return render_template('compare.html')
