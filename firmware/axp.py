# ==========================================
# FILE VERSION: 1.0.0
# DESCRIPTION: AXP2101 Power Management IC driver.
# ==========================================
import machine
import time

_shared_i2c = None
class AXP2101:
    def __init__(self, sda=47, scl=48, addr=0x34):
        global _shared_i2c
        if _shared_i2c is None:
            try:
                _shared_i2c = machine.SoftI2C(sda=machine.Pin(sda), scl=machine.Pin(scl), freq=100000)
            except Exception as e:
                print("AXP2101 SoftI2C init failed:", e)
        self.i2c = _shared_i2c
        self.addr = addr

    def write_reg(self, reg, val):
        if self.i2c is None:
            raise OSError("I2C not initialized")
        last_err = None
        for attempt in range(3):
            try:
                self.i2c.writeto_mem(self.addr, reg, bytes([val]))
                return
            except Exception as e:
                last_err = e
                time.sleep_ms(10)
        raise OSError("AXP2101 write_reg failed: " + str(last_err))

    def read_reg(self, reg):
        if self.i2c is None:
            raise OSError("I2C not initialized")
        last_err = None
        for attempt in range(3):
            try:
                return self.i2c.readfrom_mem(self.addr, reg, 1)[0]
            except Exception as e:
                last_err = e
                time.sleep_ms(10)
        raise OSError("AXP2101 read_reg failed: " + str(last_err))

    def set_bit(self, reg, bit):
        val = self.read_reg(reg)
        self.write_reg(reg, val | (1 << bit))

    def clr_bit(self, reg, bit):
        val = self.read_reg(reg)
        self.write_reg(reg, val & ~(1 << bit))

    def init(self):
        # 0. Set VBUS input current limit to 2.0A to prevent brownouts under peak load (EPD + SD + RF)
        try:
            val_lim = self.read_reg(0x16) & 0xF8
            self.write_reg(0x16, val_lim | 0x05) # 0x05 = 2.0A limit
            print("AXP2101 VBUS input current limit set to 2.0A")
        except Exception as e:
            print("Failed to set VBUS input current limit:", e)

        # 1. Set DCDC1 to 3.3V & Enable
        # Reg 0x82: steps=100mV, min=1500mV. (3300-1500)//100 = 18 = 0x12
        self.write_reg(0x82, 0x12)
        self.set_bit(0x80, 0) # Enable DC1

        # 2. Set ALDO3 to 3.3V & Enable
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

    def is_usb_connected(self):
        try:
            reg00 = self.read_reg(0x00)
            # Bit 5: VBUS_GOOD, Bit 4: VBUS_PRESENT
            return (reg00 & 0x20) != 0 or (reg00 & 0x10) != 0
        except Exception as e:
            print("AXP2101 is_usb_connected error:", e)
            return True # safe fallback to assume plugged in

    def get_battery_percentage(self):
        try:
            val = self.read_reg(0xA4)
            if val > 100:
                if self.is_usb_connected():
                    return 100
                return 0
            return val
        except Exception as e:
            print("AXP2101 read battery percentage error:", e)
            return 100

    def power_off(self):
        try:
            self.write_reg(0x10, 0x01)
        except Exception as e:
            print("AXP2101 power_off failed:", e)

