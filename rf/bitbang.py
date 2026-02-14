#!/usr/bin/env python3
"""Bit-bang SPI to nRF24L01 — bypasses SPI driver to test raw wiring."""

import RPi.GPIO as GPIO
import time

# Pin assignments (BCM)
CSN = 8
SCK = 11
MOSI = 10
MISO = 9
CE = 25

GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)
GPIO.setup(CSN, GPIO.OUT, initial=GPIO.HIGH)
GPIO.setup(SCK, GPIO.OUT, initial=GPIO.LOW)
GPIO.setup(MOSI, GPIO.OUT, initial=GPIO.LOW)
GPIO.setup(CE, GPIO.OUT, initial=GPIO.LOW)
GPIO.setup(MISO, GPIO.IN)

def bb_transfer(tx_byte):
    """Bit-bang one byte over SPI, return received byte."""
    rx = 0
    for bit in range(7, -1, -1):
        # Set MOSI
        GPIO.output(MOSI, (tx_byte >> bit) & 1)
        time.sleep(0.0001)
        # Clock high — nRF latches MOSI, drives MISO
        GPIO.output(SCK, GPIO.HIGH)
        time.sleep(0.0001)
        # Read MISO
        rx = (rx << 1) | GPIO.input(MISO)
        # Clock low
        GPIO.output(SCK, GPIO.LOW)
        time.sleep(0.0001)
    return rx

print("nRF24L01 Bit-Bang SPI Test")
print("=" * 40)

# First just check MISO level with CSN high (deselected)
miso_idle = GPIO.input(MISO)
print(f"MISO with CSN HIGH (deselected): {miso_idle}")

# Select chip
GPIO.output(CSN, GPIO.LOW)
time.sleep(0.001)

miso_selected = GPIO.input(MISO)
print(f"MISO with CSN LOW  (selected):   {miso_selected}")

# Send NOP (0xFF) to read STATUS
status = bb_transfer(0xFF)
print(f"STATUS register: 0x{status:02X}")

# Read CONFIG register (cmd 0x00, then read one byte)
GPIO.output(CSN, GPIO.HIGH)
time.sleep(0.001)
GPIO.output(CSN, GPIO.LOW)
time.sleep(0.001)
status2 = bb_transfer(0x00)  # R_REGISTER | 0x00
config = bb_transfer(0xFF)   # clock out the data
print(f"STATUS (again): 0x{status2:02X}")
print(f"CONFIG:         0x{config:02X}")

GPIO.output(CSN, GPIO.HIGH)

# Toggle test — verify SCK actually reaches the chip
# by toggling and seeing if MISO changes at all
print()
print("Wire continuity test:")
GPIO.output(CSN, GPIO.LOW)
time.sleep(0.001)
readings = set()
for i in range(16):
    GPIO.output(MOSI, i & 1)
    GPIO.output(SCK, GPIO.HIGH)
    time.sleep(0.0001)
    readings.add(GPIO.input(MISO))
    GPIO.output(SCK, GPIO.LOW)
    time.sleep(0.0001)
GPIO.output(CSN, GPIO.HIGH)

if len(readings) > 1:
    print("  MISO toggled — chip IS responding!")
else:
    val = readings.pop()
    print(f"  MISO stuck at {val} — no response from chip")
    print()
    print("Check pin 1 orientation on the nRF module!")
    print("The square pad / dot marks pin 1 (GND).")
    print("If the header is rotated 180, all pins are wrong.")

GPIO.cleanup()
