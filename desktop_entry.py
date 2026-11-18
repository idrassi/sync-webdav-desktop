"""Shared freedesktop desktop-entry helpers (stdlib only)."""


def desktop_quote(argument: str) -> str:
    """Desktop Exec has its own quoting rules, not shell quoting.

    Backslashes first escape the reserved characters inside double quotes, then
    double again for the desktop file's string-value escape layer. Percent signs
    must be doubled to avoid interpreting paths as field codes.
    """
    if any(char in argument for char in ("\n", "\r", "\x00")):
        raise ValueError("Launcher paths cannot contain newlines or NUL bytes")
    escaped = argument.replace("%", "%%")
    for char in ("\\", '"', "`", "$"):
        escaped = escaped.replace(char, "\\" + char)
    return '"' + escaped.replace("\\", "\\\\") + '"'
