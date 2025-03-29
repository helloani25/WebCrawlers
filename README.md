pip3 install chromedriver-binary-auto

## Install Beautifulsoup
```commandline
pip3 install beautifulsoup4
```

https://pypi.org/project/scrapy-user-agents/

####Configuring User-Agent type
There’s a configuration parameter RANDOM_UA_TYPE in format <device_type>.<browser_type>, default is desktop.chrome. For device_type part, only desktop, mobile, tablet are supported. For browser_type part, only chrome, firefox, safari, ie, safari are supported. If you don’t want to fix to only one browser type, you can use random to choose from all browser types.

You can set RANDOM_UA_SAME_OS_FAMILY to True to just use user agents that belong to the same os family, such as windows, mac os, linux, or android, ios, etc. Default value is True.

DOWNLOADER_MIDDLEWARES = {
    'scrapy.downloadermiddlewares.useragent.UserAgentMiddleware': None,
    'scrapy_user_agents.middlewares.RandomUserAgentMiddleware': 400,
}


#### Scraping from LinkedIn

```
href_array = $("#seo-dir").querySelectorAll("li.content a")
href_array.forEach (element => console.log(element.href));

cat >> ~/junk/a1.txt

cut -d" " -f2 ~/junk/a1.txt > ~/junk/b1.txt

sed -e 's/https\:\/\/www.linkedin.com\/company\///g'  -e 's/\?trk=companies_directory//g' ~/junk/b.txt  > ~/junk/c1.txt 

href_array = $("#seo-dir").querySelectorAll("li.content a")
href_array.forEach (element => console.log(element.text));

sed -e 's/VM[0-9]\{3,\}\:[0-9]\{1,\}//g' ~/junk/a2.txt > ~/junk/b2.txt


scrapy-user-agents -> browser list
```

## TOR
### INSTALL TOR
```commandline
brew install tor
```
Tor (short for "The Onion Router") that enhances online privacy and anonymity by routing internet traffic through a series of encrypted relays, making it difficult to trace a user's activity or location.
Here's a more detailed explanation:

#### How it works:
When you use Tor, your internet traffic is routed through a series of randomly selected relays (nodes) in the Tor network.
Each relay only knows the IP address of the previous and next relay, not the origin or destination of the traffic.
This layered encryption, like an onion, makes it difficult to track your activity or location.

the final relay (exit node) in the Tor network can potentially see your unencrypted traffic as it is the point where the traffic leaves the Tor network and is forwarded to its destination.
Mitigation:
To mitigate this, it's important to use HTTPS for all your web traffic, as HTTPS encrypts the communication between your browser and the website, making it more difficult for exit node operators to eavesdrop on your traffic.
##### Tor Browser:
The Tor Browser is a web browser specifically designed to work with the Tor network, providing easy access to the Tor network and its privacy features.
https://support.torproject.org/https/https-1/
### INSTALL TORSOCKS
```commandline
brew install torsocks
```
Torsocks is a wrapper that allows you to use SOCKS-friendly applications with Tor, ensuring safe DNS handling and rejecting UDP traffic, while tsocks is a library for transparent SOCKS proxying, primarily useful for Tor.
Here's a more detailed explanation:
Torsocks:
Designed to make applications that use SOCKS protocols (like SOCKS5) work safely with Tor.
Ensures that DNS requests are handled securely and rejects UDP traffic, which can leak information.
It's a wrapper that intercepts application connections and routes them through Tor.
The project homepage is https://gitlab.torproject.org/tpo/core/torsocks.


### Configuring TOR
```markdown
Tor configuration file at /opt/local/etc/tor/torrc.sample. Remove the .sample extension to make it effective.

```

To configure the Tor control port, you typically modify the torrc file, setting ControlPort to your desired port (default is 9051), and potentially enabling cookie authentication or setting a password.
Here's a more detailed breakdown:
1. Locate the torrc file:
   On Linux/macOS, it's often found at /usr/local/etc/tor/torrc or similar.
2. Edit the torrc file:
   ControlPort:
   Set the ControlPort directive to your desired port number (e.g., ControlPort 9051 for the default, or ControlPort 9151 for a different port).
   CookieAuthentication:
   If you want to use cookie authentication (recommended for security), set CookieAuthentication 1.
   Other Configuration:
   You can also configure other Tor settings in this file, such as logging levels, SOCKS port, etc.
