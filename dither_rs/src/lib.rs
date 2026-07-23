use ndarray::Array3;
use numpy::{IntoPyArray, PyReadonlyArray2, PyReadonlyArray3};
use pyo3::prelude::*;

/// Floyd-Steinberg dither: quantise an HxWx3 f32 image to a 6-color palette.
///
/// Faithfully ports image/pipeline.py::dither_floyd_steinberg, including:
///   - orig-pixel pure-black / pure-white passthrough (checked before error diffusion)
///   - L1 < 15 snap-to-black / snap-to-white shortcuts (on error-diffused value)
///   - squared-Euclidean palette search with first-minimum tie-break (matches numpy argmin)
///   - f32 error diffusion with 7/16, 3/16, 5/16, 1/16 weights in that order
///
/// img_array : (H, W, 3) f32 array — pixel values in [0, 255]
/// palette   : (N, 3)    f32 array — palette colors, same range
/// returns   : (H, W, 3) u8 array — quantized image
#[pyfunction]
fn dither_floyd_steinberg<'py>(
    py: Python<'py>,
    img_array: PyReadonlyArray3<'_, f32>,
    palette: PyReadonlyArray2<'_, f32>,
) -> Bound<'py, numpy::PyArray3<u8>> {
    let img = img_array.as_array();
    let pal = palette.as_array();

    let h = img.shape()[0];
    let w = img.shape()[1];
    let n_colors = pal.shape()[0];

    // Build palette as a small stack-friendly array of [f32; 3]
    let pal_data: Vec<[f32; 3]> = (0..n_colors)
        .map(|i| [pal[[i, 0]], pal[[i, 1]], pal[[i, 2]]])
        .collect();

    // padded: (h+1) × (w+2) × 3  — mirrors numpy.pad(img, ((0,1),(1,1),(0,0)), 'edge')
    // Layout: col 0 = left edge, cols 1..=w = image, col w+1 = right edge
    //         row h  = bottom edge copy
    let ph = h + 1;
    let pw = w + 2;
    let mut padded = vec![0f32; ph * pw * 3];

    // Helper closure for flat index into padded
    let pidx = |y: usize, x: usize, c: usize| -> usize { (y * pw + x) * 3 + c };

    for y in 0..h {
        for c in 0..3 {
            // left-edge pad: copy col 0
            padded[pidx(y, 0, c)] = img[[y, 0, c]];
            // main image content (shifted right by 1)
            for x in 0..w {
                padded[pidx(y, x + 1, c)] = img[[y, x, c]];
            }
            // right-edge pad: copy last col
            padded[pidx(y, w + 1, c)] = img[[y, w - 1, c]];
        }
    }
    // Bottom-edge pad: copy row h-1 into row h
    for x in 0..pw {
        for c in 0..3 {
            padded[pidx(h, x, c)] = padded[pidx(h - 1, x, c)];
        }
    }

    // Main dither loop — x ∈ [1, w] corresponds to image col x-1
    for y in 0..h {
        for x in 1..=w {
            // orig_val: the ORIGINAL (un-diffused) pixel at img[y, x-1]
            let ov_r = img[[y, x - 1, 0]];
            let ov_g = img[[y, x - 1, 1]];
            let ov_b = img[[y, x - 1, 2]];

            // Pure black passthrough — checked on orig, not diffused value
            if ov_r == 0.0 && ov_g == 0.0 && ov_b == 0.0 {
                padded[pidx(y, x, 0)] = 0.0;
                padded[pidx(y, x, 1)] = 0.0;
                padded[pidx(y, x, 2)] = 0.0;
                continue;
            }

            // Pure white passthrough
            if ov_r == 255.0 && ov_g == 255.0 && ov_b == 255.0 {
                padded[pidx(y, x, 0)] = 255.0;
                padded[pidx(y, x, 1)] = 255.0;
                padded[pidx(y, x, 2)] = 255.0;
                continue;
            }

            // old_val: the error-diffused value at padded[y, x]
            let old_r = padded[pidx(y, x, 0)];
            let old_g = padded[pidx(y, x, 1)];
            let old_b = padded[pidx(y, x, 2)];

            // L1 snap to black
            let l1_black = old_r.abs() + old_g.abs() + old_b.abs();
            if l1_black < 15.0 {
                padded[pidx(y, x, 0)] = 0.0;
                padded[pidx(y, x, 1)] = 0.0;
                padded[pidx(y, x, 2)] = 0.0;
                continue;
            }

            // L1 snap to white
            let l1_white =
                (old_r - 255.0).abs() + (old_g - 255.0).abs() + (old_b - 255.0).abs();
            if l1_white < 15.0 {
                padded[pidx(y, x, 0)] = 255.0;
                padded[pidx(y, x, 1)] = 255.0;
                padded[pidx(y, x, 2)] = 255.0;
                continue;
            }

            // Nearest palette color: squared Euclidean, first-minimum (matches numpy argmin)
            let mut best_idx = 0usize;
            let mut best_dist = f32::INFINITY;
            for (i, p) in pal_data.iter().enumerate() {
                let dr = p[0] - old_r;
                let dg = p[1] - old_g;
                let db = p[2] - old_b;
                let dist = dr * dr + dg * dg + db * db;
                if dist < best_dist {
                    best_dist = dist;
                    best_idx = i;
                }
            }

            let new_r = pal_data[best_idx][0];
            let new_g = pal_data[best_idx][1];
            let new_b = pal_data[best_idx][2];

            padded[pidx(y, x, 0)] = new_r;
            padded[pidx(y, x, 1)] = new_g;
            padded[pidx(y, x, 2)] = new_b;

            let err_r = old_r - new_r;
            let err_g = old_g - new_g;
            let err_b = old_b - new_b;

            // Diffuse error: right 7/16, lower-left 3/16, below 5/16, lower-right 1/16
            for c in 0..3 {
                let err = [err_r, err_g, err_b][c];
                padded[pidx(y, x + 1, c)] += err * (7.0 / 16.0);
                padded[pidx(y + 1, x - 1, c)] += err * (3.0 / 16.0);
                padded[pidx(y + 1, x, c)] += err * (5.0 / 16.0);
                padded[pidx(y + 1, x + 1, c)] += err * (1.0 / 16.0);
            }
        }
    }

    // Extract result: padded[0:h, 1:w+1] → (h, w, 3) u8
    let mut result = Array3::<u8>::zeros((h, w, 3));
    for y in 0..h {
        for x in 0..w {
            for c in 0..3 {
                let v = padded[pidx(y, x + 1, c)];
                result[[y, x, c]] = v.clamp(0.0, 255.0) as u8;
            }
        }
    }

    result.into_pyarray_bound(py)
}

#[pymodule]
fn dither_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(dither_floyd_steinberg, m)?)?;
    Ok(())
}
