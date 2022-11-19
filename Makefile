.PHONY: default
default: help ;
PATH  := $(PWD)/.venv/bin:$(PATH)
SHELL := /bin/bash

WHOAMI := $$(whoami)
SMP_MULTIPLIER := 1
DCOMPOSE_ID := $$( cat state/dcompose.id 2>/dev/null )
PORT1 := $$( comm -23 <(seq 20000 65535) <(ss -tan | awk '{print $$4}' | cut -d':' -f2 | grep "[0-9]\{1,5\}" | sort | uniq) | shuf | head -n 1)
PORT2 := $$( comm -23 <(seq 20000 65535) <(ss -tan | awk '{print $$4}' | cut -d':' -f2 | grep "[0-9]\{1,5\}" | sort | uniq) | shuf | head -n 1)
PORT3 := $$( comm -23 <(seq 20000 65535) <(ss -tan | awk '{print $$4}' | cut -d':' -f2 | grep "[0-9]\{1,5\}" | sort | uniq) | shuf | head -n 1)
PORT4 := $$( comm -23 <(seq 20000 65535) <(ss -tan | awk '{print $$4}' | cut -d':' -f2 | grep "[0-9]\{1,5\}" | sort | uniq) | shuf | head -n 1)
BIND := "127.0.0.1"
PREFIX_VARS := U=`id -u` G=`id -g` BIND=$(BIND) PORT1=$(PORT1) PORT2=$(PORT2) PORT3=$(PORT3) PORT4=$(PORT4) WHOAMI=`whoami` DCOMPOSE_ID=`cat state/dcompose.id 2>/dev/null`

dcompose_id:
	test -e state/dcompose.id || echo "cryptobot-network-`pwd|md5sum|cut -c1-8`" > state/dcompose.id

checks:
	@if [ "`docker --version | cut -d " " -f3 | tr -d 'v'| cut -c1`" -lt 2 ]; \
		then echo "docker version is too old"; exit 1; fi
	@if [ "`docker compose version | cut -d " " -f4 | tr -d 'v'| cut -c1`" -lt 2 ]; \
		then echo "docker compose version is too old"; exit 1; fi

latest: checks
	docker pull ghcr.io/azulinho/cryptobot:latest

logmode: checks latest dcompose_id
	$(PREFIX_VARS) docker compose --profile logmode -p $(DCOMPOSE_ID) run --rm \
		--service-ports cryptobot -s secrets/binance.prod.yaml \
		-c configs/$(CONFIG)  -m  logmode > log/logmode.$(CONFIG).txt 2>&1

testnet: checks latest dcompose_id
	$(PREFIX_VARS) docker compose --profile testnet -p $(DCOMPOSE_ID) run --rm \
		--service-ports cryptobot \
		-s secrets/binance.testnet.yaml -c configs/$(CONFIG)  \
		-m  testnet  > log/testnet.$(CONFIG).txt 2>&1

live: checks latest dcompose_id
	$(PREFIX_VARS) docker compose --profile live -p $(DCOMPOSE_ID) run \
		--rm --service-ports cryptobot \
		-s secrets/binance.prod.yaml -c configs/$(CONFIG)  \
		-m  live  >> log/live.$(CONFIG).txt 2>&1

backtesting: checks dcompose_id
	$(PREFIX_VARS) docker compose --profile backtesting -p $(DCOMPOSE_ID) run \
		--rm --service-ports \
		cryptobot -s secrets/binance.prod.yaml -c configs/$(CONFIG)  \
		-m  backtesting  > results/$(CONFIG).txt 2>&1

slice-of-log:
	cut -c1- log/backtesting.log | grep cfg: |  cut -d "|" -f 1,3,4,5,6 \
		| cut -d " " -f 1,22-40 | tr -d " " |cut -c8- | sort -n | cut -d "|" -f 1-4

compress-logs:
	find log/ -name "202*.log" -mmin +60 | xargs -i gzip -3 {}

lastfewdays:
	rm -f lastfewdays.log.gz; for ta in `find log/ -name '202*.gz' |sort -n \
		| tail -$(DAYS)` ; do zcat $$ta | grep -a "$(PAIR)" \
		| grep -vEa 'DOWN$(PAIR)|UP$(PAIR)|BULL$(PAIR)|BEAR$(PAIR)' \
		| gzip -3 >> lastfewdays.log.gz; done

automated-backtesting: checks dcompose_id
	$(PREFIX_VARS) docker compose --profile automated-backtesting -p $(DCOMPOSE_ID) run \
		--rm --service-ports \
		-e LOGFILE=/cryptobot/log/$(LOGFILE) \
		-e CONFIG=configs/$(CONFIG) -e MIN=$(MIN) -e FILTER='$(FILTER)' \
		-e SORTBY=$(SORTBY) \
		-e SMP_MULTIPLIER=$(SMP_MULTIPLIER) \
		automated-backtesting \
		> results/automated-backtesting.$(CONFIG).min$(MIN).$(SORTBY).txt

build: checks dcompose_id
	$(PREFIX_VARS) docker compose --profile build -p $(DCOMPOSE_ID) build

down: checks dcompose_id
	$(PREFIX_VARS) docker compose -p $(DCOMPOSE_ID) down ; \
		cd klines_caching_service && \
	$(PREFIX_VARS) docker compose down

download-price-logs: checks dcompose_id
	$(PREFIX_VARS) docker compose --profile download-price-logs -p $(DCOMPOSE_ID) run --rm \
		download-price-logs -s $(FROM) -e $(TO)

