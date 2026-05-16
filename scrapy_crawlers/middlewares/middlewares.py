from scrapy import signals


class NoopSpiderMiddleware:
    """Pass-through spider middleware with lightweight lifecycle logging."""

    @classmethod
    def from_crawler(cls, crawler):
        instance = cls()
        crawler.signals.connect(instance.spider_opened, signal=signals.spider_opened)
        return instance

    def process_spider_input(self, response, spider):
        return None

    def process_spider_output(self, response, result, spider):
        for item in result:
            yield item

    def process_spider_exception(self, response, exception, spider):
        return None

    async def process_start(self, start):
        async for item in start:
            yield item

    def process_start_requests(self, start_requests, spider):
        # Backward-compatible fallback for projects still using start_requests.
        for request in start_requests:
            yield request

    def spider_opened(self, spider):
        spider.logger.info("Spider opened: %s", spider.name)


class NoopDownloaderMiddleware:
    """Pass-through downloader middleware with lifecycle logging."""

    @classmethod
    def from_crawler(cls, crawler):
        instance = cls()
        crawler.signals.connect(instance.spider_opened, signal=signals.spider_opened)
        return instance

    def process_request(self, request, spider):
        return None

    def process_response(self, request, response, spider):
        return response

    def process_exception(self, request, exception, spider):
        return None

    def spider_opened(self, spider):
        spider.logger.info("Spider opened: %s", spider.name)
