import httpx
import json

url = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
ids = [108632, 115044, 21355] # Re:Zero S2 P1, S2 P2, S1

with httpx.Client() as client:
    response = client.get(url)
    data = response.json()
    for item in data:
        if item.get("anilist_id") in ids:
            print(json.dumps(item, indent=2))
