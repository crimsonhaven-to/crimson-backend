import httpx
import json
url = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
with httpx.Client() as client:
    response = client.get(url)
    data = response.json()
    for item in data:
        if item.get("anilist_id") == 163134:
            print(json.dumps(item, indent=2))
