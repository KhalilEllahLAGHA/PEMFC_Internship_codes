/*
 * ===========================================================================
 *  PRBS_generator.ino
 *  ------------------
 *  Pseudo-Random Binary Sequence (PRBS) generator for PEM fuel-cell stack
 *  system identification.
 *
 *  DESIGN PARAMETERS — derived from the measured time-response table
 *  "results experiment TR current/response_time_results.csv":
 *
 *    | Experiment | Signal  |  tau [s] | T_5% [s] | T_2% [s] |
 *    |  3.3 ohm   | Current |   8.18   |   24.5   |   32.0   |
 *    |  3.3 ohm   | Voltage |  23.74   |   71.1   |   92.9   |
 *    |  6.8 ohm   | Current |  10.27   |   30.8   |   40.2   |
 *    |  6.8 ohm   | Voltage |  15.44   |   46.2   |   60.4   |
 *
 *  Standard PRBS design rules (Landau / Ljung):
 *   1. Bit period:    T_bit ~ tau_min / 3 = 8.18 / 3 ~ 2.7 s
 *      (short enough to excite the fastest dynamics: the current loop).
 *   2. Order n:       one PRBS period (2^n - 1) * T_bit must exceed the
 *      slowest settling time (T_2% = 92.9 s) with margin, so that the
 *      lowest excited frequency is below the slowest pole.
 *        n = 7  ->  N = 127 bits  ->  period = 127 * 2.7 s = 342.9 s
 *        (342.9 s / 92.9 s ~ 3.7x margin; n = 6 would give only 1.8x).
 *   3. Repetitions:   >= 3 full periods to average noise (first period is
 *      usually discarded as transient). 4 periods chosen ~ 23 min test.
 *   4. Start delay:   the stack must be at steady state before excitation
 *      starts: longest T_2% = 92.9 s -> rounded up to 95 s.
 *   5. Amplitude:     the pin switches 0 V / 5 V and drives the gate of
 *      the load-switching MOSFET. Expected small-signal stack response
 *      (from the table): dI ~ 53...82 mA, dV ~ 1.35...1.73 V.
 *
 *  SERIAL PROTOCOL (115200 baud, same rate as the PEMstack software):
 *    On boot          -> prints a one-line config summary "PRBS_CONFIG,..."
 *    Cycle start      -> "PRBS_CYCLE,<cycle>,<t_ms>,<nHigh>,<nLow>"
 *    Completed/stopped-> "PRBS_DONE" (output pin forced LOW)
 *    Command "STOP\n" -> aborts immediately (safe stop)
 *    Command "START\n"-> skips the remaining start delay (optional)
 * ===========================================================================
 */

// ---------------------------------------------------------------------------
// USER-ADJUSTABLE CONSTANTS (all values taken/derived from the time-response
// table — see the header comment for the derivation of each number)
// ---------------------------------------------------------------------------
const uint8_t       PRBS_OUTPUT_PIN   = 9;          // digital pin -> load-switch MOSFET gate (adapt to bench wiring)
const uint8_t       LFSR_ORDER        = 7;          // PRBS order n  -> sequence length N = 2^n - 1 = 127 bits
const unsigned long BIT_PERIOD_MS     = 2700UL;     // T_bit ~ tau_min/3 = 8.18 s / 3  (current, 3.3 ohm)
const unsigned int  NUM_REPETITIONS   = 4;          // full PRBS periods to output (>= 3 recommended)
const unsigned long START_DELAY_MS    = 95000UL;    // settle time before excitation (> T_2%,max = 92.9 s)
const unsigned long SERIAL_BAUD_RATE  = 115200UL;   // must match the PEMstack acquisition software
const uint8_t       OUTPUT_LEVEL_HIGH = HIGH;       // logic level for PRBS bit "1"  (5 V at the pin)
const uint8_t       OUTPUT_LEVEL_LOW  = LOW;        // logic level for PRBS bit "0"  (0 V at the pin)
const uint32_t      LFSR_SEED         = 0x01UL;     // initial register value (any non-zero value works)

