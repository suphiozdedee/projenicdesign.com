.PHONY: test format-check install uninstall

test:
	PYTHONWARNINGS=error python3 -m unittest discover -s tests -v

format-check:
	python3 -m black --check codex_reconnect tests

install:
	./install.sh

uninstall:
	./uninstall.sh
