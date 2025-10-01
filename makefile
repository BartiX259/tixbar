# ==============================================================================
# Makefile for GTK Taskbar Project (Production Version)
# - Installs Python dependencies into a virtual environment.
# - Self-diagnoses and compiles C code with all required libraries.
# - Checks for user permissions before running.
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
WAIT_FOR_CLICK_BIN = $(BIN_DIR)/wait_for_click

# --- Named Pipe for communication ---
FIFO_PATH = /tmp/taskbar-commands.fifo


# --- Main Targets ---
.PHONY: all build run clean venv check-permissions full-clean

all: build

# 'make venv': Creates virtual env and installs dependencies.
venv: $(VENV_DIR)/.installed

$(VENV_DIR)/.installed:
	@echo "--- Setting up Python virtual environment ---"
	python3 -m venv $(VENV_DIR)
	@echo "Installing Python dependencies (PyGObject, fabric-widgets)..."
	$(VENV_PIP) install --upgrade pip
	$(VENV_PIP) install pygobject
	$(VENV_PIP) install git+https://github.com/Fabric-Development/fabric.git
	@touch $@

build: $(TOPLEVEL_MONITOR_BIN) $(WAIT_FOR_CLICK_BIN)

# NEW: 'make check-permissions': Verifies user has access to input devices.
check-permissions:
	@echo "--- Checking user permissions for input devices ---"
	@bash -c '\
		set -e; \
		REQUIRED_GROUP="input"; \
		if ! groups | grep -q -w "$$REQUIRED_GROUP"; then \
			echo "   Error: User $$USER is not in the '\''$$REQUIRED_GROUP'\'' group."; \
			echo "   This is required to read keyboard/mouse events without sudo."; \
			echo "   To fix this, run: sudo usermod -aG $$REQUIRED_GROUP $$USER"; \
			echo "   Then, you MUST log out and log back in for the change to take effect."; \
			exit 1; \
		fi; \
		has_perms=0; \
		for device in /dev/input/event*; do \
			if [ -r "$$device" ]; then \
				has_perms=1; \
				break; \
			fi; \
		done; \
		if [ "$$has_perms" -eq 0 ]; then \
			echo "   Error: Cannot read from input devices in /dev/input/."; \
			echo "   Even though you are in the '\''$$REQUIRED_GROUP'\'' group, permissions seem incorrect."; \
			echo "   This might indicate a problem with system udev rules."; \
			exit 1; \
		fi; \
		echo "Permissions look good."; \
	'

# MODIFIED: 'run' now depends on 'check-permissions'.
run: venv build check-permissions
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
				kill $$wait_pid; \
			fi; \
			rm -f $(FIFO_PATH); \
			echo "Cleanup complete."; \
		}; \
		trap cleanup INT TERM EXIT; \
		echo "Creating and configuring FIFO..."; \
		mkfifo $(FIFO_PATH) || true; \
		echo "Starting background click listener..."; \
		$(WAIT_FOR_CLICK_BIN) > $(FIFO_PATH) & \
		wait_pid=$$!; \
		echo "Launching main Python application..."; \
		$(VENV_PYTHON) main.py; \
		wait $$wait_pid || true; \
	'

clean:
	@echo "Cleaning up generated and compiled files..."
	@rm -rf $(BIN_DIR) $(GEN_DIR)
	@rm -f $(FIFO_PATH)
	@echo "Cleanup complete."

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