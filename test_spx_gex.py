import json, tastytrade
from datafeed import TastytradeBackend, compute_gex
print("tastytrade version:", tastytrade.__version__)
be = TastytradeBackend()
print("available:", be.available())
chain = be.get_chain("SPX")                      # calls Tasty directly — errors will show
print("spot:", chain["spot"], "| estimated?", chain["_spot_estimated"], "| legs:", len(chain["legs"]))
withg = [l for l in chain["legs"] if l["gamma"] is not None]
print("legs with live gamma:", len(withg), "| sample:", withg[0] if withg else None)
print(json.dumps(compute_gex(chain), indent=2))
