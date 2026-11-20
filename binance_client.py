class BinanceClient:
    def __init__(self):
        self.connected = False

    def test_connection(self):
        return {
            "status": "not_connected",
            "message": "Binance Demo connection will be configured next"
        }
