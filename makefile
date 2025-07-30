# ==============================================================================
# Makefile for GTK Taskbar Project (Production Version)
# - Installs Python dependencies into a virtual environment.
# - Self-diagnoses and compiles C code with all required libraries.
# - Runs the full application stack with automatic cleanup.
# ==============================================================================

# --- Variables ---
CC = gcc
WAYLAND_SCANNER = wayland-scanner
SRC_DIR = src
BIN_DIR = bin
GEN_DIR = gen
VENV_DIR = venv
VENV_PYTHON = $(VENV_DIR)/bin/python
VENV_PIP = $(VENV_DIR)/bin/pip

# --- Wayland Protocol Configuration ---
XML_FILE_PATH := $(shell pkg-config --variable=pkgdatadir wlr-protocols)

ifeq ($(XML_FILE_PATH),)
    $(error "wlr-protocols not found. Please install the development package. (e.g., 'sudo dnf install wlr-protocols-devel' on Fedora)")
endif

XML_FILE = $(XML_FILE_PATH)/unstable/wlr-foreign-toplevel-management-unstable-v1.xml

GEN_H = $(GEN_DIR)/wlr-foreign-toplevel-management-unstable-v1-client-protocol.h
GEN_C = $(GEN_DIR)/wlr-foreign-toplevel-management-unstable-v1-client-protocol.c

# --- Compiler and Linker Flags ---
CFLAGS = -O2 -Wall `pkg-config --cflags gtk+-3.0 wayland-client` -I$(GEN_DIR)
LDFLAGS = `pkg-config --libs gtk+-3.0 wayland-client libudev libinput`

# --- Source and Target Executables ---
TOPLEVEL_MONITOR_SRC = $(SRC_DIR)/toplevel_monitor.c
WAIT_FOR_CLICK_SRC = $(SRC_DIR)/wait_for_click.c
TOPLEVEL_MONITOR_BIN = $(BIN_DIR)/toplevel_monitor
WAIT_FOR_CLICK_BIN = $(BIN_DIR)/wait-for-click

# --- Named Pipe for communication ---
FIFO_PATH = /tmp/taskbar-commands.fifo


# --- Main Targets ---
.PHONY: all build run clean venv

all: build

# 'make venv': Creates virtual env and installs dependencies.
venv: $(VENV_DIR)/.installed

# This is a "stamp" file. The rule runs only if the stamp file doesn't exist.
$(VENV_DIR)/.installed:
	@echo "--- Setting up Python virtual environment ---"
	python3 -m venv $(VENV_DIR)
	@echo "Installing Python dependencies (PyGObject, fabric-widgets)..."
	$(VENV_PIP) install --upgrade pip
	$(VENV_PIP) install pygobject
	# Use the specific Git URL for fabric-widgets if it's not on PyPI
	$(VENV_PIP) install git+https://github.com/Fabric-Development/fabric.git
	@touch $@ # Create the stamp file to mark installation as complete.

build: $(TOPLEVEL_MONITOR_BIN) $(WAIT_FOR_CLICK_BIN)

run: venv build
	@echo "--- Starting Application Stack ---"
	@bash -c '\
		set -e; \
		cleanup_ran=0; \
		cleanup() { \
			if [ "$$cleanup_ran" -eq 1 ]; then return; fi; \
			cleanup_ran=1; \
			echo "\n--- Cleaning up resources ---"; \
			if kill -0 "$$wait_pid" 2>/dev/null; then \
				echo "Killing background listener $$wait_pid..."; \
				sudo kill $$wait_pid; \
			fi; \
			sudo rm -f $(FIFO_PATH); \
			echo "Cleanup complete."; \
		}; \
		trap cleanup INT TERM EXIT; \
		echo "Creating and configuring FIFO..."; \
		sudo mkfifo $(FIFO_PATH) || true; \
		sudo chown $(USER):$(USER) $(FIFO_PATH); \
		echo "Starting background click listener..."; \
		sudo $(WAIT_FOR_CLICK_BIN) > $(FIFO_PATH) & \
		wait_pid=$$!; \
		echo "Launching main Python application..."; \
		$(VENV_PYTHON) main.py; \
		wait $$wait_pid || true; \
	'


clean:
	@echo "Cleaning up generated and compiled files..."
	@rm -rf $(BIN_DIR) $(GEN_DIR)
	@sudo rm -f $(FIFO_PATH)
	@echo "Cleanup complete."

# 'make full-clean': A more aggressive clean that also removes the venv.
.PHONY: full-clean
full-clean: clean
	@echo "Removing venv..."
	@rm -rf $(VENV_DIR)
	@echo "Removing pycache..."
	@find . -type d -name "__pycache__" -exec rm -rf {} +
	@echo "Full cleanup complete."

# --- Wayland and Compilation Rules (Unchanged) ---
$(GEN_H): $(XML_FILE)
	@echo "Generating Wayland client header from $<..."
	@mkdir -p $(GEN_DIR)
	$(WAYLAND_SCANNER) client-header $< $@

$(GEN_C): $(XML_FILE)
	@echo "Generating Wayland protocol code from $<..."
	@mkdir -p $(GEN_DIR)
	$(WAYLAND_SCANNER) private-code $< $@

$(TOPLEVEL_MONITOR_BIN): $(TOPLEVEL_MONITOR_SRC) $(GEN_H) $(GEN_C)
	@echo "Compiling toplevel_monitor..."
	@mkdir -p $(BIN_DIR)
	$(CC) $(CFLAGS) -o $@ $(TOPLEVEL_MONITOR_SRC) $(GEN_C) $(LDFLAGS)

$(WAIT_FOR_CLICK_BIN): $(WAIT_FOR_CLICK_SRC)
	@echo "Compiling wait-for-click..."
	@mkdir -p $(BIN_DIR)
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)