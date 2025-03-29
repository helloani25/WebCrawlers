import os
from pathlib import Path

from bs4 import BeautifulSoup

root_dir = Path(__file__).parent
files_path = os.path.join(root_dir, '../../../files/quotes-wiki.html')
with open(files_path, 'r') as f:
    contents = f.read()
    soup = BeautifulSoup(contents, 'lxml')

    table = soup.find("table", {"class": "infobox vcard"})
    company_name = soup.find('caption',{'class':'infobox-title'}).text.strip()
    print("company = " + company_name)
    for row in table.findAll("tr"):
        cells = row.findAll("td")
        for cell in cells:
            if len(cell.attrs['class']) > 1 and cell.attrs['class'][0] == 'infobox-data' and cell.attrs['class'][1] == 'category' :
                company = cell.contents
                li_tags = cell.findAll("li")
                if len(li_tags) > 0:
                    print("Industry : ")
                    for li in li_tags:
                        print(li.find("a").text)
                else:
                    print(cell.contents)
        cells = row.findAll("th")
        for cell in cells:
            if cell.attrs['class'][0] == 'infobox-label' and cell.text == "Founded":
                print(cell.text + " = " +cell.nextSibling.text)
            if cell.attrs['class'][0] == 'infobox-label' and cell.text == "Number of employees":
                print(cell.text + " = " + cell.nextSibling.text)
            if cell.attrs['class'][0] == 'infobox-label' and cell.text == "Operating income":
                print(cell.text + " = " + cell.nextSibling.text)
            if cell.attrs['class'][0] == 'infobox-label' and cell.text == "Area served":
                print(cell.text + " = " + cell.nextSibling.text)
            if cell.attrs['class'][0] == 'infobox-label' and cell.text == "Headquarters":
                print(cell.text + " = " + cell.nextSibling.text)
            if cell.attrs['class'][0] == 'infobox-label' and cell.text == "Website":
                print(cell.text + " = " + cell.nextSibling.text)

