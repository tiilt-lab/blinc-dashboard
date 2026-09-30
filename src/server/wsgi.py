"""gunicorn entry point for the BLINC API.

    gunicorn -c src/server/gunicorn.conf.py --chdir src/server wsgi:app

Path resolution, so this behaves like ``python src/server/discussion_capture.py``:

* ``--chdir src/server`` (also set as ``chdir`` in gunicorn.conf.py) makes
  gunicorn ``os.chdir()`` there AND insert that absolute directory at
  ``sys.path[0]`` (gunicorn.app.base.Application.chdir). The dev runner gets
  the same thing for free as the script's own directory. That is what lets
  the bare sibling imports (``from app import ...``, ``import database``,
  ``routes.*``) resolve identically in both.
* config.ini is found relative to config.py (``config.initialize``), never
  the CWD, so the changed working directory does not matter to it.
* ``-c src/server/gunicorn.conf.py`` is resolved against the unit's
  WorkingDirectory (the repo root): gunicorn reads the config file before it
  applies ``--chdir``.

``app`` is the Flask application object. Flask-SocketIO already replaced
``app.wsgi_app`` with its engineio WSGI middleware (``SocketIO(app, ...)`` in
app.py), so gunicorn calling ``app(environ, start_response)`` serves both the
REST routes and /socket.io, including WebSocket upgrades: in threading mode
python-engineio hands the request to simple-websocket, which takes the raw
socket from ``environ['gunicorn.socket']``, runs the connection inside the
WSGI call and raises StopIteration when it ends, which gunicorn's gthread
worker treats as "close this connection". No extra setting is needed.

This module is imported in the WORKER process (the unit does not use
--preload), so ``create_app()``'s side effects run once per worker; with
``workers = 1`` that is once per service start, and a respawned worker is a
fresh process that initialises itself again.
"""
from discussion_capture import create_app

app = create_app()
