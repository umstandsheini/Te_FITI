"""
eCryptfs file decryption for Tesla dashcam (ported from encryptfs.ts).

Header (8192 B):
  0   plaintextSize  u64 BE
  8   magic1 u32 BE ; 12 magic2 u32 BE   (magic1 ^ magic2 == 0x3c81b7f5)
  16  version/flags  u32 == 0x03000002
  20  page size      u32 BE == 4096
  24  extent count   u16 == 2
  41  wrapped FEK (16 B, password-based; test path only)
  4096 .. : wrapped-key section for the API:
       u32 key_id | 65 B EC-public_key | 17 B VIN | u64 timestamp | 44 B wrapped_key
Data from offset 8192 in 4096-B pages, AES-128-CBC.
  rootIV    = MD5(FEK)
  IV(page)  = MD5( rootIV(16) ++ ascii(str(page)) zero-padded to 32 B )[:16]
"""
import hashlib, base64, struct

PAGE_SIZE = 4096
HEADER_SIZE = 8192
MAGIC = 0x3C81B7F5
KEY_SIZE = 16

try:
    from Crypto.Cipher import AES  # pycryptodome
    def _aes_cbc_decrypt(key, iv, data):
        return AES.new(bytes(key), AES.MODE_CBC, bytes(iv)).decrypt(bytes(data))
    def _aes_cbc_encrypt(key, iv, data):
        return AES.new(bytes(key), AES.MODE_CBC, bytes(iv)).encrypt(bytes(data))
except ImportError:  # fallback via cryptography
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    def _aes_cbc_decrypt(key, iv, data):
        d = Cipher(algorithms.AES(bytes(key)), modes.CBC(bytes(iv))).decryptor()
        return d.update(bytes(data)) + d.finalize()
    def _aes_cbc_encrypt(key, iv, data):
        e = Cipher(algorithms.AES(bytes(key)), modes.CBC(bytes(iv))).encryptor()
        return e.update(bytes(data)) + e.finalize()


class InvalidHeader(Exception):
    pass


class EcryptfsFile:
    def __init__(self, data: bytes):
        self.data = data
        if len(data) < HEADER_SIZE:
            raise InvalidHeader("file smaller than header")
        if len(data) % PAGE_SIZE != 0:
            raise InvalidHeader("size not a multiple of page size")
        self.plaintext_size = struct.unpack_from(">Q", data, 0)[0]
        if self.plaintext_size > len(data) - HEADER_SIZE:
            raise InvalidHeader("asserted plaintext size too large")
        m1, m2 = struct.unpack_from(">I", data, 8)[0], struct.unpack_from(">I", data, 12)[0]
        if (m1 ^ m2) != MAGIC:
            raise InvalidHeader("bad eCryptfs magic")
        if struct.unpack_from(">I", data, 16)[0] != 0x03000002:
            raise InvalidHeader("unsupported version/flags")
        if struct.unpack_from(">I", data, 20)[0] != PAGE_SIZE:
            raise InvalidHeader("unexpected data offset")
        if struct.unpack_from(">H", data, 24)[0] != 2:
            raise InvalidHeader("unexpected extent count")

    def extract_wrapped_key(self) -> dict:
        """Returns the item for POST /api/1/decrypt/batch."""
        c = PAGE_SIZE
        key_id = struct.unpack_from(">I", self.data, c)[0]; c += 4
        public_key = self.data[c:c + 65]; c += 65
        vin = self.data[c:c + 17].decode("ascii", "replace"); c += 17
        timestamp = struct.unpack_from(">Q", self.data, c)[0]; c += 8
        wrapped_key = self.data[c:c + (12 + 16 + 16)]
        if vin and vin[0] == "\x00" or public_key[0] != 4:
            raise InvalidHeader("invalid wrapped-key section")
        return {
            "vin": vin,
            "key_id": key_id,
            "timestamp": timestamp,
            "wrapped_key": base64.b64encode(wrapped_key).decode(),
            "public_key": base64.b64encode(public_key).decode(),
        }

    @staticmethod
    def _derive_iv(root_iv: bytes, page: int) -> bytes:
        buf = bytearray(32)
        buf[0:16] = root_iv
        s = str(page).encode("ascii")
        buf[16:16 + len(s)] = s
        return hashlib.md5(bytes(buf)).digest()[:16]

    def decrypt(self, fek: bytes) -> bytes:
        fek = bytes(fek)
        root_iv = hashlib.md5(fek).digest()
        out = bytearray()
        page = 0
        for off in range(HEADER_SIZE, len(self.data), PAGE_SIZE):
            iv = self._derive_iv(root_iv, page)
            out += _aes_cbc_decrypt(fek, iv, self.data[off:off + PAGE_SIZE])
            page += 1
        return bytes(out[:self.plaintext_size])


def build_test_file(plaintext: bytes, fek: bytes) -> bytes:
    """Builds an eCryptfs file (for round-trip self-test)."""
    fek = bytes(fek)
    npages = (len(plaintext) + PAGE_SIZE - 1) // PAGE_SIZE
    data = bytearray(HEADER_SIZE + npages * PAGE_SIZE)
    struct.pack_into(">Q", data, 0, len(plaintext))
    m1 = 0x624B34D3
    struct.pack_into(">I", data, 8, m1)
    struct.pack_into(">I", data, 12, m1 ^ MAGIC)
    struct.pack_into(">I", data, 16, 0x03000002)
    struct.pack_into(">I", data, 20, PAGE_SIZE)
    struct.pack_into(">H", data, 24, 2)
    root_iv = hashlib.md5(fek).digest()
    pt = plaintext + b"\x00" * (npages * PAGE_SIZE - len(plaintext))
    for p in range(npages):
        iv = EcryptfsFile._derive_iv(root_iv, p)
        ct = _aes_cbc_encrypt(fek, iv, pt[p * PAGE_SIZE:(p + 1) * PAGE_SIZE])
        data[HEADER_SIZE + p * PAGE_SIZE: HEADER_SIZE + (p + 1) * PAGE_SIZE] = ct
    return bytes(data)
