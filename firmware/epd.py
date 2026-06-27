# ==========================================
# FILE VERSION: 1.0.0
# DESCRIPTION: Hardware driver for the Waveshare Spectra 6 7.3-inch e-Paper display.
# ==========================================
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
    def read_busy(self, timeout_ms=45000):
        """Busy line is active Low; wait until it goes High, with timeout_ms timeout."""
        # Mandatory delay to allow display controller to process command and pull busy low
        time.sleep_ms(200)
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

    def draw_battery_pixel(self, x, y, percent, original_color):
        # Margin box to clear background: x from 670 to 790, y from 420 to 475
        if not (420 <= y <= 475 and 670 <= x <= 790):
            return original_color
            
        # Inside the margin but outside the battery body/tip
        is_body = (430 <= y <= 466 and 680 <= x <= 770)
        is_tip = (442 <= y <= 454 and 770 <= x <= 775)
        
        if not (is_body or is_tip):
            return 1  # white margin
            
        # Draw outer outline
        if is_body:
            if y == 430 or y == 466 or x == 680:
                return 0  # black border
            if x == 770 and (y < 442 or y > 454):
                return 0  # black border
                
        if is_tip:
            if y == 442 or y == 454 or x == 775:
                return 0  # black border
                
        # Inside the battery tip (nub) - make it white
        if is_tip:
            return 1
            
        # Inside the battery body (431 <= y <= 465, 681 <= x <= 769)
        # Fallback if percent is None
        val_percent = percent if percent is not None else 50
        
        # Determine segment color
        if val_percent >= 40:
            color = 6  # Green
            num_bars = 5 if val_percent >= 80 else (4 if val_percent >= 60 else 3)
        elif val_percent >= 20:
            color = 2  # Yellow
            num_bars = 2
        else:
            color = 3  # Red
            num_bars = 1
            
        # Define segments
        if 434 <= y <= 462:
            if 684 <= x <= 697 and num_bars >= 1: return color
            if 701 <= x <= 714 and num_bars >= 2: return color
            if 718 <= x <= 731 and num_bars >= 3: return color
            if 735 <= x <= 748 and num_bars >= 4: return color
            if 752 <= x <= 765 and num_bars >= 5: return color
            
        return 1  # White background inside battery

    def display_file(self, filepath, battery_level=None, orientation=None):
        """
        Streams 192,000 bytes 4bpp RAW display bitstream file directly 
        from the file to the panel over SPI.
        Optional orientation override skips config-file read (used for setup screens
        to avoid double-rotating pre-rotated portrait/landscape buffers).
        """
        self.init()
        self.send_command(0x10) # Write RAM command
        
        self.dc.value(1)
        self.cs.value(0)
        
        # Load orientation to check for 180-degree rotation
        if orientation is None:
            orientation = 'landscape'
            for path in ['/sd/wifi_config.json', '/wifi_config.json']:
                try:
                    import json
                    with open(path, 'r') as f:
                        cfg = json.load(f)
                        orientation = cfg.get('orientation', 'landscape')
                        break
                except Exception:
                    pass
        rotate_180 = 'upside-down' not in orientation
        is_warning = "no_images" in filepath or "warning" in filepath
        should_overlay = not is_warning
        
        if rotate_180:
            chunk_size = 4000
            num_chunks = 48
            chunk = bytearray(chunk_size)
            
            with open(filepath, 'rb') as f:
                for chunk_idx in range(num_chunks - 1, -1, -1):
                    f.seek(chunk_idx * chunk_size)
                    f.readinto(chunk)
                    
                    if should_overlay:
                        y_start = chunk_idx * 10
                        y_end = y_start + 10
                        if 420 <= y_end and y_start <= 475:
                            for i in range(chunk_size):
                                curr_byte_pos = chunk_idx * chunk_size + i
                                y = curr_byte_pos // 400
                                if 420 <= y <= 475:
                                    x_byte = curr_byte_pos % 400
                                    x_even = x_byte * 2
                                    x_odd = x_even + 1
                                    
                                    if 670 <= x_even <= 790 or 670 <= x_odd <= 790:
                                        b = chunk[i]
                                        col_even = (b >> 4) & 0x0F
                                        col_odd = b & 0x0F
                                        
                                        new_even = self.draw_battery_pixel(x_even, y, battery_level, col_even)
                                        new_odd = self.draw_battery_pixel(x_odd, y, battery_level, col_odd)
                                        
                                        chunk[i] = (new_even << 4) | new_odd
                                        
                    for j in range(chunk_size // 2):
                        b1 = chunk[j]
                        b2 = chunk[chunk_size - 1 - j]
                        
                        b1_rot = ((b1 & 0x0F) << 4) | ((b1 >> 4) & 0x0F)
                        b2_rot = ((b2 & 0x0F) << 4) | ((b2 >> 4) & 0x0F)
                        
                        chunk[j] = b2_rot
                        chunk[chunk_size - 1 - j] = b1_rot
                        
                    self.spi.write(chunk)
        else:
            chunk = bytearray(4096)
            byte_index = 0
            with open(filepath, 'rb') as f:
                while True:
                    n = f.readinto(chunk)
                    if not n:
                        break
                        
                    if should_overlay:
                        for i in range(n):
                            curr_byte_pos = byte_index + i
                            y = curr_byte_pos // 400
                            
                            if 420 <= y <= 475:
                                x_byte = curr_byte_pos % 400
                                x_even = x_byte * 2
                                x_odd = x_even + 1
                                
                                if 670 <= x_even <= 790 or 670 <= x_odd <= 790:
                                    b = chunk[i]
                                    col_even = (b >> 4) & 0x0F
                                    col_odd = b & 0x0F
                                    
                                    new_even = self.draw_battery_pixel(x_even, y, battery_level, col_even)
                                    new_odd = self.draw_battery_pixel(x_odd, y, battery_level, col_odd)
                                    
                                    chunk[i] = (new_even << 4) | new_odd
                                    
                    if n == len(chunk):
                        self.spi.write(chunk)
                    else:
                        self.spi.write(memoryview(chunk)[:n])
                        
                    byte_index += n
                    
        self.cs.value(1)
        self.turn_on_display()
        
        # Deep sleep command to display controller
        self.send_command(0x07) # Deep Sleep command
        self.send_data(0xA5)
