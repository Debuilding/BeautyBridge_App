"""Production WSGI entry point for BeautyBridge V2.1."""

from runtime_patch import app

__all__ = ["app"]

if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
