#!/usr/bin/python3
"""systemd ExecStartPre: wait for the local endpoint without credentials or shell."""
import argparse
import http.client
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        return 1
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        connection = http.client.HTTPConnection("127.0.0.1", args.port, timeout=1)
        try:
            connection.request("HEAD", "/")
            if connection.getresponse().status in {200, 204, 207, 401}:
                return 0
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        time.sleep(1)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
