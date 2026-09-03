"""ISA-style tagged U192 representation/codec contract for exact blitter kernels.

Canonical internal representation:
    [w0, w1, w2, w3, w4, w5]
where each wi is an unsigned 32-bit limb and w0 is least significant.

The external ABI uses a frozen one-byte format word, following the same design
rules as nKernel FHE ISA V2: the raw byte has a total structural decode, while a
strict transport boundary accepts only the current canonical profile, rejects
reserved/noncanonical encodings, normalizes immediately, and can be re-encoded
byte-identically.

Format byte V1:

    vv l b rrrr

    vv    format version (0..3 structurally decodable; V1 requires 0)
    l     limb order: 0 = least-significant word first, 1 = most-significant word first
    b     byte order inside each u32 limb: 0 = little, 1 = big
    rrrr  reserved; V1 requires 0000

Thus the four canonical V1 tags are:

    0x00  le-u32-lsw-first
    0x10  be-u32-lsw-first        (explicit mixed layout)
    0x20  le-u32-msw-first        (explicit mixed layout)
    0x30  be-u192-msb-first       (msw-first, each u32 big-endian)

Every byte 0..255 can be structurally decoded into these fields. Only bytes with
version=0 and reserved=0 are admitted by the V1 ABI. There is no host-native,
auto-detection, or generic mixed mode.
"""

U32_MAX = (1 << 32) - 1
U192_WORDS = 6
U192_BYTES = 24
ABI_SCHEMA = "mathpunch.u192-abi.v1"
CANONICAL_SCHEMA = "mathpunch.u192-canonical.v1"

FORMAT_VERSION = 0
FORMAT_VERSION_SHIFT = 6
FORMAT_VERSION_MASK = 0xC0
FORMAT_LIMB_MSW_MASK = 0x20
FORMAT_BYTE_BIG_MASK = 0x10
FORMAT_RESERVED_MASK = 0x0F

LE_U32_LSW_FIRST = "le-u32-lsw-first"
BE_U192_MSB_FIRST = "be-u192-msb-first"
LE_U32_MSW_FIRST = "le-u32-msw-first"
BE_U32_LSW_FIRST = "be-u32-lsw-first"

LAYOUT_TO_TAG = {
    LE_U32_LSW_FIRST: 0x00,
    BE_U32_LSW_FIRST: 0x10,
    LE_U32_MSW_FIRST: 0x20,
    BE_U192_MSB_FIRST: 0x30,
}
TAG_TO_LAYOUT = {tag: layout for layout, tag in LAYOUT_TO_TAG.items()}
SUPPORTED_LAYOUTS = frozenset(LAYOUT_TO_TAG)
SUPPORTED_FORMAT_TAGS = frozenset(TAG_TO_LAYOUT)


def _validate_byte(value, name):
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFF:
        raise ValueError(f"{name} must be an unsigned byte")
    return value


def decode_format_tag_raw(tag):
    """Total structural decode for every byte, analogous to ISA instruction decode."""
    tag = _validate_byte(tag, "format_tag")
    return {
        "raw": tag,
        "version": (tag & FORMAT_VERSION_MASK) >> FORMAT_VERSION_SHIFT,
        "limb_order": "msw" if tag & FORMAT_LIMB_MSW_MASK else "lsw",
        "byte_order": "big" if tag & FORMAT_BYTE_BIG_MASK else "little",
        "reserved": tag & FORMAT_RESERVED_MASK,
    }


def encode_format_tag_raw(version, limb_order, byte_order, reserved):
    """Inverse of raw structural decode; intentionally accepts all field values."""
    if not isinstance(version, int) or isinstance(version, bool) or not 0 <= version <= 3:
        raise ValueError("format version must be in 0..3")
    if limb_order not in ("lsw", "msw"):
        raise ValueError("limb_order must be 'lsw' or 'msw'")
    if byte_order not in ("little", "big"):
        raise ValueError("byte_order must be 'little' or 'big'")
    if not isinstance(reserved, int) or isinstance(reserved, bool) or not 0 <= reserved <= 0x0F:
        raise ValueError("reserved field must be in 0..15")
    return (
        (version << FORMAT_VERSION_SHIFT)
        | (FORMAT_LIMB_MSW_MASK if limb_order == "msw" else 0)
        | (FORMAT_BYTE_BIG_MASK if byte_order == "big" else 0)
        | reserved
    )


def validate_format_tag(tag):
    """Admit only the current canonical V1 profile and return its decoded fields."""
    decoded = decode_format_tag_raw(tag)
    if decoded["version"] != FORMAT_VERSION:
        raise ValueError(f"unsupported U192 format version {decoded['version']}")
    if decoded["reserved"] != 0:
        raise ValueError("noncanonical U192 format tag: reserved bits must be zero")
    decoded["layout"] = TAG_TO_LAYOUT[tag]
    return decoded