// Documentation constants from the time-response table (not used by the
// algorithm, kept here so the test conditions travel with the sketch):
const float TAU_MIN_S        = 8.18;    // fastest time constant  (current, 3.3 ohm)
const float TAU_MAX_S        = 23.74;   // slowest time constant  (voltage, 3.3 ohm)
const float T_SETTLE_2PCT_S  = 92.9;    // slowest 2% settling time (voltage, 3.3 ohm)
const float EXPECTED_DELTA_I_MA_3R3 = 82.08;  // expected current step, 3.3 ohm load
const float EXPECTED_DELTA_I_MA_6R8 = 52.94;  // expected current step, 6.8 ohm load
const float EXPECTED_DELTA_V_V_3R3  = 1.7275; // expected voltage step, 3.3 ohm load
const float EXPECTED_DELTA_V_V_6R8  = 1.3482; // expected voltage step, 6.8 ohm load

// ---------------------------------------------------------------------------
// COMPILE-TIME SAFETY CHECK — refuse to build outside the supported range
// ---------------------------------------------------------------------------
static_assert(LFSR_ORDER >= 2 && LFSR_ORDER <= 31,
              "LFSR_ORDER must be between 2 and 31 (maximal-length taps table)");

// ---------------------------------------------------------------------------
// MAXIMAL-LENGTH FEEDBACK TAPS (Fibonacci form), orders 2..31
// Source: Xilinx XAPP052 table of maximal-length LFSR polynomials.
// Entry [n] = bit mask of the tapped register bits: tap position p
// contributes bit (p - 1). The XOR (parity) of the masked bits is fed back.
// Index 0 and 1 are unused placeholders (order < 2 is rejected above).
// ---------------------------------------------------------------------------
const uint32_t LFSR_TAP_MASKS[32] = {
  0x00000000UL, 0x00000000UL,             // n = 0, 1  : unused
  0x00000003UL,                           // n = 2  : taps 2,1        -> x^2 + x + 1
  0x00000006UL,                           // n = 3  : taps 3,2
  0x0000000CUL,                           // n = 4  : taps 4,3
  0x00000014UL,                           // n = 5  : taps 5,3
  0x00000030UL,                           // n = 6  : taps 6,5
  0x00000060UL,                           // n = 7  : taps 7,6        -> x^7 + x^6 + 1 (used here)
  0x000000B8UL,                           // n = 8  : taps 8,6,5,4
  0x00000110UL,                           // n = 9  : taps 9,5
  0x00000240UL,                           // n = 10 : taps 10,7
  0x00000500UL,                           // n = 11 : taps 11,9
  0x00000829UL,                           // n = 12 : taps 12,6,4,1
  0x0000100DUL,                           // n = 13 : taps 13,4,3,1
  0x00002015UL,                           // n = 14 : taps 14,5,3,1
  0x00006000UL,                           // n = 15 : taps 15,14
  0x0000D008UL,                           // n = 16 : taps 16,15,13,4
  0x00012000UL,                           // n = 17 : taps 17,14
  0x00020400UL,                           // n = 18 : taps 18,11
  0x00040023UL,                           // n = 19 : taps 19,6,2,1
  0x00090000UL,                           // n = 20 : taps 20,17
  0x00140000UL,                           // n = 21 : taps 21,19
  0x00300000UL,                           // n = 22 : taps 22,21
  0x00420000UL,                           // n = 23 : taps 23,18
  0x00E10000UL,                           // n = 24 : taps 24,23,22,17
  0x01200000UL,                           // n = 25 : taps 25,22
  0x02000023UL,                           // n = 26 : taps 26,6,2,1
  0x04000013UL,                           // n = 27 : taps 27,5,2,1
  0x09000000UL,                           // n = 28 : taps 28,25
  0x14000000UL,                           // n = 29 : taps 29,27
  0x20000029UL,                           // n = 30 : taps 30,6,4,1
  0x48000000UL                            // n = 31 : taps 31,28
};

// ---------------------------------------------------------------------------
// DERIVED CONSTANTS (do not edit — computed from the settings above)
// ---------------------------------------------------------------------------
const uint32_t SEQUENCE_LENGTH   = (1UL << LFSR_ORDER) - 1UL;   // N = 2^n - 1 bits per period
const uint32_t EXPECTED_HIGH_CNT = 1UL << (LFSR_ORDER - 1);     // HIGH bits per period = 2^(n-1)
const uint32_t EXPECTED_LOW_CNT  = EXPECTED_HIGH_CNT - 1UL;     // LOW  bits per period = 2^(n-1) - 1
const uint32_t LFSR_STATE_MASK   = SEQUENCE_LENGTH;             // keeps the register within n bits

