/*
  ESP32-C3 + ADS1299：SRB1 EEG 诊断数据流（软件 SPI，供自定义 MATLAB 窗口读取）

  这版的目标不是伪装成 OpenBCI Cyton，而是把“采集链路是否可靠”说清楚：
    1. 完全保留软件 SPI，不调用 SPI.begin()；
    2. MCU 只发送 ADS1299 原始码，不在 MCU 上滤波；
    3. 每帧包含 32-bit 序号、ADS STATUS、读取耗时、模式和 CRC16；
    4. 上位机可以分别统计：串口 CRC 错、序号丢失、ADS STATUS 错位；
    5. 提供 OpenBCI 风格 SRB1 配置，以及短路噪声和内部方波诊断模式；
    6. 数据流中绝不插入状态文字，避免周期 impulse 和解析错位。

  默认上电配置：CH1-CH5 开启，CH6-CH8 禁用，SRB1 on，BIAS P-only：SENSP=0x1F, SENSN=0x00。

  Arduino IDE：
    USB CDC On Boot = Enabled
    波特率 = 921600

  引脚：
    ADS_CS=GPIO2, SCLK=GPIO21, MOSI=GPIO0, MISO=GPIO20
    DRDY=GPIO10, START=GPIO3, RESET=GPIO1
    TF_CS=GPIO4（始终拉高）, TF_DET=GPIO5
    NSC_CLK=GPIO6

  串口命令：
    b : 开始二进制数据流
    s : 停止数据流
    e : EEG 推荐模式，SRB1，BIAS 仅取 P 端（CH1-5，等同 p / m / *）
    p : EEG 推荐模式别名，SRB1，BIAS 仅取 P 端（CH1-5）
    m : EEG 推荐模式别名，SRB1，BIAS 仅取 P 端（CH1-5）
    n : EEG P/N BIAS 模式，SRB1，BIAS 同时取 P/N（CH1-5）
    o : EEG，SRB1，关闭 BIAS，用于判断 BIAS 环路是否引入问题
    q : 所有通道输入内部短路，用于测板级底噪
    t : 所有通道接 ADS1299 内部方波，用于验证 SPI/数字链路
    * : 强制进入推荐 SRB1 BIAS 配置（P-only, N=0, SRB1 on，CH1-5）
    1 / 2 / 4 / 6 / 8 / 12 / 24 : 修改 ADS1299 PGA 增益并重配当前模式
    r : 清零诊断计数
    ? : 停止数据流后打印诊断信息和寄存器读回

  固定 48 字节数据帧（小端字段）：
    [0]      0xA5
    [1]      0x5A
    [2]      协议版本 = 1
    [3]      帧类型 = 1
    [4..7]   uint32 sample sequence，little-endian
    [8..11]  uint32 micros()，little-endian
    [12..14] ADS1299 STATUS 原始 3 字节
    [15]     flags
               bit0: STATUS 高四位为 1100
               bit1: 开始读取时 DRDY 为低
               bit2: 本次唤醒积压了多个 DRDY
               bit3: 当前为内部测试方波
               bit4: 当前为内部短路
               bit5: BIAS 已开启
               bit6: BIAS 同时取 P/N（legacy）
               bit7: SRB1 已开启
    [16..39] 8 × 24-bit ADS1299 原始通道数据，MSB-first
    [40..41] uint16 本帧软件 SPI 读取耗时 us
    [42]     本次 pending DRDY 数（最大 255）
    [43]     模式：0=legacy P+N, 1=EEG P-only, 2=BIAS off, 3=shorted, 4=test
    [44]     发包前队列深度（最大 255）
    [45]     队列累计丢包数低 8 bit
    [46..47] CRC16-CCITT-FALSE，对 [0..45] 计算，little-endian
*/

#include <Arduino.h>

/*
  SRB1-only reference variant

  Wiring:
    EEG signal electrodes -> IN1P ... IN8P
    common reference      -> SRB1
    bias electrode        -> BIASOUT

  Enforced configuration:
    - CHnSET.SRB2 is always 0, even if host command A7 sets flag bit2.
    - MISC1.SRB1 is enabled only in normal EEG modes.
    - SRB1 is disabled in input-short and internal-test modes.
    - The default EEG mode uses BIAS_SENSP only; BIAS_SENSN is 0.
*/

#include "driver/gpio.h"
#include "driver/uart.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"

// ============================ ADS1299 ============================
#define CONFIG1_250SPS       0x96
#define CONFIG2_NORMAL       0xC0
#define CONFIG2_TEST_SLOW    0xD1
#define CONFIG3_INTERNAL_REF 0xEC

#define CH_MUX_NORMAL        0x00
#define CH_MUX_SHORTED       0x01
#define CH_MUX_TEST          0x05

// CHnSET = PD(bit7) + GAIN(bits6:4) + SRB2(bit3) + MUX(bits2:0)
// GAIN code: 000=1, 001=2, 010=4, 011=6, 100=8, 101=12, 110=24
#define CH_GAIN_CODE_1       0x00
#define CH_GAIN_CODE_2       0x01
#define CH_GAIN_CODE_4       0x02
#define CH_GAIN_CODE_6       0x03
#define CH_GAIN_CODE_8       0x04
#define CH_GAIN_CODE_12      0x05
#define CH_GAIN_CODE_24      0x06

#define MISC1_SRB1_ON        0x20
#define MISC1_SRB1_OFF       0x00

// 默认只启用 CH1-CH5；CH6-CH8 写 PD=1 禁用。
// BIAS_SENSP / BIAS_SENSN 只允许把有效通道位纳入 BIAS 环路。
#define ADS_ACTIVE_CH_MASK   0x1F  // bit0-bit4 = CH1-CH5
#define ADS_FIRST_CH_REG     0x05
#define ADS_LAST_CH_REG      0x0C
#define ADS_LAST_ACTIVE_REG  0x09  // CH5SET
#define CH_POWER_DOWN        0x80

// ADS1299 commands
#define ADS_WAKEUP  0x02
#define ADS_STANDBY 0x04
#define ADS_RESET   0x06
#define ADS_START   0x08
#define ADS_STOP    0x0A
#define ADS_RDATAC  0x10
#define ADS_SDATAC  0x11
#define ADS_RDATA   0x12
#define ADS_RREG    0x20
#define ADS_WREG    0x40

