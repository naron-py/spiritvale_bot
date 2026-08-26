import struct
import unittest

import memscan


class _Mem:
    def __init__(self, reads):
        self.reads = reads

    def read(self, address, size):
        data = self.reads.get(address)
        return data[:size] if data is not None else None


class NetworkIdentityTests(unittest.TestCase):
    def test_short_structural_reads_fail_closed(self):
        mem = _Mem({0x1000: b"\0", 0x2000: b"\0"})

        self.assertEqual(memscan.read_ptr(mem, 0x1000), 0)
        self.assertIsNone(memscan.read_vec3(mem, 0x2000))
        self.assertIsNone(memscan.network_object_id(mem, 0x3000))
        self.assertIsNone(memscan.resolve(mem, 0x1000, (0, 0)))
        self.assertIsNone(memscan.cs_string(mem, 0x1000))

        declared = _Mem({0x4010: struct.pack("<i", 9),
                         0x4014: "Pet_Earth".encode("utf-16-le")[:2]})
        self.assertIsNone(memscan.cs_string(declared, 0x4000))

    def test_network_object_id_is_preferred_stable_identity(self):
        unit, network_object = 0x1000, 0x50000
        mem = _Mem({unit + memscan.UNIT_NETWORK_OBJECT:
                    struct.pack("<Q", network_object),
                    network_object + memscan.NETWORK_OBJECT_ID:
                    struct.pack("<i", 321)})
        self.assertEqual(memscan.network_object_id(mem, unit), 321)

    def test_unset_or_unreadable_id_has_no_stable_identity(self):
        unit, network_object = 0x1000, 0x50000
        unset = _Mem({unit + memscan.UNIT_NETWORK_OBJECT:
                      struct.pack("<Q", network_object),
                      network_object + memscan.NETWORK_OBJECT_ID:
                      struct.pack("<i", memscan.NETWORK_OBJECT_ID_UNSET)})
        self.assertIsNone(memscan.network_object_id(unset, unit))
        self.assertIsNone(memscan.network_object_id(_Mem({}), unit))


if __name__ == "__main__":
    unittest.main()
