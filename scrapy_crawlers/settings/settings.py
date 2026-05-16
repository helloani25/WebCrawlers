# Scrapy settings for proxies project
#
# For simplicity, this file contains only settings considered important or
# commonly used. You can find more settings consulting the documentation:
#
#     https://docs.scrapy.org/en/latest/topics/settings.html
#     https://docs.scrapy.org/en/latest/topics/downloader-middleware.html
#     https://docs.scrapy.org/en/latest/topics/spider-middleware.html
import os

BOT_NAME = 'scrapy_crawlers'

SPIDER_MODULES = ['spiders']
NEWSPIDER_MODULE = 'spiders'

# Replace Scrapy's default HTTP handler with curl_cffi for browser-grade TLS fingerprinting.
DOWNLOAD_HANDLERS = {
    "http": "middlewares.curl_handler.CurlCffiDownloadHandler",
    "https": "middlewares.curl_handler.CurlCffiDownloadHandler",
}

# curl_cffi options (override per-spider via custom_settings if needed)
CURL_IMPERSONATE = "chrome124"   # impersonation target
CURL_VERIFY_SSL = True

# DataImpulse Proxy Configuration
# Format: http://username:password@gw.dataimpulse.com:823
# CURL_PROXY = "http://YOUR_USERNAME:YOUR_PASSWORD@gw.dataimpulse.com:823"


# Crawl responsibly by identifying yourself (and your website) on the user-agent
#USER_AGENT = 'proxies (+http://www.yourdomain.com)'

#RANDOM_UA_PER_PROXY = False

#USER_AGENT = 'Mozilla/4.0 (compatible; MSIE 8.0; Windows NT 6.1)'

DOWNLOADER_MIDDLEWARES = {
    'scrapy.downloadermiddlewares.useragent.UserAgentMiddleware': 400,
    'scrapy.downloadermiddlewares.retry.RetryMiddleware': 405,
    'scrapy.downloadermiddlewares.httpproxy.HttpProxyMiddleware': None,
    'scrapy.downloadermiddlewares.httpcompression.HttpCompressionMiddleware': None,
}

SPIDER_MIDDLEWARES = {
    'scrapy.spidermiddlewares.referer.RefererMiddleware': 700,
}

RETRY_TIMES = 3
RETRY_HTTP_CODES = [429, 500, 502, 503, 504]

REFERER_ENABLED = True
REFERRER_POLICY = 'origin-when-cross-origin'


# Obey robots.txt rules
ROBOTSTXT_OBEY = False

# Configure maximum concurrent requests performed by Scrapy (default: 16)
CONCURRENT_REQUESTS = 8

# Configure a delay for requests for the same website (default: 0)
# See https://docs.scrapy.org/en/latest/topics/settings.html#download-delay
# See also autothrottle settings and docs
DOWNLOAD_DELAY = 5
# The download delay setting will honor only one of:
CONCURRENT_REQUESTS_PER_DOMAIN = 4
RANDOMIZE_DOWNLOAD_DELAY = True
#CONCURRENT_REQUESTS_PER_IP = 16

# Disable cookies (enabled by default)
COOKIES_ENABLED = False

# Disable Telnet Console (enabled by default)
#TELNETCONSOLE_ENABLED = False

# Override the default request headers:
DEFAULT_REQUEST_HEADERS = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,'\
               'application/signed-exchange;v=b3;q=0.9',
    'Accept-Language': 'en',
    'Referrer': 'https://en.wikipedia.org/wiki/Main_Page',
    'Accept-Encoding': 'gzip, deflate, br'
}

# Enable or disable spider middlewares
# See https://docs.scrapy.org/en/latest/topics/spider-middleware.html
#SPIDER_MIDDLEWARES = {
#    'proxies.middlewares.TutorialSpiderMiddleware': 543,
#}

# Enable or disable downloader middlewares
# See https://docs.scrapy.org/en/latest/topics/downloader-middleware.html
#DOWNLOADER_MIDDLEWARES = {
#    'proxies.middlewares.TutorialDownloaderMiddleware': 543,
#}

# Enable or disable extensions
# See https://docs.scrapy.org/en/latest/topics/extensions.html
#EXTENSIONS = {
#    'scrapy.extensions.telnet.TelnetConsole': None,
#}

# Configure item pipelines
# See https://docs.scrapy.org/en/latest/topics/item-pipeline.html
ITEM_PIPELINES = {
    "pipelines.pipelines.DriftArtifactJsonLinesPipeline": 200,
    "pipelines.pipelines.MongoPipeline": 300,
}

# Enable and configure the AutoThrottle extension (disabled by default)
# See https://docs.scrapy.org/en/latest/topics/autothrottle.html
AUTOTHROTTLE_ENABLED = True
# The initial download delay
AUTOTHROTTLE_START_DELAY = 5
# The maximum download delay to be set in case of high latencies
#AUTOTHROTTLE_MAX_DELAY = 60
# The average number of requests Scrapy should be sending in parallel to
# each remote server
AUTOTHROTTLE_TARGET_CONCURRENCY = 2.0
# Enable showing throttling stats for every response received:
#AUTOTHROTTLE_DEBUG = False

# Enable and configure HTTP caching (disabled by default)
# See https://docs.scrapy.org/en/latest/topics/downloader-middleware.html#httpcache-middleware-settings
#HTTPCACHE_ENABLED = True
#HTTPCACHE_EXPIRATION_SECS = 0
#HTTPCACHE_DIR = 'httpcache'
#HTTPCACHE_IGNORE_HTTP_CODES = []
#HTTPCACHE_STORAGE = 'scrapy.extensions.httpcache.FilesystemCacheStorage'
