/*
  ESP32-C3 + ADS1299：SRB1 EEG 诊断数据流（软件 SPI，供自定义 MATLAB 窗口读取）

  这版的目标不是伪装成 OpenBCI Cyton，而是把“采集链路是否可靠”说清楚：
    1. 完全保留软件 SPI，不调用 SPI.begin()；
    2. MCU 只发送 ADS1299 原始码，不在 MCU 上滤波；
    3. 每帧包含 32-bit 序号、ADS STATUS、读取耗时、模式和 CRC16；
    4. 上位机可以分别统计：串口 CRC 错、序号丢失、ADS STATUS 错位；
    5. 提供 OpenBCI 风格 SRB1 配置，以及短路噪声和内部方波诊断模式；
    6. 数据流中绝不插入状态文字，避免周期 impulse 和解析错位。

  Arduino IDE：
    USB CDC On Boot = Enabled
    波特率 = 921600

  引脚：
    ADS_CS=GPIO2, SCLK=GPIO21, MOSI=GPIO0, MISO=GPIO20
    DRDY=GPIO10, START=GPIO3, RESET=GPIO1
    TF_CS=GPIO4（始终拉高）, TF_DET=GPIO5
    NSC_CLK=GPIO6

  串口命令（单字符）：
    b : 开始二进制数据流
    s : 停止数据流
    MHH : 设置 activeMask，HH 为两位十六进制；bit0=CH1 ... bit7=CH8
    1..8 : 关闭对应通道；! @ # $ % ^ & * : 打开 CH1..CH8
    e/p : EEG，SRB1，BIAS 仅取 activeMask 选中的 P 端（默认）
    o : EEG，SRB1，关闭 BIAS，用于判断 BIAS 环路是否引入问题
    q : 所有通道输入内部短路，用于测板级底噪
    t : 所有通道接 ADS1299 内部方波，用于验证 SPI/数字链路
    r : 清零诊断计数
    ? : 停止数据流后打印诊断信息

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
    [43]     模式：0=e, 1=p, 2=o, 3=q, 4=t
    [44]     发包前队列深度（最大 255）
    [45]     队列累计丢包数低 8 bit
    [46..47] CRC16-CCITT-FALSE，对 [0..45] 计算，little-endian
*/

#include <Arduino.h>

#include "driver/gpio.h"
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

#define CH_NORMAL_GAIN24     0x60
#define CH_SHORTED_GAIN24    0x61
#define CH_TEST_GAIN24       0x65
#define CH_POWERDOWN_SHORTED 0xE1

#define MISC1_SRB1_ON        0x20
#define MISC1_SRB1_OFF       0x00

#define LOFF_CHECK_CONFIG    0x03

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
constexpr uint8_t BIAS_CHANNEL_COUNT = 5;
constexpr uint8_t VALID_BIAS_MASK = 0x1F;
constexpr uint16_t QUALITY_WINDOW_SAMPLES = 500;  // 2 seconds at 250 SPS.
constexpr int32_t ADC_FULL_SCALE = 0x7FFFFF;
constexpr int32_t SATURATION_THRESHOLD = static_cast<int32_t>(ADC_FULL_SCALE * 0.7f);
constexpr uint16_t QUALITY_DISCARD_FRAMES = 375;
constexpr uint8_t BAD_WINDOWS_TO_REMOVE = 3;
constexpr uint8_t GOOD_WINDOWS_TO_RESTORE = 5;
constexpr float COUNTS_PER_UV = 44.74f;  // ADS1299 gain=24, Vref ~= 4.5 V.
constexpr float BAD_LINE_RATIO = 0.3f;
constexpr float BAD_NOISY_UV = 300.0f;
constexpr float BAD_FLAT_RMS_UV = 0.2f;
constexpr float BAD_FLAT_P2P_UV = 1.0f;
constexpr int32_t BAD_DRIFT_THRESHOLD = static_cast<int32_t>(ADC_FULL_SCALE * 0.05f);

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

// ============================ RTOS objects ============================
static QueueHandle_t frameQueue = nullptr;
static TaskHandle_t adsTaskHandle = nullptr;
static SemaphoreHandle_t adsBusMutex = nullptr;

