// Independent verifier for the six-u32 carry chain used by merge.wgsl/tailgain.wgsl.
//
// Independence: the implementation under test uses the WGSL two-step u32 carry
// rule; the reference uses three 64-bit limbs plus unsigned __int128 arithmetic.
// The verifier also pins the byte/limb endian contract and hostile mixed-endian
// cases.  No WebGPU, Rust, Python, or production-node dependency is required.

#include <array>
#include <cstdint>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <limits>
#include <random>
#include <stdexcept>
#include <string>

using Words = std::array<std::uint32_t, 6>;
using Bytes = std::array<std::uint8_t, 24>;
using Limbs64 = std::array<std::uint64_t, 3>;

static Words wgsl_add(const Words& a, const Words& b) {
    Words out{};
    std::uint32_t carry = 0;
    for (std::size_t i = 0; i < out.size(); ++i) {
        const std::uint32_t s0 = static_cast<std::uint32_t>(a[i] + b[i]);
        const std::uint32_t c0 = s0 < a[i] ? 1u : 0u;
        const std::uint32_t s1 = static_cast<std::uint32_t>(s0 + carry);
        const std::uint32_t c1 = s1 < s0 ? 1u : 0u;
        out[i] = s1;
        carry = (c0 != 0u || c1 != 0u) ? 1u : 0u;
    }
    return out; // arithmetic modulo 2^192, matching the WGSL output width
}

static Limbs64 pack64(const Words& w) {
    return {
        static_cast<std::uint64_t>(w[0]) | (static_cast<std::uint64_t>(w[1]) << 32),
        static_cast<std::uint64_t>(w[2]) | (static_cast<std::uint64_t>(w[3]) << 32),
        static_cast<std::uint64_t>(w[4]) | (static_cast<std::uint64_t>(w[5]) << 32),
    };
}

static Words unpack64(const Limbs64& x) {
    return {
        static_cast<std::uint32_t>(x[0]), static_cast<std::uint32_t>(x[0] >> 32),
        static_cast<std::uint32_t>(x[1]), static_cast<std::uint32_t>(x[1] >> 32),
        static_cast<std::uint32_t>(x[2]), static_cast<std::uint32_t>(x[2] >> 32),
    };
}

static Words reference_add64(const Words& a, const Words& b) {
    const Limbs64 aa = pack64(a);
    const Limbs64 bb = pack64(b);
    Limbs64 out{};
    std::uint64_t carry = 0;
    for (std::size_t i = 0; i < out.size(); ++i) {
        const unsigned __int128 sum =
            static_cast<unsigned __int128>(aa[i]) +
            static_cast<unsigned __int128>(bb[i]) + carry;
        out[i] = static_cast<std::uint64_t>(sum);
        carry = static_cast<std::uint64_t>(sum >> 64);
    }
    return unpack64(out);
}

static Bytes encode_le_u32_lsw_first(const Words& words) {
    Bytes out{};
    for (std::size_t i = 0; i < words.size(); ++i) {
        for (std::size_t j = 0; j < 4; ++j) {
            out[4 * i + j] = static_cast<std::uint8_t>(words[i] >> (8 * j));
        }
    }
    return out;
}

static Words decode_le_u32_lsw_first(const Bytes& bytes) {
    Words out{};
    for (std::size_t i = 0; i < out.size(); ++i) {
        std::uint32_t word = 0;
        for (std::size_t j = 0; j < 4; ++j) {
            word |= static_cast<std::uint32_t>(bytes[4 * i + j]) << (8 * j);
        }
        out[i] = word;
    }
    return out;
}

static Bytes encode_be_u192_msb_first(const Words& words) {
    const Bytes le = encode_le_u32_lsw_first(words);
    Bytes out{};
    for (std::size_t i = 0; i < out.size(); ++i) out[i] = le[out.size() - 1 - i];
    return out;
}

static Words decode_be_u192_msb_first(const Bytes& bytes) {
    Bytes le{};
    for (std::size_t i = 0; i < bytes.size(); ++i) le[i] = bytes[bytes.size() - 1 - i];
    return decode_le_u32_lsw_first(le);
}

static Bytes mixed_msw_limbs_le_bytes(const Words& words) {
    Bytes out{};
    for (std::size_t i = 0; i < words.size(); ++i) {
        const std::uint32_t word = words[words.size() - 1 - i];
        for (std::size_t j = 0; j < 4; ++j) {
            out[4 * i + j] = static_cast<std::uint8_t>(word >> (8 * j));
        }
    }
    return out;
}

