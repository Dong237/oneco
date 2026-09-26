"""Allow ``python -m oneco_os`` to run the OneCo CLI."""

from .cli import app

if __name__ == "__main__":
    app()
