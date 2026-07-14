"""
Timesheet module.

Paste a TSV export (e.g. copied straight out of Excel) and build a simple
sum-aggregated report, grouped by any combination of the supported
dimension columns. All parsing/aggregation happens client-side in the
browser — this module just serves the page.

Drop this file into modules/ to install it (see app.py header comment).
"""
from flask import Blueprint, render_template

bp = Blueprint('timesheet', __name__)

NAV_LABEL = 'Timesheet'
NAV_PATH = '/timesheet'
ORDER = 20  # adjust to taste relative to your other modules' ORDER values


@bp.route('/timesheet')
def timesheet():
    return render_template('timesheet.html')
