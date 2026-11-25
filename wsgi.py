"""WSGI entry point for mod_wsgi (deb and rpm installs) and gunicorn (container)."""

from app import create_app

application = create_app()
app = application
