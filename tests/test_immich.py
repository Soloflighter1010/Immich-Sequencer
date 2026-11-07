"""Regression tests for the Immich API client."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from immich import get_album_assets


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.responses.pop(0))


class AlbumAssetScopeTests(unittest.TestCase):
    def test_search_request_uses_album_ids_array(self):
        session = FakeSession([
            {"assets": {"items": [{"id": "asset-1"}], "nextPage": None}}
        ])

        assets = get_album_assets(session, "http://immich:2283", "album-123")

        self.assertEqual([{"id": "asset-1"}], assets)
        self.assertEqual(
            {"albumIds": ["album-123"], "page": 1, "size": 1000},
            session.calls[0][1]["json"],
        )
        self.assertFalse(session.calls[0][1]["allow_redirects"])

    def test_repeated_next_page_does_not_loop_forever(self):
        session = FakeSession([
            {"assets": {"items": [{"id": "asset-1"}], "nextPage": "2"}},
            {"assets": {"items": [{"id": "asset-2"}], "nextPage": "2"}},
        ])

        assets = get_album_assets(session, "http://immich:2283", "album-123")

        self.assertEqual(["asset-1", "asset-2"], [asset["id"] for asset in assets])
        self.assertEqual(2, len(session.calls))


if __name__ == "__main__":
    unittest.main()
