/*
  ESP32-C3 + ADS1299：SRB1 EEG 可靠 BLE 数据流 V3（软件 SPI）

  本固件目标是把采集链路状态完整、可验证地呈现出来：
    1. 完全保留软件 SPI，不调用 SPI.begin()；
    2. MCU 只发送 ADS1299 原始码，不在 MCU 上滤波；
    3. 每帧包含 32-bit 序号、ADS STATUS、读取耗时、模式和 CRC16；
    4. 上位机可以分别统计：串口 CRC 错、序号丢失、ADS STATUS 错位；
    5. 提供 SRB1 配置，以及短路噪声和内部方波诊断模式；
    6. 数据流中绝不插入状态文字，避免周期 impulse 和解析错位。

  默认上电配置：CH1-CH5 开启，CH6-CH8 禁用，SRB1 on，BIAS P-only：SENSP=0x1F, SENSN=0x00。

  Arduino IDE：
    Board = ESP32C3 Dev Module（或你的具体 C3 板型）
    USB CDC On Boot = Enabled
    波特率 = 921600
    建议 Arduino-ESP32 Core 3.3.11 或同系列 3.x

  BLE GATT：
    设备名：OmniBCI-C3-SRB1-V3
    DATA：Notify，4 帧组成一个带 block sequence/CRC 的可靠块；GUI 累计 ACK，缺块 NACK 重传
    CONTROL：Write / Write No Response；命令字节与原串口完全相同
    STATUS：Read / Notify；配置 ACK 与 BLE 状态走这里，不会污染 EEG DATA
    本机请求 MTU=247；可靠发送窗口、环形缓存与重传均在固件内完成

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
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#if defined(CONFIG_BLUEDROID_ENABLED)
#include <BLE2902.h>
#endif

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
#define LOFF_AC_6NA_31HZ     0x02

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
constexpr uint16_t FRAME_QUEUE_LENGTH = 256;
constexpr uint8_t SYNC_1 = 0xA5;
constexpr uint8_t SYNC_2 = 0x5A;
constexpr uint8_t PROTOCOL_VERSION = 1;
constexpr uint8_t FRAME_TYPE_DATA = 1;

// ============================ BLE reliable transport V4 / compact V2 ============================
constexpr char BLE_DEVICE_NAME[] = "OmniBCI-C3-SRB1-V3";
constexpr char BLE_SERVICE_UUID[] = "79f60000-3a7d-4b11-9f4e-4c57a50d0001";
constexpr char BLE_DATA_UUID[] = "79f60000-3a7d-4b11-9f4e-4c57a50d0002";
constexpr char BLE_CONTROL_UUID[] = "79f60000-3a7d-4b11-9f4e-4c57a50d0003";
constexpr char BLE_STATUS_UUID[] = "79f60000-3a7d-4b11-9f4e-4c57a50d0004";

constexpr uint16_t BLE_REQUESTED_MTU = 247;
constexpr uint16_t BLE_MIN_STREAM_MTU = 100;
constexpr uint16_t BLE_COMMAND_QUEUE_LENGTH = 512;
constexpr size_t BLE_STATUS_BYTES = 72;
constexpr bool ENABLE_USB_STREAM_WHEN_BLE_IDLE = true;
constexpr bool MIRROR_STREAM_TO_USB_WHILE_BLE = false;

// Reliable DATA packet (little-endian):
//   [0..1]   B1 4B
//   [2]      protocol version = 2
//   [3]      flags (bit0 retransmission, bit1 partial block)
//   [4..7]   uint32 stream session id
//   [8..11]  uint32 block sequence
//   [12..15] uint32 first ADS sample sequence
//   [16]     frame count (1..6)
//   [17]     reserved
//   [18..19] uint16 payload bytes
//   [20..]   compact 36-byte records: seq4 + timestamp4 + ADS27 + flags1
//   [end-2]  CRC16-CCITT-FALSE over header+payload
constexpr uint8_t BLE_BLOCK_MAGIC_0 = 0xB1;
constexpr uint8_t BLE_BLOCK_MAGIC_1 = 0x4B;
constexpr uint8_t BLE_RELIABLE_VERSION = 2;
constexpr size_t BLE_COMPACT_FRAME_BYTES = 36;
constexpr size_t BLE_RELIABLE_FRAMES_PER_BLOCK = 6;
constexpr size_t BLE_RELIABLE_PAYLOAD_BYTES = BLE_COMPACT_FRAME_BYTES * BLE_RELIABLE_FRAMES_PER_BLOCK;
constexpr size_t BLE_RELIABLE_HEADER_BYTES = 20;
constexpr size_t BLE_RELIABLE_CRC_BYTES = 2;
constexpr size_t BLE_RELIABLE_PACKET_MAX_BYTES = BLE_RELIABLE_HEADER_BYTES + BLE_RELIABLE_PAYLOAD_BYTES + BLE_RELIABLE_CRC_BYTES;
static_assert(BLE_RELIABLE_PACKET_MAX_BYTES <= (BLE_REQUESTED_MTU - 3u), "compact BLE block must fit one MTU-247 notification");
constexpr uint16_t BLE_RELIABLE_RING_BLOCKS = 320;       // about 7.68 s at 250 SPS
constexpr uint16_t BLE_RELIABLE_WINDOW_BLOCKS = 16;      // about 0.38 s unacked in flight
constexpr uint32_t BLE_RELIABLE_TX_PACE_FAST_MS = 9;     // fast adapter / low in-flight occupancy
constexpr uint32_t BLE_RELIABLE_TX_PACE_NORMAL_MS = 14;  // moderate Windows batching
constexpr uint32_t BLE_RELIABLE_TX_PACE_SLOW_MS = 20;    // near-full in-flight window
constexpr uint32_t BLE_RELIABLE_RETRY_MIN_MS = 800;
constexpr uint32_t BLE_RELIABLE_RETRY_MAX_MS = 3000;
constexpr uint32_t BLE_RELIABLE_RETRY_INITIAL_MS = 1200;
constexpr uint8_t BLE_RELIABLE_MAX_RETRIES = 20;

// Reliable CONTROL packet:
//   BA 43, version, type, session uint32, seqA uint32, seqB uint32, CRC16
constexpr uint8_t BLE_CTRL_MAGIC_0 = 0xBA;
constexpr uint8_t BLE_CTRL_MAGIC_1 = 0x43;
constexpr uint8_t BLE_CTRL_VERSION = 1;
constexpr uint8_t BLE_CTRL_ACK = 1;
constexpr uint8_t BLE_CTRL_NACK_RANGE = 2;
constexpr uint8_t BLE_CTRL_RESET = 3;
constexpr size_t BLE_CTRL_PACKET_BYTES = 18;

struct ReliableControlCommand {
  uint8_t type;
  uint32_t sessionId;
  uint32_t seqA;
  uint32_t seqB;
};

struct ReliableBleBlock {
  bool valid;
  bool sent;
  uint8_t frameCount;
  uint8_t retries;
  uint16_t payloadLength;
  uint32_t blockSequence;
  uint32_t firstSampleSequence;
  uint32_t lastSentMs;
  uint8_t payload[BLE_RELIABLE_PAYLOAD_BYTES];
};

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
static QueueHandle_t bleCommandQueue = nullptr;
static QueueHandle_t bleReliableCommandQueue = nullptr;
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

// BLE callbacks only update these small flags/counters or enqueue command bytes.
// All ADS reconfiguration and all DATA notifications run in transportTask.
static BLEServer *bleServer = nullptr;
static BLECharacteristic *bleDataCharacteristic = nullptr;
static BLECharacteristic *bleControlCharacteristic = nullptr;
static BLECharacteristic *bleStatusCharacteristic = nullptr;
#if defined(CONFIG_BLUEDROID_ENABLED)
static BLE2902 *bleDataCccd = nullptr;
#endif
volatile bool bleInitialized = false;
volatile bool bleConnected = false;
volatile bool bleDataSubscribed = false;
volatile bool bleRestartAdvertisingRequested = false;
volatile bool bleResetReliableRequested = false;
volatile uint16_t bleConnectionId = 0xFFFF;
volatile uint16_t blePeerMtu = 23;
volatile uint32_t bleNotifySuccessCount = 0;
volatile uint32_t bleNotifyErrorCount = 0;
volatile uint32_t bleCommandDropCount = 0;
volatile uint32_t bleLastConfigAckMs = 0;
static uint8_t bleLastConfigAck[12] = {0};
volatile bool bleLastConfigAckValid = false;
volatile uint32_t bleMtuBlockedFrameCount = 0;
volatile uint32_t bleBlocksSent = 0;
volatile uint32_t bleBytesSent = 0;
volatile uint32_t bleReliableAckCount = 0;
volatile uint32_t bleReliableNackCount = 0;
volatile uint32_t bleReliableRetransmitCount = 0;
volatile uint32_t bleReliableRecoveredCount = 0;
volatile uint32_t bleReliableOverflowBlocks = 0;
volatile uint32_t bleReliableUnknownNacks = 0;
volatile uint32_t bleReliableProtocolErrors = 0;
volatile uint32_t bleReliableSessionId = 0;
volatile uint32_t bleReliableHighestAcked = 0xFFFFFFFFu;
volatile uint32_t bleReliableNextBlockSequence = 0;
volatile uint32_t bleReliableNextNewTxSequence = 0;
volatile uint32_t bleReliableLastTxMs = 0;
volatile uint32_t bleReliableLastAckRxMs = 0;
volatile uint32_t bleReliableAckIntervalEwmaMs = 120;
volatile uint32_t bleReliableAdaptiveRetryTimeoutMs = BLE_RELIABLE_RETRY_INITIAL_MS;
volatile bool bleReliableSessionActive = false;
volatile uint32_t bleReliableNackFirst = 0;
volatile uint32_t bleReliableNackLast = 0;
volatile bool bleReliableNackPending = false;
static ReliableBleBlock bleReliableRing[BLE_RELIABLE_RING_BLOCKS] = {};
static uint16_t bleReliableStoredBlocks = 0;
static uint8_t bleReliableBuildPayload[BLE_RELIABLE_PAYLOAD_BYTES] = {};
static uint8_t bleReliableBuildFrameCount = 0;
static uint16_t bleReliableBuildPayloadLength = 0;
static uint32_t bleReliableBuildFirstSampleSequence = 0;

// 当前 PGA 增益。默认 24，与原始版本一致。
volatile uint8_t currentGain = 24;
volatile uint8_t currentGainCode = CH_GAIN_CODE_24;

// GUI 可动态修改的 BIAS_SENSP mask；默认 CH1-CH5。
// 注意：这只控制 BIAS_SENSP(0x0D)，不会打开/关闭 CHnSET 通道。
volatile uint8_t currentBiasSensPMask = ADS_ACTIVE_CH_MASK;
volatile uint8_t currentLeadOffMask = 0x00;

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
// A5 bulk config payload: reference, enabled mask, BIAS mask, SRB2 mask, gains[8].
static uint8_t binaryBulkConfig[12] = {0};
static uint8_t binaryBulkConfigIndex = 0;

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
uint8_t readAdsRegisterOnce(uint8_t address);
uint8_t readAdsRegister(uint8_t address);
bool readAdsFrame(uint8_t *destination, bool &drdyWasLow);

void adsAcquireTask(void *argument);
void transportTask(void *argument);
void processSerialByte(char c);
void flushNumericCommand();
void handleCommand(char command);
void setGainByValue(uint8_t gain);
void setBiasSensPMask(uint8_t mask);
void setChannelConfig(uint8_t channel, uint8_t gain, uint8_t flags);
bool setBulkChannelConfig(const uint8_t *payload);
void setLeadOffMask(uint8_t mask);
bool gainToCode(uint8_t gain, uint8_t &code);
uint8_t makeChannelSetting(uint8_t gainCode, uint8_t mux);
uint8_t makePoweredDownChannelSetting(uint8_t gainCode);
void printHelpAndDiagnostics();
void clearDiagnostics();
void printRegisterReadback();
void sendConfigAck(uint8_t command, uint8_t argument);

bool initBle();
bool bleDataNotificationsEnabled();
uint16_t refreshBlePeerMtu();
bool sendBleBytes(const uint8_t *data, size_t length);
void packBleCompactFrame(const StreamFrame &frame, uint8_t *destination);
void transportFrame(const StreamFrame &frame);
void flushReliableBuildBlock();
void resetReliableTransport(bool resetSequence);
void serviceReliableBleTx();
void handleReliableControl(const ReliableControlCommand &command);
bool storeReliableBlock(const uint8_t *payload, uint16_t payloadLength, uint8_t frameCount, uint32_t firstSampleSequence);
ReliableBleBlock *findReliableBlock(uint32_t blockSequence);
bool sendReliableBlock(ReliableBleBlock &block, bool retransmission);
void releaseAckedBlocks(uint32_t highestContiguousBlock);
uint16_t reliableOutstandingBlocks();
uint16_t reliableInFlightBlocks();
uint32_t reliableAdaptiveTxPaceMs();
void updateReliableAckTiming();
void buildBleStatus(uint8_t *destination);
void publishBleStatus(bool notifyClient);

uint16_t crc16CcittFalse(const uint8_t *data, size_t length);
uint32_t readU32LE(const uint8_t *source);
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

// ============================ BLE callbacks ============================
class EegBleServerCallbacks : public BLEServerCallbacks {
 private:
  void markConnected(BLEServer *server) {
    bleConnected = true;
    bleDataSubscribed = false;
    bleRestartAdvertisingRequested = false;
    bleResetReliableRequested = true;
    bleConnectionId = server ? server->getConnId() : 0xFFFF;
    blePeerMtu = (server && bleConnectionId != 0xFFFF)
      ? server->getPeerMTU(bleConnectionId)
      : 23;
  }

  void markDisconnected() {
    bleConnected = false;
    bleDataSubscribed = false;
    bleConnectionId = 0xFFFF;
    blePeerMtu = 23;
    bleResetReliableRequested = true;
    // Restart advertising from transportTask, not from the BLE callback.
    bleRestartAdvertisingRequested = true;
  }

 public:
  void onConnect(BLEServer *server) override {
    markConnected(server);
  }

  void onDisconnect(BLEServer *server) override {
    (void)server;
    markDisconnected();
  }

#if defined(CONFIG_BLUEDROID_ENABLED)
  void onConnect(BLEServer *server, esp_ble_gatts_cb_param_t *param) override {
    markConnected(server);
    if (server && param) {
      // 6..12 units = 7.5..15 ms. The Windows central may choose another
      // value, but requesting this range gives reliable EEG blocks
      // enough connection events instead of silently falling behind.
      server->updateConnParams(param->connect.remote_bda, 6, 12, 0, 400);
    }
  }

  void onDisconnect(BLEServer *server, esp_ble_gatts_cb_param_t *param) override {
    (void)server;
    (void)param;
    markDisconnected();
  }

  void onMtuChanged(BLEServer *server, esp_ble_gatts_cb_param_t *param) override {
    (void)server;
    if (param && param->mtu.mtu >= 23) blePeerMtu = param->mtu.mtu;
  }
#endif

#if defined(CONFIG_NIMBLE_ENABLED)
  void onConnect(BLEServer *server, ble_gap_conn_desc *description) override {
    markConnected(server);
    if (server && description) {
      server->updateConnParams(description->conn_handle, 6, 12, 0, 400);
    }
  }

  void onDisconnect(BLEServer *server, ble_gap_conn_desc *description) override {
    (void)server;
    (void)description;
    markDisconnected();
  }

  void onMtuChanged(BLEServer *server, ble_gap_conn_desc *description, uint16_t mtu) override {
    (void)server;
    (void)description;
    if (mtu >= 23) blePeerMtu = mtu;
  }
#endif
};

class EegBleControlCallbacks : public BLECharacteristicCallbacks {
 public:
  void onWrite(BLECharacteristic *characteristic) override {
    if (!characteristic) return;

    const uint8_t *data = characteristic->getData();
    const size_t length = characteristic->getLength();
    if (!data || length == 0) return;

    // Reliable ACK/NACK packets are parsed as one characteristic write and
    // forwarded as a compact RTOS command. Ordinary GUI commands keep their
    // existing byte-stream behavior.
    if (length == BLE_CTRL_PACKET_BYTES &&
        data[0] == BLE_CTRL_MAGIC_0 && data[1] == BLE_CTRL_MAGIC_1 &&
        data[2] == BLE_CTRL_VERSION) {
      const uint16_t receivedCrc = static_cast<uint16_t>(data[16]) |
        static_cast<uint16_t>(data[17] << 8);
      const uint16_t calculatedCrc = crc16CcittFalse(data, 16);
      if (receivedCrc != calculatedCrc) {
        bleReliableProtocolErrors++;
        return;
      }
      ReliableControlCommand command = {};
      command.type = data[3];
      command.sessionId = readU32LE(&data[4]);
      command.seqA = readU32LE(&data[8]);
      command.seqB = readU32LE(&data[12]);
      if (!bleReliableCommandQueue ||
          xQueueSend(bleReliableCommandQueue, &command, 0) != pdTRUE) {
        bleCommandDropCount++;
      }
      return;
    }

    if (!bleCommandQueue) return;
    for (size_t i = 0; i < length; ++i) {
      const uint8_t byteValue = data[i];
      if (xQueueSend(bleCommandQueue, &byteValue, 0) != pdTRUE) {
        bleCommandDropCount++;
      }
    }
  }
};

class EegBleDataCallbacks : public BLECharacteristicCallbacks {
 public:
  void onStatus(BLECharacteristic *characteristic, Status status, uint32_t code) override {
    (void)characteristic;
    (void)code;
    if (status == SUCCESS_NOTIFY) {
      bleNotifySuccessCount++;
    } else {
      bleNotifyErrorCount++;
    }
  }

#if defined(CONFIG_NIMBLE_ENABLED)
  void onSubscribe(
    BLECharacteristic *characteristic,
    ble_gap_conn_desc *description,
    uint16_t subscriptionValue
  ) override {
    (void)characteristic;
    (void)description;
    bleDataSubscribed = (subscriptionValue & 0x0001u) != 0;
  }
#endif
};

class EegBleStatusCallbacks : public BLECharacteristicCallbacks {
 public:
  void onRead(BLECharacteristic *characteristic) override {
    if (!characteristic) return;
    // Keep the most recent configuration ACK readable for a short window.
    // This lets the Windows GUI recover when the STATUS notify itself is lost.
    if (bleLastConfigAckValid && (millis() - bleLastConfigAckMs) < 1500u) {
      characteristic->setValue(bleLastConfigAck, sizeof(bleLastConfigAck));
      return;
    }
    uint8_t status[BLE_STATUS_BYTES] = {};
    buildBleStatus(status);
    characteristic->setValue(status, sizeof(status));
  }
};

static EegBleServerCallbacks eegBleServerCallbacks;
static EegBleControlCallbacks eegBleControlCallbacks;
static EegBleDataCallbacks eegBleDataCallbacks;
static EegBleStatusCallbacks eegBleStatusCallbacks;

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
  bleCommandQueue = xQueueCreate(BLE_COMMAND_QUEUE_LENGTH, sizeof(uint8_t));
  bleReliableCommandQueue = xQueueCreate(32, sizeof(ReliableControlCommand));
  if (!frameQueue || !bleCommandQueue || !bleReliableCommandQueue) {
    while (true) delay(1000);
  }

  // BLE failure must not take down the proven ADS + USB path.
  bleInitialized = initBle();

  const BaseType_t adsTaskResult = xTaskCreate(
    adsAcquireTask,
    "ads_acquire",
    4096,
    nullptr,
    5,
    &adsTaskHandle
  );

  const BaseType_t transportTaskResult = xTaskCreate(
    transportTask,
    "usb_ble_transport",
    6144,
    nullptr,
    2,
    nullptr
  );

  if (adsTaskResult != pdPASS || transportTaskResult != pdPASS) {
    while (true) delay(1000);
  }

  acquisitionSequence = 0;
  drdyCount = 0;
  attachInterrupt(digitalPinToInterrupt(PIN_DRDY), onDrdyFalling, FALLING);

  runPhase = PHASE_STOPPED;

  // 这里只在尚未开始二进制数据流时打印。MATLAB 连接后会 flush 掉这些文字。
  Serial.println();
  Serial.println("ESP32C3 ADS1299 SRB1 + RELIABLE BLE V16 ADAPTIVE READY");
  Serial.printf("BLE=%s name=%s requestedMTU=%u minStreamMTU=%u\n",
                bleInitialized ? "READY" : "FAILED",
                BLE_DEVICE_NAME,
                (unsigned)BLE_REQUESTED_MTU,
                (unsigned)BLE_MIN_STREAM_MTU);
  Serial.println("Commands over USB or BLE CONTROL: b/s/e/p/m/*/n/o/q/t/1/2/4/6/8/12/24/r/? plus binary A6/A7/A9");
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

    // 采集任务永不等待 USB/BLE。队列满时丢帧，下一帧 sequence 会产生缺口。
    if (xQueueSend(frameQueue, &frame, 0) != pdTRUE) {
      queueDropCount++;
    }
  }
}

