"""
App shell.

This file contains NO module-specific business logic or routes — it only
discovers, registers, and wires up whatever modules are present in modules/.

To install a module: drop a .py file into modules/ that defines a Flask
Blueprint named `bp`. It's picked up automatically the next time the app
starts — nothing here needs to change.

To remove/disable a module: delete its file, or rename it with a leading
underscore (e.g. compare.py -> _compare.py). Its routes and its sidebar
entry disappear automatically.

Optional attributes a module file can define:
    bp         (required) the Flask Blueprint to register
    NAV_LABEL  sidebar link text (default: the module's filename, titlecased)
    NAV_PATH   sidebar link target / this module's primary URL (default: /<filename>)
    ORDER      sort position in the sidebar, lower first (default: 999)
    on_load()  called once, right after the blueprint is registered at startup
               (e.g. to kick off a background thread or one-time init)
"""
import importlib
import os
import pkgutil

from flask import Flask, redirect

app = Flask(__name__)

MODULES_PACKAGE = 'modules'
installed_modules = []


def _load_modules():
    package_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), MODULES_PACKAGE)
    discovered = []

    for _, module_name, _ in pkgutil.iter_modules([package_dir]):
        if module_name.startswith('_'):
            continue  # leading underscore = disabled, skip entirely

        module = importlib.import_module(f'{MODULES_PACKAGE}.{module_name}')
        blueprint = getattr(module, 'bp', None)
        if blueprint is None:
            continue  # no blueprint exposed -> not an installable module

        order = getattr(module, 'ORDER', 999)
        discovered.append((order, module_name, module, blueprint))

    # Register in ORDER, so sidebar position is stable and configurable per-module
    for order, module_name, module, blueprint in sorted(discovered, key=lambda m: m[0]):
        app.register_blueprint(blueprint)
        installed_modules.append({
            'name':  module_name,
            'label': getattr(module, 'NAV_LABEL', module_name.title()),
            'path':  getattr(module, 'NAV_PATH', f'/{module_name}'),
        })

        on_load = getattr(module, 'on_load', None)
        if callable(on_load):
            on_load()


_load_modules()


@app.context_processor
def inject_nav():
    """Makes the sidebar in layout.html render itself from whatever modules
    are actually installed, instead of a hardcoded list of links."""
    return {'nav_modules': installed_modules}


@app.route('/')
def index():
    if installed_modules:
        return redirect(installed_modules[0]['path'])
    return 'No modules installed. Add one to the modules/ folder.', 404


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5001, use_reloader=False)