// ============================ Pins ============================
#define PIN_ADS_CS    2
#define PIN_SCLK      21
#define PIN_MOSI      0
#define PIN_MISO      20
#define PIN_DRDY      10
#define PIN_START     3
#define PIN_RESET     1
#define PIN_TF_CS     4
#define PIN_TF_DET    5
#define PIN_NSC_CLK   6

#define NSC_CLK_FREQ_HZ   200000
#define NSC_CLK_RES_BITS  8
#define NSC_CLK_DUTY      128

// ============================ Protocol ============================
constexpr uint32_t SERIAL_BAUD = 921600;
constexpr size_t ADS_FRAME_BYTES = 27;
constexpr size_t STREAM_FRAME_BYTES = 48;
constexpr uint16_t FRAME_QUEUE_LENGTH = 160;
constexpr uint8_t SYNC_1 = 0xA5;
constexpr uint8_t SYNC_2 = 0x5A;
constexpr uint8_t PROTOCOL_VERSION = 1;
constexpr uint8_t FRAME_TYPE_DATA = 1;

struct StreamFrame {
  uint8_t bytes[STREAM_FRAME_BYTES];
};

enum FrontendMode : uint8_t {
  MODE_EEG_BIAS_PN     = 0,
  MODE_EEG_BIAS_P_ONLY = 1,
  MODE_EEG_BIAS_OFF    = 2,
  MODE_INPUT_SHORTED   = 3,
  MODE_INTERNAL_TEST   = 4
};

enum RunPhase : uint8_t {
  PHASE_CONFIG = 0,
  PHASE_STREAMING = 1,
  PHASE_STOPPED = 2
};

// ============================ RTOS objects ============================
static QueueHandle_t frameQueue = nullptr;
static TaskHandle_t adsTaskHandle = nullptr;
static SemaphoreHandle_t adsBusMutex = nullptr;

// ============================ State / diagnostics ============================
volatile bool streamingEnabled = false;
volatile bool adsConversionsRunning = false;
volatile bool configurationVerified = false;
volatile RunPhase runPhase = PHASE_CONFIG;
volatile FrontendMode currentMode = MODE_EEG_BIAS_P_ONLY;
volatile uint32_t acquisitionSequence = 0;
volatile uint32_t drdyCount = 0;
volatile uint32_t missedDrdyCount = 0;
volatile uint32_t lateDrdyCount = 0;
volatile uint32_t mutexBusyCount = 0;
volatile uint32_t badStatusCount = 0;
volatile uint32_t queueDropCount = 0;
volatile uint32_t validReadCount = 0;
volatile uint32_t maxReadTimeUs = 0;
volatile uint8_t discardFramesAfterReconfigure = 0;

// 当前 PGA 增益。默认 24，与原始版本一致。
volatile uint8_t currentGain = 24;
volatile uint8_t currentGainCode = CH_GAIN_CODE_24;

// GUI 可动态修改的 BIAS_SENSP mask；默认 CH1-CH5。
// 注意：这只控制 BIAS_SENSP(0x0D)，不会打开/关闭 CHnSET 通道。
volatile uint8_t currentBiasSensPMask = ADS_ACTIVE_CH_MASK;

// 二进制控制协议：0xA6 <register> <value>
// Python/MATLAB GUI 用 0xA6 0x0D mask 来运行时修改 BIAS_SENSP。
static uint8_t binaryControlState = 0;
volatile uint8_t currentEnabledMask = ADS_ACTIVE_CH_MASK;
volatile uint8_t channelGain[8] = {24, 24, 24, 24, 24, 24, 24, 24};
volatile uint8_t channelGainCode[8] = {
  CH_GAIN_CODE_24, CH_GAIN_CODE_24, CH_GAIN_CODE_24, CH_GAIN_CODE_24,
  CH_GAIN_CODE_24, CH_GAIN_CODE_24, CH_GAIN_CODE_24, CH_GAIN_CODE_24
};

// Binary controls: A6 0D mask, or A7 channel gain flags.
// A7 channel is 0..7; flags: bit0 enabled, bit1 BIAS_P.
// bit2 (SRB2) is deliberately ignored in this SRB1-only firmware.
static uint8_t binaryControlRegister = 0;
static uint8_t binaryControlChannel = 0;
static uint8_t binaryControlGain = 24;

// 串口数字命令缓冲：支持发送 1/2/4/6/8/12/24 修改 PGA 增益。
static char numericCommandBuffer[4] = {0};
static uint8_t numericCommandLength = 0;
static uint32_t lastNumericCommandMs = 0;

// ============================ Declarations ============================
void IRAM_ATTR onDrdyFalling();

void initPins();
void startNscClock();
void hardwareResetAds();
void configureFrontend(FrontendMode mode);
void configureFrontendLocked(FrontendMode mode);
bool verifyFrontendLocked(FrontendMode mode);
void startStreaming();
void stopStreamingGracefully();
void startAdsConversionsLocked();
void stopAdsConversionsLocked();

uint8_t softSpiTransfer(uint8_t tx);
void deselectAllSpi();
void selectAds();
void sendAdsCommand(uint8_t command);
void writeAdsRegister(uint8_t address, uint8_t value);
uint8_t readAdsRegister(uint8_t address);
bool readAdsFrame(uint8_t *destination, bool &drdyWasLow);

void adsAcquireTask(void *argument);
void serialStreamTask(void *argument);
void processSerialByte(char c);
void flushNumericCommand();
void handleCommand(char command);
void setGainByValue(uint8_t gain);
void setBiasSensPMask(uint8_t mask);
void setChannelConfig(uint8_t channel, uint8_t gain, uint8_t flags);
bool gainToCode(uint8_t gain, uint8_t &code);
uint8_t makeChannelSetting(uint8_t gainCode, uint8_t mux);
uint8_t makePoweredDownChannelSetting(uint8_t gainCode);
void printHelpAndDiagnostics();
void clearDiagnostics();
void printRegisterReadback();
void sendConfigAck(uint8_t command, uint8_t argument);

