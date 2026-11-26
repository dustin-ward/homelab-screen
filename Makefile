.PHONY: all test install rollback status clean

all: test

test:
	sh tests/check.sh

install:
	sh scripts/install.sh $(ARGS)

rollback:
	sh scripts/install.sh --rollback $(TAG)

status:
	sh scripts/install.sh --status

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
