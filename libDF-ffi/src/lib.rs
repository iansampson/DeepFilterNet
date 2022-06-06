#[no_mangle]
pub extern "C" fn add(a: i32, b: i32) -> i32 {
    a + b
}

#[no_mangle]
pub extern "C" fn analysis(input: *const u8, output: *mut u8, length: usize) {
    let input = unsafe { std::slice::from_raw_parts(input, length) };
    let output = unsafe { std::slice::from_raw_parts_mut(output, length) };
}
