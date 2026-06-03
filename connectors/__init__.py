"""Source connectors for the Shop Apotheke weekly dashboard.

Each connector exposes a single `fetch(start, end, **opts) -> pd.DataFrame`
function that returns a normalized DataFrame. Connectors read their secrets
from environment variables so they work both locally and in GitHub Actions.

Missing credentials → the connector raises `ConnectorSkipped`; the
orchestrator logs and continues without that source.
"""


class ConnectorSkipped(RuntimeError):
    """Raised when a connector cannot run (missing creds, etc.)."""
