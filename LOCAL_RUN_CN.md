# 本机运行说明

本项目已经去掉作者机器上的固定路径，所有运行时文件默认放在项目目录内：

- Python 虚拟环境：`.venv`
- Arduino CLI：`.local_tools\arduino-cli`
- Arduino ESP32 core/data：`.arduino`
- 固件编译输出：`build`
- GUI 打包输出：`dist\ActiveMaskGUI\ActiveMaskGUI.exe`

## 直接运行上位机

双击项目根目录：

```text
run_all.cmd
```

它会自动创建 `.venv`，安装 `pc_app\requirements.txt`，然后启动 GUI。

也可以命令行运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\run_gui.ps1
```

## 打包成 exe

双击项目根目录：

```text
package_gui.cmd
```

完成后运行：

```text
dist\ActiveMaskGUI\ActiveMaskGUI.exe
```

## 固件工具链

第一次编译/烧录前安装 ESP32 core：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\install_esp32_core.ps1
```

这会把 Arduino CLI 和 ESP32 core 安装到项目内的 `.local_tools` / `.arduino`，不会再写死到 `D:\arduino_IDE`。

编译：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\compile_firmware.ps1
```

自动选择当前 USB 串口烧录：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\upload_firmware.ps1
```

指定串口烧录，例如当前电脑看到的是 COM4：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\upload_firmware.ps1 COM4
```

## 串口

GUI 现在不再默认写死 COM3，会自动选择系统当前列出的第一个串口。你这台电脑当前识别到的是 `COM4 USB Serial Device`。
