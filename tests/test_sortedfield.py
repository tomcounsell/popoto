import random
from datetime import date, datetime
from decimal import Decimal
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto
from src.popoto.models.query import QueryException


class SortedDateModel(popoto.Model):
    name = popoto.KeyField()
    birthday = popoto.SortedField(type=date)


lisa = SortedDateModel.create(name="Lisa", birthday=date(1997, 3, 27))
rose = SortedDateModel.create(name="Rose", birthday=date(1997, 2, 11))
jisoo = SortedDateModel.create(name="Jisoo", birthday=date(1995, 1, 3))
jennie = SortedDateModel.create(name="Jennie", birthday=date(1996, 1, 16))

assert lisa in SortedDateModel.query.all()
oldest = SortedDateModel.query.filter(birthday__lt=date(1996, 1, 1))[0]
assert jisoo == oldest
younger_than_rose = SortedDateModel.query.filter(birthday__gt=rose.birthday)
assert len(younger_than_rose) == 1
assert lisa in younger_than_rose

for item in SortedDateModel.query.all():
    item.delete()


class SortedIntModel(popoto.Model):
    product = popoto.KeyField()
    count = popoto.SortedField(type=int)


beans = SortedIntModel.create(product="beans", count=15)
cans = SortedIntModel.create(product="cans", count=2)

assert beans.count > cans.count
more_than_cans = SortedIntModel.query.filter(count__gt=cans.count)
assert beans in more_than_cans

for item in SortedIntModel.query.all():
    item.delete()


class SortedFloatModel(popoto.Model):
    wrestler = popoto.KeyField()
    height = popoto.SortedField(type=float)


john = SortedFloatModel.create(wrestler="John Cena", height=1.85)
rock = SortedFloatModel.create(wrestler="Dwayne Johnson", height=1.96)

assert john in SortedFloatModel.query.filter(height__gte=john.height)
assert john not in SortedFloatModel.query.filter(height__gt=john.height)

for item in SortedFloatModel.query.all():
    item.delete()


class Racer(popoto.Model):
    name = popoto.KeyField()
    fastest_lap = popoto.SortedField(type=float)


tim = Racer.create(name="Tim", fastest_lap=54.92)
bob = Racer.create(name="Bob", fastest_lap=57.11)
joe = Racer.create(name="Joe", fastest_lap=51.90)
assert len(Racer.query.filter(fastest_lap__lt=55)) == 2
assert len(Racer.query.filter(fastest_lap__gte=joe.fastest_lap)) == 3

for item in Racer.query.all():
    item.delete()


class SortedAssetsModel(popoto.Model):
    uuid = popoto.AutoKeyField(auto_uuid_length=6)
    market = popoto.KeyField()
    asset_id = popoto.KeyField(null=False)
    timestamp = popoto.SortedKeyField(
        type=datetime, partition_by=("asset_id", "market")
    )
    market_cap = popoto.SortedField(type=Decimal, partition_by="market")
    price = popoto.DecimalField()


timestamps = [datetime(2022, 1, 1, hour) for hour in range(23)]
for timestamp in timestamps:
    for market in ["beefi", "damnance"]:
        for asset_id in ["SPOON", "BENT"]:
            SortedAssetsModel.create(
                market=market,
                asset_id=asset_id,
                timestamp=timestamp,
                market_cap=Decimal(str(random.randint(int(10e3), int(10e5)))),
                price=Decimal(
                    str(random.randint(1, 100)) + "." + str(random.randint(1, 100))
                ),
            )

assert (
    len(SortedAssetsModel.query.filter(market="beefi", order_by="market_cap"))
    == len(timestamps) * 2
)
assert len(SortedAssetsModel.query.filter(market="damnance", asset_id="BENT")) == len(
    timestamps
)
assert (
    len(
        SortedAssetsModel.query.filter(
            timestamp__gte=datetime(2022, 1, 1, 12),
            timestamp__lt=datetime(2022, 1, 1, 20),
            asset_id="SPOON",
            market="damnance",
        )
    )
    == 8
)
values_result = SortedAssetsModel.query.filter(
    timestamp__gte=datetime(2022, 1, 1, 12),
    timestamp__lte=datetime(2022, 1, 1, 18),
    asset_id="BENT",
    market="damnance",
    values=("timestamp", "price"),
    order_by="timestamp",
)
assert values_result[-1]["timestamp"] == datetime(2022, 1, 1, 18)
assert list(values_result[4].keys()) == ["timestamp", "price"]


for sam in SortedAssetsModel.objects.all():
    sam.delete()


# ===================================================================
# __between range query tests
# ===================================================================


# Test __between with int SortedField
print("Test: __between with int SortedField")


class BetweenIntModel(popoto.Model):
    name = popoto.KeyField()
    score = popoto.SortedField(type=int)


bi_a = BetweenIntModel.create(name="a", score=1)
bi_b = BetweenIntModel.create(name="b", score=5)
bi_c = BetweenIntModel.create(name="c", score=10)
bi_d = BetweenIntModel.create(name="d", score=15)

results = BetweenIntModel.query.filter(score__between=(5, 10))
assert len(results) == 2, f"Expected 2, got {len(results)}"
names = {r.name for r in results}
assert names == {"b", "c"}, f"Expected {{'b', 'c'}}, got {names}"
print("  PASSED: __between with int SortedField")

for item in BetweenIntModel.query.all():
    item.delete()

# Test __between with float SortedField
print("Test: __between with float SortedField")


class BetweenFloatModel(popoto.Model):
    name = popoto.KeyField()
    height = popoto.SortedField(type=float)


