import argparse
import secrets

from .server import create_app


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m web", description="spacetui web dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--token")
    args = p.parse_args()
    token = args.token
    if args.host not in ("127.0.0.1", "localhost", "::1") and not token:
        token = secrets.token_urlsafe(12)
    app = create_app(token=token)
    url = f"http://{args.host}:{args.port}/" + (f"?token={token}" if token else "")
    print(f"spacetui web on {url}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