// ---------------------------------------------------------------------------
// STATE MACHINE — the loop() is fully non-blocking (no delay() anywhere):
//   STATE_IDLE    : waiting for the start delay to elapse (stack settling)
//   STATE_RUNNING : PRBS bits are clocked out every BIT_PERIOD_MS
//   STATE_DONE    : output LOW, generator stopped (after N reps or "STOP")
// ---------------------------------------------------------------------------
enum GeneratorState { STATE_IDLE, STATE_RUNNING, STATE_DONE };

GeneratorState generatorState = STATE_IDLE;

uint32_t      lfsrRegister    = LFSR_SEED;  // current LFSR register content
uint32_t      bitIndex        = 0;          // bit position inside the current cycle [0 .. N-1]
unsigned int  cycleNumber     = 0;          // completed-or-running PRBS period counter [1 .. NUM_REPETITIONS]
uint32_t      cycleHighCount  = 0;          // HIGH bits emitted in the current cycle (LFSR self-check)
uint32_t      cycleLowCount   = 0;          // LOW  bits emitted in the current cycle
unsigned long lastBitTimeMs   = 0;          // millis() timestamp of the last bit transition
unsigned long bootTimeMs      = 0;          // millis() timestamp when setup() finished

char          serialLineBuffer[16];         // small buffer for incoming serial commands
uint8_t       serialLineLength = 0;

// ---------------------------------------------------------------------------
// advanceLfsr() — clocks the Fibonacci LFSR once and returns the output bit.
// Output bit = MSB of the register (bit n-1). Feedback bit = XOR (parity)
// of all tapped bits, shifted in at the LSB side.
// ---------------------------------------------------------------------------
uint8_t advanceLfsr() {
  uint8_t outputBit = (lfsrRegister >> (LFSR_ORDER - 1)) & 1U;

  // Parity of the tapped bits = feedback bit (XOR of all taps)
  uint32_t tapped = lfsrRegister & LFSR_TAP_MASKS[LFSR_ORDER];
  uint8_t feedbackBit = 0;
  while (tapped) {              // Brian Kernighan parity loop (few taps -> fast)
    feedbackBit ^= 1U;
    tapped &= (tapped - 1UL);
  }

  lfsrRegister = ((lfsrRegister << 1) | feedbackBit) & LFSR_STATE_MASK;
  return outputBit;
}

// ---------------------------------------------------------------------------
// printCycleSummary() — one line per PRBS period so the acquisition software
// (PEMstack) can synchronise. Format:
//   PRBS_CYCLE,<cycle>,<timestamp ms>,<expected HIGH>,<expected LOW>
// ---------------------------------------------------------------------------
void printCycleSummary() {
  Serial.print(F("PRBS_CYCLE,"));
  Serial.print(cycleNumber);
  Serial.print(F(","));
  Serial.print(millis());
  Serial.print(F(","));
  Serial.print(EXPECTED_HIGH_CNT);
  Serial.print(F(","));
  Serial.println(EXPECTED_LOW_CNT);
}

// ---------------------------------------------------------------------------
// printCycleEndSummary() — measured bit counts of the cycle that just
// finished. For a healthy maximal-length LFSR these always equal the
// expected counts (2^(n-1) HIGH, 2^(n-1)-1 LOW) — a runtime self-check.
//   PRBS_CYCLE_END,<cycle>,<timestamp ms>,<measured HIGH>,<measured LOW>
// ---------------------------------------------------------------------------
void printCycleEndSummary() {
  Serial.print(F("PRBS_CYCLE_END,"));
  Serial.print(cycleNumber);
  Serial.print(F(","));
  Serial.print(millis());
  Serial.print(F(","));
  Serial.print(cycleHighCount);
  Serial.print(F(","));
  Serial.println(cycleLowCount);
}

// ---------------------------------------------------------------------------
// stopGenerator() — safe stop: output pin LOW and final marker printed.
// Called after NUM_REPETITIONS periods or on the serial command "STOP".
// ---------------------------------------------------------------------------
void stopGenerator() {
  digitalWrite(PRBS_OUTPUT_PIN, OUTPUT_LEVEL_LOW);
  generatorState = STATE_DONE;
  Serial.println(F("PRBS_DONE"));
}

