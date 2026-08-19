import os, json
os.environ["IB_ENABLED"]="1"; os.environ["IB_PORT"]="4002"; os.environ["IB_CLIENT_ID"]="19"
from datafeed import IBKRBackend
be = IBKRBackend()
print("available:", be.available())
print("equity quotes:", json.dumps(be.get_quotes(["SPY","GLD"])))
for root, exch in [("VIX","CFE"), ("NG","NYMEX"), ("CL","NYMEX")]:
    print(root, "curve:", json.dumps(be.futures_curve(root, exch)))
be.disconnect()