uint16_t crc16CcittFalse(const uint8_t *data, size_t length);
void writeU16LE(uint8_t *destination, uint16_t value);
void writeU32LE(uint8_t *destination, uint32_t value);
void buildStreamFrame(
  StreamFrame &frame,
  const uint8_t *adsRaw,
  uint32_t sequence,
  uint32_t timestampUs,
  uint16_t readTimeUs,
  uint8_t pendingCount,
  bool drdyWasLow
);

// ============================ Setup ============================
void setup() {
  delay(300);
  Serial.begin(SERIAL_BAUD);

  const uint32_t serialWaitStart = millis();
  while (!Serial && (millis() - serialWaitStart < 2500)) {
    delay(10);
  }

  adsBusMutex = xSemaphoreCreateMutex();
  if (!adsBusMutex) {
    while (true) delay(1000);
  }

  initPins();
  startNscClock();
  hardwareResetAds();
  configureFrontendLocked(MODE_EEG_BIAS_P_ONLY);

  frameQueue = xQueueCreate(FRAME_QUEUE_LENGTH, sizeof(StreamFrame));
  if (!frameQueue) {
    while (true) delay(1000);
  }

  const BaseType_t adsTaskResult = xTaskCreate(
    adsAcquireTask,
    "ads_acquire",
    4096,
    nullptr,
    5,
    &adsTaskHandle
  );

  const BaseType_t serialTaskResult = xTaskCreate(
    serialStreamTask,
    "binary_stream",
    4096,
    nullptr,
    2,
    nullptr
  );

  if (adsTaskResult != pdPASS || serialTaskResult != pdPASS) {
    while (true) delay(1000);
  }

  acquisitionSequence = 0;
  drdyCount = 0;
  attachInterrupt(digitalPinToInterrupt(PIN_DRDY), onDrdyFalling, FALLING);

  runPhase = PHASE_STOPPED;

  // 这里只在尚未开始二进制数据流时打印。MATLAB 连接后会 flush 掉这些文字。
  Serial.println();
  Serial.println("ESP32C3 ADS1299 SRB1-ONLY READY - INxP SIGNAL / SRB1 REF / P-ONLY BIAS");
  Serial.println("Commands: b/s/e/p/m/*/n/o/q/t/1/2/4/6/8/12/24/r/? plus binary A6 0D mask for BIAS_SENSP");
}

void loop() {
  vTaskDelay(pdMS_TO_TICKS(1000));
}

// ============================ DRDY ISR ============================
void IRAM_ATTR onDrdyFalling() {
  drdyCount++;

  if (adsTaskHandle) {
    BaseType_t higherPriorityTaskWoken = pdFALSE;
    vTaskNotifyGiveFromISR(adsTaskHandle, &higherPriorityTaskWoken);
    if (higherPriorityTaskWoken == pdTRUE) {
      portYIELD_FROM_ISR();
    }
  }
}

// ============================ Acquisition task ============================
void adsAcquireTask(void *argument) {
  (void)argument;

  for (;;) {
    const uint32_t pending = ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    if (pending == 0) {
      continue;
    }

    // counting notification 被清空时，只能读取当前 ADS 输出寄存器中的最新一帧。
    // 序号按 pending 整体推进，让上位机准确看到中间丢了多少个 DRDY。
    acquisitionSequence += pending;
    const uint32_t sequenceForThisFrame = acquisitionSequence;

    if (pending > 1) {
      missedDrdyCount += pending - 1;
    }

    uint8_t raw[ADS_FRAME_BYTES] = {};
    bool drdyWasLow = false;
    const uint32_t readStartUs = micros();

    if (!readAdsFrame(raw, drdyWasLow)) {
      lateDrdyCount++;
      continue;
    }

    const uint32_t elapsedUs32 = micros() - readStartUs;
    const uint16_t elapsedUs = elapsedUs32 > 65535u
      ? 65535u
      : static_cast<uint16_t>(elapsedUs32);

    if (elapsedUs32 > maxReadTimeUs) {
      maxReadTimeUs = elapsedUs32;
    }

    validReadCount++;

    const bool statusValid = (raw[0] & 0xF0u) == 0xC0u;
    if (!statusValid) {
      badStatusCount++;
    }

    if (discardFramesAfterReconfigure > 0) {
      discardFramesAfterReconfigure--;
      continue;
    }

    if (!streamingEnabled || runPhase != PHASE_STREAMING) {
      continue;
    }

    StreamFrame frame = {};
    buildStreamFrame(
      frame,
      raw,
      sequenceForThisFrame,
      micros(),
      elapsedUs,
      pending > 255u ? 255u : static_cast<uint8_t>(pending),
      drdyWasLow
    );

    // 采集任务永不等待串口。队列满时丢帧，下一帧的 sequence 会产生缺口。
    if (xQueueSend(frameQueue, &frame, 0) != pdTRUE) {
      queueDropCount++;
    }
  }
}

// ============================ Serial task ============================
void serialStreamTask(void *argument) {
  (void)argument;
  StreamFrame frame = {};

  for (;;) {
    while (Serial.available() > 0) {
      const char c = static_cast<char>(Serial.read());
      processSerialByte(c);
    }

    // 允许 MATLAB/串口助手直接发送 "24" 而不带换行；
    // 两个数字到达后等待一个很短的静默间隔再解析。
    if (numericCommandLength > 0 && (millis() - lastNumericCommandMs) > 35) {
      flushNumericCommand();
    }

    if (xQueueReceive(frameQueue, &frame, pdMS_TO_TICKS(2)) == pdTRUE) {
      if (runPhase == PHASE_STREAMING) {
        Serial.write(frame.bytes, STREAM_FRAME_BYTES);
      }
    } else {
      taskYIELD();
    }
  }
}

