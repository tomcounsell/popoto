from alpha_vantage.techindicators import TechIndicators
from alpha_vantage.timeseries import TimeSeries
from pprint import pprint
from settings import ALPHA_VANTAGE_API_KEY

ts = TimeSeries(key=ALPHA_VANTAGE_API_KEY, output_format='pandas')
data, meta_data = ts.get_intraday(symbol='MSFT',interval='1min', outputsize='full')
pprint(data.head(2))

ti = TechIndicators(key=ALPHA_VANTAGE_API_KEY, output_format='pandas')
data, meta_data = ti.get_bbands(symbol='BTC', interval='1day', time_period=60)

from polygon import RESTClient
from settings import POLYGON_API_KEY
with RESTClient(POLYGON_API_KEY) as client:
    response = client.crypto_daily_open_close("X:BTCUSD", "2020-11-14")

