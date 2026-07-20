# Contributing

Contributions are welcome. Please keep changes focused and include tests for
behavioral changes.

1. Create a virtual environment.
2. Install `requirements-dev.txt`.
3. Run `.venv/bin/python -m pytest`.
4. Verify `docker build .` succeeds.

Never commit OAuth credentials, `.env`, downloaded media, SQLite databases, or
deployment files containing private hostnames and paths.

