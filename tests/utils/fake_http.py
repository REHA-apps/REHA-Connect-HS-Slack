# tests/utils/fake_http.py  # noqa: D100
class FakeHTTPResponse:
    def __init__(self, status=200, json_data=None):
        self.status_code = status
        self._json = json_data or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:  # noqa: PLR2004
            raise Exception("HTTP error")


class FakeHTTPClient:
    async def post(self, url, json=None):
        return FakeHTTPResponse()