3.  torrc entries:
```commandline
vi /usr/local/etc/tor/torrc
  ControlPort 9051 
  CookieAuthentication 1 
```
4. Test TOR
```commandline
brew services start tor
torify curl http://icanhazip.com/
curl --socks5-hostname localhost:9050 https://check.torproject.org
curl --socks5-hostname localhost:9050 http://icanhazip.com/
brew services stop tor

```


## PRIVOXY

Privoxy is a free, non-caching web proxy server with advanced filtering capabilities designed to enhance privacy, filter web page content, and remove ads and other unwanted elements.
Privoxy intercepts HTTP requests and responses, allowing you to modify or remove elements like ads, banners, pop-ups, and other unwanted content before it reaches your browser.
Privacy Enhancement:
By filtering and modifying web pages, Privoxy can help protect your privacy by reducing tracking and data collection by websites.
How it Works:
1. Configure Your Browser:
   You configure your browser to use Privoxy as a proxy server, typically by setting the proxy address to "localhost" or "127.0.0.1" and the port to 8118.
2. Privoxy Intercepts Traffic:
   When your browser sends a request to a website, Privoxy intercepts the request and response.

```commandline
brew install privoxy
cp /usr/local/etc/privoxy/config /usr/local/opt/privoxy/sbin/
vi  /usr/local/etc/privoxy/config
```
https://www.privoxy.org/3.0.31/user-manual/config.html 
To chain Privoxy and Tor, both running on the same system, you would use something like:

```commandline
forward-socks5t   /               127.0.0.1:9050 .
brew services tor start
brew services  restart privoxy
```

## NYX
Nyx is designed for command-line usage and provides a way to monitor the health and performance of a Tor relay.
Functionality:
1. Real-time monitoring: Nyx displays real-time information about bandwidth usage, connections, logs, and other relevant details.
2. Relay Operator Tool: It's particularly useful for Tor relay operators who need to monitor their relay's performance and troubleshoot issues.
3. Command-line interface: Nyx operates through a command-line interface, allowing users to interact with the tool and view information in a terminal. 

Example of Nyx usage:
1. To view the list of Tor connections and circuits, you can click the right arrow key on your keyboard after running nyx.
2. To exit Nyx, type q and then type q again to confirm

* https://blog.torproject.org/meet-nyx-command-line-tor-relay-monitor/
* https://witestlab.poly.edu/blog/anonymous-networking-with-tor/
* https://www.whonix.org/wiki/Tor_Controller
```commandline
brew  install nyx
Run nyx
```
## List all brew services

```commandline
brew services 
```
```markdown
Name    Status  User              Plist
privoxy started anithasubramanian /Users/anithasubramanian/Library/LaunchAgents/homebrew.mxcl.privoxy.plist 
tor     started anithasubramanian /Users/anithasubramanian/Library/LaunchAgents/homebrew.mxcl.tor.plist
```
 
#### Stopping privoxy and tor
```markdown
Anithas-MBP:files anithasubramanian$ brew services stop privoxy
Stopping `privoxy`... (might take a while)
==> Successfully stopped `privoxy` (label: homebrew.mxcl.privoxy)
Anithas-MBP:files anithasubramanian$ brew services stop tor
Stopping `tor`... (might take a while)
==> Successfully stopped `tor` (label: homebrew.mxcl.tor)
Anithas-MBP:files anithasubramanian$ 
```

### scrapy-tor-proxy-rotation

"toripchanger" is a Python library that allows you to easily manage and change the IP address of a Tor network connection, potentially for applications requiring frequent IP address updates.

Purpose: The library provides a way to interact with Tor, specifically to request a new IP address (exit node) and manage the reuse of Tor IPs.
Functionality:
1. You can define how often a Tor IP can be reused using the reuse_threshold parameter.
2. It can be used to automate the IP address change process, which is useful for applications that require frequent IP changes for anonymity or other reasons.

```commandline
pip install toripchanger
```
##### Example Implementation
```
Implemented in the middlewares.py as ProxyMiddleWare. 

There is also a python module someone implemented which is a bit complicated. But nice guide https://pypi.org/project/scrapy-tor-proxy-rotation/
```

