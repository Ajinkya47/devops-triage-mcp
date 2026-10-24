import pytest

import approve


@pytest.fixture(autouse=True)
def no_real_github_lookups(monkeypatch):
    """Tests must never reach the network; individual tests may override this."""
    monkeypatch.setattr(approve, "lookup_target",
                        lambda repo, number: {"status": "unavailable"})