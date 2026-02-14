#!/usr/bin/env python3
"""Diagnose nRF24L01 SPI connectivity."""

import RPi.GPIO as GPIO
import spidev
import time

GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)

# Try manual CSN toggle (bypass hardware CS)
spi = spidev.SpiDev()
spi.open(0, 0)
spi.max_speed_hz = 500_000
spi.mode = 0

# Normal read
rx = spi.xfer2([0xFF])
print(f"Normal SPI read:    STATUS=0x{rx[0]:02X}")

# Try with manual CS
spi.no_cs = True
GPIO.setup(8, GPIO.OUT)
GPIO.output(8, GPIO.HIGH)
time.sleep(0.001)
GPIO.output(8, GPIO.LOW)
time.sleep(0.001)
rx = spi.xfer2([0xFF])
GPIO.output(8, GPIO.HIGH)
print(f"Manual CS toggle:   STATUS=0x{rx[0]:02X}")

spi.close()

# Read MISO pin directly
GPIO.setup(9, GPIO.IN)
miso = GPIO.input(9)
print(f"MISO (GPIO 9) raw:  {miso}")
print()

if miso == 0:
    print("MISO stuck LOW — chip is not driving the line.")
    print("Likely causes:")
    print("  1. No common ground between external PSU and Pi")
    print("  2. MOSI/MISO swapped")
    print("  3. Module is dead")
    print("  4. VCC not reaching the chip")
else:
    print("MISO is HIGH — chip may be responding but CS/CLK issue")

GPIO.cleanup()
