"""WSGI entry point for gunicorn (deb and rpm installs, and the container image)."""

from app import create_app

application = create_app()
app = application
