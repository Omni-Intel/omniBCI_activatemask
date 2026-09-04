cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(bciband_feature_logic_tests)

target_sources(app PRIVATE src/main.c ../../src/feature_logic.c)
target_include_directories(app PRIVATE ../../src)
