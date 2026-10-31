# debug-dsi build.
#   make nds     DSi library, loader and demo (needs BlocksDS: run it as `tools/docker.sh make nds`)
#   make sim     host simulator used by the tests (host compiler)
#   make test    host tests (builds the simulator first)
#   make all     tools/docker.sh make nds, then make test

BUILD    := build
BLOCKSDS ?= /opt/wonderful/thirdparty/blocksds/core

.PHONY: all nds sim test clean
all:
	tools/docker.sh make nds
	$(MAKE) test

# ---------------------------------------------------------------------------------------------------------------
# DSi (BlocksDS)

NDS_CC   := arm-none-eabi-gcc
NDS_AR   := arm-none-eabi-ar
SPECS    := $(BLOCKSDS)/sys/crts/dsi_arm9.specs
NDS_ARCH := -mthumb -mcpu=arm946e-s+nofp
NDS_INC  := -Iinclude -I$(BLOCKSDS)/libs/libnds/include -I$(BLOCKSDS)/libs/dswifi/include
NDS_CFLAGS  := $(NDS_ARCH) -specs=$(SPECS) -std=gnu11 -O2 -g -Wall -Wextra -Wno-unused-parameter \
               -ffunction-sections -fdata-sections -DDSIDBG_NDS $(NDS_INC)
NDS_LDFLAGS := $(NDS_ARCH) -specs=$(SPECS) -g -Wl,--gc-sections \
               -L$(BUILD)/nds -L$(BLOCKSDS)/libs/dswifi/lib -L$(BLOCKSDS)/libs/libnds/lib
NDS_LIBS := -Wl,--start-group -ldsidbg -ldswifi9 -lnds9 -lc -Wl,--end-group
ARM7ELF  := $(BLOCKSDS)/sys/arm7/main_core/arm7_dswifi.elf
NDSTOOL  := $(BLOCKSDS)/tools/ndstool/ndstool
ICON     := $(BLOCKSDS)/sys/icon.bmp

LIB_SRC  := src/agent.c src/hash.c src/platform_nds.c
LIB_OBJ  := $(LIB_SRC:src/%.c=$(BUILD)/nds/lib/%.o) $(BUILD)/nds/lib/irq_nds.o

nds: $(BUILD)/nds/libdsidbg.a $(BUILD)/dsidbg-loader.nds $(BUILD)/dsidbg-demo.nds

$(BUILD)/nds/lib/%.o: src/%.c src/internal.h include/dsidbg.h
	@mkdir -p $(@D)
	$(NDS_CC) $(NDS_CFLAGS) -c $< -o $@

$(BUILD)/nds/lib/irq_nds.o: src/irq_nds.s
	@mkdir -p $(@D)
	$(NDS_CC) $(NDS_ARCH) -specs=$(SPECS) -g -c $< -o $@

$(BUILD)/nds/libdsidbg.a: $(LIB_OBJ)
	rm -f $@
	$(NDS_AR) rcs $@ $^

$(BUILD)/nds/%/main.o: %/main.c include/dsidbg.h
	@mkdir -p $(@D)
	$(NDS_CC) $(NDS_CFLAGS) -c $< -o $@

$(BUILD)/nds/dsidbg-%.elf: $(BUILD)/nds/%/main.o $(BUILD)/nds/libdsidbg.a
	$(NDS_CC) $(NDS_LDFLAGS) $< $(NDS_LIBS) -Wl,-Map,$(@:.elf=.map) -o $@

$(BUILD)/dsidbg-loader.nds: $(BUILD)/nds/dsidbg-loader.elf
	$(NDSTOOL) -c $@ -7 $(ARM7ELF) -9 $< -b $(ICON) "dsidbg loader;debug-dsi;remote debugging" >/dev/null
	@echo "built $@"

$(BUILD)/dsidbg-demo.nds: $(BUILD)/nds/dsidbg-demo.elf
	$(NDSTOOL) -c $@ -7 $(ARM7ELF) -9 $< -b $(ICON) "dsidbg demo;debug-dsi;remote debugging" >/dev/null
	@echo "built $@"

# ---------------------------------------------------------------------------------------------------------------
# Host simulator and tests

HOST_CC     ?= cc
HOST_CFLAGS := -std=gnu11 -O1 -g -Wall -Wextra -Wno-unused-parameter -Iinclude

sim: $(BUILD)/dsidbg-sim

$(BUILD)/dsidbg-sim: src/agent.c src/hash.c src/platform_host.c sim/main.c src/internal.h include/dsidbg.h
	@mkdir -p $(@D)
	$(HOST_CC) $(HOST_CFLAGS) src/agent.c src/hash.c src/platform_host.c sim/main.c -o $@

test: sim
	python3 -m unittest discover -s tests -v

clean:
	rm -rf $(BUILD)