// ---------------------------------------------------------------------------
// handleSerialCommands() — non-blocking line reader. Recognised commands:
//   "STOP"  -> abort immediately (output LOW, PRBS_DONE)
//   "START" -> skip the remaining start delay and begin the PRBS now
// ---------------------------------------------------------------------------
void handleSerialCommands() {
  while (Serial.available() > 0) {
    char incoming = (char)Serial.read();

    if (incoming == '\n' || incoming == '\r') {
      serialLineBuffer[serialLineLength] = '\0';
      if (serialLineLength > 0) {
        if (strcmp(serialLineBuffer, "STOP") == 0 && generatorState != STATE_DONE) {
          stopGenerator();
        } else if (strcmp(serialLineBuffer, "START") == 0 && generatorState == STATE_IDLE) {
          startPrbs();                       // skip the remaining settle delay
        }
      }
      serialLineLength = 0;
    } else if (serialLineLength < sizeof(serialLineBuffer) - 1) {
      serialLineBuffer[serialLineLength++] = incoming;
    } else {
      serialLineLength = 0;                  // overlong line -> discard safely
    }
  }
}

// ---------------------------------------------------------------------------
// startPrbs() — transition IDLE -> RUNNING: first cycle, first bit.
// ---------------------------------------------------------------------------
void startPrbs() {
  generatorState  = STATE_RUNNING;
  cycleNumber     = 1;
  bitIndex        = 0;
  cycleHighCount  = 0;
  cycleLowCount   = 0;
  lfsrRegister    = LFSR_SEED;
  printCycleSummary();

  uint8_t firstBit = advanceLfsr();
  digitalWrite(PRBS_OUTPUT_PIN, firstBit ? OUTPUT_LEVEL_HIGH : OUTPUT_LEVEL_LOW);
  if (firstBit) { cycleHighCount++; } else { cycleLowCount++; }
  bitIndex      = 1;
  lastBitTimeMs = millis();
}

// ---------------------------------------------------------------------------
// setup() — pin + serial initialisation and a one-line config summary so the
// acquisition log records exactly which PRBS settings were used.
// ---------------------------------------------------------------------------
void setup() {
  pinMode(PRBS_OUTPUT_PIN, OUTPUT);
  digitalWrite(PRBS_OUTPUT_PIN, OUTPUT_LEVEL_LOW);   // start safe: pin LOW

  Serial.begin(SERIAL_BAUD_RATE);

  // Config summary: order, N, bit period, repetitions, start delay, pin
  Serial.print(F("PRBS_CONFIG,order="));
  Serial.print(LFSR_ORDER);
  Serial.print(F(",N="));
  Serial.print(SEQUENCE_LENGTH);
  Serial.print(F(",Tbit_ms="));
  Serial.print(BIT_PERIOD_MS);
  Serial.print(F(",reps="));
  Serial.print(NUM_REPETITIONS);
  Serial.print(F(",delay_ms="));
  Serial.print(START_DELAY_MS);
  Serial.print(F(",pin="));
  Serial.println(PRBS_OUTPUT_PIN);

  bootTimeMs    = millis();
  lastBitTimeMs = bootTimeMs;
}

// ---------------------------------------------------------------------------
// loop() — non-blocking state machine clocked by millis() comparisons.
// The subtraction idiom (now - last >= interval) is overflow-safe.
// ---------------------------------------------------------------------------
void loop() {
  handleSerialCommands();

  unsigned long nowMs = millis();

  switch (generatorState) {

    case STATE_IDLE:
      // Wait for the stack to settle at its operating point before exciting
      // it (START_DELAY_MS > slowest 2% settling time from the table).
      if (nowMs - bootTimeMs >= START_DELAY_MS) {
        startPrbs();
      }
      break;

    case STATE_RUNNING:
      // Time to clock out the next PRBS bit?
      if (nowMs - lastBitTimeMs >= BIT_PERIOD_MS) {
        lastBitTimeMs += BIT_PERIOD_MS;      // drift-free: schedule from the previous edge

        if (bitIndex >= SEQUENCE_LENGTH) {
          // One full PRBS period completed -> next cycle or finished
          printCycleEndSummary();
          if (cycleNumber >= NUM_REPETITIONS) {
            stopGenerator();
            break;
          }
          cycleNumber++;
          bitIndex       = 0;
          cycleHighCount = 0;
          cycleLowCount  = 0;
          printCycleSummary();
        }

        uint8_t prbsBit = advanceLfsr();
        digitalWrite(PRBS_OUTPUT_PIN, prbsBit ? OUTPUT_LEVEL_HIGH : OUTPUT_LEVEL_LOW);
        if (prbsBit) { cycleHighCount++; } else { cycleLowCount++; }
        bitIndex++;
      }
      break;

    case STATE_DONE:
      // Nothing to do — output stays LOW. A reset restarts the experiment.
      break;
  }
}
