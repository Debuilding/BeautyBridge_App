"""Production WSGI entry point.

The legacy main.py is intentionally kept intact. universal_runtime adds the
CRM-agnostic V2.1 layer, migrations, queue worker and safer webhook/payment
flow, then exports the same Flask app object for Gunicorn/Railway/Replit.
"""

from universal_runtime import app

__all__ = ["app"]

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(__import__("os").getenv("PORT", "5000")))
