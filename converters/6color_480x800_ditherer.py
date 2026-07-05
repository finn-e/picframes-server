#!/usr/bin/env python3
# ==========================================================================================
# DESCRIPTION: Floyd-Steinberg ditherer for portrait images (480x800). Dithers to 6-color Spectra 6 palette and packs as 4bpp RAW binary.
# DEPENDENCIES: numpy, PIL (Pillow)
# ==========================================================================================
import sys
import os
import numpy as np
from PIL import Image, ImageOps
# E6 Spectra 7.3" 6-color WRGBYK palette
PALETTE = np.array([
    [0,   0,   0  ], # 0: Black (K)
    [255, 255, 255], # 1: White (W)
    [0,   255, 0  ], # 2: Green (G)
    [0,   0,   255], # 3: Blue (B)
    [255, 0,   0  ], # 4: Red (R)
    [255, 255, 0  ], # 5: Yellow (Y)
], dtype=np.float32)

# Hardware register color mappings for EPD_7in3f (Spectra 6):
# 0: Black, 1: White, 2: Yellow, 3: Red, 5: Blue, 6: Green
HARDWARE_MAP = np.array([0, 1, 6, 5, 3, 2], dtype=np.uint8)


def dither_floyd_steinberg(img_array, palette):
    h, w, _ = img_array.shape
    padded = np.pad(img_array, ((0, 1), (1, 1), (0, 0)), mode='edge').astype(np.float32)
    for y in range(h):
        for x in range(1, w + 1):
            old_val = padded[y, x].copy()
            
            # Prevent color error bleeding into pure/clean black or white pixels
            l1_dist_black = np.sum(np.abs(old_val - [0, 0, 0]))
            l1_dist_white = np.sum(np.abs(old_val - [255, 255, 255]))
            
            if l1_dist_black < 15:
                padded[y, x] = [0, 0, 0]
                continue
            elif l1_dist_white < 15:
                padded[y, x] = [255, 255, 255]
                continue
                
            diff = palette - old_val
            dist = np.sum(diff ** 2, axis=1)
            idx = np.argmin(dist)
            new_val = palette[idx]
            padded[y, x] = new_val
            err = old_val - new_val
            padded[y,     x + 1] += err * (7.0 / 16.0)
            padded[y + 1, x - 1] += err * (3.0 / 16.0)
            padded[y + 1, x    ] += err * (5.0 / 16.0)
            padded[y + 1, x + 1] += err * (1.0 / 16.0)
    return padded[0:h, 1:w+1].astype(np.uint8)


def rgb_array_to_spectra6_bitstream(img_array):
    h, w, _ = img_array.shape
    pixels = img_array.reshape(-1, 3)
    dists = np.sum((pixels[:, None, :] - PALETTE[None, :, :])**2, axis=2)
    palette_indices = np.argmin(dists, axis=1)
    hw_indices = HARDWARE_MAP[palette_indices]
    hw_indices = hw_indices.reshape(h, w)
    packed = (hw_indices[:, 0::2] << 4) | hw_indices[:, 1::2]
    return packed.tobytes()

def process_image(src_path, dst_path):
    print(f"Opening image: {src_path}")
    img = Image.open(src_path)
    img = ImageOps.exif_transpose(img).convert('RGB')
    w, h = img.size
    
    # Target ratio is 3:5 (0.6)
    target_w, target_h = 480, 800
    target_ratio = target_w / target_h
    
    current_ratio = w / h
    if current_ratio > target_ratio:
        # Image is wider than 3:5 -> crop sides
        crop_h = h
        crop_w = int(h * target_ratio)
        x_offset = (w - crop_w) // 2
        y_offset = 0
    else:
        # Image is taller than 3:5 -> crop top/bottom
        crop_w = w
        crop_h = int(w / target_ratio)
        x_offset = 0
        y_offset = (h - crop_h) // 2
        
    print(f"Center cropping from {w}x{h} to {crop_w}x{crop_h} (offset: {x_offset}, {y_offset})")
    cropped = img.crop((x_offset, y_offset, x_offset + crop_w, y_offset + crop_h))
    print(f"Resizing to {target_w}x{target_h}")
    resized = cropped.resize((target_w, target_h), Image.Resampling.NEAREST)

    print("Rotating 90 degrees CW for portrait alignment...")
    rotated = resized.rotate(270, expand=True)

    print("Applying Floyd-Steinberg dithering to 7-color palette...")
    img_array = np.array(rotated, dtype=np.float32)
    dithered_rgb = dither_floyd_steinberg(img_array, PALETTE)

    print("Converting to packed 4bpp bitstream...")
    bitstream = rgb_array_to_spectra6_bitstream(dithered_rgb)

    print(f"Saving to: {dst_path}")
    with open(dst_path, 'wb') as f:
        f.write(bitstream)
    print("Done!")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 6color_480x800_ditherer.py <input_image> [output_bin]")
        sys.exit(1)
        
    src_file = sys.argv[1]
    if len(sys.argv) >= 3:
        dst_file = sys.argv[2]
    else:
        base, _ = os.path.splitext(src_file)
        dst_file = base + ".bin"
        
    process_image(src_file, dst_file)
