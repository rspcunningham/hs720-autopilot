#!/usr/bin/env python3
"""Loop SPI probe — wiggle wires while this runs."""

import RPi.GPIO as GPIO
import time

CSN = 8
SCK = 11
MOSI = 10
MISO = 9

GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)
GPIO.setup(SCK, GPIO.OUT, initial=GPIO.LOW)
GPIO.setup(MOSI, GPIO.OUT, initial=GPIO.LOW)
GPIO.setup(MISO, GPIO.IN)
# CSN tied to GND by user, but set GPIO 8 as input so it's not fighting
GPIO.setup(CSN, GPIO.IN)

def bb_byte(tx):
    rx = 0
    for bit in range(7, -1, -1):
        GPIO.output(MOSI, (tx >> bit) & 1)
        GPIO.output(SCK, GPIO.HIGH)
        time.sleep(0.00005)
        rx = (rx << 1) | GPIO.input(MISO)
        GPIO.output(SCK, GPIO.LOW)
        time.sleep(0.00005)
    return rx

print("Looping SPI reads — wiggle SCK/MOSI/MISO wires...")
print("Press Ctrl+C to stop")
last = None
try:
    while True:
        status = bb_byte(0xFF)
        if status != last:
            print(f"  STATUS = 0x{status:02X}  {'<-- ALIVE!' if status not in (0x00, 0xFF) else ''}")
            last = status
        time.sleep(0.1)
except KeyboardInterrupt:
    pass

GPIO.cleanup()
