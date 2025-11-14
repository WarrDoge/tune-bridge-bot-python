TOKEN := $(TELEGRAM_BOT_TOKEN)
IP := $(SERVER_IP)
APP=music-searcher

.PHONY: all deps lint build deploy help

help:  ## Show this help
	@echo "Targets:"
	@awk 'BEGIN {FS = ":.*##"; printf ""} /^[a-zA-Z0-9_%-]+:.*?##/ { printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""

all: deploy

deps:  ##
	pip3 install -r requirements.txt
	pip3 install nuitka

lint:  ##
	golangci-lint run

build:  ##
	python3 -m nuitka \
		--standalone \
		--onefile \
		--output-filename=$(APP) \
		--enable-plugin=anti-bloat \
		--nofollow-import-to=pytest,setuptools,pip,wheel,pkg_resources,distutils \
		--include-package=telegram \
		--include-package=httpx \
		--include-package=bs4 \
		--include-package=lxml \
		--include-package=fuzzywuzzy \
		--lto=yes \
		--assume-yes-for-downloads \
		--python-flag=no_docstrings \
		--python-flag=no_asserts \
		--remove-output \
		__main__.py

deploy:   ##
	ssh root@$(IP) "systemctl stop $(APP)" || true

	@sed "s/CHANGE_ME_1/$(APP)/g; s/CHANGE_ME_2/$(TOKEN)/g; w $(APP).service" service.tpl >/dev/null

	scp ./$(APP) root@$(IP):/root/
	scp ./$(APP).service root@$(IP):/etc/systemd/system/

	ssh root@$(IP) "chmod +x /root/$(APP)"
	ssh root@$(IP) "systemctl enable $(APP)"
	ssh root@$(IP) "systemctl restart $(APP)"

clean:  ##
	rm ./$(APP) ./$(APP).service

	ssh root@$(IP) "systemctl stop $(APP)"
	ssh root@$(IP) "systemctl disable $(APP)"
	ssh root@$(IP) "rm -f /etc/systemd/system/$(APP).service"
	ssh root@$(IP) "rm -f /root/$(APP)"
