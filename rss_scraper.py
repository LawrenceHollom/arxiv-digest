import urllib.request

url = "https://rss.arxiv.org/rss/math.CO"
with urllib.request.urlopen(url) as response:
    print(response.read().decode("utf-8"))