// ============================ State / diagnostics ============================
volatile bool streamingEnabled = false;
volatile FrontendMode currentMode = MODE_EEG_BIAS_P_ONLY;
volatile uint8_t activeMask = VALID_BIAS_MASK;
volatile uint32_t acquisitionSequence = 0;
volatile uint32_t drdyCount = 0;
volatile uint32_t missedDrdyCount = 0;
volatile uint32_t lateDrdyCount = 0;
volatile uint32_t mutexBusyCount = 0;
volatile uint32_t badStatusCount = 0;
volatile uint32_t queueDropCount = 0;
volatile uint32_t validReadCount = 0;
volatile uint32_t maxReadTimeUs = 0;
volatile uint16_t discardFramesAfterReconfigure = 0;
volatile uint32_t saturationBiasRemoveCount = 0;
volatile uint8_t lastSaturationRemoveMask = 0x00;
volatile uint32_t qualityWindowCount = 0;
volatile uint8_t lastQualityBadMask = 0x00;
volatile uint8_t lastQualityGoodMask = VALID_BIAS_MASK;
int32_t qualitySamples[BIAS_CHANNEL_COUNT][QUALITY_WINDOW_SAMPLES] = {};
uint16_t qualitySampleCount = 0;
uint8_t badWindowCount[BIAS_CHANNEL_COUNT] = {};
uint8_t goodWindowCount[BIAS_CHANNEL_COUNT] = {};
int32_t previousMedian[BIAS_CHANNEL_COUNT] = {};
bool hasPreviousMedian = false;

// ============================ Declarations ============================
void IRAM_ATTR onDrdyFalling();

void initPins();
void startNscClock();
void hardwareResetAds();
void configureFrontend(FrontendMode mode);
void configureFrontendLocked(FrontendMode mode);
void writeBiasMaskLocked(uint8_t mask);
void applyActiveMask(uint8_t mask, bool acknowledge);
void initialMaskFromLeadOff(bool acknowledge);
void collectQualitySample(const uint8_t *adsRaw);
void analyzeQualityWindow();
void setChannelEnabled(uint8_t channelIndex, bool enabled);
void printActiveMaskAck(bool wasStreaming);
void printLeadOffResult(uint8_t statP, uint8_t statN, uint8_t goodMask, bool wasStreaming);
bool isHexDigit(char value);
uint8_t hexValue(char value);
int32_t readSigned24MsbFirst(const uint8_t *data);
int compareInt32(const void *left, const void *right);
float goertzelPower(const int32_t *values, uint16_t count, float binHz, float dc);

uint8_t softSpiTransfer(uint8_t tx);
void deselectAllSpi();
void selectAds();
void sendAdsCommand(uint8_t command);
void writeAdsRegister(uint8_t address, uint8_t value);
uint8_t readAdsRegister(uint8_t address);
bool readAdsFrame(uint8_t *destination, bool &drdyWasLow);

void adsAcquireTask(void *argument);
void serialStreamTask(void *argument);
void handleCommand(char command);
void handleSerialByte(char command);
void printHelpAndDiagnostics();
void clearDiagnostics();

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

  // 这里只在尚未开始二进制数据流时打印。MATLAB 连接后会 flush 掉这些文字。
  Serial.println();
  Serial.println("ESP32C3 ADS1299 SRB1 DIAG STREAM READY");
  Serial.println("Commands: b/s/MHH/1-8/!@#$%^&*/e/p/o/q/t/i/r/?");
  Serial.println("b runs one lead-off check before streaming; 2s quality windows adjust BIAS_SENSP.");
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

    collectQualitySample(raw);

    if (!streamingEnabled) {
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
      const char command = static_cast<char>(Serial.read());
      handleSerialByte(command);
    }

    if (xQueueReceive(frameQueue, &frame, pdMS_TO_TICKS(2)) == pdTRUE) {
      if (streamingEnabled) {
        Serial.write(frame.bytes, STREAM_FRAME_BYTES);
      }
    } else {
      taskYIELD();
    }
  }
}