// ============================ USB + BLE transport task ============================
void transportTask(void *argument) {
  (void)argument;
  StreamFrame frame = {};
  ReliableControlCommand reliableCommand = {};
  uint32_t disconnectSeenAtMs = 0;
  uint32_t lastStatusRefreshMs = 0;

  for (;;) {
    // ACK/NACK control has priority so an interrupted Windows receiver can
    // immediately release or request retained blocks.
    while (bleReliableCommandQueue &&
           xQueueReceive(bleReliableCommandQueue, &reliableCommand, 0) == pdTRUE) {
      handleReliableControl(reliableCommand);
    }

    // Ordinary BLE callbacks enqueue bytes only. Parsing and ADS register
    // writes stay in this task, never in the BLE callback.
    uint8_t bleCommandByte = 0;
    while (bleCommandQueue && xQueueReceive(bleCommandQueue, &bleCommandByte, 0) == pdTRUE) {
      processSerialByte(static_cast<char>(bleCommandByte));
    }

    while (Serial.available() > 0) {
      const char c = static_cast<char>(Serial.read());
      processSerialByte(c);
    }

    if (numericCommandLength > 0 && (millis() - lastNumericCommandMs) > 35) {
      flushNumericCommand();
    }

    if (bleResetReliableRequested && !streamingEnabled) {
      resetReliableTransport(false);
      bleResetReliableRequested = false;
    }

    if (bleRestartAdvertisingRequested) {
      if (disconnectSeenAtMs == 0) disconnectSeenAtMs = millis();
      if ((millis() - disconnectSeenAtMs) >= 250) {
        BLEDevice::startAdvertising();
        bleRestartAdvertisingRequested = false;
        disconnectSeenAtMs = 0;
      }
    } else {
      disconnectSeenAtMs = 0;
    }

    const uint32_t statusRefreshIntervalMs = streamingEnabled ? 10000u : 2000u;
    if (bleInitialized &&
        (millis() - lastStatusRefreshMs) >= statusRefreshIntervalMs &&
        (millis() - bleLastConfigAckMs) >= 1000u) {
      publishBleStatus(bleConnected);
      lastStatusRefreshMs = millis();
    }

    // V14 USB fast path: when no BLE DATA subscription/session exists, run
    // the same simple queue -> Serial.write loop as the proven P0P1 firmware.
    // The reliable BLE path below is left unchanged once BLE streaming begins.
    const bool bleStreamPathActive =
      bleDataNotificationsEnabled() || bleReliableSessionActive;
    if (!bleStreamPathActive) {
      if (xQueueReceive(frameQueue, &frame, pdMS_TO_TICKS(2)) == pdTRUE) {
        if (runPhase == PHASE_STREAMING && Serial) {
          Serial.write(frame.bytes, STREAM_FRAME_BYTES);
        }
      } else {
        taskYIELD();
      }
      continue;
    }

    // When the retention ring is nearly full, stop draining new frames for a
    // short time and use the 160-frame acquisition queue as an extra shock
    // absorber.  This gives ACK/NACK repair a chance to free retained blocks
    // instead of immediately discarding a complete four-frame BLE block.
    if (bleReliableStoredBlocks < (BLE_RELIABLE_RING_BLOCKS - 1u)) {
      if (xQueueReceive(frameQueue, &frame, pdMS_TO_TICKS(1)) == pdTRUE) {
        if (runPhase == PHASE_STREAMING) {
          transportFrame(frame);
        }
      }
    } else {
      vTaskDelay(1);
    }

    // At most one reliable block is submitted per paced service call. This
    // keeps the BLE host queue bounded while still allowing backlog catch-up.
    serviceReliableBleTx();
    taskYIELD();
  }
}