static Bytes mixed_lsw_limbs_be_bytes(const Words& words) {
    Bytes out{};
    for (std::size_t i = 0; i < words.size(); ++i) {
        const std::uint32_t word = words[i];
        for (std::size_t j = 0; j < 4; ++j) {
            out[4 * i + j] = static_cast<std::uint8_t>(word >> (8 * (3 - j)));
        }
    }
    return out;
}

static void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

static void verify_endian_contract() {
    const Words vector = {
        0x03020100u, 0x07060504u, 0x0B0A0908u,
        0x0F0E0D0Cu, 0x13121110u, 0x17161514u,
    };
    const Bytes le = encode_le_u32_lsw_first(vector);
    const Bytes be = encode_be_u192_msb_first(vector);
    for (std::size_t i = 0; i < 24; ++i) {
        require(le[i] == i, "little-endian known vector mismatch");
        require(be[i] == 23 - i, "big-endian known vector mismatch");
    }
    require(decode_le_u32_lsw_first(le) == vector, "little-endian round-trip mismatch");
    require(decode_be_u192_msb_first(be) == vector, "big-endian round-trip mismatch");

    const Words hostile = {
        0x00112233u, 0x44556677u, 0x8899AABBu,
        0xCCDDEEFFu, 0x10203040u, 0x50607080u,
    };
    for (const Bytes mixed : {mixed_msw_limbs_le_bytes(hostile), mixed_lsw_limbs_be_bytes(hostile)}) {
        require(decode_le_u32_lsw_first(mixed) != hostile,
                "mixed-endian bytes silently accepted as canonical little-endian");
        require(decode_be_u192_msb_first(mixed) != hostile,
                "mixed-endian bytes silently accepted as canonical big-endian");
    }
}

static void verify_edge_cases() {
    const std::array<std::pair<Words, Words>, 8> cases = {{
        {Words{0,0,0,0,0,0}, Words{0,0,0,0,0,0}},
        {Words{0xFFFFFFFFu,0,0,0,0,0}, Words{1,0,0,0,0,0}},
        {Words{0xFFFFFFFFu,0xFFFFFFFFu,0,0,0,0}, Words{1,0,0,0,0,0}},
        {Words{0xFFFFFFFFu,0xFFFFFFFFu,0xFFFFFFFFu,0,0,0}, Words{1,0,0,0,0,0}},
        {Words{0xFFFFFFFFu,0xFFFFFFFFu,0xFFFFFFFFu,0xFFFFFFFFu,0,0}, Words{1,0,0,0,0,0}},
        {Words{0xFFFFFFFFu,0xFFFFFFFFu,0xFFFFFFFFu,0xFFFFFFFFu,0xFFFFFFFFu,0}, Words{1,0,0,0,0,0}},
        {Words{0xFFFFFFFFu,0xFFFFFFFFu,0xFFFFFFFFu,0xFFFFFFFFu,0xFFFFFFFFu,0xFFFFFFFFu}, Words{1,0,0,0,0,0}},
        {Words{0x80000000u,0x7FFFFFFFu,0xFFFFFFFFu,0,0xFFFFFFFFu,0}, Words{0x80000000u,0x80000001u,1u,0xFFFFFFFFu,1u,0xFFFFFFFFu}},
    }};
    for (const auto& [a, b] : cases) {
        require(wgsl_add(a, b) == reference_add64(a, b), "carry edge-case mismatch");
    }
}

int main(int argc, char** argv) {
    try {
        std::uint64_t samples = 1'000'000;
        std::uint64_t seed = 0xB1177E2192ULL;
        if (argc >= 2) samples = std::stoull(argv[1]);
        if (argc >= 3) seed = std::stoull(argv[2], nullptr, 0);

        verify_endian_contract();
        verify_edge_cases();

        std::mt19937_64 rng(seed);
        for (std::uint64_t sample = 0; sample < samples; ++sample) {
            Words a{}, b{};
            for (std::size_t i = 0; i < 6; ++i) {
                a[i] = static_cast<std::uint32_t>(rng());
                b[i] = static_cast<std::uint32_t>(rng());
            }
            if (wgsl_add(a, b) != reference_add64(a, b)) {
                std::cerr << "mismatch at sample " << sample << "\n";
                return 1;
            }
        }

        std::cout << "{\"status\":\"PASS\",\"samples\":" << samples
                  << ",\"seed\":\"0x" << std::hex << seed << std::dec
                  << "\",\"reference\":\"cpp-u64-u128\",\"endianness\":\"PASS\"}\n";
        return 0;
    } catch (const std::exception& ex) {
        std::cerr << "FAIL: " << ex.what() << "\n";
        return 2;
    }
}
