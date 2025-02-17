""" prove backtesting """
import glob
import json
import os
import re
import subprocess
import sys
from argparse import ArgumentParser, Namespace
from datetime import datetime, timedelta
from itertools import islice
from multiprocessing import Pool
from string import Template
from time import sleep
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
import requests
import yaml
from tenacity import retry, wait_fixed, stop_after_delay


@retry(wait=wait_fixed(2), stop=stop_after_delay(10))
def get_index_json(query: str) -> requests.Response:
    """retry wrapper for requests calls"""
    response: requests.Response = requests.get(query, timeout=5)
    status: int = response.status_code
    if status != 200:
        with open("log/price_log_service.response.log", "at") as l:
            l.write(f"{query} {status} {response}\n")
        response.raise_for_status()
    return response


def log_msg(msg: str) -> None:
    """logs out message prefixed with timestamp"""
    now: str = datetime.now().strftime("%H:%M:%S")
    print(f"{now} PROVE-BACKTESTING: {msg}")


def cleanup() -> None:
    """clean files"""
    for item in glob.glob("configs/coin.*.yaml"):
        os.remove(item)
    for item in glob.glob("results/backtesting.coin.*.txt"):
        os.remove(item)
    for item in glob.glob("results/backtesting.coin.*.log.gz"):
        os.remove(item)
    if os.path.exists("log/backtesting.log"):
        os.remove("log/backtesting.log")


def flag_checks() -> None:
    """checks for flags in control/"""
    while os.path.exists("control/PAUSE"):
        log_msg("control/PAUSE flag found. Sleeping 1min.")
        sleep(60)


def wrap_subprocessing(conf: str, timeout: Optional[int] = 0) -> None:
    """wraps subprocess call"""
    if timeout == 0:
        timeout = None
    subprocess.run(
        "python app.py -m backtesting -s tests/fake.yaml "
        + f"-c configs/{conf} >results/backtesting.{conf}.txt 2>&1",
        shell=True,
        timeout=timeout,
        check=False,
    )


