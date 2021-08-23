from ticker_subscriber import TickerSubscriber
from ...finance import PriceVolumeHistoryStorage

class VolumeSubscriber(TickerSubscriber):

    classes_subscribing_to = [
        PriceVolumeHistoryStorage
    ]

    def handle(self, channel, data, *args, **kwargs):

        # parse timestamp from data
        # f'{data_history.ticker}:{data_history.publisher}:{data_history.timestamp}'
        [ticker, publisher, timestamp] = data.split(":")

        if not timestamp_is_near_5min(timestamp):
            return

        # logger.debug("near to a 5 min time marker")
        timestamp = get_nearest_5min_timestamp(timestamp)

        volume = VolumeStorage(ticker=ticker, publisher=publisher, timestamp=timestamp)
        index_values = {}

        for index in default_volume_indexes:
            logger.debug(f'process volume for ticker: {ticker}')

            # example key = "XPM_BTC:poloniex:PriceVolumeHistoryStorage:close_price"
            sorted_set_key = f'{ticker}:{publisher}:PriceVolumeHistoryStorage:{index}'

            index_values[index] = [
                float(db_value.decode("utf-8").split(":")[0])
                for db_value
                in self.database.zrangebyscore(sorted_set_key, timestamp - 300, timestamp + 45) # todo: update to scores
            ]

            try:
                if not len(index_values[index]):
                    volume.value = None

                elif index == "close_volume":
                    volume.value = index_values["close_volume"][-1]


            except IndexError:
                pass  # couldn't find a useful value
            except ValueError:
                pass  # couldn't find a useful value
            else:
                if volume.value:
                    volume.index = index
                    volume.save()
                    # logger.info("saved new thing: " + volume.get_db_key())

            # all_values_set = (
            #         set(index_values["open_volume"])
            # )
            #
            # if not len(all_values_set):
            #     return
            #
            # for index in derived_volume_indexes:
            #     volume.value = None
            #     values_set = all_values_set.copy()
            #
            #     if index == "open_volume":
            #         volume.value = index_values["open_volume"][0]
            #
            #     elif index == "low_volume":
            #         volume.value = min(index_values["low_volume"])
            #     elif index == "high_volume":
            #         volume.value = max(index_values["high_volume"])
            #
            #     if volume.value:
            #         volume.index = index
            #         volume.save(publish=True)
