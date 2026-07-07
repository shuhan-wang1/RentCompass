from uk_rent_agent.config import Config
from uk_rent_agent.logging_setup import configure_logging


def main() -> None:
    """Run the production ASGI shell required by streaming and async graph calls."""
    try:
        from uk_rent_agent.web.asgi import main as run_asgi
    except ImportError as exc:
        raise RuntimeError("Install project runtime dependencies with: pip install -e .") from exc
    configure_logging()
    Config.from_env(require_secret=True)
    run_asgi()


if __name__ == "__main__":
    main()