void processSerialByte(char c) {
  const uint8_t byteValue = static_cast<uint8_t>(c);

  // 二进制控制协议必须先于 ASCII 数字/命令解析：
  // 0xA6 0x0D mask -> 只修改 ADS1299 BIAS_SENSP(0x0D)，BIAS_SENSN 不变。
  if (binaryControlState == 1) {
    binaryControlRegister = byteValue;
    binaryControlState = 2;
    return;
  }

  if (binaryControlState == 2) {
    if (binaryControlRegister == 0x0D) {
      setBiasSensPMask(byteValue);
      sendConfigAck(0xA6, byteValue);
    }
    binaryControlState = 0;
    binaryControlRegister = 0;
    return;
  }

  if (binaryControlState == 10) {
    binaryControlChannel = byteValue;
    binaryControlState = 11;
    return;
  }

  if (binaryControlState == 11) {
    binaryControlGain = byteValue;
    binaryControlState = 12;
    return;
  }

  if (binaryControlState == 12) {
    setChannelConfig(binaryControlChannel, binaryControlGain, byteValue);
    sendConfigAck(0xA7, binaryControlChannel);
    binaryControlState = 0;
    return;
  }

  if (byteValue == 0xA6) {
    flushNumericCommand();
    binaryControlState = 1;
    return;
  }

  if (byteValue == 0xA7) {
    flushNumericCommand();
    binaryControlState = 10;
    return;
  }

  if (c >= '0' && c <= '9') {
    if (numericCommandLength < sizeof(numericCommandBuffer) - 1) {
      numericCommandBuffer[numericCommandLength++] = c;
      numericCommandBuffer[numericCommandLength] = '\0';
      lastNumericCommandMs = millis();
    } else {
      // 超长数字命令直接丢弃，避免把异常文本误判为增益。
      numericCommandLength = 0;
      numericCommandBuffer[0] = '\0';
    }
    return;
  }

  if (c == '\r' || c == '\n' || c == ' ' || c == '\t' || c == ',' || c == ';') {
    flushNumericCommand();
    return;
  }

  // 收到非数字命令前，先把已经收到的数字命令处理掉。
  flushNumericCommand();
  handleCommand(c);
}

void flushNumericCommand() {
  if (numericCommandLength == 0) return;

  numericCommandBuffer[numericCommandLength] = '\0';
  const int requestedGain = atoi(numericCommandBuffer);
  numericCommandLength = 0;
  numericCommandBuffer[0] = '\0';

  if (requestedGain >= 1 && requestedGain <= 24) {
    setGainByValue(static_cast<uint8_t>(requestedGain));
  } else if (!streamingEnabled) {
    Serial.printf("Invalid gain command: %d. Use 1/2/4/6/8/12/24.\n", requestedGain);
  }
}

void handleCommand(char command) {
  switch (command) {
    case 'b':
    case 'B':
      startStreaming();
      return;

    case 's':
    case 'S':
      stopStreamingGracefully();
      return;

    case 'e':
    case 'E':
    case 'p':
    case 'P':
    case 'm':
    case 'M':
    case '*':
      // 推荐 SRB1 配置：只把实际测量 P 端纳入 BIAS，N/SRB1 参考端不纳入。
      configureFrontend(MODE_EEG_BIAS_P_ONLY);
      return;

    case 'n':
    case 'N':
      // legacy 对比：P/N 都纳入 BIAS，不建议长期使用。
      configureFrontend(MODE_EEG_BIAS_PN);
      return;

    case 'o':
    case 'O':
      configureFrontend(MODE_EEG_BIAS_OFF);
      return;

    case 'q':
    case 'Q':
      configureFrontend(MODE_INPUT_SHORTED);
      return;

    case 't':
    case 'T':
      configureFrontend(MODE_INTERNAL_TEST);
      return;

    case 'r':
    case 'R':
      clearDiagnostics();
      return;

    case '?':
      if (!streamingEnabled) {
        printHelpAndDiagnostics();
      }
      return;

    default:
      return;
  }
}

// ============================ Gain control ============================
bool gainToCode(uint8_t gain, uint8_t &code) {
  switch (gain) {
    case 1:  code = CH_GAIN_CODE_1;  return true;
    case 2:  code = CH_GAIN_CODE_2;  return true;
    case 4:  code = CH_GAIN_CODE_4;  return true;
    case 6:  code = CH_GAIN_CODE_6;  return true;
    case 8:  code = CH_GAIN_CODE_8;  return true;
    case 12: code = CH_GAIN_CODE_12; return true;
    case 24: code = CH_GAIN_CODE_24; return true;
    default: return false;
  }
}

uint8_t makeChannelSetting(uint8_t gainCode, uint8_t mux) {
  // SRB2 (CHnSET bit3) is intentionally never set in the SRB1-only variant.
  return static_cast<uint8_t>(((gainCode & 0x07u) << 4) | (mux & 0x07u));
}

uint8_t makePoweredDownChannelSetting(uint8_t gainCode) {
  // PD=1 禁用通道，同时把输入 MUX 设为 shorted，避免 CH6-CH8 悬空乱跳。
  return static_cast<uint8_t>(CH_POWER_DOWN | makeChannelSetting(gainCode, CH_MUX_SHORTED));
}

void setGainByValue(uint8_t gain) {
  if (runPhase == PHASE_STREAMING) return;

  uint8_t code = 0;
  if (!gainToCode(gain, code)) {
    if (!streamingEnabled) {
      Serial.printf("Unsupported PGA gain %u. Use 1/2/4/6/8/12/24.\n", (unsigned)gain);
    }
    return;
  }

  currentGain = gain;
  currentGainCode = code;
  for (uint8_t ch = 0; ch < 8; ++ch) {
    channelGain[ch] = gain;
    channelGainCode[ch] = code;
  }

  // 按当前模式重新写 CHnSET。configureFrontend 会自动暂停/恢复 streaming。
  configureFrontend(static_cast<FrontendMode>(currentMode));

  if (!streamingEnabled) {
    Serial.printf("PGA gain set to %ux\n", (unsigned)gain);
  }
}

void setBiasSensPMask(uint8_t mask) {
  if (runPhase == PHASE_STREAMING) return;
  streamingEnabled = false;
  if (frameQueue) xQueueReset(frameQueue);

  // Only enabled channels may enter the BIAS summing network. Reconfigure
  // the whole front end so BIAS_SENSN and MISC1 remain consistent with mode.
  currentBiasSensPMask = static_cast<uint8_t>(mask & currentEnabledMask);
  xSemaphoreTake(adsBusMutex, portMAX_DELAY);
  configureFrontendLocked(static_cast<FrontendMode>(currentMode));
  xSemaphoreGive(adsBusMutex);

  discardFramesAfterReconfigure = 4;
  runPhase = PHASE_STOPPED;
}

