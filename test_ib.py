from ib_async import IB
ib = IB()
ib.connect('127.0.0.1', 4002, clientId=17)
print('connected:', ib.isConnected(), '| server time:', ib.reqCurrentTime())
ib.disconnect()
