// use df::{Complex32, DFState, UNIT_NORM_INIT};
use df::{DFState};
use num_complex::Complex;

#[no_mangle]
pub extern "C" fn analysis(input: *const f32, output: *mut Complex<f32>, length: usize) {
    let in_slice = unsafe { std::slice::from_raw_parts(input, length) };
    let out_slice = unsafe { std::slice::from_raw_parts_mut(output, length) };
    let mut state: DFState = DFState::default();
    state.analysis(in_slice, out_slice)
}

//
// TODO: Make names consistent once you figure out how to handle slices

// TODO: Write directly into the output buffer
// or let Rust own the memory and provide an external release function
// or publish an external Complex32 struct
// but the caller has to know how long to make it, which is dangerous
// let freq_steps = length.div_euclid(frame_size);
// # let mut output = Array3::<Complex32>::zeros((channels, freq_steps, freq_size));
// let mut output =

// let out_slice = out_ch.as_slice_mut().ok_or_else(|| {
    // PyErr::new::<PyRuntimeError, _>("[df] Output array empty or not contiguous.")
    // TODO: Throw a Rust error (i.e. an exception)
// })?;

// TODO: Pass in state as an argument
// let frame_size = state.frame_size;
// let freq_size = state.freq_size;

// let in_chunks = in_slice.chunks_exact(frame_size);
// let out_chunks = out_slice.chunks_exact_mut(freq_size);
// for (ichunk, ochunk) in in_chunks.into_iter().zip(out_chunks.into_iter()) {
// }
