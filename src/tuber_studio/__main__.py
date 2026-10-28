import argparse

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Tuber Studio local character runtime")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8020)
    args = parser.parse_args()
    uvicorn.run("tuber_studio.app:create_app", factory=True, host=args.host, port=args.port, ws="wsproto")


if __name__ == "__main__":
    main()
