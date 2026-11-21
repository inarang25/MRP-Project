"""Lesson 3 (demand): the plan files and the latest run against the rules of the lesson."""
import pandas as pd

from planner import config


def plan(name):
    return pd.read_csv(f"{config.PLAN_DIR}/{name}.csv", parse_dates=["due_date"])


def latest(name):
    return pd.read_csv(f"{config.RUN_DIR}/latest/{name}.csv")


def monthly(forecast, item):
    rows = forecast[forecast["item"] == item]
    return rows.groupby(rows["due_date"].dt.month)["qty"].mean()


def test_acute_medicines_peak_in_the_monsoon_chronic_ones_are_steady():
    forecast = plan("forecast")
    amoxicillin, metformin = monthly(forecast, "F005"), monthly(forecast, "F003")
    assert amoxicillin[8] > 1.3 * amoxicillin[4]          # August vs April
    assert metformin.max() == metformin.min()
    # over the year the seasonal forecast still averages the yearly figure
    assert abs(forecast[forecast["item"] == "F005"]["qty"].mean() / config.FINISHED["F005"]["weekly_demand"] - 1) < 0.02


def test_export_packs_ship_on_contract_dates_and_are_firm_for_twelve_weeks():
    forecast, orders = plan("forecast"), plan("customer_orders")
    for code, contract in config.EXPORT_CONTRACTS.items():
        assert (forecast.loc[forecast["item"] == code, "qty"] == contract["packs"]).all()
        firm = orders[orders["item"] == code]
        assert (firm["type"] == "export").all() and len(firm) == 3
    assert not forecast["item"].eq("F009").any()          # tender packs have no forecast


def test_every_week_plans_the_larger_of_forecast_and_orders():
    forecast, orders = plan("forecast"), plan("customer_orders")
    booked = orders.groupby(["item", "due_date"])["qty"].sum()
    expected = forecast.set_index(["item", "due_date"])["qty"].combine(booked, max, fill_value=0)
    requirements = latest("requirements")
    finished = requirements[requirements["item"].isin(config.FINISHED)].copy()
    finished["due_date"] = pd.to_datetime(finished["date"])
    planned = finished.groupby(["item", "due_date"])["qty"].sum()
    # demand due before the planning date is moved to the planning date, so compare from then on
    expected = expected[expected.index.get_level_values("due_date") >= planned.index.get_level_values("due_date").min()]
    assert (planned.reindex(expected.index) - expected).abs().max() < 1e-6
    assert finished["source"].str.startswith("SO-").any()     # orders stay pegged to the customer


def test_open_tender_is_not_planned_but_its_needs_are_reported():
    orders, impact = latest("planned_orders"), latest("tender_impact")
    assert not orders["item"].eq("F009").any()
    carton = impact[impact["item"] == "M123"].iloc[0]
    assert not carton["regular_use"]
    assert carton["extra_qty"] == 200000 * 1.01         # 2 lakh packs + 1% printing and set-up loss
    api = impact[impact["item"] == "M001"].iloc[0]
    assert api["regular_use"]
    # 2 lakh packs x 100 tablets + 0.5% packing loss, through 98% yield = 20.5 bulk batches of 1,000
    # thousand tablets; batches are whole, and the regular plan's last batch has some spare, so 20 or 21
    per_batch = 500 / 0.993                                  # kg of API per batch at planning potency
    batches = api["extra_qty"] / per_batch
    assert abs(batches - round(batches)) < 1e-3 and round(batches) in (20, 21)
