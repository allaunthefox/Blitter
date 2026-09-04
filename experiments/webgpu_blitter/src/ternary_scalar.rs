use std::error::Error;
use std::fmt;

pub const REQUEST_SIZE: usize = 26;
pub const RESULT_SIZE: usize = 26;
pub const PROFILE_ID: &str = "blitter-ternary-step-i32-v1";

pub const ACCEPTED: u8 = 0;
pub const WRITE_CONFLICT: u8 = 1;
pub const DOMAIN_EXCEEDED: u8 = 2;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DecodeError {
    InvalidLength { actual: usize },
    InvalidDirection(u8),
}

impl fmt::Display for DecodeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidLength { actual } => write!(
                formatter,
                "request must be exactly {REQUEST_SIZE} bytes, got {actual}"
            ),
            Self::InvalidDirection(value) => {
                write!(formatter, "direction outside ternary domain: 0x{value:02x}")
            }
        }
    }
}

impl Error for DecodeError {}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct State {
    rails: [i32; 4],
    direction: i8,
    cycles: u64,
}

#[derive(Clone, Copy)]
struct Write {
    target: usize,
    value: i32,
}

fn decode(request: &[u8]) -> Result<(State, u8), DecodeError> {
    if request.len() != REQUEST_SIZE {
        return Err(DecodeError::InvalidLength {
            actual: request.len(),
        });
    }

    let mut rails = [0; 4];
    for (rail, bytes) in rails.iter_mut().zip(request[..16].chunks_exact(4)) {
        *rail = i32::from_le_bytes(bytes.try_into().expect("four-byte rail chunk"));
    }
    let direction = match request[16] {
        0xff => -1,
        0 => 0,
        1 => 1,
        other => return Err(DecodeError::InvalidDirection(other)),
    };
    let cycles = u64::from_le_bytes(request[17..25].try_into().expect("eight-byte cycle"));

    Ok((
        State {
            rails,
            direction,
            cycles,
        },
        request[25],
    ))
}

fn candidate(state: State, code: u8) -> Option<Write> {
    let source = usize::from(code >> 2);
    let target = usize::from(code & 0x03);
    let value = match state.direction {
        -1 => state.rails[target].checked_sub(state.rails[source]),
        0 => Some(state.rails[target]),
        1 => state.rails[target].checked_add(state.rails[source]),
        _ => unreachable!("decoded direction is ternary"),
    }?;
    Some(Write { target, value })
}

fn encode(status: u8, state: State) -> [u8; RESULT_SIZE] {
    let mut result = [0; RESULT_SIZE];
    result[0] = status;
    for (index, rail) in state.rails.into_iter().enumerate() {
        result[1 + index * 4..5 + index * 4].copy_from_slice(&rail.to_le_bytes());
    }
    result[17] = state.direction as u8;
    result[18..26].copy_from_slice(&state.cycles.to_le_bytes());
    result
}

