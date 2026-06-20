import machine
import time

# Pin Definitions (Waveshare ESP32-S3-PhotoPainter)
EPD_DC_PIN   = 8
EPD_CS_PIN   = 9
EPD_SCK_PIN  = 10
EPD_MOSI_PIN = 11
EPD_RST_PIN  = 12
EPD_BUSY_PIN = 13 # Active Low (Low = Busy, High = Ready/Idle)

class EPD_7in3f:
    def __init__(self):
        # Configure control pins
        self.dc = machine.Pin(EPD_DC_PIN, machine.Pin.OUT)
        self.cs = machine.Pin(EPD_CS_PIN, machine.Pin.OUT)
        self.rst = machine.Pin(EPD_RST_PIN, machine.Pin.OUT)
        self.busy = machine.Pin(EPD_BUSY_PIN, machine.Pin.IN, machine.Pin.PULL_UP)
        
        # Configure hardware SPI at 10 MHz (Polarity = 0, Phase = 0)
        self.spi = machine.SPI(1, baudrate=10000000, polarity=0, phase=0, 
                               sck=machine.Pin(EPD_SCK_PIN), mosi=machine.Pin(EPD_MOSI_PIN))
        
        self.cs.value(1)
        self.dc.value(0)
        self.rst.value(1)

    def reset(self):
        self.rst.value(1)
        time.sleep_ms(50)
        self.rst.value(0)
        time.sleep_ms(20)
        self.rst.value(1)
        time.sleep_ms(50)

    def read_busy(self, timeout_ms=15000):
        """Busy line is active Low; wait until it goes High, with timeout_ms timeout."""
        start = time.ticks_ms()
        while self.busy.value() == 0:
            time.sleep_ms(10)
            if time.ticks_diff(time.ticks_ms(), start) > timeout_ms:
                print("E-Paper busy wait timeout ({}s)!".format(timeout_ms // 1000))
                break

    def send_command(self, cmd):
        self.dc.value(0)
        self.cs.value(0)
        self.spi.write(bytes([cmd]))
        self.cs.value(1)

    def send_data(self, data):
        self.dc.value(1)
        self.cs.value(0)
        if isinstance(data, int):
            self.spi.write(bytes([data]))
        else:
            self.spi.write(data)
        self.cs.value(1)

    def init(self):
        self.reset()
        self.read_busy()
        time.sleep_ms(50)

        # 7.3-inch 6-Color initialization commands from register specs
        self.send_command(0xAA) # CMDH
        self.send_data(bytes([0x49, 0x55, 0x20, 0x08, 0x09, 0x18]))

        self.send_command(0x01)
        self.send_data(0x3F)

        self.send_command(0x00)
        self.send_data(bytes([0x5F, 0x69]))

        self.send_command(0x03)
        self.send_data(bytes([0x00, 0x54, 0x00, 0x44]))

        self.send_command(0x05)
        self.send_data(bytes([0x40, 0x1F, 0x1F, 0x2C]))

        self.send_command(0x06)
        self.send_data(bytes([0x6F, 0x1F, 0x17, 0x49]))

        self.send_command(0x08)
        self.send_data(bytes([0x6F, 0x1F, 0x1F, 0x22]))

        self.send_command(0x30)
        self.send_data(0x03)

        self.send_command(0x50)
        self.send_data(0x3F)

        self.send_command(0x60)
        self.send_data(bytes([0x02, 0x00]))

        self.send_command(0x61)
        self.send_data(bytes([0x03, 0x20, 0x01, 0xE0]))

        self.send_command(0x84)
        self.send_data(0x01)

        self.send_command(0xE3)
        self.send_data(0x2F)

        self.send_command(0x04) # PWR ON
        self.read_busy()

    def turn_on_display(self):
        self.send_command(0x04) # POWER_ON
        self.read_busy()

        self.send_command(0x06)
        self.send_data(bytes([0x6F, 0x1F, 0x17, 0x49]))

        self.send_command(0x12) # DISPLAY_REFRESH
        self.send_data(0x00)
        self.read_busy(30000)

        self.send_command(0x02) # POWER_OFF
        self.send_data(0x00)
        self.read_busy()

    def display_file(self, filepath):
        """
        Streams 192,000 bytes 4bpp RAW display bitstream file directly 
        from the SD card to the panel over SPI in 4KB chunks.
        Keeps CS low during the entire transmission to match physical timing.
        """
        self.init()
        self.send_command(0x10) # Write RAM command
        
        self.dc.value(1)
        self.cs.value(0)
        
        chunk = bytearray(4096)
        with open(filepath, 'rb') as f:
            while True:
                n = f.readinto(chunk)
                if not n:
                    break
                if n == len(chunk):
                    self.spi.write(chunk)
                else:
                    self.spi.write(memoryview(chunk)[:n])
                    
        self.cs.value(1)
        self.turn_on_display()
        
        # Deep sleep command to display controller
        self.send_command(0x07) # Deep Sleep command
        self.send_data(0xA5)
