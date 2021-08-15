pip3 install chromedriver-binary-auto

pip3 install beautifulsoup4

https://pypi.org/project/scrapy-user-agents/

####Configuring User-Agent type
There’s a configuration parameter RANDOM_UA_TYPE in format <device_type>.<browser_type>, default is desktop.chrome. For device_type part, only desktop, mobile, tablet are supported. For browser_type part, only chrome, firefox, safari, ie, safari are supported. If you don’t want to fix to only one browser type, you can use random to choose from all browser types.

You can set RANDOM_UA_SAME_OS_FAMILY to True to just use user agents that belong to the same os family, such as windows, mac os, linux, or android, ios, etc. Default value is True.

DOWNLOADER_MIDDLEWARES = {
    'scrapy.downloadermiddlewares.useragent.UserAgentMiddleware': None,
    'scrapy_user_agents.middlewares.RandomUserAgentMiddleware': 400,
}