def format_tag_for_layout(layout):
    try:
        return LAYOUT_TO_TAG[layout]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"unsupported or ambiguous U192 layout {layout!r}; "
            f"expected one of {sorted(SUPPORTED_LAYOUTS)}"
        ) from exc


def validate_words(words):
    if not isinstance(words, (list, tuple)) or len(words) != U192_WORDS:
        raise ValueError("U192 value must contain exactly six u32 words")
    checked = []
    for index, word in enumerate(words):
        if (
            not isinstance(word, int)
            or isinstance(word, bool)
            or word < 0
            or word > U32_MAX
        ):
            raise ValueError(f"word {index} is not a u32")
        checked.append(word)
    return checked


def u192_key(words):
    """Numeric comparison key for the canonical low-word-first representation."""
    checked = validate_words(words)
    return tuple(reversed(checked))


def u192_to_int(words):
    """Interpret six canonical u32 limbs as an unsigned 192-bit integer."""
    checked = validate_words(words)
    return sum(word << (32 * index) for index, word in enumerate(checked))


def u192_from_int(value):
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value >= (1 << 192)
    ):
        raise ValueError("value is not an unsigned 192-bit integer")
    return [(value >> (32 * index)) & U32_MAX for index in range(U192_WORDS)]


def _encode_limb_sequence(words, limb_order, byte_order):
    ordered = words if limb_order == "lsw" else list(reversed(words))
    return b"".join(word.to_bytes(4, byte_order) for word in ordered)


def _decode_limb_sequence(raw, limb_order, byte_order):
    limbs = [
        int.from_bytes(raw[offset : offset + 4], byte_order)
        for offset in range(0, U192_BYTES, 4)
    ]
    return limbs if limb_order == "lsw" else list(reversed(limbs))


def encode_u192(words, layout):
    """Encode canonical U192 words under a named canonical V1 layout."""
    checked = validate_words(words)
    fields = validate_format_tag(format_tag_for_layout(layout))
    return _encode_limb_sequence(checked, fields["limb_order"], fields["byte_order"])


def decode_u192(data, layout):
    """Decode exactly 24 bytes under a named canonical V1 layout."""
    return decode_u192_tagged_bytes(data, format_tag_for_layout(layout))


def decode_u192_tagged_bytes(data, format_tag):
    """Normalize tagged bytes directly to canonical low-word-first words."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ValueError("U192 byte encoding must be bytes-like")
    raw = bytes(data)
    if len(raw) != U192_BYTES:
        raise ValueError(f"U192 byte encoding must be exactly {U192_BYTES} bytes")
    fields = validate_format_tag(format_tag)
    return _decode_limb_sequence(raw, fields["limb_order"], fields["byte_order"])


def make_tagged_u192(words, layout):
    """Create the strict JSON-safe ABI structure consumed by normalization boundaries."""
    format_tag = format_tag_for_layout(layout)
    payload = encode_u192(words, layout)
    return {
        "schema": ABI_SCHEMA,
        "format_tag": format_tag,
        "payload_hex": payload.hex(),
    }


def normalize_tagged_u192(structure):
    """Normalize a tagged external structure to the canonical internal form.

    The tag is authoritative for the transport transform, not mathematical truth.
    Unknown/missing fields, future versions, reserved bits, malformed/noncanonical
    hex, and malformed payload lengths fail closed. A producer that lies about its
    tag can still encode the wrong value; receipts/tests bind producer codec identity
    where that matters.
    """
    if not isinstance(structure, dict):
        raise ValueError("tagged U192 value must be an object")
    required = {"schema", "format_tag", "payload_hex"}
    keys = set(structure)
    missing = sorted(required - keys)
    extra = sorted(keys - required)
    if missing:
        raise ValueError(f"tagged U192 value missing fields: {missing}")
    if extra:
        raise ValueError(f"tagged U192 value has unknown fields: {extra}")
    if structure["schema"] != ABI_SCHEMA:
        raise ValueError("tagged U192 schema mismatch")

    format_tag = structure["format_tag"]
    validate_format_tag(format_tag)

    payload_hex = structure["payload_hex"]
    if not isinstance(payload_hex, str):
        raise ValueError("tagged U192 payload_hex must be a string")
    if len(payload_hex) != 2 * U192_BYTES:
        raise ValueError("tagged U192 payload_hex must encode exactly 24 bytes")
    if any(ch not in "0123456789abcdef" for ch in payload_hex):
        raise ValueError("tagged U192 payload_hex must be canonical lowercase hexadecimal")
    payload = bytes.fromhex(payload_hex)
    words = decode_u192_tagged_bytes(payload, format_tag)

    # The accepted descriptor must be its own canonical re-encoding under the
    # decoded profile, matching the ISA transport discipline.
    layout = validate_format_tag(format_tag)["layout"]
    if make_tagged_u192(words, layout) != structure:
        raise ValueError("noncanonical tagged U192 descriptor")

    return {
        "schema": CANONICAL_SCHEMA,
        "words": words,
    }
