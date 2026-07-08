import argparse

from .server import create_app


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m web", description="spacetui web dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    app = create_app()
    print(f"spacetui web on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
