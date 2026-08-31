# NCS 3.4.0 carries a Nordic cleanup call in MCUboot's common main.c even for
# non-Nordic boards. Force-include a no-op compatibility macro for STM32 builds.
zephyr_compile_options(-include ${CMAKE_CURRENT_LIST_DIR}/mcuboot_stm32_compat.h)
