cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(bciband_stm32_production)

target_sources(app PRIVATE
	src/main.c
	src/feature_logic.c
	src/status_io.c
	src/sd_recorder.c
)
target_include_directories(app PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/../protocol)
