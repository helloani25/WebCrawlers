import threading
import logging

from curl_cffi.requests import Session
from scrapy import signals
from scrapy.responsetypes import responsetypes
from scrapy.utils.defer import maybe_deferred_to_future
from twisted.internet import threads

logger = logging.getLogger(__name__)

# Supported curl_cffi impersonation targets — keep in sync with your target site's browser
CHROME = "chrome124"


class CurlCffiDownloadHandler:
    """
    Replaces Scrapy's Http11DownloadHandler with curl_cffi so every request
    carries a real Chrome TLS fingerprint (JA3/JA4, HTTP/2 ALPN, header order).
    One curl_cffi Session per OS thread (thread-safe isolation).
    """

    lazy = True

    def __init__(self, settings, crawler=None):
        self._impersonate = settings.get("CURL_IMPERSONATE", CHROME)
        self._timeout = settings.getfloat("DOWNLOAD_TIMEOUT", 30)
        self._verify = settings.getbool("CURL_VERIFY_SSL", True)
        self._proxy = settings.get("CURL_PROXY")
        self._local = threading.local()
        if crawler:
            crawler.signals.connect(self._on_spider_closed, signal=signals.spider_closed)

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler.settings, crawler)

    # ------------------------------------------------------------------
    # Scrapy interface
    # ------------------------------------------------------------------

    async def download_request(self, request):
        return await maybe_deferred_to_future(
            threads.deferToThread(self._fetch, request)
        )

    async def close(self):
        """
        Required by Scrapy's DownloadHandlers._close signal handler.
        """
        self._on_spider_closed(None)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _session(self):
        if not hasattr(self._local, "session"):
            self._local.session = Session(
                impersonate=self._impersonate,
                verify=self._verify,
            )
        return self._local.session

    def _fetch(self, request, spider=None):
        headers = {
            k.decode("latin-1"): b", ".join(v).decode("latin-1")
            for k, v in request.headers.items()
        }
        # Remove Accept-Encoding so curl-cffi handles it automatically
        headers.pop("Accept-Encoding", None)
        headers.pop("accept-encoding", None)
        # Let curl-cffi use proxy creds from the proxy URL to avoid header/url auth conflicts.
        headers.pop("Proxy-Authorization", None)
        headers.pop("proxy-authorization", None)

        # Force a fresh User-Agent if none or generic
        if "User-Agent" not in headers or "Scrapy" in headers.get("User-Agent", ""):
            headers["User-Agent"] = (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )

        kwargs = dict(
            method=request.method,
            url=request.url,
            headers=headers,
            timeout=self._timeout,
            allow_redirects=request.meta.get("dont_redirect", False) is False,
        )
        if request.body:
            kwargs["data"] = request.body
        if request.cookies:
            kwargs["cookies"] = request.cookies

        # Proxy support: check request.meta first, then global setting
        proxy = request.meta.get("proxy") or self._proxy
        if proxy:
            kwargs["proxies"] = {"http": proxy, "https": proxy}

        resp = self._session().request(**kwargs)

        # Pick the most specific Scrapy response class for the Content-Type
        content_type = resp.headers.get("Content-Type", "").encode()
        response_cls = responsetypes.from_args(headers={"Content-Type": content_type}, url=resp.url)

        return response_cls(
            url=resp.url,
            status=resp.status_code,
            headers=dict(resp.headers),
            body=resp.content,          # bytes
            request=request,
        )

    def _on_spider_closed(self, spider):
        # Close only the session on the signal-dispatch thread (if any)
        session = getattr(self._local, "session", None)
        if session:
            try:
                session.close()
            except Exception:
                pass
