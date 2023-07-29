""" load_klines_for_coin: manages the cache/ directory """
import hashlib
import json
import logging
import sys
import threading
from datetime import datetime
from functools import lru_cache
from hashlib import md5
from os import getpid, mkdir
from os.path import exists
from time import sleep
import ccxt


import colorlog  # pylint: disable=E0401
import requests
from flask import Flask, request  # pylint: disable=E0401
from pyrate_limiter import Duration, Limiter, RequestRate
from tenacity import retry, wait_exponential

rate: RequestRate = RequestRate(
    600, Duration.MINUTE
)  # 600 requests per minute
limiter: Limiter = Limiter(rate)

DEBUG = False
PID = getpid()

LOCK = threading.Lock()

c_handler = colorlog.StreamHandler(sys.stdout)
c_handler.setFormatter(
    colorlog.ColoredFormatter(
        "%(log_color)s[%(levelname)s] %(message)s",
        log_colors={
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "red,bg_white",
        },
    )
)
c_handler.setLevel(logging.INFO)

if DEBUG:
    f_handler = logging.FileHandler("log/debug.log")
    f_handler.setLevel(logging.DEBUG)

    logging.basicConfig(
        level=logging.DEBUG,
        format=" ".join(
            [
                "(%(asctime)s)",
                f"({PID})",
                "(%(lineno)d)",
                "(%(funcName)s)",
                "[%(levelname)s]",
                "%(message)s",
            ]
        ),
        handlers=[f_handler, c_handler],
        datefmt="%Y-%m-%d %H:%M:%S",
    )
else:
    logging.basicConfig(
        level=logging.INFO,
        handlers=[c_handler],
    )


app = Flask(__name__)


@lru_cache(64)
def c_from_timestamp(date: float) -> datetime:
    """returns a cached datetime.fromtimestamp()"""
    return datetime.fromtimestamp(date)


@retry(wait=wait_exponential(multiplier=1, max=3))
@limiter.ratelimit("kucoin", delay=True)
def requests_with_backoff(exchange, query: str):
    """retry wrapper for ccxt calls"""

    while True:
        try:
            response = exchange.fetch_ohlcv(query)
            return response
        except (ccxt.RequestTimeout, ccxt.DDoSProtection) as e:
            backoff = int(e.seconds)
            logging.warning(f"HTTP {e.status} from kucoin, sleeping for {backoff}s")
            sleep(backoff)

def process_klines_line(kline):
    """returns date, low, avg, high from a kline"""
    (closetime, _, high, low, _, _) = kline

    date = float(c_from_timestamp(closetime / 1000).timestamp())
    low = float(low)
    high = float(high)
    avg = (low + high) / 2

    return date, low, avg, high


def read_from_local_cache(f_path, symbol):
    """reads kline from local cache if it exists"""

    # wrap results in a try call, in case our cached files are corrupt
    # and attempt to pull the required fields from our data.

    if exists(f"cache/{symbol}/{f_path}"):
        try:
            with open(f"cache/{symbol}/{f_path}", "r") as f:
                results = json.load(f)
        except Exception as err:  # pylint: disable=W0703
            logging.critical(err)
            return (False, [])

        # new listed coins will return an empty array
        # so we bail out early here
        if not results:
            return (True, [])

        # check for valid values by reading one line
        try:
            # pylint: disable=W0612
            (
                closetime,
                _,
                high,
                low,
                _,
                _,
            ) = results[0]
        except Exception as err:  # pylint: disable=W0703
            logging.critical(err)
            return (False, [])

        return (True, results)
    logging.info(f"no file cache/{symbol}/{f_path}")
    return (False, [])