```commandline
    from toripchanger import TorIpChanger

    # Tor IP reuse is prohibited.
    tor_ip_changer_0 = TorIpChanger(reuse_threshold=0)
    current_ip = tor_ip_changer_0.get_new_ip()

    # Current Tor IP address can be reused after one other IP was used (default setting).
    tor_ip_changer_1 = TorIpChanger(local_http_proxy='127.0.0.1:8888')
    current_ip = tor_ip_changer_1.get_new_ip()

    # Current Tor IP address can be reused after 5 other Tor IPs were used.
    tor_ip_changer_5 = TorIpChanger(tor_address="localhost", reuse_threshold=5)
    current_ip = tor_ip_changer_5.get_new_ip()
```
### UserAgent Faker
PYPI: https://pypi.org/project/scrapy-fake-useragent/ or Github : https://github.com/alecxe/scrapy-fake-useragent/tree/master 

<p> Random User-Agent middleware for Scrapy scraping framework based on fake-useragent, which picks up User-Agent strings based on usage statistics from a real world database, but also has the option to configure a generator of fake UA strings, as a backup, powered by Faker. </p>
https://faker.readthedocs.io/en/master/providers/faker.providers.user_agent.html

```commandline
pip install scrapy-fake-useragent
```

#### Running mysql
```markdown
docker run -d -it  -e MYSQL_ROOT_PASSWORD=pa55w0rd -e MYSQL_DATABASE=db_example --name mysql_test_db mysql
  docker images
  docker ps
  docker exec mysql_test_db -it /bin/bash
  docker exec -it mysql_test_db /bin/bash
```

#### Scraping example
 scrapy crawl wikipedia -o wikiurls.csv 

```markdown
  redis-server
  pip install Scrapy
  scrapy startproject tutorial
  scrapy crawl quotes
  scrapy runspider quotes
  scrapy runspider spiders/quotes_spider
  pip install scrapy-user-agents
  pip install scrapy_proxies
  scrapy crawl quotes
  pip uninstall scrapy_proxies
```

```markdown
  scrapy crawl wikipedia
  401  scrapy parse --spider=myspider -d 3 https://en.wikipedia.org/wiki/google
  402  scrapy parse --spider=wikipedia -d 3 https://en.wikipedia.org/wiki/google
  404  pip3 install beautifulsoup4
  408   scrapy parse --spider=myspider -c parse_item -d 2 https://en.wikipedia.org/w/api.php?action=opensearch&format=json&formatversion=2&search=apple&namespace=0&limit=10
  410   scrapy parse --spider=wikipedia -c parse_item -d 2 https://en.wikipedia.org/w/api.php?action=opensearch&format=json&formatversion=2&search=apple&namespace=0&limit=10
  412  pip3 install chromedriver-binary
  413  pip3 uninstall chromedriver-binary
  414  pip3 install chromedriver-binary-auto
  416  pip install scrapy crawl quotes
  419  scrapy crawl quotes
  422  pip3 install scrapy_proxies
  423  scrapy crawl wikipedia
  424  pip3 uninstall scrapy_proxies
  425  pip list
```

```markdown
  441  scrapy crawl wikipedia -t csv -o wikiurls.csv
  442  scrapy crawl wikipedia -t csv -o wikiurls.csv -a CSV_DELIMITER="|"
  443  scrapy crawl wikipedia  -o wikiurls.csv 
  444  scrapy crawl wikipedia -t csv -o wikiurls.csv -a CSV_DELIMITER="|"
  448  scrapy crawl wikipedia -o wikiurls.csv --set delimiter="|"
  449  scrapy crawl spidername --set FEED_URI=output.csv --set FEED_FORMAT=csv --set CSV_DELIMITER=';'
  451  scrapy crawl spidername -o wikiurls.csv  --set CSV_DELIMITER='|'
  461  pip install toripchanger
  471  pip list
  472  pip uninstall scrapy-user-agents
  473  pip install os-scrapy-random-useragent
  474  pip uninstall os-scrapy-random-useragent
  475  pip install scrapy-user-agents
  479  pip uninstall scrapy-user-agents
  480  pip install scrapy-fake-useragent
  481  scrapy crawl wikipedia -o wikiurls.csv 
```
  