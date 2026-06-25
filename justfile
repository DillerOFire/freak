# Default recipe to list available tasks
default:
    @just --list

# Sync virtual environment and python dependencies
sync:
    uv sync

# Run the Telegram bot
run:
    uv run python main.py

# Run all unit and integration tests
test:
    PYTHONPATH=. uv run pytest tests/ -v

# Clean up Python cache and temporary files
clean:
    rm -rf .pytest_cache .venv __pycache__ bot/__pycache__ tests/__pycache__