void handleSerialByte(char command) {
  static bool readingMask = false;
  static uint8_t pendingMask = 0;
  static uint8_t nibbleCount = 0;

  if (readingMask) {
    if (isHexDigit(command)) {
      pendingMask = static_cast<uint8_t>((pendingMask << 4) | hexValue(command));
      nibbleCount++;
      if (nibbleCount == 2) {
        readingMask = false;
        applyActiveMask(pendingMask, true);
      }
      return;
    }

    if (command == '\r' || command == '\n' || command == ' ' || command == '\t') {
      if (nibbleCount == 0) {
        return;
      }
      readingMask = false;
      Serial.println("#ERR activeMask expects MHH");
      return;
    }

    readingMask = false;
    Serial.println("#ERR activeMask expects hex");
    return;
  }

  if (command == '\r' || command == '\n' || command == ' ' || command == '\t') {
    return;
  }

  if (command == 'M' || command == 'm') {
    readingMask = true;
    pendingMask = 0;
    nibbleCount = 0;
    return;
  }

  handleCommand(command);
}

void handleCommand(char command) {
  if (command >= '1' && command <= '8') {
    setChannelEnabled(static_cast<uint8_t>(command - '1'), false);
    return;
  }

  const char enableCommands[8] = {'!', '@', '#', '$', '%', '^', '&', '*'};
  for (uint8_t index = 0; index < 8; index++) {
    if (command == enableCommands[index]) {
      setChannelEnabled(index, true);
      return;
    }
  }

  switch (command) {
    case 'b':
    case 'B':
      if (frameQueue) xQueueReset(frameQueue);
      initialMaskFromLeadOff(true);
      streamingEnabled = true;
      return;

    case 's':
    case 'S':
      streamingEnabled = false;
      if (frameQueue) xQueueReset(frameQueue);
      return;

    case 'e':
    case 'E':
      configureFrontend(MODE_EEG_BIAS_P_ONLY);
      return;

    case 'p':
    case 'P':
      configureFrontend(MODE_EEG_BIAS_P_ONLY);
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

    case 'i':
    case 'I':
      initialMaskFromLeadOff(true);
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

// ============================ Frontend modes ============================
void applyActiveMask(uint8_t mask, bool acknowledge) {
  const bool wasStreaming = streamingEnabled;
  streamingEnabled = false;
  if (frameQueue) xQueueReset(frameQueue);

  activeMask = static_cast<uint8_t>(mask & VALID_BIAS_MASK);
  if (activeMask == 0) {
    activeMask = VALID_BIAS_MASK;
  }
  xSemaphoreTake(adsBusMutex, portMAX_DELAY);
  configureFrontendLocked(MODE_EEG_BIAS_P_ONLY);
  xSemaphoreGive(adsBusMutex);

  currentMode = MODE_EEG_BIAS_P_ONLY;
  discardFramesAfterReconfigure = 4;

  if (acknowledge) {
    printActiveMaskAck(wasStreaming);
  }
}

void initialMaskFromLeadOff(bool acknowledge) {
  const bool wasStreaming = streamingEnabled;
  streamingEnabled = false;
  if (frameQueue) xQueueReset(frameQueue);

  xSemaphoreTake(adsBusMutex, portMAX_DELAY);
  sendAdsCommand(ADS_SDATAC);
  delay(2);

  writeAdsRegister(0x0F, VALID_BIAS_MASK);  // LOFF_SENSP: check CH1-CH5 P electrodes.
  writeAdsRegister(0x10, 0x00);             // LOFF_SENSN: SRB1/P-only hardware, keep N out.
  writeAdsRegister(0x11, 0x00);             // LOFF_FLIP
  writeAdsRegister(0x04, LOFF_CHECK_CONFIG);
  delay(120);

  const uint8_t statP = readAdsRegister(0x12);
  const uint8_t statN = readAdsRegister(0x13);
  uint8_t goodMask = static_cast<uint8_t>(VALID_BIAS_MASK & ~statP);
  if (goodMask == 0) {
    goodMask = activeMask & VALID_BIAS_MASK;
  }

  activeMask = goodMask;
  configureFrontendLocked(MODE_EEG_BIAS_P_ONLY);
  xSemaphoreGive(adsBusMutex);

  currentMode = MODE_EEG_BIAS_P_ONLY;
  discardFramesAfterReconfigure = 8;
  if (acknowledge) {
    printLeadOffResult(statP, statN, goodMask, wasStreaming);
  }
}

void collectQualitySample(const uint8_t *adsRaw) {
  if (currentMode != MODE_EEG_BIAS_P_ONLY && currentMode != MODE_EEG_BIAS_PN) {
    return;
  }

  if (qualitySampleCount >= QUALITY_WINDOW_SAMPLES) {
    return;
  }

  for (uint8_t channel = 0; channel < BIAS_CHANNEL_COUNT; channel++) {
    qualitySamples[channel][qualitySampleCount] = readSigned24MsbFirst(&adsRaw[3 + channel * 3]);
  }
  qualitySampleCount++;

  if (qualitySampleCount >= QUALITY_WINDOW_SAMPLES) {
    analyzeQualityWindow();
    qualitySampleCount = 0;
  }
}

void analyzeQualityWindow() {
  int32_t medianValues[BIAS_CHANNEL_COUNT] = {};
  float rmsValues[BIAS_CHANNEL_COUNT] = {};
  bool badFlags[BIAS_CHANNEL_COUNT] = {};
  uint8_t statP = 0;

  if (xSemaphoreTake(adsBusMutex, 0) == pdTRUE) {
    sendAdsCommand(ADS_SDATAC);
    delay(2);
    statP = static_cast<uint8_t>(readAdsRegister(0x12) & VALID_BIAS_MASK);
    sendAdsCommand(ADS_RDATAC);
    xSemaphoreGive(adsBusMutex);
  }

  for (uint8_t channel = 0; channel < BIAS_CHANNEL_COUNT; channel++) {
    int32_t sorted[QUALITY_WINDOW_SAMPLES];
    uint16_t saturatedCount = 0;
    int32_t minValue = qualitySamples[channel][0];
    int32_t maxValue = qualitySamples[channel][0];

    for (uint16_t index = 0; index < QUALITY_WINDOW_SAMPLES; index++) {
      const int32_t value = qualitySamples[channel][index];
      sorted[index] = value;
      if (value >= SATURATION_THRESHOLD || value <= -SATURATION_THRESHOLD) {
        saturatedCount++;
      }
      if (value < minValue) minValue = value;
      if (value > maxValue) maxValue = value;
    }

    qsort(sorted, QUALITY_WINDOW_SAMPLES, sizeof(int32_t), compareInt32);
    const int32_t median = sorted[QUALITY_WINDOW_SAMPLES / 2];
    medianValues[channel] = median;

    float bandPower1_40 = 0.0f;
    for (uint8_t hz = 1; hz <= 40; hz++) {
      bandPower1_40 += goertzelPower(
        qualitySamples[channel],
        QUALITY_WINDOW_SAMPLES,
        static_cast<float>(hz),
        static_cast<float>(median)
      );
    }

    float linePower48_52 = 0.0f;
    for (uint8_t hz = 48; hz <= 52; hz++) {
      linePower48_52 += goertzelPower(
        qualitySamples[channel],
        QUALITY_WINDOW_SAMPLES,
        static_cast<float>(hz),
        static_cast<float>(median)
      );
    }

    if (bandPower1_40 < 1.0f) {
      bandPower1_40 = 1.0f;
    }
    const float rms1_40 = sqrt(bandPower1_40);
    rmsValues[channel] = rms1_40;

    const float satRatio = static_cast<float>(saturatedCount) / QUALITY_WINDOW_SAMPLES;
    const bool badSaturation = abs(median) > SATURATION_THRESHOLD || satRatio > 0.001f;
    const bool badLine = (linePower48_52 / bandPower1_40) > BAD_LINE_RATIO;
    const bool badFlat = rms1_40 < (BAD_FLAT_RMS_UV * COUNTS_PER_UV) ||
                          (maxValue - minValue) < (BAD_FLAT_P2P_UV * COUNTS_PER_UV);
    const bool badDrift = hasPreviousMedian &&
                          abs(median - previousMedian[channel]) > BAD_DRIFT_THRESHOLD;
    const bool badLeadOff = (statP & (1u << channel)) != 0;

    badFlags[channel] = badSaturation || badLine || badFlat || badDrift || badLeadOff;
  }

  float sortedRms[BIAS_CHANNEL_COUNT];
  for (uint8_t channel = 0; channel < BIAS_CHANNEL_COUNT; channel++) {
    sortedRms[channel] = rmsValues[channel];
  }
  for (uint8_t i = 1; i < BIAS_CHANNEL_COUNT; i++) {
    const float value = sortedRms[i];
    int8_t j = static_cast<int8_t>(i) - 1;
    while (j >= 0 && sortedRms[j] > value) {
      sortedRms[j + 1] = sortedRms[j];
      j--;
    }
    sortedRms[j + 1] = value;
  }
  const float channelMedianRms = sortedRms[BIAS_CHANNEL_COUNT / 2];

  uint8_t badMask = 0;
  uint8_t goodMask = 0;
  for (uint8_t channel = 0; channel < BIAS_CHANNEL_COUNT; channel++) {
    const bool badNoisy = rmsValues[channel] > (8.0f * channelMedianRms) &&
                          rmsValues[channel] > (BAD_NOISY_UV * COUNTS_PER_UV);
    const bool isBad = badFlags[channel] || badNoisy;
    const uint8_t bit = static_cast<uint8_t>(1u << channel);
    if (isBad) {
      badMask = static_cast<uint8_t>(badMask | bit);
      if (badWindowCount[channel] < 255) badWindowCount[channel]++;
      goodWindowCount[channel] = 0;
    } else {
      goodMask = static_cast<uint8_t>(goodMask | bit);
      if (goodWindowCount[channel] < 255) goodWindowCount[channel]++;
      badWindowCount[channel] = 0;
    }
    previousMedian[channel] = medianValues[channel];
  }
  hasPreviousMedian = true;

  uint8_t nextMask = activeMask & VALID_BIAS_MASK;
  for (uint8_t channel = 0; channel < BIAS_CHANNEL_COUNT; channel++) {
    const uint8_t bit = static_cast<uint8_t>(1u << channel);
    if (badWindowCount[channel] >= BAD_WINDOWS_TO_REMOVE) {
      nextMask = static_cast<uint8_t>(nextMask & ~bit);
    } else if (goodWindowCount[channel] >= GOOD_WINDOWS_TO_RESTORE) {
      nextMask = static_cast<uint8_t>(nextMask | bit);
    }
  }

  if ((nextMask & VALID_BIAS_MASK) == 0) {
    nextMask = activeMask & VALID_BIAS_MASK;
  }

  lastQualityBadMask = badMask;
  lastQualityGoodMask = goodMask;
  qualityWindowCount++;

  if (nextMask == (activeMask & VALID_BIAS_MASK)) {
    return;
  }

  if (xSemaphoreTake(adsBusMutex, 0) != pdTRUE) {
    return;
  }
  activeMask = nextMask;
  writeBiasMaskLocked(activeMask);
  xSemaphoreGive(adsBusMutex);

  lastSaturationRemoveMask = badMask;
  saturationBiasRemoveCount++;
  discardFramesAfterReconfigure = QUALITY_DISCARD_FRAMES;
}

void setChannelEnabled(uint8_t channelIndex, bool enabled) {
  if (channelIndex >= BIAS_CHANNEL_COUNT) {
    return;
  }

  uint8_t mask = activeMask;
  const uint8_t bit = static_cast<uint8_t>(1u << channelIndex);
  if (enabled) {
    mask = static_cast<uint8_t>(mask | bit);
  } else {
    mask = static_cast<uint8_t>(mask & ~bit);
  }
  applyActiveMask(mask, true);
}

void printActiveMaskAck(bool wasStreaming) {
  Serial.printf(
    "#ACK activeMask=0x%02X streaming=0 wasStreaming=%u\n",
    activeMask,
    wasStreaming ? 1u : 0u
  );
}

void printLeadOffResult(uint8_t statP, uint8_t statN, uint8_t goodMask, bool wasStreaming) {
  Serial.printf(
    "#IMP statP=0x%02X statN=0x%02X goodMask=0x%02X activeMask=0x%02X streaming=0 wasStreaming=%u\n",
    statP,
    statN,
    goodMask,
    activeMask,
    wasStreaming ? 1u : 0u
  );
}

bool isHexDigit(char value) {
  return (value >= '0' && value <= '9') ||
         (value >= 'a' && value <= 'f') ||
         (value >= 'A' && value <= 'F');
}

uint8_t hexValue(char value) {
  if (value >= '0' && value <= '9') {
    return static_cast<uint8_t>(value - '0');
  }
  if (value >= 'a' && value <= 'f') {
    return static_cast<uint8_t>(value - 'a' + 10);
  }
  return static_cast<uint8_t>(value - 'A' + 10);
}

void configureFrontend(FrontendMode mode) {
  const bool shouldResumeStreaming = streamingEnabled;
  streamingEnabled = false;
  if (frameQueue) xQueueReset(frameQueue);

  xSemaphoreTake(adsBusMutex, portMAX_DELAY);
  configureFrontendLocked(mode);
  xSemaphoreGive(adsBusMutex);

  currentMode = mode;
  discardFramesAfterReconfigure = 4;
  streamingEnabled = shouldResumeStreaming;
}

void writeBiasMaskLocked(uint8_t mask) {
  mask = static_cast<uint8_t>(mask & VALID_BIAS_MASK);
  if (mask == 0) {
    mask = activeMask == 0 ? VALID_BIAS_MASK : static_cast<uint8_t>(activeMask & VALID_BIAS_MASK);
  }
  sendAdsCommand(ADS_SDATAC);
  delay(2);
  writeAdsRegister(0x0D, mask);
  writeAdsRegister(0x0E, 0x00);
  writeAdsRegister(0x0F, VALID_BIAS_MASK);
  writeAdsRegister(0x10, 0x00);
  sendAdsCommand(ADS_RDATAC);
  delay(2);
}

void configureFrontendLocked(FrontendMode mode) {
  gpio_set_level(static_cast<gpio_num_t>(PIN_START), 0);
  delay(5);

  sendAdsCommand(ADS_SDATAC);
  delay(2);
  sendAdsCommand(ADS_STOP);
  delay(2);

  writeAdsRegister(0x01, CONFIG1_250SPS);
  writeAdsRegister(0x03, CONFIG3_INTERNAL_REF);

  uint8_t config2 = CONFIG2_NORMAL;
  uint8_t channelSetting = CH_NORMAL_GAIN24;
  uint8_t biasP = activeMask & VALID_BIAS_MASK;
  uint8_t biasN = 0x00;
  bool useActiveMaskForChannels = false;

  switch (mode) {
    case MODE_EEG_BIAS_PN:
      config2 = CONFIG2_NORMAL;
      channelSetting = CH_NORMAL_GAIN24;
      biasP = activeMask & VALID_BIAS_MASK;
      biasN = 0x00;
      break;

    case MODE_EEG_BIAS_P_ONLY:
      config2 = CONFIG2_NORMAL;
      channelSetting = CH_NORMAL_GAIN24;
      biasP = activeMask & VALID_BIAS_MASK;
      biasN = 0x00;
      useActiveMaskForChannels = true;
      break;

    case MODE_EEG_BIAS_OFF:
      config2 = CONFIG2_NORMAL;
      channelSetting = CH_NORMAL_GAIN24;
      biasP = 0x00;
      biasN = 0x00;
      useActiveMaskForChannels = true;
      break;

    case MODE_INPUT_SHORTED:
      config2 = CONFIG2_NORMAL;
      channelSetting = CH_SHORTED_GAIN24;
      biasP = 0x00;
      biasN = 0x00;
      break;

    case MODE_INTERNAL_TEST:
      config2 = CONFIG2_TEST_SLOW;
      channelSetting = CH_TEST_GAIN24;
      biasP = 0x00;
      biasN = 0x00;
      break;
  }

  writeAdsRegister(0x02, config2);

  for (uint8_t channel = 0; channel < 8; channel++) {
    uint8_t setting = channelSetting;
    (void)useActiveMaskForChannels;
    writeAdsRegister(static_cast<uint8_t>(0x05 + channel), setting);
  }

  if (biasP == 0 && mode != MODE_EEG_BIAS_OFF) {
    biasP = VALID_BIAS_MASK;
    activeMask = VALID_BIAS_MASK;
  }
  writeAdsRegister(0x0D, biasP);
  writeAdsRegister(0x0E, 0x00);
  writeAdsRegister(0x0F, VALID_BIAS_MASK);  // LOFF_SENSP
  writeAdsRegister(0x10, 0x00);             // LOFF_SENSN
  writeAdsRegister(0x11, 0x00);             // LOFF_FLIP
  writeAdsRegister(0x15, MISC1_SRB1_ON);

  gpio_set_level(static_cast<gpio_num_t>(PIN_START), 1);
  delay(10);
  sendAdsCommand(ADS_RDATAC);
  delay(2);
  sendAdsCommand(ADS_START);
  delay(10);

  currentMode = mode;
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
  flags |= (1u << 7); // SRB1 ON
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

int32_t readSigned24MsbFirst(const uint8_t *data) {
  int32_t value = (static_cast<int32_t>(data[0]) << 16) |
                  (static_cast<int32_t>(data[1]) << 8) |
                  static_cast<int32_t>(data[2]);
  if (value & 0x800000) {
    value |= 0xFF000000;
  }
  return value;
}

int compareInt32(const void *left, const void *right) {
  const int32_t a = *static_cast<const int32_t *>(left);
  const int32_t b = *static_cast<const int32_t *>(right);
  if (a < b) return -1;
  if (a > b) return 1;
  return 0;
}

float goertzelPower(const int32_t *values, uint16_t count, float binHz, float dc) {
  const float normalized = binHz / 250.0f;
  const float coeff = 2.0f * cosf(2.0f * PI * normalized);
  float q0 = 0.0f;
  float q1 = 0.0f;
  float q2 = 0.0f;

  for (uint16_t index = 0; index < count; index++) {
    q0 = static_cast<float>(values[index]) - dc + coeff * q1 - q2;
    q2 = q1;
    q1 = q0;
  }

  const float power = q1 * q1 + q2 * q2 - coeff * q1 * q2;
  return power / (static_cast<float>(count) * static_cast<float>(count));
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
  // Keep Arduino Serial installed; deleting UART0 makes Serial.available/read
  // spam "uart driver error" in serial terminals.

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
  uint8_t config1 = 0;
  uint8_t config2 = 0;
  uint8_t config3 = 0;
  uint8_t biasSensp = 0;
  uint8_t biasSensn = 0;
  uint8_t misc1 = 0;
  uint8_t channelSet[8] = {};

  xSemaphoreTake(adsBusMutex, portMAX_DELAY);
  sendAdsCommand(ADS_SDATAC);
  delay(2);
  config1 = readAdsRegister(0x01);
  config2 = readAdsRegister(0x02);
  config3 = readAdsRegister(0x03);
  for (uint8_t channel = 0; channel < 8; channel++) {
    channelSet[channel] = readAdsRegister(static_cast<uint8_t>(0x05 + channel));
  }
  biasSensp = readAdsRegister(0x0D);
  biasSensn = readAdsRegister(0x0E);
  misc1 = readAdsRegister(0x15);
  sendAdsCommand(ADS_RDATAC);
  xSemaphoreGive(adsBusMutex);

  Serial.println();
  Serial.println("=== ADS1299 SRB1 diagnostic ===");
  Serial.printf("mode=%u streaming=%u\n", (unsigned)currentMode, streamingEnabled ? 1u : 0u);
  Serial.printf("activeMask=0x%02X\n", activeMask);
  Serial.printf("CONFIG1=0x%02X CONFIG2=0x%02X CONFIG3=0x%02X MISC1=0x%02X\n",
                config1, config2, config3, misc1);
  Serial.printf("BIAS_SENSP=0x%02X BIAS_SENSN=0x%02X\n", biasSensp, biasSensn);
  Serial.printf("CH1SET=0x%02X CH2SET=0x%02X CH3SET=0x%02X CH4SET=0x%02X\n",
                channelSet[0], channelSet[1], channelSet[2], channelSet[3]);
  Serial.printf("CH5SET=0x%02X CH6SET=0x%02X CH7SET=0x%02X CH8SET=0x%02X\n",
                channelSet[4], channelSet[5], channelSet[6], channelSet[7]);
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
  Serial.printf("qualityWindows=%lu biasUpdates=%lu lastBadMask=0x%02X lastGoodMask=0x%02X lastUpdateMask=0x%02X\n",
                (unsigned long)qualityWindowCount,
                (unsigned long)saturationBiasRemoveCount,
                lastQualityBadMask,
                lastQualityGoodMask,
                lastSaturationRemoveMask);
  Serial.println("commands: b s MHH 1..8 !@#$%^&* e p o q t i r ?");
  Serial.println("===============================");
}