void processSerialByte(char c) {
  const uint8_t byteValue = static_cast<uint8_t>(c);

  // 二进制控制协议必须先于 ASCII 数字/命令解析。
  // A5 uses one atomic frontend reconfiguration for all eight channels.
  if (binaryControlState == 40) {
    binaryBulkConfig[binaryBulkConfigIndex++] = byteValue;
    if (binaryBulkConfigIndex >= sizeof(binaryBulkConfig)) {
      const bool applied = setBulkChannelConfig(binaryBulkConfig);
      sendConfigAck(0xA5, binaryBulkConfig[1]);
      (void)applied;
      binaryBulkConfigIndex = 0;
      binaryControlState = 0;
    }
    return;
  }

  // 0xA6 0x0D mask -> 修改逻辑 BIAS mask。
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

  if (binaryControlState == 30) {
    setLeadOffMask(byteValue);
    sendConfigAck(0xA9, byteValue);
    binaryControlState = 0;
    return;
  }

  if (byteValue == 0xA5) {
    flushNumericCommand();
    binaryBulkConfigIndex = 0;
    binaryControlState = 40;
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

  if (byteValue == 0xA9) {
    flushNumericCommand();
    binaryControlState = 30;
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


bool setBulkChannelConfig(const uint8_t *payload) {
  if (!payload || runPhase == PHASE_STREAMING) return false;

  uint8_t gainCodes[8] = {0};
  for (uint8_t ch = 0; ch < 8; ++ch) {
    if (!gainToCode(payload[4 + ch], gainCodes[ch])) return false;
  }

  currentEnabledMask = payload[1];
  currentBiasSensPMask = static_cast<uint8_t>(payload[2] & currentEnabledMask);
  for (uint8_t ch = 0; ch < 8; ++ch) {
    channelGain[ch] = payload[4 + ch];
    channelGainCode[ch] = gainCodes[ch];
  }
  currentGain = channelGain[0];
  currentGainCode = channelGainCode[0];

  // SRB1-only firmware intentionally ignores payload[0] reference and
  // payload[3] SRB2 mask, then performs one complete write/readback cycle.
  configureFrontend(static_cast<FrontendMode>(currentMode));
  return configurationVerified;
}

void setLeadOffMask(uint8_t mask) {
  if (runPhase == PHASE_STREAMING) return;
  currentLeadOffMask = static_cast<uint8_t>(mask & currentEnabledMask);
  configureFrontend(static_cast<FrontendMode>(currentMode));
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
  const uint8_t leadOffMask =
    srb1Enabled ? static_cast<uint8_t>(currentLeadOffMask & currentEnabledMask) : 0x00;
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
  writeAdsRegister(0x04, leadOffMask ? LOFF_AC_6NA_31HZ : 0x00);
  writeAdsRegister(0x0F, leadOffMask);  // LOFF_SENSP
  writeAdsRegister(0x10, 0x00);         // LOFF_SENSN
  writeAdsRegister(0x11, 0x00);         // LOFF_FLIP
  writeAdsRegister(0x15, srb1Enabled ? MISC1_SRB1_ON : MISC1_SRB1_OFF);

  currentMode = mode;
  configurationVerified = false;
  for (uint8_t attempt = 0; attempt < 3 && !configurationVerified; ++attempt) {
    if (attempt) delay(3);
    configurationVerified = verifyFrontendLocked(mode);
  }
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
  const uint8_t expectedLeadOffMask =
    expectedSrb1 ? static_cast<uint8_t>(currentLeadOffMask & currentEnabledMask) : 0x00;

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
  ok &= readAdsRegister(0x04) == (expectedLeadOffMask ? LOFF_AC_6NA_31HZ : 0x00);
  ok &= readAdsRegister(0x0F) == expectedLeadOffMask;
  ok &= readAdsRegister(0x10) == 0x00;
  ok &= readAdsRegister(0x11) == 0x00;
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
  resetReliableTransport(true);
  bleResetReliableRequested = false;
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

  // transportTask remains the sole DATA TX owner and drains every complete frame
  // accepted before the converter stopped, then seals a partial reliable block.
  StreamFrame tail = {};
  while (frameQueue && xQueueReceive(frameQueue, &tail, 0) == pdTRUE) {
    transportFrame(tail);
  }
  flushReliableBuildBlock();
  // Give already-retained blocks a short chance to leave. ACKed blocks are
  // released by the normal control path; no EEG value is fabricated here.
  const uint32_t drainStart = millis();
  while (bleDataNotificationsEnabled() && reliableOutstandingBlocks() > 0 &&
         (millis() - drainStart) < 300) {
    serviceReliableBleTx();
    vTaskDelay(1);
  }
  if (Serial) Serial.flush();
  if (frameQueue) xQueueReset(frameQueue);
}

// ============================ BLE implementation ============================
bool initBle() {
  // BLEDevice::init() is void in the user's Arduino-ESP32 core.
  // Do not test its return value.
  BLEDevice::init(BLE_DEVICE_NAME);

  // This sets our local maximum. The central still has to negotiate its peer MTU.
  BLEDevice::setMTU(BLE_REQUESTED_MTU);

  bleServer = BLEDevice::createServer();
  if (!bleServer) return false;
  bleServer->setCallbacks(&eegBleServerCallbacks);

  BLEService *service = bleServer->createService(BLE_SERVICE_UUID);
  if (!service) return false;

  bleDataCharacteristic = service->createCharacteristic(
    BLE_DATA_UUID,
    BLECharacteristic::PROPERTY_NOTIFY
  );
  bleControlCharacteristic = service->createCharacteristic(
    BLE_CONTROL_UUID,
    BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR
  );
  bleStatusCharacteristic = service->createCharacteristic(
    BLE_STATUS_UUID,
    BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY
  );
  if (!bleDataCharacteristic || !bleControlCharacteristic || !bleStatusCharacteristic) {
    return false;
  }

  bleDataCharacteristic->setCallbacks(&eegBleDataCallbacks);
  bleControlCharacteristic->setCallbacks(&eegBleControlCallbacks);
  bleStatusCharacteristic->setCallbacks(&eegBleStatusCallbacks);

#if defined(CONFIG_BLUEDROID_ENABLED)
  // Bluedroid needs an explicit CCCD. NimBLE creates it automatically.
  bleDataCccd = new BLE2902();
  bleDataCharacteristic->addDescriptor(bleDataCccd);
  bleStatusCharacteristic->addDescriptor(new BLE2902());
#endif

  uint8_t initialStatus[BLE_STATUS_BYTES] = {};
  buildBleStatus(initialStatus);
  bleStatusCharacteristic->setValue(initialStatus, sizeof(initialStatus));

  service->start();
  BLEAdvertising *advertising = BLEDevice::getAdvertising();
  advertising->addServiceUUID(BLE_SERVICE_UUID);
  advertising->setScanResponse(false);
  // Do not force an advertising connection-interval hint; the central controls it.
  advertising->setMinPreferred(0x00);
  BLEDevice::startAdvertising();
  return true;
}

bool bleDataNotificationsEnabled() {
  if (!bleInitialized || !bleConnected || !bleDataCharacteristic) return false;
#if defined(CONFIG_BLUEDROID_ENABLED)
  return bleDataCccd && bleDataCccd->getNotifications();
#elif defined(CONFIG_NIMBLE_ENABLED)
  return bleDataSubscribed;
#else
  return false;
#endif
}

uint16_t refreshBlePeerMtu() {
  uint16_t mtu = 23;
  if (bleConnected && bleServer && bleConnectionId != 0xFFFF) {
    const uint16_t reported = bleServer->getPeerMTU(bleConnectionId);
    if (reported >= 23) mtu = reported;
  }
  if (mtu > BLE_REQUESTED_MTU) mtu = BLE_REQUESTED_MTU;
  blePeerMtu = mtu;
  return mtu;
}

bool sendBleBytes(const uint8_t *data, size_t length) {
  if (!data || length == 0) return true;
  if (!bleDataNotificationsEnabled()) return false;

  const uint16_t mtu = refreshBlePeerMtu();
  if (mtu < BLE_MIN_STREAM_MTU) return false;

  const size_t payloadCapacity = static_cast<size_t>(mtu - 3u);
  size_t offset = 0;
  while (offset < length) {
    if (!bleConnected || !bleDataNotificationsEnabled()) return false;

    size_t chunk = length - offset;
    if (chunk > payloadCapacity) chunk = payloadCapacity;
    bleDataCharacteristic->setValue(data + offset, chunk);
    bleDataCharacteristic->notify();
    bleBytesSent += chunk;
    offset += chunk;
    vTaskDelay(1);
  }
  return true;
}

static bool reliableSeqLessOrEqual(uint32_t lhs, uint32_t rhs) {
  return static_cast<int32_t>(lhs - rhs) <= 0;
}

static uint32_t reliableFirstUnackedSequence() {
  return bleReliableHighestAcked == 0xFFFFFFFFu
    ? 0u
    : bleReliableHighestAcked + 1u;
}

uint16_t reliableOutstandingBlocks() {
  return bleReliableStoredBlocks;
}

uint16_t reliableInFlightBlocks() {
  uint16_t count = 0;
  for (uint16_t i = 0; i < BLE_RELIABLE_RING_BLOCKS; ++i) {
    const ReliableBleBlock &slot = bleReliableRing[i];
    if (slot.valid && slot.sent) count++;
  }
  return count;
}

uint32_t reliableAdaptiveTxPaceMs() {
  const uint16_t inFlight = reliableInFlightBlocks();
  if (inFlight >= 12u) return BLE_RELIABLE_TX_PACE_SLOW_MS;
  if (inFlight >= 8u) return BLE_RELIABLE_TX_PACE_NORMAL_MS;
  return BLE_RELIABLE_TX_PACE_FAST_MS;
}

void updateReliableAckTiming() {
  const uint32_t now = millis();
  if (bleReliableLastAckRxMs != 0u) {
    const uint32_t interval = now - bleReliableLastAckRxMs;
    if (interval > 0u && interval < 5000u) {
      bleReliableAckIntervalEwmaMs =
        (bleReliableAckIntervalEwmaMs * 7u + interval) / 8u;
      uint32_t retry = bleReliableAckIntervalEwmaMs * 4u + 250u;
      if (retry < BLE_RELIABLE_RETRY_MIN_MS) retry = BLE_RELIABLE_RETRY_MIN_MS;
      if (retry > BLE_RELIABLE_RETRY_MAX_MS) retry = BLE_RELIABLE_RETRY_MAX_MS;
      bleReliableAdaptiveRetryTimeoutMs = retry;
    }
  }
  bleReliableLastAckRxMs = now;
}

void resetReliableTransport(bool resetSequence) {
  memset(bleReliableRing, 0, sizeof(bleReliableRing));
  memset(bleReliableBuildPayload, 0, sizeof(bleReliableBuildPayload));
  bleReliableStoredBlocks = 0;
  bleReliableBuildFrameCount = 0;
  bleReliableBuildPayloadLength = 0;
  bleReliableBuildFirstSampleSequence = 0;
  bleReliableHighestAcked = 0xFFFFFFFFu;
  if (resetSequence) {
    bleReliableNextBlockSequence = 0;
    bleReliableSessionId++;
    if (bleReliableSessionId == 0) bleReliableSessionId = 1;
  }
  bleReliableNextNewTxSequence = bleReliableNextBlockSequence;
  bleReliableLastTxMs = 0;
  bleReliableLastAckRxMs = 0;
  bleReliableAckIntervalEwmaMs = 120;
  bleReliableAdaptiveRetryTimeoutMs = BLE_RELIABLE_RETRY_INITIAL_MS;
  bleReliableNackFirst = 0;
  bleReliableNackLast = 0;
  bleReliableNackPending = false;
  bleReliableSessionActive = bleDataNotificationsEnabled();
}

ReliableBleBlock *findReliableBlock(uint32_t blockSequence) {
  ReliableBleBlock &slot = bleReliableRing[blockSequence % BLE_RELIABLE_RING_BLOCKS];
  if (!slot.valid || slot.blockSequence != blockSequence) return nullptr;
  return &slot;
}

bool storeReliableBlock(
  const uint8_t *payload,
  uint16_t payloadLength,
  uint8_t frameCount,
  uint32_t firstSampleSequence
) {
  if (!payload || payloadLength == 0 || frameCount == 0 ||
      payloadLength > BLE_RELIABLE_PAYLOAD_BYTES) return false;

  const uint32_t blockSequence = bleReliableNextBlockSequence;
  ReliableBleBlock &slot = bleReliableRing[blockSequence % BLE_RELIABLE_RING_BLOCKS];
  if (slot.valid) {
    // The retention ring is full. Drop this ADS payload, but DO NOT consume a
    // reliable block sequence number. Consuming it created a permanent protocol
    // hole: the receiver waited for a block that never existed and both ends
    // could deadlock once the transmit window filled. The next stored block
    // keeps a contiguous block sequence; its ADS sample sequence still exposes
    // the exact lost samples to the GUI.
    bleReliableOverflowBlocks++;
    return false;
  }
  bleReliableNextBlockSequence++;

  memset(&slot, 0, sizeof(slot));
  slot.valid = true;
  slot.sent = false;
  slot.frameCount = frameCount;
  slot.payloadLength = payloadLength;
  slot.blockSequence = blockSequence;
  slot.firstSampleSequence = firstSampleSequence;
  memcpy(slot.payload, payload, payloadLength);
  bleReliableStoredBlocks++;
  return true;
}

void flushReliableBuildBlock() {
  if (bleReliableBuildFrameCount == 0 || bleReliableBuildPayloadLength == 0) return;
  storeReliableBlock(
    bleReliableBuildPayload,
    bleReliableBuildPayloadLength,
    bleReliableBuildFrameCount,
    bleReliableBuildFirstSampleSequence
  );
  bleReliableBuildFrameCount = 0;
  bleReliableBuildPayloadLength = 0;
  bleReliableBuildFirstSampleSequence = 0;
}

static bool sendReliableGapMarker(uint32_t blockSequence) {
  uint8_t packet[BLE_RELIABLE_HEADER_BYTES + BLE_RELIABLE_CRC_BYTES] = {};
  packet[0] = BLE_BLOCK_MAGIC_0;
  packet[1] = BLE_BLOCK_MAGIC_1;
  packet[2] = BLE_RELIABLE_VERSION;
  packet[3] = 0x04u;  // unrecoverable block marker
  writeU32LE(&packet[4], bleReliableSessionId);
  writeU32LE(&packet[8], blockSequence);
  writeU32LE(&packet[12], 0);
  packet[16] = 0;
  packet[17] = 0;
  writeU16LE(&packet[18], 0);
  const uint16_t crc = crc16CcittFalse(packet, BLE_RELIABLE_HEADER_BYTES);
  writeU16LE(&packet[BLE_RELIABLE_HEADER_BYTES], crc);
  return sendBleBytes(packet, sizeof(packet));
}

bool sendReliableBlock(ReliableBleBlock &block, bool retransmission) {
  if (!block.valid) return false;
  uint8_t packet[BLE_RELIABLE_PACKET_MAX_BYTES] = {};
  packet[0] = BLE_BLOCK_MAGIC_0;
  packet[1] = BLE_BLOCK_MAGIC_1;
  packet[2] = BLE_RELIABLE_VERSION;
  uint8_t flags = retransmission ? 0x01u : 0x00u;
  if (block.frameCount < BLE_RELIABLE_FRAMES_PER_BLOCK) flags |= 0x02u;
  packet[3] = flags;
  writeU32LE(&packet[4], bleReliableSessionId);
  writeU32LE(&packet[8], block.blockSequence);
  writeU32LE(&packet[12], block.firstSampleSequence);
  packet[16] = block.frameCount;
  packet[17] = 0;
  writeU16LE(&packet[18], block.payloadLength);
  memcpy(&packet[BLE_RELIABLE_HEADER_BYTES], block.payload, block.payloadLength);
  const size_t crcOffset = BLE_RELIABLE_HEADER_BYTES + block.payloadLength;
  const uint16_t crc = crc16CcittFalse(packet, crcOffset);
  writeU16LE(&packet[crcOffset], crc);

  if (!sendBleBytes(packet, crcOffset + BLE_RELIABLE_CRC_BYTES)) return false;
  block.sent = true;
  if (block.retries < 255) block.retries++;
  block.lastSentMs = millis();
  bleReliableLastTxMs = block.lastSentMs;
  bleBlocksSent++;
  if (retransmission) bleReliableRetransmitCount++;
  return true;
}

void releaseAckedBlocks(uint32_t highestContiguousBlock) {
  if (bleReliableNextBlockSequence == 0) return;
  const uint32_t newestProduced = bleReliableNextBlockSequence - 1u;
  if (!reliableSeqLessOrEqual(highestContiguousBlock, newestProduced)) {
    highestContiguousBlock = newestProduced;
  }
  if (bleReliableHighestAcked != 0xFFFFFFFFu &&
      reliableSeqLessOrEqual(highestContiguousBlock, bleReliableHighestAcked)) {
    return;
  }

  updateReliableAckTiming();
  for (uint16_t i = 0; i < BLE_RELIABLE_RING_BLOCKS; ++i) {
    ReliableBleBlock &slot = bleReliableRing[i];
    if (!slot.valid) continue;
    if (reliableSeqLessOrEqual(slot.blockSequence, highestContiguousBlock)) {
      if (slot.retries > 1) bleReliableRecoveredCount++;
      slot.valid = false;
      if (bleReliableStoredBlocks > 0) bleReliableStoredBlocks--;
    }
  }
  bleReliableHighestAcked = highestContiguousBlock;
  const uint32_t next = highestContiguousBlock + 1u;
  if (reliableSeqLessOrEqual(bleReliableNextNewTxSequence, highestContiguousBlock)) {
    bleReliableNextNewTxSequence = next;
  }
  bleReliableAckCount++;
}

void handleReliableControl(const ReliableControlCommand &command) {
  if (command.type != BLE_CTRL_RESET && command.sessionId != bleReliableSessionId) {
    // Stale ACKs from a previous recording must never release current blocks.
    bleReliableProtocolErrors++;
    return;
  }
  switch (command.type) {
    case BLE_CTRL_ACK:
      releaseAckedBlocks(command.seqA);
      break;

    case BLE_CTRL_NACK_RANGE: {
      uint32_t first = command.seqA;
      uint32_t last = command.seqB;
      if (!reliableSeqLessOrEqual(first, last)) {
        bleReliableProtocolErrors++;
        return;
      }
      // Bound a malformed request to one retained-ring span.
      if ((last - first) >= BLE_RELIABLE_RING_BLOCKS) {
        last = first + BLE_RELIABLE_RING_BLOCKS - 1u;
      }
      if (!bleReliableNackPending || reliableSeqLessOrEqual(first, bleReliableNackFirst)) {
        bleReliableNackFirst = first;
        bleReliableNackLast = last;
      } else if (reliableSeqLessOrEqual(first, bleReliableNackLast + 1u)) {
        if (reliableSeqLessOrEqual(bleReliableNackLast, last)) bleReliableNackLast = last;
      }
      bleReliableNackPending = true;
      bleReliableNackCount++;
      break;
    }

    case BLE_CTRL_RESET:
      resetReliableTransport(true);
      break;

    default:
      bleReliableProtocolErrors++;
      break;
  }
}

void serviceReliableBleTx() {
  if (!bleReliableSessionActive || !bleDataNotificationsEnabled()) return;
  if (refreshBlePeerMtu() < BLE_MIN_STREAM_MTU) return;

  const uint32_t now = millis();
  const uint32_t txPaceMs = reliableAdaptiveTxPaceMs();
  if ((now - bleReliableLastTxMs) < txPaceMs) return;

  // Explicit receiver repair requests always win over new data.
  if (bleReliableNackPending) {
    const uint32_t requested = bleReliableNackFirst;
    ReliableBleBlock *block = findReliableBlock(requested);
    if (block) {
      sendReliableBlock(*block, true);
    } else {
      bleReliableUnknownNacks++;
      if (sendReliableGapMarker(requested)) bleReliableLastTxMs = millis();
    }
    if (requested == bleReliableNackLast) {
      bleReliableNackPending = false;
    } else {
      bleReliableNackFirst = requested + 1u;
    }
    return;
  }

  if (bleReliableNextBlockSequence == 0) return;

  // If the cumulative ACK was lost, retry the oldest unacknowledged block.
  ReliableBleBlock *oldestTimedOut = nullptr;
  const uint32_t firstUnacked = reliableFirstUnackedSequence();
  for (uint32_t seq = firstUnacked;
       reliableSeqLessOrEqual(seq, bleReliableNextBlockSequence - (bleReliableNextBlockSequence ? 1u : 0u));
       ++seq) {
    ReliableBleBlock *candidate = findReliableBlock(seq);
    if (!candidate || !candidate->sent) continue;
    const uint32_t noAckAge = bleReliableLastAckRxMs == 0u
      ? (now - candidate->lastSentMs)
      : (now - bleReliableLastAckRxMs);
    if ((now - candidate->lastSentMs) >= bleReliableAdaptiveRetryTimeoutMs &&
        noAckAge >= bleReliableAdaptiveRetryTimeoutMs) {
      oldestTimedOut = candidate;
      break;
    }
  }
  if (oldestTimedOut) {
    sendReliableBlock(*oldestTimedOut, true);
    return;
  }

  const uint32_t windowFirst = reliableFirstUnackedSequence();
  const uint32_t windowLast = windowFirst + BLE_RELIABLE_WINDOW_BLOCKS - 1u;
  while (reliableSeqLessOrEqual(bleReliableNextNewTxSequence, windowLast) &&
         reliableSeqLessOrEqual(bleReliableNextNewTxSequence,
           bleReliableNextBlockSequence - (bleReliableNextBlockSequence ? 1u : 0u))) {
    const uint32_t seq = bleReliableNextNewTxSequence++;
    ReliableBleBlock *block = findReliableBlock(seq);
    if (!block || block->sent) continue;
    sendReliableBlock(*block, false);
    return;
  }
}

void packBleCompactFrame(const StreamFrame &frame, uint8_t *destination) {
  if (!destination) return;
  // Preserve every EEG count and the sample identity, while removing fields
  // that are only useful for USB diagnostics.  The host reconstructs the
  // standard 48-byte BIN frame before parsing/writing, so file compatibility
  // is unchanged.
  memcpy(&destination[0], &frame.bytes[4], 4);    // ADS sample sequence
  memcpy(&destination[4], &frame.bytes[8], 4);    // device timestamp
  memcpy(&destination[8], &frame.bytes[12], 3);   // ADS status
  memcpy(&destination[11], &frame.bytes[16], 24); // 8 x signed 24-bit EEG
  destination[35] = frame.bytes[15];              // mode/reference flags
}

void transportFrame(const StreamFrame &frame) {
  const bool bleActive = bleDataNotificationsEnabled();
  if (bleActive) bleReliableSessionActive = true;
  const bool writeUsb = Serial && (
    MIRROR_STREAM_TO_USB_WHILE_BLE ||
    (ENABLE_USB_STREAM_WHEN_BLE_IDLE && !bleActive && !bleReliableSessionActive)
  );
  if (writeUsb) Serial.write(frame.bytes, STREAM_FRAME_BYTES);

  // USB-only operation keeps the original raw stream and does not consume the
  // reliable ring. Once a BLE stream has started, temporary disconnects are
  // buffered until the client returns or the retention ring fills.
  if (!bleReliableSessionActive) return;

  if (bleReliableBuildFrameCount == 0) {
    bleReliableBuildFirstSampleSequence = readU32LE(&frame.bytes[4]);
  }
  if (bleReliableBuildPayloadLength + BLE_COMPACT_FRAME_BYTES > BLE_RELIABLE_PAYLOAD_BYTES) {
    flushReliableBuildBlock();
    bleReliableBuildFirstSampleSequence = readU32LE(&frame.bytes[4]);
  }
  packBleCompactFrame(
    frame,
    &bleReliableBuildPayload[bleReliableBuildPayloadLength]
  );
  bleReliableBuildPayloadLength += BLE_COMPACT_FRAME_BYTES;
  bleReliableBuildFrameCount++;
  if (bleReliableBuildFrameCount >= BLE_RELIABLE_FRAMES_PER_BLOCK) {
    flushReliableBuildBlock();
  }
}

void buildBleStatus(uint8_t *destination) {
  if (!destination) return;
  memset(destination, 0, BLE_STATUS_BYTES);
  destination[0] = 0xBC;
  destination[1] = 0x53;  // 'S' = status
  destination[2] = 0x04;  // reliable BLE status protocol V4 / compact DATA V2
  destination[3] = static_cast<uint8_t>(runPhase);
  destination[4] = static_cast<uint8_t>(currentMode);

  uint8_t flags = 0;
  if (bleConnected) flags |= (1u << 0);
  if (bleDataNotificationsEnabled()) flags |= (1u << 1);
  if (streamingEnabled) flags |= (1u << 2);
  if (configurationVerified) flags |= (1u << 3);
  if (refreshBlePeerMtu() >= BLE_MIN_STREAM_MTU) flags |= (1u << 4);
  if (bleReliableSessionActive) flags |= (1u << 5);
  if (bleReliableNackPending) flags |= (1u << 6);
  destination[5] = flags;
  writeU16LE(&destination[6], blePeerMtu);
  writeU32LE(&destination[8], acquisitionSequence);
  writeU32LE(&destination[12], queueDropCount);
  writeU32LE(&destination[16], bleNotifyErrorCount);
  writeU32LE(&destination[20], bleCommandDropCount);
  writeU32LE(&destination[24], bleMtuBlockedFrameCount);
  writeU32LE(&destination[28], bleBlocksSent);
  uint16_t sentUnacked = 0;
  for (uint16_t i = 0; i < BLE_RELIABLE_RING_BLOCKS; ++i) {
    if (bleReliableRing[i].valid && bleReliableRing[i].sent) sentUnacked++;
  }
  writeU16LE(&destination[32], bleReliableStoredBlocks);
  writeU16LE(&destination[34], sentUnacked);
  writeU32LE(&destination[36], bleReliableHighestAcked);
  writeU32LE(&destination[40], bleReliableNextBlockSequence);
  writeU32LE(&destination[44], bleReliableAckCount);
  writeU32LE(&destination[48], bleReliableNackCount);
  writeU32LE(&destination[52], bleReliableRetransmitCount);
  writeU32LE(&destination[56], bleReliableRecoveredCount);
  writeU32LE(&destination[60], bleReliableOverflowBlocks);
  writeU32LE(&destination[64], bleReliableUnknownNacks);
  writeU32LE(&destination[68], bleReliableProtocolErrors);
}

void publishBleStatus(bool notifyClient) {
  if (!bleInitialized || !bleStatusCharacteristic) return;
  uint8_t status[BLE_STATUS_BYTES] = {};
  buildBleStatus(status);
  bleStatusCharacteristic->setValue(status, sizeof(status));
  if (notifyClient && bleConnected) {
    bleStatusCharacteristic->notify();
  }
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

uint32_t readU32LE(const uint8_t *source) {
  if (!source) return 0;
  return static_cast<uint32_t>(source[0]) |
    (static_cast<uint32_t>(source[1]) << 8) |
    (static_cast<uint32_t>(source[2]) << 16) |
    (static_cast<uint32_t>(source[3]) << 24);
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

uint8_t readAdsRegisterOnce(uint8_t address) {
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

uint8_t readAdsRegister(uint8_t address) {
  // Configuration is infrequent, so favor deterministic readback over speed.
  // Return as soon as any value has appeared three times among at most five
  // reads.  This tolerates two isolated software-SPI glitches.
  uint8_t samples[5] = {0};
  for (uint8_t i = 0; i < 5; ++i) {
    samples[i] = readAdsRegisterOnce(address);
    uint8_t matches = 0;
    for (uint8_t j = 0; j <= i; ++j) {
      if (samples[j] == samples[i]) matches++;
    }
    if (matches >= 3) return samples[i];
    delay(1);
  }
  return samples[4];
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
    if (command == 0xA9) {
      reply[3] = currentLeadOffMask;
      reply[4] = readAdsRegister(0x0F);
      reply[5] = readAdsRegister(0x10);
      reply[6] = readAdsRegister(0x04);
    } else {
      reply[3] = command == 0xA7
        ? readAdsRegister(static_cast<uint8_t>(ADS_FIRST_CH_REG + (argument & 0x07u)))
        : currentBiasSensPMask;
      reply[4] = readAdsRegister(0x0D);
      reply[5] = readAdsRegister(0x0E);
      reply[6] = readAdsRegister(0x15);
    }
    xSemaphoreGive(adsBusMutex);
    reply[9] = configurationVerified ? 0x01u : 0x00u;
  }

  for (uint8_t i = 0; i < 11; ++i) reply[11] ^= reply[i];
  bleLastConfigAckValid = false;
  memcpy(bleLastConfigAck, reply, sizeof(reply));
  bleLastConfigAckValid = true;
  bleLastConfigAckMs = millis();
  if (Serial) Serial.write(reply, sizeof(reply));
  if (bleInitialized && bleConnected && bleStatusCharacteristic) {
    // ACK is isolated on STATUS, never inserted into the EEG DATA byte stream.
    bleStatusCharacteristic->setValue(reply, sizeof(reply));
    bleStatusCharacteristic->notify();
  }
}

void clearDiagnostics() {
  missedDrdyCount = 0;
  lateDrdyCount = 0;
  mutexBusyCount = 0;
  badStatusCount = 0;
  queueDropCount = 0;
  validReadCount = 0;
  maxReadTimeUs = 0;
  bleNotifySuccessCount = 0;
  bleNotifyErrorCount = 0;
  bleCommandDropCount = 0;
  bleMtuBlockedFrameCount = 0;
  bleBlocksSent = 0;
  bleBytesSent = 0;
  bleReliableAckCount = 0;
  bleReliableNackCount = 0;
  bleReliableRetransmitCount = 0;
  bleReliableRecoveredCount = 0;
  bleReliableOverflowBlocks = 0;
  bleReliableUnknownNacks = 0;
  bleReliableProtocolErrors = 0;
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
  Serial.printf("BLE init=%u connected=%u subscribed=%u mtu=%u blocks=%lu bytes=%lu\n",
                bleInitialized ? 1u : 0u,
                bleConnected ? 1u : 0u,
                bleDataNotificationsEnabled() ? 1u : 0u,
                (unsigned)refreshBlePeerMtu(),
                (unsigned long)bleBlocksSent,
                (unsigned long)bleBytesSent);
  Serial.printf("BLE notifyOK=%lu notifyErr=%lu commandDrop=%lu mtuBlockedFrames=%lu\n",
                (unsigned long)bleNotifySuccessCount,
                (unsigned long)bleNotifyErrorCount,
                (unsigned long)bleCommandDropCount,
                (unsigned long)bleMtuBlockedFrameCount);
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