class ProveBacktesting:
    """ProveBacktesting"""

    def __init__(self, cfg: Dict[str, Any]) -> None:
        """init"""
        self.min: float = float(cfg["MIN"])
        self.max: float = float(cfg["MAX"])
        self.filter_by: str = cfg["FILTER_BY"]
        self.from_date: datetime = datetime.strptime(
            str(cfg["FROM_DATE"]), "%Y%m%d"
        )
        self.end_date: datetime = datetime.strptime(
            str(cfg["END_DATE"]), "%Y%m%d"
        )
        self.roll_backwards: int = int(cfg["ROLL_BACKWARDS"])
        self.roll_forward: int = int(cfg["ROLL_FORWARD"])
        self.strategy: str = cfg["STRATEGY"]
        self.runs: Dict[str, Any] = dict(cfg["RUNS"])
        self.pause_for: float = float(cfg["PAUSE_FOR"])
        self.initial_investment: float = float(cfg["INITIAL_INVESTMENT"])
        self.re_invest_percentage: float = float(cfg["RE_INVEST_PERCENTAGE"])
        self.max_coins: int = int(cfg["MAX_COINS"])
        self.pairing: str = str(cfg["PAIRING"])
        self.clear_coin_stats_at_boot: bool = bool(
            cfg["CLEAR_COIN_STATS_AT_BOOT"]
        )
        self.clear_coin_stats_at_sale: bool = bool(
            cfg["CLEAR_COIN_STATS_AT_SALE"]
        )
        self.debug: bool = bool(cfg["DEBUG"])
        self.trading_fee: float = float(cfg["TRADING_FEE"])
        self.sell_as_soon_it_drops: bool = bool(cfg["SELL_AS_SOON_IT_DROPS"])
        self.stop_bot_on_loss: bool = bool(cfg["STOP_BOT_ON_LOSS"])
        self.stop_bot_on_stale: bool = bool(cfg["STOP_BOT_ON_STALE"])
        self.enable_new_listing_checks: bool = bool(
            cfg["ENABLE_NEW_LISTING_CHECKS"]
        )
        self.enable_new_listing_checks_age_in_days: int = int(
            cfg["ENABLE_NEW_LISTING_CHECKS_AGE_IN_DAYS"]
        )
        self.klines_caching_service_url: str = cfg[
            "KLINES_CACHING_SERVICE_URL"
        ]
        self.price_log_service_url: str = cfg["PRICE_LOG_SERVICE_URL"]
        self.concurrency: int = int(cfg["CONCURRENCY"])
        self.start_dates: List[str] = self.generate_start_dates(
            self.from_date, self.end_date, self.roll_forward
        )
        self.sort_by: str = cfg["SORT_BY"]

    def check_for_invalid_values(self) -> None:
        """check for invalid values in the config"""

        if self.sort_by not in [
            "max_profit_on_clean_wins",
            "number_of_clean_wins",
            "greed",
        ]:
            log_msg("SORT_BY set to invalid value")
            sys.exit(1)

    def generate_start_dates(
        self, start_date: datetime, end_date: datetime, jump: Optional[int] = 7
    ) -> List[str]:
        """returns a list of dates, with a gap in 'jump' days"""
        dates = pd.date_range(start_date, end_date, freq="d").strftime(
            "%Y%m%d"
        )
        start_dates: List[str] = list(islice(dates, 0, None, jump))
        return start_dates

    def rollback_dates_from(self, end_date: str) -> List[str]:
        """returns a list of dates, up to 'days' before the 'end_date'"""
        dates: List[str] = (
            pd.date_range(
                datetime.strptime(str(end_date), "%Y%m%d")
                - timedelta(days=self.roll_backwards - 1),
                end_date,
                freq="d",
            )
            .strftime("%Y%m%d")
            .tolist()
        )
        return dates

    def rollforward_dates_from(self, end_date: str) -> List[str]:
        """returns a list of dates, up to 'days' past the 'end_date'"""
        start_date: datetime = datetime.strptime(
            str(end_date), "%Y%m%d"
        ) + timedelta(days=1)
        _end_date: datetime = datetime.strptime(
            str(end_date), "%Y%m%d"
        ) + timedelta(days=self.roll_forward)
        dates: List[str] = (
            pd.date_range(start_date, _end_date, freq="d")
            .strftime("%Y%m%d")
            .tolist()
        )
        return dates

    def generate_price_log_list(
        self, dates: List[str], symbol: Optional[str] = None
    ) -> List[str]:
        """makes up the price log url list"""
        urls: List[str] = []
        for day in dates:
            if symbol:
                if self.filter_by in symbol:
                    urls.append(f"{symbol}/{day}.log.gz")
            else:
                urls.append(f"{day}.log.gz")
        return urls

    def write_single_coin_config(
        self, symbol: str, _price_logs: List[str], thisrun: Dict[str, Any]
    ) -> None:
        """generates a config.yaml for a coin"""

        if self.filter_by not in symbol:
            return

        tmpl: Template = Template(
            """{
        "CLEAR_COIN_STATS_AT_BOOT": $CLEAR_COIN_STATS_AT_BOOT,
        "CLEAR_COIN_STATS_AT_SALE": $CLEAR_COIN_STATS_AT_SALE,
        "DEBUG": $DEBUG,
        "ENABLE_NEW_LISTING_CHECKS": $ENABLE_NEW_LISTING_CHECKS,
        "ENABLE_NEW_LISTING_CHECKS_AGE_IN_DAYS": $ENABLE_NEW_LISTING_CHECKS_AGE_IN_DAYS,
        "INITIAL_INVESTMENT": $INITIAL_INVESTMENT,
        "KLINES_CACHING_SERVICE_URL": "$KLINES_CACHING_SERVICE_URL",
        "MAX_COINS": 1,
        "PAIRING": "$PAIRING",
        "PAUSE_FOR": $PAUSE_FOR,
        "PRICE_LOGS": $PRICE_LOGS,
        "PRICE_LOG_SERVICE_URL": "$PRICE_LOG_SERVICE_URL",
        "RE_INVEST_PERCENTAGE": $RE_INVEST_PERCENTAGE,
        "SELL_AS_SOON_IT_DROPS": $SELL_AS_SOON_IT_DROPS,
        "STOP_BOT_ON_LOSS": $STOP_BOT_ON_LOSS,
        "STOP_BOT_ON_STALE": $STOP_BOT_ON_STALE,
        "STRATEGY": "$STRATEGY",
        "TICKERS": {
          "$COIN": {
              "BUY_AT_PERCENTAGE": "$BUY_AT_PERCENTAGE",
              "SELL_AT_PERCENTAGE": "$SELL_AT_PERCENTAGE",
              "STOP_LOSS_AT_PERCENTAGE": "$STOP_LOSS_AT_PERCENTAGE",
              "TRAIL_TARGET_SELL_PERCENTAGE": "$TRAIL_TARGET_SELL_PERCENTAGE",
              "TRAIL_RECOVERY_PERCENTAGE": "$TRAIL_RECOVERY_PERCENTAGE",
              "SOFT_LIMIT_HOLDING_TIME": "$SOFT_LIMIT_HOLDING_TIME",
              "HARD_LIMIT_HOLDING_TIME": "$HARD_LIMIT_HOLDING_TIME",
              "NAUGHTY_TIMEOUT": "$NAUGHTY_TIMEOUT",
              "KLINES_TREND_PERIOD": "$KLINES_TREND_PERIOD",
              "KLINES_SLICE_PERCENTAGE_CHANGE": "$KLINES_SLICE_PERCENTAGE_CHANGE"
          }
         },
        "TRADING_FEE": $TRADING_FEE,
        }"""
        )

        # on our coin backtesting runs, we want to quit early if we are using
        # a sort_by mode that discards runs with STALES or LOSSES
        if self.sort_by == "greed":
            stop_bot_on_loss = False
            stop_bot_on_stale = False
        else:
            stop_bot_on_loss = True
            stop_bot_on_stale = True

        with open(f"configs/coin.{symbol}.yaml", "wt") as c:
            c.write(
                tmpl.substitute(
                    {
                        "CLEAR_COIN_STATS_AT_BOOT": self.clear_coin_stats_at_boot,
                        "CLEAR_COIN_STATS_AT_SALE": self.clear_coin_stats_at_sale,
                        "COIN": symbol,
                        "DEBUG": self.debug,
                        "ENABLE_NEW_LISTING_CHECKS": False,
                        "ENABLE_NEW_LISTING_CHECKS_AGE_IN_DAYS": 1,
                        "INITIAL_INVESTMENT": self.initial_investment,
                        "KLINES_CACHING_SERVICE_URL": self.klines_caching_service_url,
                        # each coin backtesting run should only use one coin
                        # MAX_COINS will only be applied to the final optimized run
                        "MAX_COINS": 1,
                        "PAIRING": self.pairing,
                        "PAUSE_FOR": self.pause_for,
                        "PRICE_LOGS": _price_logs,
                        "PRICE_LOG_SERVICE_URL": self.price_log_service_url,
                        "RE_INVEST_PERCENTAGE": 100,
                        "SELL_AS_SOON_IT_DROPS": self.sell_as_soon_it_drops,
                        "STOP_BOT_ON_LOSS": stop_bot_on_loss,
                        "STOP_BOT_ON_STALE": stop_bot_on_stale,
                        "STRATEGY": self.strategy,
                        "TRADING_FEE": self.trading_fee,
                        "BUY_AT_PERCENTAGE": thisrun["BUY_AT_PERCENTAGE"],
                        "SELL_AT_PERCENTAGE": thisrun["SELL_AT_PERCENTAGE"],
                        "STOP_LOSS_AT_PERCENTAGE": thisrun[
                            "STOP_LOSS_AT_PERCENTAGE"
                        ],
                        "TRAIL_TARGET_SELL_PERCENTAGE": thisrun[
                            "TRAIL_TARGET_SELL_PERCENTAGE"
                        ],
                        "TRAIL_RECOVERY_PERCENTAGE": thisrun[
                            "TRAIL_RECOVERY_PERCENTAGE"
                        ],
                        "SOFT_LIMIT_HOLDING_TIME": thisrun[
                            "SOFT_LIMIT_HOLDING_TIME"
                        ],
                        "HARD_LIMIT_HOLDING_TIME": thisrun[
                            "HARD_LIMIT_HOLDING_TIME"
                        ],
                        "NAUGHTY_TIMEOUT": thisrun["NAUGHTY_TIMEOUT"],
                        "KLINES_TREND_PERIOD": thisrun["KLINES_TREND_PERIOD"],
                        "KLINES_SLICE_PERCENTAGE_CHANGE": thisrun[
                            "KLINES_SLICE_PERCENTAGE_CHANGE"
                        ],
                    }
                )
            )

    def write_optimized_strategy_config(
        self,
        _price_logs: List[str],
        _tickers: Dict[str, Any],
        s_balance: float,
    ) -> None:
        """generates a config.yaml for a coin"""

        # we keep "state" between optimized runs, by soaking up an existing
        # optimized config file and an existing wallet.json file
        # while this could cause the bot as it starts to run  to pull old
        # optimized config files from old runs, we only consume those for
        # matching ticker info to the contents of our wallet.json, and we clean
        # up the json files at the start and end of the prove-backtesting.
        # so we don't expect to ever consume old tickers info from an old
        # config file.
        old_tickers: Dict[str, Any] = {}
        old_wallet: List[str] = []
        if os.path.exists(f"configs/optimized.{self.strategy}.yaml"):
            with open(
                f"configs/optimized.{self.strategy}.yaml", encoding="utf-8"
            ) as c:
                old_tickers = yaml.safe_load(c.read())["TICKERS"]

        if os.path.exists(f"tmp/optimized.{self.strategy}.yaml.wallet.json"):
            with open(f"tmp/optimized.{self.strategy}.yaml.wallet.json") as w:
                old_wallet = json.load(w)

        # now generate tickers from the contents of our wallet and the previous
        # config file, we will merge this with a new config file.
        x: Dict[str, Any] = {}
        for symbol in old_wallet:
            x[symbol] = old_tickers[symbol]

        log_msg(f" wallet: {old_wallet}")

        z: Dict[str, Any] = x | _tickers
        _tickers = z

        tmpl: Template = Template(
            """{
        "CLEAR_COIN_STATS_AT_BOOT": $CLEAR_COIN_STATS_AT_BOOT,
        "CLEAR_COIN_STATS_AT_SALE": $CLEAR_COIN_STATS_AT_SALE,
        "DEBUG": $DEBUG,
        "ENABLE_NEW_LISTING_CHECKS": $ENABLE_NEW_LISTING_CHECKS,
        "ENABLE_NEW_LISTING_CHECKS_AGE_IN_DAYS": $ENABLE_NEW_LISTING_CHECKS_AGE_IN_DAYS,
        "INITIAL_INVESTMENT": $INITIAL_INVESTMENT,
        "KLINES_CACHING_SERVICE_URL": "$KLINES_CACHING_SERVICE_URL",
        "MAX_COINS": $MAX_COINS,
        "PAIRING": "$PAIRING",
        "PAUSE_FOR": $PAUSE_FOR,
        "PRICE_LOGS": $PRICE_LOGS,
        "PRICE_LOG_SERVICE_URL": "$PRICE_LOG_SERVICE_URL",
        "RE_INVEST_PERCENTAGE": $RE_INVEST_PERCENTAGE,
        "SELL_AS_SOON_IT_DROPS": $SELL_AS_SOON_IT_DROPS,
        "STOP_BOT_ON_LOSS": $STOP_BOT_ON_LOSS,
        "STOP_BOT_ON_STALE": $STOP_BOT_ON_STALE,
        "STRATEGY": "$STRATEGY",
        "TICKERS": $TICKERS,
        "TRADING_FEE": $TRADING_FEE
        }"""
        )

        with open(f"configs/optimized.{self.strategy}.yaml", "wt") as c:
            c.write(
                tmpl.substitute(
                    {
                        "CLEAR_COIN_STATS_AT_BOOT": self.clear_coin_stats_at_boot,
                        "CLEAR_COIN_STATS_AT_SALE": self.clear_coin_stats_at_sale,
                        "DEBUG": self.debug,
                        "ENABLE_NEW_LISTING_CHECKS": self.enable_new_listing_checks,
                        "ENABLE_NEW_LISTING_CHECKS_AGE_IN_DAYS": self.enable_new_listing_checks_age_in_days,  # pylint: disable=line-too-long
                        "INITIAL_INVESTMENT": s_balance,
                        "KLINES_CACHING_SERVICE_URL": self.klines_caching_service_url,
                        "MAX_COINS": self.max_coins,
                        "PAIRING": self.pairing,
                        "PAUSE_FOR": self.pause_for,
                        "PRICE_LOGS": _price_logs,
                        "PRICE_LOG_SERVICE_URL": self.price_log_service_url,
                        "RE_INVEST_PERCENTAGE": self.re_invest_percentage,
                        "SELL_AS_SOON_IT_DROPS": self.sell_as_soon_it_drops,
                        "STOP_BOT_ON_LOSS": self.stop_bot_on_loss,
                        "STOP_BOT_ON_STALE": self.stop_bot_on_stale,
                        "STRATEGY": self.strategy,
                        "TICKERS": _tickers,
                        "TRADING_FEE": self.trading_fee,
                    }
                )
            )

    def filter_on_avail_days_with_log(
        self, dates: List[str], data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """build a dictionary with all the coins that have price log entries
        available for the dates we asked to backtest.
        then append the list of available price logs to that { coin: [] }
        """

        next_run_coins: Dict[str, Any] = {}

        for day in data.keys():
            if day in dates:
                for coin in data[day]:
                    # discard any BULL/BEAR tokens
                    if any(
                        f"{w}{self.pairing}" in coin
                        for w in ["UP", "DOWN", "BULL", "BEAR"]
                    ) or any(
                        f"{self.pairing}{w}" in coin
                        for w in ["UP", "DOWN", "BULL", "BEAR"]
                    ):
                        continue
                    if (
                        self.filter_by in coin
                        and self.pairing in coin
                        and coin.endswith(self.pairing)
                    ):
                        if coin not in next_run_coins:
                            next_run_coins[coin] = []
                        next_run_coins[coin].append(f"{coin}/{day}.log.gz")

        return next_run_coins

    def filter_on_coins_with_min_age_logs(
        self,
        index: Dict[str, Any],
        last_day: str,
        next_run_coins: Dict[str, Any],
    ) -> Dict[str, Any]:
        """from the dict containing all the coins and the price logs to test,
        drop any coin that doesn't have the required number of logs
        as per the enable_new_listing_checks_age_in_days setting
        """

        all_logs: Dict[str, Any] = {}

        # from the dict containing all the coins and the price logs to test,
        # drop any coin that doesn't have the required number of logs
        # as per the enable_new_listing_checks_age_in_days setting
        for day in index.keys():
            # skip any empty dates in index.json.gz
            if not index[day]:
                continue
            # we need to make sure we don't keep dates past the last day
            # we're backtesting
            if datetime.strptime(day, "%Y%m%d") > datetime.strptime(
                last_day, "%Y%m%d"
            ):
                continue
            for coin in list(next_run_coins.keys()):
                if coin not in all_logs:
                    all_logs[coin] = []
                all_logs[coin].append(f"{coin}/{day}.log.gz")

        for coin in list(next_run_coins.keys()):
            if (
                len(all_logs[coin])
                <= self.enable_new_listing_checks_age_in_days
            ):
                del next_run_coins[coin]

        return next_run_coins

    def write_all_coin_configs(
        self, dates: List[str], thisrun: Dict[str, Any]
    ) -> Set[str]:
        """generate all coinfiles"""

        r: requests.Response = get_index_json(
            f"{self.price_log_service_url}/index_v2.json.gz"
        )
        index: Any = json.loads(r.content)
        index_dates = index["DATES"]

        next_run_coins: Dict[str, Any] = self.filter_on_avail_days_with_log(
            dates, index_dates
        )

        if self.enable_new_listing_checks:
            next_run_coins = self.filter_on_coins_with_min_age_logs(
                index_dates, dates[-1], next_run_coins
            )
        for coin, _price_logs in next_run_coins.items():
            self.write_single_coin_config(coin, _price_logs, thisrun)

        return set(next_run_coins.keys())

    def parallel_backtest_all_coins(
        self, _coin_list: Set[str], n_tasks: int, _run: str
    ) -> Dict[str, Any]:
        """parallel_backtest_all_coins"""

        tasks: List[Any] = []
        with Pool(processes=n_tasks) as pool:
            for coin in _coin_list:
                if self.filter_by in coin and self.pairing in coin:
                    # then we backtesting this strategy run against each coin
                    # ocasionally we get stuck runs, so we timeout a coin run
                    # to a maximum of 15 minutes
                    job: Any = pool.apply_async(
                        wrap_subprocessing,
                        (f"coin.{coin}.yaml",),
                    )
                    tasks.append(job)

            for t in tasks:
                try:
                    t.get()
                except subprocess.TimeoutExpired as excp:
                    log_msg(f"timeout while running: {excp}")

        for coin in _coin_list:
            try:
                os.remove(f"tmp/coin.{coin}.yaml.coins.json")
                os.remove(f"tmp/coin.{coin}.yaml.wallet.json")
                os.remove(f"tmp/coin.{coin}.yaml.results.json")
            except:  # pylint: disable=bare-except
                pass

        return self.gather_best_results_from_run(_coin_list, _run)

    def gather_best_results_from_run(
        self, _coin_list: Set[str], run_id: str
    ) -> Dict[str, Any]:
        """finds the best results from run"""
        wins_re: str = r".*INFO.*\swins:([0-9]+)\slosses:([0-9]+)\sstales:([0-9]+)\sholds:([0-9]+)"
        balance_re: str = r".*INFO.*final\sbalance:\s(-?[0-9]+\.[0-9]+)"

        highest_profit: float = float(0)
        coin_with_highest_profit: str = ""

        _run: Dict[str, Any] = {}
        _run["total_wins"] = 0
        _run["total_losses"] = 0
        _run["total_stales"] = 0
        _run["total_holds"] = 0
        _run["total_profit"] = 0

        # TODO: parsing logfiles is not nice, rework this in app.py
        for symbol in _coin_list:
            results_txt: str = f"results/backtesting.coin.{symbol}.yaml.txt"
            with open(results_txt) as r:
                run_results: str = r.read()

            try:
                wins, losses, stales, holds = re.search(
                    wins_re, run_results
                ).groups()  # type: ignore
                balance = float(
                    re.search(balance_re, run_results).groups()[0]  # type: ignore
                )
            except AttributeError as e:
                log_msg(
                    f"Exception while collecting results from {results_txt}"
                )
                log_msg(str(e))
                log_msg(f"Contents of file below: \n{run_results}")
                wins, losses, stales, holds = [0, 0, 0, 0]
                balance = float(0)

            if self.sort_by in [
                "number_of_clean_wins",
                "max_profit_on_clean_wins",
            ]:
                if (int(losses) + int(stales) + int(holds)) == 0:
                    _run["total_wins"] += int(wins)
                    _run["total_losses"] += int(losses)
                    _run["total_stales"] += int(stales)
                    _run["total_holds"] += int(holds)
                    _run["total_profit"] += float(balance)
            else:
                # greed
                _run["total_wins"] += int(wins)
                _run["total_losses"] += int(losses)
                _run["total_stales"] += int(stales)
                _run["total_holds"] += int(holds)
                _run["total_profit"] += float(balance)

            if balance > highest_profit:
                if self.sort_by in [
                    "number_of_clean_wins",
                    "max_profit_on_clean_wins",
                ]:
                    if (int(losses) + int(stales) + int(holds)) == 0:
                        coin_with_highest_profit = symbol
                        highest_profit = float(balance)
                else:
                    # greed
                    coin_with_highest_profit = symbol
                    highest_profit = balance

        log_msg(
            f" {run_id}: sum of all coins profit:{_run['total_profit']:.3f}|"
            + f"w:{_run['total_wins']},l:{_run['total_losses']},"
            + f"s:{_run['total_stales']},h:{_run['total_holds']}|"
            + "coin with highest profit:"
            + f"{coin_with_highest_profit}:{highest_profit:.3f}"
        )
        return _run

    def parse_backtesting_line(
        self, line, coins
    ) -> Tuple[bool, Dict[str, Any]]:
        """process line of the backtesting log"""

        _profit, _, _, wls, cfgname, _cfg = line[7:].split("|")
        if not self.filter_by in cfgname:
            return (False, {})
        profit: float = float(_profit)
        if profit < 0 or profit < float(self.min) or profit > float(self.max):
            return (False, {})

        coin: str = cfgname[9:].split(".")[0]
        w, l, s, h = [int(x[1:]) for x in wls.split(",")]

        if self.sort_by in [
            "number_of_clean_wins",
            "max_profit_on_clean_wins",
        ]:
            # drop any results containing losses, stales, or holds
            if 0 not in [l, s, h] or w == 0:
                return (False, {})

        blob: Dict[str, Any] = json.loads(_cfg)
        if "TICKERS" in blob.keys():
            coincfg = blob["TICKERS"][coin]  # pylint: disable=W0123

        if coin not in coins:
            coins[coin] = {
                "profit": profit,
                "wls": wls,
                "w": w,
                "l": l,
                "s": s,
                "h": h,
                "cfgname": cfgname,
                "coincfg": coincfg,
            }
        else:
            if self.sort_by in [
                "greed",
                "max_profit_on_clean_wins",
            ]:
                if profit > coins[coin]["profit"]:
                    coins[coin] = {
                        "profit": profit,
                        "wls": wls,
                        "w": w,
                        "l": l,
                        "s": s,
                        "h": h,
                        "cfgname": cfgname,
                        "coincfg": coincfg,
                    }
            if self.sort_by == "number_of_clean_wins":
                if w >= coins[coin]["w"]:
                    # if this run has the same amount of wins but higher
                    # profit, then keep this one.
                    if (
                        w == coins[coin]["w"]
                        and profit < coins[coin]["profit"]
                    ):
                        return (False, {})
                    coins[coin] = {
                        "profit": profit,
                        "wls": wls,
                        "w": w,
                        "l": l,
                        "s": s,
                        "h": h,
                        "cfgname": cfgname,
                        "coincfg": coincfg,
                    }
        return (True, coins)

    def gather_best_results_from_backtesting_log(
        self, kind: str
    ) -> Dict[str, Any]:
        """parses backtesting.log for the best result for a coin"""
        coins: Dict[str, Any] = {}
        _results: Dict[str, Any] = {}
        log: str = "log/backtesting.log"
        if os.path.exists(log):
            with open(log, encoding="utf-8") as lines:
                for line in lines:
                    ok, _coins = self.parse_backtesting_line(line, coins)
                    if ok:
                        coins = _coins

        for coin in coins:  # pylint: disable=consider-using-dict-items
            if kind == "coincfg":
                _results[coin] = coins[coin]["coincfg"]
        return _results

    def gather_best_results_per_strategy(self, this: Dict[str, Any]) -> None:
        """finds the best results in the strategy"""
        best_run: str = ""
        best_profit_in_runs: int = 0
        for _run in this.keys():
            if this[_run]["total_profit"] >= best_profit_in_runs:
                best_run = _run
                best_profit_in_runs = this[_run]["total_profit"]
        log_msg(
            f"{self.strategy} best run {best_run} profit: {best_profit_in_runs:.3f}"
        )

    def run_optimized_config(self, s_investment: float) -> float:
        """runs optimized config"""
        with open(f"configs/optimized.{self.strategy}.yaml") as cf:
            _tickers: Dict[str, Any] = yaml.safe_load(cf.read())["TICKERS"]

        wrap_subprocessing(f"optimized.{self.strategy}.yaml")
        with open(
            f"results/backtesting.optimized.{self.strategy}.yaml.txt"
        ) as results_txt:
            r = results_txt.read()

            end_investment = float(
                re.findall(r"investment: start: .* end: (\d+\.?\d+)", r)[0]
            )

            _diff = str(int(100 - ((s_investment / end_investment) * 100)))
            if int(_diff) > 0:
                _diff = f"+{_diff}"
            log_msg(
                f" final investment for {self.strategy}: {str(end_investment)} {_diff}%"
            )

        return end_investment


if __name__ == "__main__":
    for f in glob.glob("tmp/*"):
        os.remove(f)

    parser: ArgumentParser = ArgumentParser()
    parser.add_argument("-c", "--cfgs", help="backtesting cfg")
    args: Namespace = parser.parse_args()

    with open(args.cfgs, encoding="utf-8") as _c:
        config: Any = yaml.safe_load(_c.read())

    if config["KIND"] != "PROVE_BACKTESTING":
        log_msg("Incorrect KIND: type")
        sys.exit(1)

    if os.path.exists("cache/binance.client"):
        os.remove("cache/binance.client")

    n_cpus: Optional[int] = os.cpu_count()

    pv: ProveBacktesting = ProveBacktesting(config)
    pv.check_for_invalid_values()

    # generate start_dates
    log_msg(
        f"running from {pv.start_dates[0]} to {pv.start_dates[-1]} "
        + f"backtesting previous {pv.roll_backwards} days every {pv.roll_forward} days"
    )
    final_investment: float = pv.initial_investment
    starting_investment: float = pv.initial_investment
    for date in pv.start_dates:
        cleanup()

        rollbackward_dates: List[str] = pv.rollback_dates_from(date)
        log_msg(
            f"now backtesting {rollbackward_dates[0]}...{rollbackward_dates[-1]}"
        )

        results: Dict[str, Any] = {}
        for run in pv.runs:
            flag_checks()
            # TODO: do we consume the price_logs ?
            coin_list: Set[str] = pv.write_all_coin_configs(
                rollbackward_dates, pv.runs[run]
            )
            results[run] = pv.parallel_backtest_all_coins(
                coin_list, pv.concurrency, run
            )

        # TODO: this simply prints out the best run
        pv.gather_best_results_per_strategy(results)
        rollforward_dates: List[str] = pv.rollforward_dates_from(date)
        price_logs = pv.generate_price_log_list(rollforward_dates)
        tickers = pv.gather_best_results_from_backtesting_log("coincfg")

        log_msg(
            f"now forwardtesting {rollforward_dates[0]}...{rollforward_dates[-1]}"
        )
        log_msg(
            f" starting investment for {pv.strategy}: {starting_investment}"
        )

        pv.write_optimized_strategy_config(
            price_logs, tickers, starting_investment
        )
        final_investment = pv.run_optimized_config(starting_investment)
        starting_investment = final_investment

    log_msg("COMPLETED WITH RESULTS:")
    diff = str(int(100 - ((pv.initial_investment / final_investment) * 100)))
    if int(diff) > 0:
        diff = f"+{diff}"
    log_msg(f" {pv.strategy}: {final_investment} {diff}%")
    for f in glob.glob("tmp/*"):
        os.remove(f)
    log_msg("PROVE-BACKTESTING: FINISHED")
