# OmniBCI 本地实时 EEG Python SDK 使用说明

## 1. 适用范围

SDK 用于同一台电脑上的 Python 脚本读取 GUI 实时脑电。GUI 使用有线 USB
串口或 BLE 都可以，脚本的连接代码不变。

当前接口只监听本机：

```text
ws://127.0.0.1:8765/v1/stream
```

这是实时接口，不提供历史回放、局域网访问、云端访问或串口原始字节流。

## 2. 启动环境

在项目根目录执行一次：

```powershell
uv sync
```

启动 GUI：

```powershell
uv run python ads1299_eeg_gui_native.py
```

然后在 GUI 中选择 USB 或 BLE，连接设备并开始采集。另开一个终端运行同事
的脚本：

```powershell
uv run python your_model_script.py
```

如果脚本不在项目根目录，可以把 `onmibci_sdk.py` 和
`onmibci_stream.py` 放到脚本目录，或者设置项目根目录到 `PYTHONPATH`：

```powershell
$env:PYTHONPATH = "D:\PycharmProjects\onmiBCI_activatemask"
uv run python D:\work\your_model_script.py
```

## 3. 最小读取示例

### 读取未滤波 raw 数据

```python
from onmibci_sdk import GapEvent, connect_local


client = connect_local()
with client.stream_raw() as stream:
    for item in stream:
        if isinstance(item, GapEvent):
            print(
                "API queue gap:",
                item.dropped_batches,
                "batches /",
                item.dropped_samples,
                "samples",
            )
            continue

        # shape = (samples, 8), dtype = float32, unit = uV
        eeg_uv = item.values
        print(item.sequence[0], eeg_uv.shape, item.valid.tolist())
        # prediction = model.predict(eeg_uv)
```

### 读取 GUI 滤波后的数据

```python
from onmibci_sdk import GapEvent, connect_local


with connect_local().stream_filtered() as stream:
    for item in stream:
        if isinstance(item, GapEvent):
            continue
        filtered_uv = item.values
        # prediction = model.predict(filtered_uv)
```

每个 `stream_raw()` 或 `stream_filtered()` 迭代器都是一条独立的 WebSocket
连接。需要同时读取两条流时，分别创建两个迭代器；模型处理不要阻塞读取循环。

## 4. 数据对象

正常数据项是 `StreamBatch`，主要字段如下：

| 字段 | 含义 |
| --- | --- |
| `values` | `(samples, 8)` 的 `float32` 数组，通道顺序为 CH1 到 CH8，单位为 µV |
| `sequence` | 每个样本的采集序号，`uint32` |
| `valid` | 每个样本是否有效的布尔数组 |
| `modes` | 每个样本的采集模式标记，`uint8` |
| `stream` | `raw` 或 `filtered` |
| `session_id` | 当前 GUI API 会话 ID |
| `generation` | raw 为 `None`；filtered 为当前滤波配置代数 |
| `sample_rate` | 当前为 250 Hz |
| `channels` | `("CH1", ..., "CH8")` |
| `unit` | 当前为 `uV` |

`raw` 是已经解码成 µV、但还没有经过 GUI 滤波的数据，不是串口上的 48 字节
原始帧。raw 保留输入端的饱和值；filtered 是 GUI
`LiveFilterWorker` 的实际输出，可能包含用于屏蔽无效/饱和输入的 `NaN`。

## 5. 缺口处理

需要区分两种缺口：

1. `GapEvent`：Python 客户端读取太慢，API 为保护实时链路丢弃了一个或多个
     完整批次。模型通常应该清空当前窗口，重新积累连续数据。
2. `valid[i] == False` 或 `sequence` 不连续：采集时间线本身存在缺口。BLE
   可恢复的序号缺口会保留在时间线中；不要把无效样本当成真实 EEG。

建议模型至少执行：

```python
import numpy as np


good = item.valid & np.isfinite(item.values).all(axis=1)
usable_values = item.values[good]
```

filtered 数据还应在 `generation` 变化时切分窗口，避免把滤波配置变化前后的
样本直接拼在同一个模型窗口中。

## 6. USB/BLE 行为

USB 和 BLE 使用完全相同的 SDK 调用：

```python
stream = connect_local().stream_raw()
```

差异只在 GUI 的接收层：USB 读取串口解析帧，BLE 读取 BLE 重组后的时间线。
API 的 `values`、`sequence`、`valid` 和 `modes` 字段在两种模式下都保留；同事
的模型不需要判断当前是 USB 还是 BLE 才能读取数据。

## 7. 常见问题

### `ConnectionRefusedError` 或连接超时

确认 GUI 已经启动，并检查是否监听 `127.0.0.1:8765`。API 只在 GUI 启动时
存在；GUI 没有连接设备时也不会产生新的数据批次。

### 能连接但一直没有数据

确认 GUI 已连接 USB/BLE 设备并点击开始采集。此接口只发送连接后的实时数据，
不会自动补发连接前的历史数据。

### 经常收到 `GapEvent`

说明模型推理或后处理阻塞了读取循环。可以把读取和推理拆成两个线程/队列，或
先降低模型处理耗时；不要在收到 gap 后伪造样本。

## 8. 事件标记与带事件 BDF 导出

SDK 的控制接口和数据接口都只连接本机 GUI。事件标记只能在一次实时测量已经开始后发送；GUI 会把事件广播给 raw/filtered 订阅者，并保存到当前测量会话。

```python
from onmibci_sdk import MarkerEvent, connect_local

client = connect_local()

event = client.send_marker(
    code="stimulus_on",
    value=1,
    sequence=12500,       # 推荐：对应脑电采样序号
    description="左视觉刺激开始",
)
print(event.event_id, event.timestamp, event.sequence)

# 也可以不传 sequence；GUI 会使用 SDK 请求到达时的时间戳。
client.send_marker("button_press", "A")

# 测量结束后由 SDK 请求 GUI 停止采集，再导出完整分段 BIN 对应的 BDF+。
client.stop_measurement()
result = client.export_bdf(
    r"D:\recordings\session_001.bdf",
    overwrite=False,
)
print(result.path, result.event_count, result.sample_count)
```

BDF 中的事件写入标准 BDF+ Annotation 通道，包含事件的 onset、duration 和文本（事件 code、value、description）。事件的 onset 优先按 `sequence` 与本次测量首个采样序号对齐；没有 sequence 时才按时间戳相对测量开始时间计算。

导出 BDF 需要可选依赖：

```powershell
uv sync --extra export
```

如果 SDK 收到 `MarkerEvent`，不要把它当作 EEG 数组；`StreamBatch` 才有 `values`、`sequence`、`valid` 和 `modes`。USB 和 BLE 的 SDK 调用完全相同，物理传输差异已经在 GUI 的共同 `process_frames` 链路中处理。

### `ModuleNotFoundError: onmibci_sdk`

确认已在项目环境执行 `uv sync`，并按第 2 节从项目根目录运行，或设置正确的
`PYTHONPATH`。