prove-backtesting: checks dcompose_id
	$(PREFIX_VARS) docker compose --profile prove-backtesting -p $(DCOMPOSE_ID) run --rm \
		--service-ports \
		-e FROM=$(FROM) -e BACKTRACK=$(BACKTRACK) -e CONFIG=$(CONFIG) -e MIN=$(MIN) \
		-e FORWARD=$(FORWARD) -e TO=$(TO) -e SORTBY=$(SORTBY) \
		-e SMP_MULTIPLIER=$(SMP_MULTIPLIER) \
		prove-backtesting \
		> results/prove-backtesting.$(CONFIG).min$(MIN).$(SORTBY).$(FROM)_$(TO).f$(FORWARD)d.b$(BACKTRACK)d.txt

config-endpoint-service: checks
	$(PREFIX_VARS) docker compose --profile config-endpoint-service -p $(DCOMPOSE_ID) run --rm \
		--service-ports \
		-e CONFIG=$(CONFIG) -e BACKTRACK=$(BACKTRACK) -e SORTBY=$(SORTBY) -e PAIRING=$(PAIRING) -e MIN=$(MIN) -e TUNED_CONFIG=$(TUNED_CONFIG) \
		config-endpoint-service

klines-caching-service: checks
	$(PREFIX_VARS) docker compose --profile klines -p $(DCOMPOSE_ID) run --rm \
		--service-ports klines

.venv:
	python -m venv .venv
	cd klines_caching_service && python -m venv .venv

pip_packages: .venv
	.venv/bin/pip --disable-pip-version-check install -q wheel
	.venv/bin/pip --disable-pip-version-check install -q -r requirements.txt
	.venv/bin/pip --disable-pip-version-check install -q -r requirements-dev.txt
	klines_caching_service/.venv/bin/pip --disable-pip-version-check install -q wheel
	klines_caching_service/.venv/bin/pip --disable-pip-version-check install -q -r klines_caching_service/requirements.txt
	klines_caching_service/.venv/bin/pip --disable-pip-version-check install -q -r requirements-dev.txt

pre-commit-checks: pip_packages
	.venv/bin/black -q --check app.py
	.venv/bin/black -q --check strategies/
	.venv/bin/black -q --check lib/
	.venv/bin/black -q --check tests/
	.venv/bin/pylint -E app.py
	.venv/bin/pylint -E lib/*.py
	ls strategies/*.py |grep -v Local | xargs .venv/bin/pylint -E
	.venv/bin/mypy  app.py
	.venv/bin/mypy lib/*.py
	ls strategies/*.py |grep -v Local | xargs .venv/bin/mypy
	.venv/bin/pytest --quiet -W ignore --disable-pytest-warnings tests/
	cd klines_caching_service; .venv/bin/black -q --check klines_caching_service.py
	cd klines_caching_service; .venv/bin/pylint -E klines_caching_service.py
	cd klines_caching_service; .venv/bin/mypy klines_caching_service.py
	cd klines_caching_service; .venv/bin/pytest -W ignore --quiet --disable-pytest-warnings tests/

tests: pre-commit-checks

help:
	@echo "USAGE:"
	@echo "make logmode CONFIG=< config.yaml >"
	@echo "make backtesting CONFIG=< config.yaml >"
	@echo "make testnet CONFIG=< config.yaml >"
	@echo "make live CONFIG=< config.yaml >"
	@echo "make slice-of-log"
	@echo "make support"
	@echo "make compress-logs"
	@echo "make lastfewdays DAYS=3 PAIR=USDT"
	@echo "make automated-backtesting LOGFILE=lastfewdays.log.gz \
		CONFIG=backtesting.yaml MIN=10 FILTER='' SORTBY='profit|wins'"
	@echo "make download-price-logs FROM=20210101 TO=20211231"
	@echo "make prove-backtesting CONFIG=myconfig.yaml \
		FROM=20220101 BACKTRACK=90 MIN=20 FORWARD=30 TO=20220901 SORTBY=profit|wins"
	@echo "make config-endpoint-service BIND=0.0.0.0 CONFIG=myconfig.yaml BACKTRACK=30 PAIRING=USDT MIN=10 TUNED_CONFIG=BuyDropSellRecoveryStrategy.yaml SORTBY=wins|profit"
	@echo "make klines_caching_service BIND=0.0.0.0 CONFIG=myconfig.yaml BACKTRACK=30 PAIRING=USDT MIN=10 TUNED_CONFIG=BuyDropSellRecoveryStrategy.yaml SORTBY=wins|profit"

support:
	echo > support.txt
	echo "kernel:" >> support.txt
	uname -a >> support.txt
	echo "os-release:" >> support.txt
	cat /etc/os-release >> support.txt
	echo "docker cryptobot images:" >> support.txt
	docker images | grep cryptobot >> support.txt
	echo "git tag:" >> support.txt
	git tag --sort=v:refname | tail -1 >> support.txt
	echo "configs:" >> support.txt
	ls -l configs/ >> support.txt
	echo "secrets:" >> support.txt
	ls -l secrets/ >> support.txt
	echo "id:" >> support.txt
	id >> support.txt
	echo "docker version:" >> support.txt
	docker --version >> support.txt
	echo "docker compose version:" >> support.txt
	docker compose version >> support.txt
	echo "latest run:"
	cat results/`ls -ltr results/| tail -1 | awk '{ print $$NF }' ` >> support.txt
