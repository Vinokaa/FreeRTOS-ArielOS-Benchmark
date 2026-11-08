ifdef OS
	INSTALL = install.bat
	EXPORT = export.bat
	PATH_SEP = \\

	IF = if not exist "$(1)" $(2)
else
	SHELL := /bin/bash
	INSTALL = install.sh
	EXPORT = export.sh
	PATH_SEP = /

	IF = if [ ! -d "$(1)" ]; then $(2); fi
endif

.PHONY: setup blinky uart-to-led uart iot

setup:
	$(call IF,esp-idf,git clone https://github.com/espressif/esp-idf.git)
	.$(PATH_SEP)esp-idf$(PATH_SEP)$(INSTALL)

blinky:
ifdef OS
	call .$(PATH_SEP)esp-idf$(PATH_SEP)$(EXPORT) && cd blinky && idf.py build flash monitor
else
	source ./esp-idf/export.sh && cd blinky && idf.py build flash monitor
endif

uart-to-led:
ifdef OS
	call .$(PATH_SEP)esp-idf$(PATH_SEP)$(EXPORT) && cd uart-to-led && idf.py build flash monitor
else
	source ./esp-idf/export.sh && cd uart-to-led && idf.py build flash monitor
endif

uart: uart-to-led

iot:
ifdef OS
	call .$(PATH_SEP)esp-idf$(PATH_SEP)$(EXPORT) && cd iot && idf.py build flash monitor
else
	source ./esp-idf/export.sh && cd iot && idf.py build flash monitor
endif
