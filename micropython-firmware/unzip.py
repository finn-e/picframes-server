import os
import deflate
import io

def extract_zip(zip_filepath, dest_dir):
    """
    A lightweight ZIP file extractor for MicroPython using uzlib.
    """
    print("Opening ZIP file:", zip_filepath)
    with open(zip_filepath, 'rb') as f:
        while True:
            sig = f.read(4)
            if not sig:
                break
            if sig != b'PK\x03\x04':
                # Reached end of local files or central directory
                break
            
            # Local file header fields (refer to ZIP format spec)
            f.read(4) # skip version and flags
            comp_method = int.from_bytes(f.read(2), 'little')
            f.read(8) # skip modification time, date, and CRC-32
            comp_size = int.from_bytes(f.read(4), 'little')
            uncomp_size = int.from_bytes(f.read(4), 'little')
            filename_len = int.from_bytes(f.read(2), 'little')
            extra_len = int.from_bytes(f.read(2), 'little')
            
            filename = f.read(filename_len).decode('utf-8')
            f.read(extra_len) # skip extra field
            
            out_path = dest_dir + '/' + filename
            
            # Read the compressed (or raw) data payload
            data = f.read(comp_size)
            
            # Create subdirectories if they don't exist
            parts = out_path.split('/')
            for i in range(1, len(parts)):
                dir_path = '/'.join(parts[:i])
                if dir_path:
                    try:
                        os.mkdir(dir_path)
                    except OSError:
                        pass # Directory already exists
            
            # If not a directory entry, decompress and write to file
            if not filename.endswith('/'):
                if comp_method == 8: # Deflated
                    # wbits = 15 (32KB window size) for raw deflate stream to support max compression level
                    decompressed = deflate.DeflateIO(io.BytesIO(data), deflate.RAW, 15).read()
                elif comp_method == 0: # Stored (no compression)
                    decompressed = data
                else:
                    print("Warning: Unsupported compression method:", comp_method)
                    continue
                
                with open(out_path, 'wb') as out_f:
                    out_f.write(decompressed)
                print("Extracted:", filename)
    print("ZIP extraction complete.")
