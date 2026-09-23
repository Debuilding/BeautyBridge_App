"""
Regression test for a production-breaking bug found via live testing:
crm/bookon.py's BookonAdapter._client() was fixed to pass widget_id
through to BOCRMManualAdapter, but main.py's OWN, separate BookonAdapter
class - which universal_runtime.py's BookonCRMAdapter actually calls via
legacy.BookonAdapter(cfg), not crm/bookon.py - never got the same fix.

Manual testing via crm.registry.get_crm_adapter() worked (found a real
slot), while the live bot hit "BOCRM widget_id is not configured" on the
exact same brand/service/date, because the live call path goes through
main.py's duplicate adapter instead.

This locks down that main.py's _client() actually forwards widget_id -
the same thing crm/bookon.py already does.
"""

import main


def test_legacy_bookon_adapter_client_forwards_widget_id():
    cfg = {
        "crm": {
            "email": "a@b.com",
            "password": "pw",
            "branch_id": "9970",
            "widget_id": "NZCaOgUGpRIKFkgt5h5e",
            "storage_state": "/tmp/does-not-matter.json",
        }
    }
    adapter = main.BookonAdapter(cfg)
    client = adapter._client()
    assert client.widget_id == "NZCaOgUGpRIKFkgt5h5e", (
        "main.py's BookonAdapter._client() must forward widget_id - "
        "this is the exact duplicate-code path that broke live slot lookups."
    )
    assert client.branch_id == "9970"
