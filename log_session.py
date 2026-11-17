#!/usr/bin/python3
"""Timestamp a service attempt before the vendor daemon redirects its output."""
from diagnostics import mark_server_start


if __name__ == '__main__':
    mark_server_start()
