"""Account-policy guidance only; never read or change users' security settings."""

SECURITY_URL = 'https://cp.sync.com/account/security'
SECURITY_HELP_URL = 'https://help.sync.com/hc/en-us/articles/38275611260947-Account-security'
AUTO_LOGOUT_HINT = (
    'Check Sync → Settings → Security → Auto logout. This account setting can also affect WebDAV sessions. '
    'Choose the policy appropriate for your security needs; this app does not read or change it.'
)
AUTO_LOGOUT_HELP = (
    'Sync offers Never, 1 hour, and 24 hours in the web panel’s Auto logout settings. '
    'A 1-hour policy caused recurring WebDAV expiry in a confirmed deployment. '
    'Other causes of expiry are still possible; the app cannot see your selected policy. '
    'If you change it, use Sign in & restore to obtain a fresh session. '
    'Longer sessions increase exposure on shared or unattended computers.'
)
KEEPALIVE_POLICY_NOTE = (
    'Read-only keep-alive cannot override account Auto logout policy. '
    'If that policy caused the expiry, keep-alive is not a fix; you may disable it to avoid periodic requests.'
)
