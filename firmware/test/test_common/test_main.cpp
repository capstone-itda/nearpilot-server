// One Unity program for every Arduino-free module, so the ESP32 run needs a single flash.
#include <unity.h>

void runConfigTests();
void runClockTests();
void runCommandTests();
void runLedgerTests();
void runExecutorTests();
void runBeaconTests();
void runReadyTests();

void setUp() {}
void tearDown() {}

int runAll() {
    UNITY_BEGIN();
    runConfigTests();
    runClockTests();
    runCommandTests();
    runLedgerTests();
    runExecutorTests();
    runBeaconTests();
    runReadyTests();
    return UNITY_END();
}

#ifdef ARDUINO
#include <Arduino.h>
void setup() {
    delay(2000);  // Let the test runner open the serial port after reset.
    runAll();
}
void loop() {}
#else
int main() { return runAll(); }
#endif
