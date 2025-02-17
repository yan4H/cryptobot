""" helpers module """
import logging
import math
import pickle  # nosec
import re
from datetime import datetime
from functools import lru_cache
from os.path import exists, getctime
from time import sleep, time
import ccxt
from ccxt import kucoin

import udatetime
from filelock import SoftFileLock
from tenacity import retry, wait_fixed, stop_after_delay


def mean(values: list[float]) -> float:
    """returns the mean value of an array of integers"""
    return sum(values) / len(values)


@lru_cache(1024)
def percent(part: float, whole: float) -> float:
    """returns the percentage value of a number"""
    result: float = float(whole) / 100 * float(part)
    return result


@lru_cache(1024)
def add_100(number: float) -> float:
    """adds 100 to a number"""
    return 100 + float(number)


@lru_cache(64)
def c_date_from(day: str) -> float:
    """returns a cached datetime.fromisoformat()"""
    return datetime.fromisoformat(day).timestamp()


@lru_cache(64)
def c_from_timestamp(date: float) -> datetime:
    """returns a cached datetime.fromtimestamp()"""
    return datetime.fromtimestamp(date)


@retry(wait=wait_fixed(2), stop=stop_after_delay(10))
def cached_kucoin_client(api_key: str, secret_key: str) -> kucoin:
    """retry wrapper for kucoin client first call"""

    lock = SoftFileLock("state/kucoin.client.lockfile", timeout=10)
    # when running automated-testing with multiple threads, we will hit
    # api requests limits, this happens during the client initialization
    # which mostly issues a ping. To avoid this when running multiple processes
    # we cache the client in a pickled state on disk and load it if it already
    # exists.
    cachefile = "cache/kucoin.client"
    with lock:
        if exists(cachefile) and (
            udatetime.now().timestamp() - getctime(cachefile) < (30 * 60)
        ):
            logging.debug("re-using local cached kucoin.client file")
            with open(cachefile, "rb") as f:
                _client = pickle.load(f)  # nosec
        else:
            try:
                logging.debug("refreshing cached kucoin.client")
                _client = kucoin({
                    "apiKey": api_key,
                    "secret": secret_key
                })
            except Exception as err:
                logging.warning(f"API client exception: {err}")
                if "much request weight used" in str(err):
                    timestamp = (
                        int(re.findall(r"IP banned until (\d+)", str(err))[0])
                        / 1000
                    )
                    logging.info(
                        f"Pausing until {datetime.fromtimestamp(timestamp)}"
                    )
                    while int(time()) < timestamp:
                        sleep(1)
                raise Exception from err  # pylint: disable=broad-exception-raised
            with open(cachefile, "wb") as f:
                pickle.dump(_client, f)

        return _client


def step_size_to_precision(step_size: str) -> int:
    """returns step size"""
    precision: int = step_size.find("1") - 1
    with open("log/binance.step_size_to_precision.log", "at") as f:
        f.write(f"{step_size} {precision}\n")
    return precision


def floor_value(val: float, step_size: str) -> str:
    """floors quantity depending on precision"""
    value: str = ""
    precision: int = step_size_to_precision(step_size)
    if precision > 0:
        value = "{:0.0{}f}".format(  # pylint: disable=consider-using-f-string
            val, precision
        )
    else:
        value = str(math.floor(int(val)))
    with open("log/binance.floor_value.log", "at") as f:
        f.write(f"{val} {step_size} {precision} {value}\n")
    return value