void setChannelConfig(uint8_t channel, uint8_t gain, uint8_t flags) {
  if (runPhase == PHASE_STREAMING || channel >= 8) return;
  uint8_t gainCode = 0;
  if (!gainToCode(gain, gainCode)) return;

  const uint8_t bit = static_cast<uint8_t>(1u << channel);
  channelGain[channel] = gain;
  channelGainCode[channel] = gainCode;
  if (flags & 0x01u) currentEnabledMask |= bit; else currentEnabledMask &= static_cast<uint8_t>(~bit);
  if ((flags & 0x02u) && (flags & 0x01u)) currentBiasSensPMask |= bit;
  else currentBiasSensPMask &= static_cast<uint8_t>(~bit);
  configureFrontend(static_cast<FrontendMode>(currentMode));
  if (!streamingEnabled) {
    Serial.printf("CH%u config: %s PGA=%ux BIAS_P=%u SRB1=GLOBAL SRB2=0\n",
      (unsigned)(channel + 1), (flags & 0x01u) ? "ON" : "OFF", (unsigned)gain,
      (unsigned)((flags >> 1) & 1u));
  }
}

// ============================ Frontend modes ============================
void configureFrontend(FrontendMode mode) {
  if (runPhase == PHASE_STREAMING) return;
  streamingEnabled = false;
  if (frameQueue) xQueueReset(frameQueue);

  xSemaphoreTake(adsBusMutex, portMAX_DELAY);
  configureFrontendLocked(mode);
  xSemaphoreGive(adsBusMutex);

  currentMode = mode;
  discardFramesAfterReconfigure = 4;
  runPhase = PHASE_STOPPED;
}

void configureFrontendLocked(FrontendMode mode) {
  runPhase = PHASE_CONFIG;
  configurationVerified = false;
  stopAdsConversionsLocked();

  writeAdsRegister(0x01, CONFIG1_250SPS);
  writeAdsRegister(0x03, CONFIG3_INTERNAL_REF);

  uint8_t config2 = CONFIG2_NORMAL;
  uint8_t biasP = currentBiasSensPMask;
  uint8_t biasN = currentEnabledMask;

  switch (mode) {
    case MODE_EEG_BIAS_PN:
      config2 = CONFIG2_NORMAL;
      biasP = currentBiasSensPMask;
      biasN = currentBiasSensPMask;
      break;

    case MODE_EEG_BIAS_P_ONLY:
      config2 = CONFIG2_NORMAL;
      biasP = currentBiasSensPMask;
      biasN = 0x00;
      break;

    case MODE_EEG_BIAS_OFF:
      config2 = CONFIG2_NORMAL;
      biasP = 0x00;
      biasN = 0x00;
      break;

    case MODE_INPUT_SHORTED:
      config2 = CONFIG2_NORMAL;
      biasP = 0x00;
      biasN = 0x00;
      break;

    case MODE_INTERNAL_TEST:
      config2 = CONFIG2_TEST_SLOW;
      biasP = 0x00;
      biasN = 0x00;
      break;
  }

  writeAdsRegister(0x02, config2);

  uint8_t selectedMux = CH_MUX_NORMAL;
  if (mode == MODE_INPUT_SHORTED) selectedMux = CH_MUX_SHORTED;
  if (mode == MODE_INTERNAL_TEST) selectedMux = CH_MUX_TEST;
  const bool srb1Enabled =
    mode == MODE_EEG_BIAS_PN ||
    mode == MODE_EEG_BIAS_P_ONLY ||
    mode == MODE_EEG_BIAS_OFF;
  biasP &= currentEnabledMask;
  biasN &= currentEnabledMask;
  for (uint8_t address = ADS_FIRST_CH_REG; address <= ADS_LAST_CH_REG; address++) {
    const uint8_t ch = static_cast<uint8_t>(address - ADS_FIRST_CH_REG);
    const bool channelIsActive = (currentEnabledMask & (1u << ch)) != 0;
    const uint8_t activeSetting = makeChannelSetting(channelGainCode[ch], selectedMux);
    const uint8_t poweredDownSetting = makePoweredDownChannelSetting(channelGainCode[ch]);
    writeAdsRegister(address, channelIsActive ? activeSetting : poweredDownSetting);
  }

  writeAdsRegister(0x0D, biasP);
  writeAdsRegister(0x0E, biasN);
  writeAdsRegister(0x0F, 0x00);  // LOFF_FLIP
  writeAdsRegister(0x15, srb1Enabled ? MISC1_SRB1_ON : MISC1_SRB1_OFF);

  currentMode = mode;
  configurationVerified = verifyFrontendLocked(mode);
}

bool verifyFrontendLocked(FrontendMode mode) {
  uint8_t expectedConfig2 = (mode == MODE_INTERNAL_TEST) ? CONFIG2_TEST_SLOW : CONFIG2_NORMAL;
  uint8_t expectedMux = CH_MUX_NORMAL;
  uint8_t expectedBiasP = currentBiasSensPMask;
  uint8_t expectedBiasN = 0x00;
  const bool expectedSrb1 =
    mode == MODE_EEG_BIAS_PN ||
    mode == MODE_EEG_BIAS_P_ONLY ||
    mode == MODE_EEG_BIAS_OFF;

  if (mode == MODE_INPUT_SHORTED) expectedMux = CH_MUX_SHORTED;
  if (mode == MODE_INTERNAL_TEST) expectedMux = CH_MUX_TEST;
  if (mode == MODE_EEG_BIAS_PN) expectedBiasN = currentBiasSensPMask;
  if (mode == MODE_EEG_BIAS_OFF || mode == MODE_INPUT_SHORTED || mode == MODE_INTERNAL_TEST) {
    expectedBiasP = 0x00;
    expectedBiasN = 0x00;
  }

  bool ok = true;
  ok &= readAdsRegister(0x01) == CONFIG1_250SPS;
  ok &= readAdsRegister(0x02) == expectedConfig2;
  ok &= readAdsRegister(0x03) == CONFIG3_INTERNAL_REF;
  expectedBiasP &= currentEnabledMask;
  expectedBiasN &= currentEnabledMask;
  for (uint8_t address = ADS_FIRST_CH_REG; address <= ADS_LAST_CH_REG; address++) {
    const uint8_t ch = static_cast<uint8_t>(address - ADS_FIRST_CH_REG);
    const bool enabled = (currentEnabledMask & (1u << ch)) != 0;
    const uint8_t expectedActive = makeChannelSetting(channelGainCode[ch], expectedMux);
    const uint8_t expectedDisabled = makePoweredDownChannelSetting(channelGainCode[ch]);
    ok &= readAdsRegister(address) == (enabled ? expectedActive : expectedDisabled);
  }
  ok &= readAdsRegister(0x0D) == expectedBiasP;
  ok &= readAdsRegister(0x0E) == expectedBiasN;
  ok &= readAdsRegister(0x0F) == 0x00;
  ok &= readAdsRegister(0x15) == (expectedSrb1 ? MISC1_SRB1_ON : MISC1_SRB1_OFF);
  return ok;
}

