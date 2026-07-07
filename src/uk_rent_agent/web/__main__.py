from uk_rent_agent.config import Config
from uk_rent_agent.logging_setup import configure_logging
from uk_rent_agent.web.app import create_app


def main() -> None:
    try:
        from waitress import serve
    except ImportError as exc:
        raise RuntimeError("Install project runtime dependencies with: pip install -e .") from exc
    configure_logging()
    app = create_app(Config.from_env(require_secret=True))
    serve(app, host="127.0.0.1", port=5001)


if __name__ == "__main__":
    main()