bf_a = BetweenFloatModel.create(name="a", height=1.5)
bf_b = BetweenFloatModel.create(name="b", height=2.0)
bf_c = BetweenFloatModel.create(name="c", height=2.5)
bf_d = BetweenFloatModel.create(name="d", height=3.0)

results = BetweenFloatModel.query.filter(height__between=(2.0, 2.5))
assert len(results) == 2, f"Expected 2, got {len(results)}"
names = {r.name for r in results}
assert names == {"b", "c"}, f"Expected {{'b', 'c'}}, got {names}"
print("  PASSED: __between with float SortedField")

for item in BetweenFloatModel.query.all():
    item.delete()

# Test __between with Decimal SortedField
print("Test: __between with Decimal SortedField")


class BetweenDecimalModel(popoto.Model):
    name = popoto.KeyField()
    price = popoto.SortedField(type=Decimal)


bd_a = BetweenDecimalModel.create(name="a", price=Decimal("9.99"))
bd_b = BetweenDecimalModel.create(name="b", price=Decimal("19.99"))
bd_c = BetweenDecimalModel.create(name="c", price=Decimal("29.99"))
bd_d = BetweenDecimalModel.create(name="d", price=Decimal("39.99"))

results = BetweenDecimalModel.query.filter(
    price__between=(Decimal("10.00"), Decimal("30.00"))
)
assert len(results) == 2, f"Expected 2, got {len(results)}"
names = {r.name for r in results}
assert names == {"b", "c"}, f"Expected {{'b', 'c'}}, got {names}"
print("  PASSED: __between with Decimal SortedField")

for item in BetweenDecimalModel.query.all():
    item.delete()

# Test __between with datetime SortedField
print("Test: __between with datetime SortedField")


class BetweenDatetimeModel(popoto.Model):
    name = popoto.KeyField()
    created = popoto.SortedField(type=datetime)


bdt_a = BetweenDatetimeModel.create(name="a", created=datetime(2022, 1, 1, 0))
bdt_b = BetweenDatetimeModel.create(name="b", created=datetime(2022, 1, 1, 6))
bdt_c = BetweenDatetimeModel.create(name="c", created=datetime(2022, 1, 1, 12))
bdt_d = BetweenDatetimeModel.create(name="d", created=datetime(2022, 1, 1, 18))

results = BetweenDatetimeModel.query.filter(
    created__between=(datetime(2022, 1, 1, 6), datetime(2022, 1, 1, 12))
)
assert len(results) == 2, f"Expected 2, got {len(results)}"
names = {r.name for r in results}
assert names == {"b", "c"}, f"Expected {{'b', 'c'}}, got {names}"
print("  PASSED: __between with datetime SortedField")

for item in BetweenDatetimeModel.query.all():
    item.delete()

# Test __between with date SortedField
print("Test: __between with date SortedField")


class BetweenDateModel(popoto.Model):
    name = popoto.KeyField()
    birthday = popoto.SortedField(type=date)


bda_a = BetweenDateModel.create(name="a", birthday=date(1990, 1, 1))
bda_b = BetweenDateModel.create(name="b", birthday=date(1995, 6, 15))
bda_c = BetweenDateModel.create(name="c", birthday=date(2000, 12, 31))
bda_d = BetweenDateModel.create(name="d", birthday=date(2005, 3, 20))

results = BetweenDateModel.query.filter(
    birthday__between=(date(1995, 1, 1), date(2001, 1, 1))
)
assert len(results) == 2, f"Expected 2, got {len(results)}"
names = {r.name for r in results}
assert names == {"b", "c"}, f"Expected {{'b', 'c'}}, got {names}"
print("  PASSED: __between with date SortedField")

for item in BetweenDateModel.query.all():
    item.delete()

# Test __between with partitioned SortedField (partition_by)
print("Test: __between with partitioned SortedField (partition_by)")


class BetweenPartitionedModel(popoto.Model):
    uuid = popoto.AutoKeyField(auto_uuid_length=6)
    category = popoto.KeyField()
    price = popoto.SortedField(type=float, partition_by="category")


bp_a = BetweenPartitionedModel.create(category="fruit", price=1.50)
bp_b = BetweenPartitionedModel.create(category="fruit", price=3.00)
bp_c = BetweenPartitionedModel.create(category="fruit", price=5.00)
bp_d = BetweenPartitionedModel.create(category="veggie", price=3.00)

results = BetweenPartitionedModel.query.filter(
    category="fruit", price__between=(2.00, 4.00)
)
assert len(results) == 1, f"Expected 1, got {len(results)}"
assert results[0].price == 3.00
print("  PASSED: __between with partitioned SortedField")

for item in BetweenPartitionedModel.query.all():
    item.delete()

# Test __between error: non-tuple value
print("Test: __between error with non-tuple value")
try:
    BetweenIntModel.create(name="x", score=5)
    list(BetweenIntModel.query.filter(score__between=42))
    assert False, "Should have raised QueryException"
except QueryException as e:
    assert "tuple or list" in str(e), f"Unexpected error message: {e}"
    print(f"  PASSED: Raised QueryException: {e}")
finally:
    for item in BetweenIntModel.query.all():
        item.delete()

# Test __between error: tuple of wrong length
print("Test: __between error with wrong-length tuple")
try:
    BetweenIntModel.create(name="x", score=5)
    list(BetweenIntModel.query.filter(score__between=(1, 2, 3)))
    assert False, "Should have raised QueryException"
except QueryException as e:
    assert "2 elements" in str(e), f"Unexpected error message: {e}"
    print(f"  PASSED: Raised QueryException: {e}")
finally:
    for item in BetweenIntModel.query.all():
        item.delete()

print("\nAll __between tests passed!")
