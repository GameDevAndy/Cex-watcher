import requests
import json
import os

URL = "https://uk.webuy.com/search?stext=psp"

WEBHOOK = os.environ.get("DISCORD_WEBHOOK")

data = requests.get(URL, headers={"User-Agent": "Mozilla/5.0"}).text

state_file = "state.txt"

try:
    with open(state_file) as f:
        old = f.read()
except:
    old = ""

if data != old:
    print("Change detected")

    if WEBHOOK:
        requests.post(WEBHOOK, json={
            "content": "CeX PSP stock page changed:\n" + URL
        })

    with open(state_file, "w") as f:
        f.write(data)
else:
    print("No change")
