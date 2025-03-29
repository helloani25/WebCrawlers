import json
import os
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
import chromedriver_binary  # Adds chromedriver binary to path
from selenium.common.exceptions import StaleElementReferenceException
import time


def waitForLoad(driver):
    elem = driver.find_element(By.TAG_NAME, "html")
    count = 0
    while True:
        count += 1
        if count > 10:
            print("Timing out after 10 seconds and returning")
            return
        time.sleep(.5)
        try:
            elem == driver.find_element(By.TAG_NAME, "html")
        except StaleElementReferenceException:
            return

chrome_options = Options()
chrome_options.add_argument("--headless")
proxy = '127.0.0.1:9050'
chrome_options.add_argument('--proxy-server=socks5://' + proxy)
driver = webdriver.Chrome(options=chrome_options)
root_dir = Path(__file__).parent.parent.parent
file_path = os.path.join(root_dir, 'files/companies.txt')
file = open(file_path, "r")
companies = file.readlines()
file.close()
files_path = os.path.join(root_dir, 'files/wikiurls.txt')
file = open(files_path, "a+")
for company in companies:
    driver.get("https://en.wikipedia.org/w/api.php?action=opensearch&format=json&formatversion=2&search=" + company + "&namespace=0&limit=10")
    waitForLoad(driver)
    pre = driver.find_element(By.TAG_NAME, "pre").text
    data = json.loads(pre)
    print(data)
    if len(data) > 3:
        url = ""
        company = ""
        if len(data[1]) > 0:
            company = data[1][0]
        if len(data[3]) > 0:
            url = data[3][0]
        if company:
            file.write(company + "|" + url + '\n')
    #print(driver.page_source)
file.close()
driver.close()