void startAdsConversionsLocked() {
  if (adsConversionsRunning) return;
  gpio_set_level(static_cast<gpio_num_t>(PIN_START), 1);
  delay(10);
  sendAdsCommand(ADS_RDATAC);
  delay(2);
  sendAdsCommand(ADS_START);
  delay(10);
  adsConversionsRunning = true;
}

void stopAdsConversionsLocked() {
  gpio_set_level(static_cast<gpio_num_t>(PIN_START), 0);
  delay(5);
  sendAdsCommand(ADS_SDATAC);
  delay(2);
  sendAdsCommand(ADS_STOP);
  delay(2);
  adsConversionsRunning = false;
}

void startStreaming() {
  if (runPhase == PHASE_STREAMING || !configurationVerified) return;
  if (frameQueue) xQueueReset(frameQueue);
  xTaskNotifyStateClear(adsTaskHandle);
  acquisitionSequence = 0;
  discardFramesAfterReconfigure = 4;
  xSemaphoreTake(adsBusMutex, portMAX_DELAY);
  startAdsConversionsLocked();
  xSemaphoreGive(adsBusMutex);
  runPhase = PHASE_STREAMING;
  streamingEnabled = true;
}

void stopStreamingGracefully() {
  if (runPhase != PHASE_STREAMING) return;

  // Close the producer first, then stop DRDY. Taking the ADS mutex waits for
  // any read already in progress to finish and enqueue its last complete frame.
  streamingEnabled = false;
  runPhase = PHASE_STOPPED;
  xSemaphoreTake(adsBusMutex, portMAX_DELAY);
  stopAdsConversionsLocked();
  xSemaphoreGive(adsBusMutex);

  // This serial task remains the sole TX owner and drains every complete frame
  // accepted before the converter stopped.
  StreamFrame tail = {};
  while (frameQueue && xQueueReceive(frameQueue, &tail, 0) == pdTRUE) {
    Serial.write(tail.bytes, STREAM_FRAME_BYTES);
  }
  Serial.flush();
  if (frameQueue) xQueueReset(frameQueue);
}

// ============================ Packet builder ============================
void buildStreamFrame(
  StreamFrame &frame,
  const uint8_t *adsRaw,
  uint32_t sequence,
  uint32_t timestampUs,
  uint16_t readTimeUs,
  uint8_t pendingCount,
  bool drdyWasLow
) {
  uint8_t *p = frame.bytes;
  p[0] = SYNC_1;
  p[1] = SYNC_2;
  p[2] = PROTOCOL_VERSION;
  p[3] = FRAME_TYPE_DATA;
  writeU32LE(&p[4], sequence);
  writeU32LE(&p[8], timestampUs);

  p[12] = adsRaw[0];
  p[13] = adsRaw[1];
  p[14] = adsRaw[2];

  uint8_t flags = 0;
  if ((adsRaw[0] & 0xF0u) == 0xC0u) flags |= (1u << 0);
  if (drdyWasLow) flags |= (1u << 1);
  if (pendingCount > 1) flags |= (1u << 2);
  if (currentMode == MODE_INTERNAL_TEST) flags |= (1u << 3);
  if (currentMode == MODE_INPUT_SHORTED) flags |= (1u << 4);
  if (currentMode != MODE_EEG_BIAS_OFF &&
      currentMode != MODE_INPUT_SHORTED &&
      currentMode != MODE_INTERNAL_TEST) {
    flags |= (1u << 5);
  }
  if (currentMode == MODE_EEG_BIAS_PN) flags |= (1u << 6);
  if (currentMode == MODE_EEG_BIAS_PN ||
      currentMode == MODE_EEG_BIAS_P_ONLY ||
      currentMode == MODE_EEG_BIAS_OFF) {
    flags |= (1u << 7); // SRB1 ON only for normal EEG input modes
  }
  p[15] = flags;

  memcpy(&p[16], &adsRaw[3], 24);
  writeU16LE(&p[40], readTimeUs);
  p[42] = pendingCount;
  p[43] = static_cast<uint8_t>(currentMode);

  const UBaseType_t queueDepth = frameQueue
    ? uxQueueMessagesWaiting(frameQueue)
    : 0;
  p[44] = queueDepth > 255u ? 255u : static_cast<uint8_t>(queueDepth);
  p[45] = static_cast<uint8_t>(queueDropCount & 0xFFu);

  const uint16_t crc = crc16CcittFalse(p, 46);
  writeU16LE(&p[46], crc);
}

uint16_t crc16CcittFalse(const uint8_t *data, size_t length) {
  uint16_t crc = 0xFFFFu;

  for (size_t i = 0; i < length; i++) {
    crc ^= static_cast<uint16_t>(data[i]) << 8;
    for (uint8_t bit = 0; bit < 8; bit++) {
      if (crc & 0x8000u) {
        crc = static_cast<uint16_t>((crc << 1) ^ 0x1021u);
      } else {
        crc = static_cast<uint16_t>(crc << 1);
      }
    }
  }

  return crc;
}