def populate_values(klines, unit):
    """builds averages[], lowest[], highest[] out of klines"""
    _lowest = []
    _averages = []
    _highest = []

    # retrieve and calculate the lowest, highest, averages
    # from the klines data.
    # we need to transform the dates into consumable timestamps
    # that work for our bot.
    for line in klines:
        date, low, avg, high = process_klines_line(line)
        _lowest.append((date, low))
        _averages.append((date, avg))
        _highest.append((date, high))

    # finally, populate all the data coin buckets
    values = {}
    for metric in ["lowest", "averages", "highest"]:
        values[metric] = []

    unit_values = {
        "m": 60,
        "h": 24,
        # for 'Days' we retrieve 1000 days, binance API default
        "d": 1000,
    }

    timeslice = unit_values[unit]
    # we gather all the data we collected and only populate
    # the required number of records we require.
    # this could possibly be optimized, but at the same time
    # this only runs the once when we initialise a coin
    for d, v in _lowest[-timeslice:]:
        values["lowest"].append((d, v))

    for d, v in _averages[-timeslice:]:
        values["averages"].append((d, v))

    for d, v in _highest[-timeslice:]:
        values["highest"].append((d, v))

    return (True, values)

def call_kucoin_for_klines(query):
    """calls upstream kucoin and retrieves the klines for a coin"""
    logging.info(f"calling kucoin on {query}")
    
    with LOCK:
        response = requests_with_backoff(query)
        
    if not response:
        # Empty response typically means kucoin has no klines for this coin
        logging.warning(f"got an empty response from kucoin for {query}")
        return (True, [])
    
    return (True, response.json())


def save_kucoin_klines(query, f_path, klines, mode, symbol):
    """saves kucoin klines for a coin locally"""
    logging.info(f"caching kucoin {query} on cache/{symbol}/{f_path}")
    if mode == "backtesting":
        if not exists(f"cache/{symbol}"):
            mkdir(f"cache/{symbol}")

        with open(f"cache/{symbol}/{f_path}", "w") as f:
            f.write(json.dumps(klines))

@app.route("/")
def load_klines_for_coin():
    """fetches from kucoin or a local cache klines for a coin"""

    symbol = request.args.get("symbol")
    date = int(float(request.args.get("date")))
    mode = request.args.get("mode")

    # Instantiate kucoin exchange class with rate limit options
    kucoin = ccxt.kucoin({
        'apiKey': 'YOUR_API_KEY',
        'secret': 'YOUR_SECRET',
        'enableRateLimit': True, # enable built-in rate limiter
        'rateLimit': 1000, # set delay between requests in milliseconds
    })


    # when we initialize a coin, we pull a bunch of klines from kucoin
    # for that coin and save it to disk, so that if we need to fetch the
    # exact same data, we can pull it from disk instead.
    
    unit_values = {
        "m": (60 * 1000),
        "h": (60 * 60 * 1000),
        "d": (24 * 60 * 60 * 1000)
    }
    
    unit_url_fpath = []
    for unit in ["m", "h", "d"]:
        minutes_before_now = unit_values[unit]
         
        backtest_end_time_ms = date - minutes_before_now
         
        query_params ={
            'symbol': symbol,
            'endTime': backtest_end_time_ms,
            'interval': f'1{unit}'
        }

        response_data= requests_with_backoff(kucoin,query_params)
         
        md5_query= hashlib.md5(repr(query_params).encode()).hexdigest()
        file_path=f"{symbol}.{md5_query}"
          
        unit_url_fpath.append((unit,response_data,file_path))

    values = {}
    for metric in ["lowest", "averages", "highest"]:
        values[metric] = {}
        for unit in ["m", "h", "d", "s"]:
            values[metric][unit] = []

    for unit, query, f_path in unit_url_fpath:
        klines = []
        ok, klines = read_from_local_cache(f_path, symbol)
        if not ok:
            ok, klines = call_kucoin_for_klines(query)
            if ok:
                save_kucoin_klines(query, f_path, klines, mode, symbol)

        if ok:
            ok, low_avg_high = populate_values(klines, unit)

        if ok:
            for metric in low_avg_high.keys():  # pylint: disable=C0201,C0206
                values[metric][unit] = low_avg_high[metric]
                # make sure we don't keep more values that we should
                timeslice, _ = unit_values[unit]
                while len(values[metric][unit]) > timeslice:
                    values[metric][unit].pop()
    return values


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8996)