/// Executes one canonical `blitter-ternary-step-i32-v1` request.
pub fn execute(request: &[u8]) -> Result<[u8; RESULT_SIZE], DecodeError> {
    let (state, instruction) = decode(request)?;
    if state.cycles == u64::MAX {
        return Ok(encode(DOMAIN_EXCEEDED, state));
    }

    let Some(lower) = candidate(state, instruction & 0x0f) else {
        return Ok(encode(DOMAIN_EXCEEDED, state));
    };
    let Some(upper) = candidate(state, instruction >> 4) else {
        return Ok(encode(DOMAIN_EXCEEDED, state));
    };

    let next_direction = upper.value.cmp(&lower.value) as i8;
    if state.direction != 0
        && lower.target == upper.target
        && lower.value != upper.value
    {
        return Ok(encode(WRITE_CONFLICT, state));
    }

    let mut next = state;
    if state.direction != 0 {
        next.rails[lower.target] = lower.value;
        next.rails[upper.target] = upper.value;
    }
    next.direction = next_direction;
    next.cycles = state
        .cycles
        .checked_add(1)
        .expect("u64::MAX was rejected before execution");
    Ok(encode(ACCEPTED, next))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(
        rails: [i32; 4],
        direction: i8,
        cycles: u64,
        instruction: u8,
    ) -> [u8; 26] {
        let mut bytes = [0; 26];
        for (index, rail) in rails.into_iter().enumerate() {
            bytes[index * 4..index * 4 + 4].copy_from_slice(&rail.to_le_bytes());
        }
        bytes[16] = direction as u8;
        bytes[17..25].copy_from_slice(&cycles.to_le_bytes());
        bytes[25] = instruction;
        bytes
    }

    fn hex(value: &str) -> Vec<u8> {
        value
            .as_bytes()
            .chunks_exact(2)
            .map(|pair| {
                u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap()
            })
            .collect()
    }

    #[test]
    fn matches_exported_differential_fixtures_byte_for_byte() {
        let fixtures = [
            (
                "03000000feffffff05000000010000000111000000000000006c",
                "0004000000feffffff0300000001000000ff1200000000000000",
            ),
            (
                "03000000feffffff0500000001000000ff11000000000000006c",
                "0002000000feffffff0700000001000000011200000000000000",
            ),
            (
                "03000000feffffff05000000010000000011000000000000006c",
                "0003000000feffffff0500000001000000011200000000000000",
            ),
            (
                "03000000feffffff050000000100000001110000000000000004",
                "0103000000feffffff0500000001000000011100000000000000",
            ),
            (
                "ffffff7f00000000000000000000000001000000000000000000",
                "02ffffff7f000000000000000000000000010000000000000000",
            ),
            (
                "00000080000000000000000000000000ff000000000000000000",
                "0000000000000000000000000000000000000100000000000000",
            ),
            (
                "070000000b0000000d0000001100000001040000000000000010",
                "000e000000120000000d00000011000000010500000000000000",
            ),
            (
                "0000000001000000ffffffff0200000001ffffffffffffffffff",
                "020000000001000000ffffffff0200000001ffffffffffffffff",
            ),
            (
                "15cd5b074f9721c5ffffff7f00000080fffeffffffffffffffb4",
                "0215cd5b074f9721c5ffffff7f00000080fffeffffffffffffff",
            ),
        ];

        for (encoded_request, expected) in fixtures {
            assert_eq!(
                execute(&hex(encoded_request)).unwrap().as_slice(),
                hex(expected)
            );
        }
    }

    #[test]
    fn exported_fixed_corpus_covers_every_instruction() {
        let states = [
            ([3, -2, 5, 1], 1, 17),
            ([3, -2, 5, 1], -1, 17),
            ([3, -2, 5, 1], 0, 17),
            ([i32::MIN, 0, 0, 0], 1, 0),
            ([i32::MAX, 0, 0, 0], 1, 0),
            ([i32::MIN, i32::MAX, -1, 1], -1, 0),
            ([i32::MAX, i32::MIN, 1, -1], 1, 0),
            ([i32::MAX; 4], 1, 0),
            ([i32::MIN; 4], -1, 0),
            ([i32::MIN, 1, i32::MAX, -1], 1, 0),
            ([123456789, -987654321, i32::MAX, i32::MIN], -1, 0),
            ([0, 1, -1, 2], 1, u64::MAX),
        ];
        let mut counts = [0usize; 3];
        let mut result_hash = 0xcbf29ce484222325u64;

        for (rails, direction, cycles) in states {
            for instruction in 0..=u8::MAX {
                let result = execute(&request(rails, direction, cycles, instruction)).unwrap();
                counts[usize::from(result[0])] += 1;
                for byte in result {
                    result_hash = (result_hash ^ u64::from(byte)).wrapping_mul(0x100000001b3);
                }
            }
        }

        assert_eq!(counts, [1693, 202, 1177]);
        assert_eq!(result_hash, 0xfc055f31698f3e83);
    }

    #[test]
    fn rejects_noncanonical_requests() {
        assert_eq!(
            execute(&[0; 25]),
            Err(DecodeError::InvalidLength { actual: 25 })
        );
        let mut malformed = [0; 26];
        malformed[16] = 2;
        assert_eq!(execute(&malformed), Err(DecodeError::InvalidDirection(2)));
    }
}