void writeU16LE(uint8_t *destination, uint16_t value) {
  destination[0] = static_cast<uint8_t>(value & 0xFFu);
  destination[1] = static_cast<uint8_t>((value >> 8) & 0xFFu);
}

void writeU32LE(uint8_t *destination, uint32_t value) {
  destination[0] = static_cast<uint8_t>(value & 0xFFu);
  destination[1] = static_cast<uint8_t>((value >> 8) & 0xFFu);
  destination[2] = static_cast<uint8_t>((value >> 16) & 0xFFu);
  destination[3] = static_cast<uint8_t>((value >> 24) & 0xFFu);
}

// ============================ GPIO / power ============================
void initPins() {
  // GPIO20/21 在 ESP32-C3 上可能属于 UART0。使用原工程的释放流程，
  // 前提是 Arduino IDE 已打开 USB CDC On Boot。
  uart_driver_delete(UART_NUM_0);

  const int pinsToReset[] = {
    PIN_ADS_CS, PIN_SCLK, PIN_MOSI, PIN_MISO, PIN_DRDY,
    PIN_START, PIN_RESET, PIN_TF_CS, PIN_TF_DET, PIN_NSC_CLK
  };

  for (const int pin : pinsToReset) {
    esp_rom_gpio_pad_unhold(pin);
    gpio_reset_pin(static_cast<gpio_num_t>(pin));
  }

  gpio_set_direction(static_cast<gpio_num_t>(PIN_ADS_CS), GPIO_MODE_OUTPUT);
  gpio_set_direction(static_cast<gpio_num_t>(PIN_TF_CS), GPIO_MODE_OUTPUT);
  gpio_set_direction(static_cast<gpio_num_t>(PIN_SCLK), GPIO_MODE_OUTPUT);
  gpio_set_direction(static_cast<gpio_num_t>(PIN_MOSI), GPIO_MODE_OUTPUT);
  gpio_set_direction(static_cast<gpio_num_t>(PIN_START), GPIO_MODE_OUTPUT);
  gpio_set_direction(static_cast<gpio_num_t>(PIN_RESET), GPIO_MODE_OUTPUT);

  gpio_set_direction(static_cast<gpio_num_t>(PIN_MISO), GPIO_MODE_INPUT);
  gpio_set_direction(static_cast<gpio_num_t>(PIN_DRDY), GPIO_MODE_INPUT);
  gpio_set_pull_mode(static_cast<gpio_num_t>(PIN_MISO), GPIO_PULLUP_ONLY);
  gpio_set_pull_mode(static_cast<gpio_num_t>(PIN_DRDY), GPIO_PULLUP_ONLY);

  pinMode(PIN_TF_DET, INPUT_PULLUP);

  gpio_set_level(static_cast<gpio_num_t>(PIN_TF_CS), 1);
  gpio_set_level(static_cast<gpio_num_t>(PIN_ADS_CS), 1);
  gpio_set_level(static_cast<gpio_num_t>(PIN_SCLK), 0);
  gpio_set_level(static_cast<gpio_num_t>(PIN_MOSI), 0);
  gpio_set_level(static_cast<gpio_num_t>(PIN_START), 0);
  gpio_set_level(static_cast<gpio_num_t>(PIN_RESET), 1);
}

void startNscClock() {
  gpio_reset_pin(static_cast<gpio_num_t>(PIN_NSC_CLK));
  delay(10);

  bool attached = ledcAttach(PIN_NSC_CLK, NSC_CLK_FREQ_HZ, NSC_CLK_RES_BITS);
  if (!attached) {
    attached = ledcAttach(PIN_NSC_CLK, 100000, 8);
  }

  if (attached) {
    ledcWrite(PIN_NSC_CLK, NSC_CLK_DUTY);
  }

  delay(1500);
}

void hardwareResetAds() {
  gpio_set_level(static_cast<gpio_num_t>(PIN_RESET), 0);
  delay(100);
  gpio_set_level(static_cast<gpio_num_t>(PIN_RESET), 1);
  delay(500);
  sendAdsCommand(ADS_WAKEUP);
  delay(10);
}

// ============================ Software SPI ============================
uint8_t softSpiTransfer(uint8_t tx) {
  uint8_t rx = 0;

  // 保留你之前能稳定工作的软件 SPI 边沿：
  // MOSI 建立 -> SCLK 上升 -> 采 MISO -> SCLK 下降。
  for (int bit = 7; bit >= 0; bit--) {
    gpio_set_level(
      static_cast<gpio_num_t>(PIN_MOSI),
      (tx >> bit) & 0x01u
    );
    delayMicroseconds(1);

    gpio_set_level(static_cast<gpio_num_t>(PIN_SCLK), 1);
    delayMicroseconds(1);

    if (gpio_get_level(static_cast<gpio_num_t>(PIN_MISO))) {
      rx |= static_cast<uint8_t>(1u << bit);
    }

    gpio_set_level(static_cast<gpio_num_t>(PIN_SCLK), 0);
    delayMicroseconds(1);
  }

  return rx;
}

bool readAdsFrame(uint8_t *destination, bool &drdyWasLow) {
  drdyWasLow = gpio_get_level(static_cast<gpio_num_t>(PIN_DRDY)) == 0;
  if (!drdyWasLow) {
    return false;
  }

  if (xSemaphoreTake(adsBusMutex, 0) != pdTRUE) {
    mutexBusyCount++;
    return false;
  }

  // 读取 216 bit 期间保持时钟边沿连续。采集与串口已经由队列解耦。
  noInterrupts();
  selectAds();
  delayMicroseconds(1);

  for (size_t i = 0; i < ADS_FRAME_BYTES; i++) {
    destination[i] = softSpiTransfer(0x00);
  }

  deselectAllSpi();
  interrupts();
  xSemaphoreGive(adsBusMutex);
  return true;
}

void deselectAllSpi() {
  gpio_set_level(static_cast<gpio_num_t>(PIN_ADS_CS), 1);
  gpio_set_level(static_cast<gpio_num_t>(PIN_TF_CS), 1);
}

void selectAds() {
  gpio_set_level(static_cast<gpio_num_t>(PIN_TF_CS), 1);
  gpio_set_level(static_cast<gpio_num_t>(PIN_ADS_CS), 0);
}

