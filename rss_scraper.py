import xmltodict
import pprint
import urllib.request

url = "https://rss.arxiv.org/rss/math.CO+math.NT+math.PR+math.LO"
with urllib.request.urlopen(url) as response:
    text = response.read().decode("utf-8")
    papers_list = xmltodict.parse(text)['rss']['channel']['item']

    out_list = []

    for paper in papers_list:
        # pprint.pprint(paper, indent=2)
        if paper['arxiv:announce_type'] == 'new':
            out_list.append(paper)

    # Print the dictionary
    pprint.pprint(out_list, indent=2)