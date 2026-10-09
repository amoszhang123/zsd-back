# factory

A FastAPI web API project.

## Quick Start

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e ".[dev]"

# Run the server
uvicorn app.main:app --reload

# Run tests
pytest
```

## Project Structure

```
factory/
├── app/
│   ├── main.py          # FastAPI application entry point
│   ├── config.py         # Settings & configuration
│   ├── routers/          # API route handlers
│   ├── models/           # Data models
│   └── schemas/          # Pydantic request/response schemas
├── tests/                # Test suite
├── pyproject.toml        # Project metadata & build config
└── requirements.txt      # Pinned dependencies
```
