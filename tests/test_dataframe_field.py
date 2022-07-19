import sys
import os

from src.popoto.redis_db import POPOTO_REDIS_DB

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto
import pandas as pd


class DataFrameModel(popoto.Model):
    title = popoto.KeyField()
    df = popoto.DataFrameField()


car_prices_data = {
    "Brand": [
        "Honda Civic",
        "Ford Focus",
        "Toyota Corolla",
        "Toyota Corolla",
        "Audi A4",
    ],
    "Price": [22000, 27000, 25000, 29000, 35000],
    "Year": [2014, 2015, 2016, 2017, 2018],
}

car_prices = DataFrameModel(title="Car Prices")
car_prices.df = pd.DataFrame(car_prices_data, columns=["Brand", "Price", "Year"])
car_prices.save()

assert car_prices.df.size == 15

assert car_prices in DataFrameModel.query.all()
car_prices = DataFrameModel.query.get(title="Car Prices")
assert isinstance(car_prices.df, pd.DataFrame)
assert car_prices.df.size == 15

for item in DataFrameModel.query.all():
    item.delete()
