use std::io::{self, Read, Write};

use webgpu_blitter::ternary_scalar::{execute, REQUEST_SIZE};

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let mut requests = Vec::new();
    io::stdin().read_to_end(&mut requests)?;
    if requests.len() % REQUEST_SIZE != 0 {
        return Err(format!(
            "request stream length must be a multiple of {REQUEST_SIZE} bytes, got {}",
            requests.len()
        )
        .into());
    }

    let results = requests
        .chunks_exact(REQUEST_SIZE)
        .map(execute)
        .collect::<Result<Vec<_>, _>>()?;
    let stdout = io::stdout();
    let mut output = stdout.lock();
    for result in results {
        output.write_all(&result)?;
    }
    output.flush()?;
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("blitter-ternary-scalar: {error}");
        std::process::exit(1);
    }
}
