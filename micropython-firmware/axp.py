import machine
import time

class AXP2101:
    def __init__(self, sda=47, scl=48, addr=0x34):
        self.i2c = machine.SoftI2C(sda=machine.Pin(sda), scl=machine.Pin(scl))
        self.addr = addr

    def write_reg(self, reg, val):
        self.i2c.writeto_mem(self.addr, reg, bytes([val]))

    def read_reg(self, reg):
        return self.i2c.readfrom_mem(self.addr, reg, 1)[0]

    def set_bit(self, reg, bit):
        val = self.read_reg(reg)
        self.write_reg(reg, val | (1 << bit))

    def clr_bit(self, reg, bit):
        val = self.read_reg(reg)
        self.write_reg(reg, val & ~(1 << bit))

    def init(self):
        # 1. Set DCDC1 to 3.3V & Enable
        # Reg 0x82: steps=100mV, min=1500mV. (3300-1500)//100 = 18 = 0x12
        self.write_reg(0x82, 0x12)
        self.set_bit(0x80, 0) # Enable DC1

        # 2. Set ALDO3 to 3.3V & Enable
        # Reg 0x94: steps=100mV, min=500mV. (3300-500)//100 = 28 = 0x1C
        val3 = self.read_reg(0x94) & 0xE0
        self.write_reg(0x94, val3 | 0x1C)
        self.set_bit(0x90, 2) # Enable ALDO3 (EPD_VCC)

        # 3. Set ALDO4 to 3.3V & Enable
        # Reg 0x95: steps=100mV, min=500mV. (3300-500)//100 = 28 = 0x1C
        val4 = self.read_reg(0x95) & 0xE0
        self.write_reg(0x95, val4 | 0x1C)
        self.set_bit(0x90, 3) # Enable ALDO4

        # 4. Enable ALDO2 (Audio VCC)
        self.set_bit(0x90, 1)

    def disable_power(self):
        # Disable all rails except DC1 (which powers the ESP32) to conserve energy
        val = self.read_reg(0x90)
        self.write_reg(0x90, val & ~0x0E) # Clear bits 1 (ALDO2), 2 (ALDO3), 3 (ALDO4)
