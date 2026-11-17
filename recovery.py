#!/usr/bin/python3
"""Interactive terminal helper. Its login output is never collected by the GUI."""
import argparse
import signal
import sys

from backend import OperationError, result_path, sign_in_restore, write_result


def main() -> int:
    parser = argparse.ArgumentParser(description="Sign in and restore the Sync WebDAV mount")
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args()
    result_path(args.operation_id)  # Validate before modifying any services.

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    write_result(args.operation_id, "running", "Waiting for browser sign-in")
    print("Sync WebDAV — Sign in & restore\n", flush=True)
    try:
        snapshot = sign_in_restore()
        message = snapshot.title + ". You can close this terminal."
        write_result(args.operation_id, "success", message)
        print("\n" + message, flush=True)
        code = 0
    except KeyboardInterrupt:
        message = "Recovery was cancelled. Check the tray status before retrying."
        write_result(args.operation_id, "error", message)
        print("\n" + message, flush=True)
        code = 1
    except (OperationError, OSError) as exc:
        message = str(exc)
        write_result(args.operation_id, "error", message)
        print("\nRecovery failed: " + message, flush=True)
        code = 1
    try:
        input("\nPress Enter to close this window… ")
    except (EOFError, KeyboardInterrupt):
        pass
    return code


if __name__ == "__main__":
    sys.exit(main())
