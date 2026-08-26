board_runner_args(jlink "--device=STM32H563VG" "--reset-after-load")
include(${ZEPHYR_BASE}/boards/common/jlink.board.cmake)
