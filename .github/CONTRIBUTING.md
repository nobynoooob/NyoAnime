# Contributing to NyoAnime

Bug fixes, provider updates, UI improvements, and documentation are welcome.

## Development setup

```bash
git clone <repository-url> nyoanime
cd nyoanime
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python3 main.py
```

## Code style

- PEP 8, four-space indentation.
- Type hints where they add clarity.
- Docstrings for non-obvious public functions.
- Keep modules focused; avoid new dependencies unless necessary.

## Testing

Run the suite before opening a pull request:

```bash
python3 -m pytest -q
```

Provider tests may hit live services. If a provider is temporarily down, note it in the PR instead of weakening assertions.

## Pull requests

1. Branch from `main`.
2. Keep changes scoped and describe the behavior change.
3. Include reproduction steps for bug fixes.
4. Update tests and docs when behavior changes.

## Reporting issues

Include:

- OS and version
- Python version
- Provider and query used
- Steps to reproduce
- Expected vs actual behavior
- Relevant lines from `~/.nyoanime/debug.log`

## Security

Do not open public issues for vulnerabilities. See [SECURITY.md](SECURITY.md).
