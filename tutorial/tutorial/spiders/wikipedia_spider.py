import json
import os
from pathlib import Path
import logging
import scrapy
from scrapy.shell import inspect_response

logger = logging.getLogger(__name__)


class WikipediaSpider(scrapy.Spider):
    name = "wikipedia"
    headers = {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,'\
               'application/signed-exchange;v=b3;q=0.9',
        'Accept-Language': 'en',
        'Referrer': 'https://en.wikipedia.org/wiki/Main_Page',
        'Accept-Encoding': 'gzip, deflate, br'
}

    def readUrls(self):
        root_dir = Path(__file__).parent.parent.parent.parent
        file_path = os.path.join(root_dir, 'files/companies.txt')
        file = open(file_path, "r")
        companies = file.readlines()
        for company in companies:
            self.start_urls.append("https://en.wikipedia.org/w/api.php?action=opensearch&format=json&formatversion=2&search="+ company +"&namespace=0&limit=10")

    def start_requests(self):
        # count = 0

        self.readUrls()
        for url in self.start_urls:
            # if count > 15:
            #    break
            # count += 1
            request = scrapy.Request(url=url, callback=self.parse, headers=self.headers)
            request.meta['proxy'] = 'http://127.0.0.1:8118'
            print(" *********************************************")
            yield request

    def parse(self, response):
        page = response.url.split("/")[-2]
        #inspect_response(response, self)
        logger.debug('Assigned User-Agent %s', (response.request.headers['User-Agent']).decode('UTF-8'))

        json_data = json.loads(response.text)

        if len(json_data) > 3:
            url = ""
            company = ""
            if len(json_data[1]) > 0:
                company = json_data[1][0]
            if len(json_data[3]) > 0:
                url = json_data[3][0]

            if company:
                yield {
                    "company": company,
                    "url": url,
                }