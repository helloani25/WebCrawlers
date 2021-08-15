import os
from pathlib import Path

import scrapy
from scrapy.shell import inspect_response


class WikipediaSpider(scrapy.Spider):
    name = "wikipedia"
    urls = []

    def readUrls(self):
        root_dir = Path(__file__).parent.parent.parent.parent
        file_path = os.path.join(root_dir, 'files/b.txt')
        file = open(file_path, "r")
        companies = file.readlines()
        for company in companies:
            self.urls.append("https://en.wikipedia.org/w/api.php?action=opensearch&format=json&formatversion=2&search="+ company +"&namespace=0&limit=10")

    def start_requests(self):
        count = 0
        self.readUrls()
        #self.urls = ["https://en.wikipedia.org/wiki/google"]
        for url in self.urls:
            if count > 1:
                break
            count += 1
            request = scrapy.Request(url=url, callback=self.parse)
            request.meta['proxy'] = 'http://127.0.0.1:8118'
            print(" *********************************************")
            yield request

    def parse(self, response):
        page = response.url.split("/")[-2]
        inspect_response(response, self)
        filename = f'quotes-{page}.html'
        with open(filename, 'wb') as f:
            f.write(response.body)
        self.log(f'Saved file {filename}')