void sendAdsCommand(uint8_t command) {
  deselectAllSpi();
  gpio_set_level(static_cast<gpio_num_t>(PIN_ADS_CS), 0);
  delayMicroseconds(4);
  softSpiTransfer(command);
  delayMicroseconds(4);
  gpio_set_level(static_cast<gpio_num_t>(PIN_ADS_CS), 1);
  delay(2);
}

void writeAdsRegister(uint8_t address, uint8_t value) {
  deselectAllSpi();
  gpio_set_level(static_cast<gpio_num_t>(PIN_ADS_CS), 0);
  delayMicroseconds(4);
  softSpiTransfer(static_cast<uint8_t>(ADS_WREG | (address & 0x1Fu)));
  softSpiTransfer(0x00);
  softSpiTransfer(value);
  delayMicroseconds(4);
  gpio_set_level(static_cast<gpio_num_t>(PIN_ADS_CS), 1);
  delay(2);
}

uint8_t readAdsRegister(uint8_t address) {
  uint8_t value = 0;
  deselectAllSpi();
  gpio_set_level(static_cast<gpio_num_t>(PIN_ADS_CS), 0);
  delayMicroseconds(4);
  softSpiTransfer(static_cast<uint8_t>(ADS_RREG | (address & 0x1Fu)));
  softSpiTransfer(0x00);
  value = softSpiTransfer(0x00);
  delayMicroseconds(4);
  gpio_set_level(static_cast<gpio_num_t>(PIN_ADS_CS), 1);
  delay(2);
  return value;
}

// ============================ Diagnostics ============================
void sendConfigAck(uint8_t command, uint8_t argument) {
  uint8_t reply[12] = {
    0xBC, command, argument, 0xFF, 0xFF, 0xFF,
    0xFF, 0x00, static_cast<uint8_t>(currentMode), 0x00,
    currentEnabledMask, 0x00
  };

  if (runPhase != PHASE_STREAMING) {
    xSemaphoreTake(adsBusMutex, portMAX_DELAY);
    reply[3] = command == 0xA7
      ? readAdsRegister(static_cast<uint8_t>(ADS_FIRST_CH_REG + (argument & 0x07u)))
      : currentBiasSensPMask;
    reply[4] = readAdsRegister(0x0D);
    reply[5] = readAdsRegister(0x0E);
    reply[6] = readAdsRegister(0x15);
    xSemaphoreGive(adsBusMutex);
    reply[9] = configurationVerified ? 0x01u : 0x00u;
  }

  for (uint8_t i = 0; i < 11; ++i) reply[11] ^= reply[i];
  Serial.write(reply, sizeof(reply));
}

void clearDiagnostics() {
  missedDrdyCount = 0;
  lateDrdyCount = 0;
  mutexBusyCount = 0;
  badStatusCount = 0;
  queueDropCount = 0;
  validReadCount = 0;
  maxReadTimeUs = 0;
}

void printHelpAndDiagnostics() {
  Serial.println();
  Serial.println("=== ADS1299 SRB1 diagnostic ===");
  Serial.printf("phase=%u mode=%u streaming=%u configVerified=%u\n",
                (unsigned)runPhase,
                (unsigned)currentMode,
                streamingEnabled ? 1u : 0u,
                configurationVerified ? 1u : 0u);
  Serial.printf("seq=%lu drdy=%lu validRead=%lu\n",
                (unsigned long)acquisitionSequence,
                (unsigned long)drdyCount,
                (unsigned long)validReadCount);
  Serial.printf("missedDrdy=%lu lateDrdy=%lu mutexBusy=%lu\n",
                (unsigned long)missedDrdyCount,
                (unsigned long)lateDrdyCount,
                (unsigned long)mutexBusyCount);
  Serial.printf("badStatus=%lu queueDrop=%lu maxReadUs=%lu\n",
                (unsigned long)badStatusCount,
                (unsigned long)queueDropCount,
                (unsigned long)maxReadTimeUs);
  Serial.printf("gain=%ux gainCode=%u\n", (unsigned)currentGain, (unsigned)currentGainCode);
  Serial.printf("enabledMask=0x%02X biasPMask=0x%02X SRB2=FORCED_OFF\n",
                currentEnabledMask, currentBiasSensPMask);
  for (uint8_t ch = 0; ch < 8; ++ch) {
    Serial.printf("CH%u: %s PGA=%ux BIAS_P=%u SRB2=0\n",
      (unsigned)(ch + 1), (currentEnabledMask & (1u << ch)) ? "ON" : "OFF",
      (unsigned)channelGain[ch],
      (unsigned)((currentBiasSensPMask >> ch) & 1u));
  }
  Serial.println("commands: b s e/p/m/* n o q t 1/2/4/6/8/12/24 r ?");
  printRegisterReadback();
  Serial.println("===============================");
}

void printRegisterReadback() {
  if (streamingEnabled) return;

  xSemaphoreTake(adsBusMutex, portMAX_DELAY);
  sendAdsCommand(ADS_SDATAC);
  delay(2);

  const uint8_t config1 = readAdsRegister(0x01);
  const uint8_t config2 = readAdsRegister(0x02);
  const uint8_t config3 = readAdsRegister(0x03);
  const uint8_t ch1set = readAdsRegister(0x05);
  const uint8_t ch5set = readAdsRegister(0x09);
  const uint8_t ch6set = readAdsRegister(0x0A);
  const uint8_t ch8set = readAdsRegister(0x0C);
  const uint8_t biasP = readAdsRegister(0x0D);
  const uint8_t biasN = readAdsRegister(0x0E);
  const uint8_t misc1 = readAdsRegister(0x15);

  xSemaphoreGive(adsBusMutex);

  Serial.printf("CONFIG1=0x%02X CONFIG2=0x%02X CONFIG3=0x%02X\n", config1, config2, config3);
  Serial.printf("CH1SET=0x%02X CH5SET=0x%02X CH6SET=0x%02X CH8SET=0x%02X\n", ch1set, ch5set, ch6set, ch8set);
  Serial.printf("BIAS_SENSP=0x%02X BIAS_SENSN=0x%02X MISC1=0x%02X\n", biasP, biasN, misc1);
}
