import unittest
from unittest.mock import MagicMock
import os
import sys

# Add scrapy_crawlers root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../scrapy_crawlers")))

from middlewares.curl_handler import CurlCffiDownloadHandler
from scrapy.utils.test import get_crawler
from scrapy.http import Request

class TestProxyIntegration(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "CURL_IMPERSONATE": "chrome110",
            "CURL_PROXY": "http://global-proxy:8080"
        }
        self.crawler = get_crawler(settings_dict=self.settings)
        self.handler = CurlCffiDownloadHandler(self.crawler.settings, self.crawler)

    def test_global_proxy(self):
        request = Request("https://httpbin.org/ip")
        spider = MagicMock()
        
        # We can't easily mock the internal curl_cffi session without heavy monkeypatching
        # but we can test if the _fetch method prepares the kwargs correctly if we refactor it
        # or just verify the handler initialized correctly.
        self.assertEqual(self.handler._proxy, "http://global-proxy:8080")

    def test_request_meta_proxy_override(self):
        # This is more of a logic check on how _fetch handles meta
        request = Request("https://httpbin.org/ip", meta={"proxy": "http://request-proxy:9999"})
        
        # Mocking the session.request call
        self.handler._session = MagicMock()
        mock_session = self.handler._session.return_value
        
        # Trigger the fetch (it will run in a thread usually, but we call _fetch directly for testing)
        try:
            self.handler._fetch(request, MagicMock())
        except Exception:
            pass # We expect it to fail later as we didn't mock everything
            
        # Check if request was called with the correct proxy
        args, kwargs = mock_session.request.call_args
        self.assertEqual(kwargs['proxies'], {"http": "http://request-proxy:9999", "https": "http://request-proxy:9999"})

if __name__ == "__main__":
    unittest.main()
