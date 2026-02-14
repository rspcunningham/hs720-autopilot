#!/usr/bin/env python3
"""Probe nRF24L01 over SPI to verify wiring."""

import spidev
import RPi.GPIO as GPIO
import time

CE_PIN = 25  # GPIO 25, physical pin 22

# nRF24L01 registers
REG_CONFIG = 0x00
REG_EN_AA = 0x01
REG_EN_RXADDR = 0x02
REG_SETUP_AW = 0x03
REG_SETUP_RETR = 0x04
REG_RF_CH = 0x05
REG_RF_SETUP = 0x06
REG_STATUS = 0x07
REG_TX_ADDR = 0x10
REG_RX_ADDR_P0 = 0x0A
REG_FIFO_STATUS = 0x17

# Commands
CMD_R_REGISTER = 0x00
CMD_NOP = 0xFF


def read_register(spi, reg, length=1):
    """Read nRF24L01 register. Returns list of bytes."""
    tx = [CMD_R_REGISTER | reg] + [CMD_NOP] * length
    rx = spi.xfer2(tx)
    status = rx[0]
    return status, rx[1:]


def read_address(spi, reg, width=5):
    """Read a multi-byte address register."""
    return read_register(spi, reg, width)


def main():
    # Setup CE pin (active low = standby)
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(CE_PIN, GPIO.OUT, initial=GPIO.LOW)

    # Open SPI0, CE0
    spi = spidev.SpiDev()
    spi.open(0, 0)
    spi.max_speed_hz = 1_000_000  # 1 MHz (nRF24 supports up to 10)
    spi.mode = 0  # CPOL=0, CPHA=0
    spi.bits_per_word = 8

    print("nRF24L01 SPI Probe")
    print("=" * 40)

    # Read STATUS via NOP
    rx = spi.xfer2([CMD_NOP])
    status = rx[0]
    print(f"STATUS:      0x{status:02X}")
    if status == 0x00 or status == 0xFF:
        print("  *** BAD — chip not responding (check wiring/power)")
        return
    print(f"  RX_DR={bool(status & 0x40)}, TX_DS={bool(status & 0x20)}, "
          f"MAX_RT={bool(status & 0x10)}")
    print(f"  RX_P_NO={((status >> 1) & 0x07)}, TX_FULL={bool(status & 0x01)}")

    # Read key registers
    regs = {
        "CONFIG": REG_CONFIG,
        "EN_AA": REG_EN_AA,
        "EN_RXADDR": REG_EN_RXADDR,
        "SETUP_AW": REG_SETUP_AW,
        "SETUP_RETR": REG_SETUP_RETR,
        "RF_CH": REG_RF_CH,
        "RF_SETUP": REG_RF_SETUP,
        "FIFO_STATUS": REG_FIFO_STATUS,
    }

    print()
    for name, reg in regs.items():
        _, val = read_register(spi, reg)
        v = val[0]
        extra = ""
        if name == "CONFIG":
            extra = (f"  PWR_UP={bool(v & 0x02)}, PRIM_RX={bool(v & 0x01)}, "
                     f"CRC={'2byte' if v & 0x04 else '1byte' if v & 0x08 else 'off'}")
        elif name == "RF_CH":
            freq = 2400 + v
            extra = f"  freq={freq} MHz"
        elif name == "RF_SETUP":
            rate = {0x00: "1 Mbps", 0x08: "2 Mbps", 0x20: "250 kbps"}.get(v & 0x28, "?")
            pa = {0x00: "-18 dBm", 0x02: "-12 dBm", 0x04: "-6 dBm", 0x06: "0 dBm"}.get(v & 0x06, "?")
            extra = f"  rate={rate}, PA={pa}"
        elif name == "SETUP_AW":
            aw = {0x01: "3 bytes", 0x02: "4 bytes", 0x03: "5 bytes"}.get(v & 0x03, "illegal")
            extra = f"  addr_width={aw}"
        print(f"{name:15s} 0x{v:02X}{extra}")

    # Read addresses
    print()
    _, tx_addr = read_address(spi, REG_TX_ADDR)
    _, rx_addr = read_address(spi, REG_RX_ADDR_P0)
    print(f"TX_ADDR:     {bytes(tx_addr).hex(':')}")
    print(f"RX_ADDR_P0:  {bytes(rx_addr).hex(':')}")

    print()
    print("Chip is alive and responding!")

    spi.close()
    GPIO.cleanup()


if __name__ == "__main__":
    main()
